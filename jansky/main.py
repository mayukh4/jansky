"""Jansky v2: the voice loop.

    python -m jansky.main                  live: wake word -> listen -> think -> speak
    python -m jansky.main --inject a.wav   feed recorded utterances instead of the mic (for testing)

Threads: the mic loop (this thread) runs the wake word, VAD and turn-taking. Each turn runs in its own
thread: Whisper on the HAT, then the tool router, and only if no tool claims the question, Qwen3 on the
HAT. Sentences go to the voice thread, which synthesises on the CPU and queues audio to the player.
"""
import argparse
import collections
import difflib
import json
import os
import queue
import random
import re
import signal
import subprocess
import threading
import time
import traceback

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from . import config
from .audio import Mic, Player, chime, find_device
from .doa import Doa
from .face_server import EventBus, serve
from .hat import HatClient
from .home import Home
from .listen import VAD, CommandSpotter, WakeWord
from .persona import OPENERS
from .tools import Router
from .voice import Chunker, Voice

IDLE, LISTENING, THINKING, SPEAKING = "idle", "listening", "thinking", "speaking"

# Whisper hears the wake phrase when it is said again inside a turn. Its spellings of Mayukh's "Hey Jansky":
# "Hejanski", "Hye-Jun's key", "Hey Janski". Up to two short words before it covers "okay so" and the like.
WAKE_PREFIX = re.compile(r"^\W*(?:[a-z]{1,5}[\s,.-]+){0,2}?(?:he|hey|hi|hye|a)?[\s,.-]*"
                         r"(?:jarvis|j[aeu]n'?s'? ?k(?:i|y|ey|ie)?)(?![\w'])[\s,.!?]*", re.I)
NOISE = {"", "you", "thank you", "thanks for watching", "bye", "so", "the", "i'm sorry"}


_log_lock = threading.Lock()


def log(kind, **data):
    line = json.dumps({"t": round(time.time(), 3), "event": kind, **data})
    with _log_lock:  # threads interleaved lines without it
        print(line, flush=True)


def is_wake_phrase(text):
    """Whisper's spellings of the wake phrase on its own: "Hajarvis", "Kajarmas", "head service"."""
    words = re.sub(r"[^a-z ]", "", text.lower()).split()
    if not words or len(words) > 3:
        return False
    squashed = "".join(words)
    return max(difflib.SequenceMatcher(None, squashed, w).ratio()
               for w in ("heyjansky", "hejanski", "jansky", "heyjarvis")) >= 0.55


OPENER_WORDS = {re.sub(r"[^a-z]", "", o.split()[0].lower()) for o in OPENERS}
FILLERS = ["Hmm,", "Well,", "So,"]


def sentence_key(s):
    return re.sub(r"[^a-z ]", "", s.lower()).strip()


class Jansky:
    def __init__(self):
        t = time.time()
        # The face comes up first, so the screen shows Jansky waking while the HAT loads (~20 s).
        self.state = "starting"
        self.tap_pending = False
        self.quit_requested = threading.Event()
        self.announce_lock = threading.Lock()  # before the router: a reminder can come due during start-up
        self.turn = None
        self.muted = False  # a long press on the screen: the wake word is ignored (for filming narration)
        self.face = EventBus()
        self.face.publish("hello", doa_front=config.DOA_FRONT_DEG, doa_sign=config.DOA_SIGN, captions=config.CAPTIONS)
        self.face.publish("state", state=self.state)
        self.router = Router(announce=self.announce,
                             on_timers=lambda items: self.face.publish("timers", items=items),
                             on_todo=lambda items, shown, highlight: self.face.publish(
                                 "todo", items=items, shown=shown, highlight=highlight))
        todo = self.router.reminders.list
        todo._changed()  # the list is on screen from the start when it has items
        serve(self.face, port=config.FACE_PORT, routes={
            "/tap": lambda _b: self.tap(),
            "/quit": lambda _b: self.quit_requested.set(),
            "/mute": lambda _b: self.toggle_mute(),
            "/todo/done": lambda b: todo.done(str(b.get("id", ""))),
            "/todo/show": lambda _b: todo.show(),
            "/todo/hide": lambda _b: todo.hide(),
            "/timer/cancel": lambda b: self.router.timers.cancel(int(b.get("id", -1))),
        })
        self.spotter = None
        threading.Thread(target=self._command_spotter, daemon=True).start()  # reads HA's devices, builds the spotter

        device = find_device()
        self.player = Player(device)
        self.mic = Mic(device)
        self.vad = VAD()
        self.wake = WakeWord()
        self.hat = HatClient()  # Whisper + Qwen3 in their own process; primes the persona on start
        self.voice = Voice()
        self.doa = Doa(on_change=self._on_doa)
        self.heard_doa = []  # angles reported while the user was talking, logged with each turn
        self.doa.start()
        self.mic_level = 0.0
        log("ready", seconds=round(time.time() - t, 1), context=self.hat.context_used, greeting=self.hat.greeting)
        config.DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        self.reply_mic = []       # mic frames while Jansky thinks/speaks, saved per turn to check the AEC
        self.reply_vad_peak = 0.0

        self.turn = None          # the running turn's cancel Event
        self.follow_up_until = 0.0
        self.relisten = False     # set when a turn was only the wake phrase: open the mic again
        self.speech_q = queue.Queue()
        threading.Thread(target=self._voice_worker, daemon=True).start()
        threading.Thread(target=self._level_worker, daemon=True).start()

    # ---- state ----

    def set_state(self, state):
        if state != self.state:
            self.state = state
            log("state", state=state)
            self.face.publish("state", state=state)

    def _level_worker(self):
        """Voice and mic levels for the face's mouth and rings, 25 times a second while they matter."""
        while True:
            if self.state in (SPEAKING, LISTENING) or self.player.busy:
                self.face.publish("level", spk=round(self.player.level, 4), mic=round(self.mic_level, 4))
            time.sleep(0.04)

    @staticmethod
    def from_speaker(angle):
        return abs((angle - config.SPEAKER_DOA_DEG + 180) % 360 - 180) <= config.SPEAKER_DOA_HALF_WIDTH

    def _on_doa(self, angle, speaking):
        if self.from_speaker(angle):
            return  # that's Jansky's own speaker
        self.face.publish("doa", angle=angle, speaking=speaking)
        if speaking and self.state in (IDLE, LISTENING):
            log("doa", angle=angle)
        if speaking and self.state == LISTENING:
            self.heard_doa.append(angle)

    def _command_spotter(self):
        """The keyword spotter for home commands, rebuilt when Home Assistant's device names change."""
        home = self.router.home
        if not config.HA_TOKEN:
            return None
        try:
            if not home.names():
                home.refresh()
        except Exception as e:
            log("home_unreachable", error=repr(e))
            return None
        names = sorted(home.names())
        if names and (self.spotter is None or self.spotter.names != names):
            self.spotter = CommandSpotter(names)
            log("command_spotter", names=names)
        return self.spotter

    def tap(self):
        """The screen was touched: wake up, or stop talking and listen."""
        self.tap_pending = True

    def toggle_mute(self):
        self.muted = not self.muted
        log("muted", on=self.muted)
        self.face.publish("muted", on=self.muted)

    # ---- speaking ----

    def _voice_worker(self):
        """Synthesise queued sentences and hand them to the player. None marks the end of a turn."""
        while True:
            cancel, text, timing = self.speech_q.get()
            if cancel.is_set():
                continue
            if text is None:
                timing["voice_done"] = time.time()
                continue
            audio = self.voice.synth(text)
            if cancel.is_set():
                continue
            if "first_audio" not in timing:
                timing["first_audio"] = time.time()
                self.set_state(SPEAKING)
            self.player.enqueue(audio)
            self.face.publish("speak", text=text)

    def say(self, text):
        """Speak a fixed line, blocking until it has played (startup greeting)."""
        self.player.enqueue(self.voice.synth(text))
        while self.player.busy:
            time.sleep(0.05)

    def announce(self, text):
        """Speak up unprompted (a timer firing). Waits for any conversation in progress to finish."""
        threading.Thread(target=self._announce, args=(text,), daemon=True).start()

    def _announce(self, text):
        with self.announce_lock:
            while self.turn is not None or self.state in (LISTENING, "starting"):
                time.sleep(0.2)
            log("announce", text=text)
            self.set_state(SPEAKING)
            self.player.enqueue(chime(rising=True))
            self.player.enqueue(chime(rising=True))
            self.player.enqueue(self.voice.synth(text))
            while self.player.busy:
                time.sleep(0.05)
            if self.turn is None and self.state == SPEAKING:
                self.set_state(IDLE)

    # ---- a turn ----

    def start_turn(self, audio, spoken_frames):
        cancel = threading.Event()
        self.turn = cancel
        self.set_state(THINKING)
        threading.Thread(target=self._run_turn, args=(audio, spoken_frames, cancel), daemon=True).start()

    def _save_debug(self, name, audio):
        sf.write(config.DEBUG_DIR / name, audio, 16000)
        for old in sorted(config.DEBUG_DIR.glob("*.wav"))[:-config.DEBUG_KEEP]:
            old.unlink()

    def _run_turn(self, audio, spoken_frames, cancel):
        """Any error in a turn is said out loud and the turn ends, so Jansky never freezes mid-turn."""
        try:
            self._turn(audio, spoken_frames, cancel)
        except Exception as e:
            log("error", error=repr(e), trace=traceback.format_exc(limit=4))
            self.face.publish("emotion", name="sad", seconds=2)
            self.speech_q.put((cancel, "Oh dear, something went wrong there. Try asking me again.", {}))
            time.sleep(0.5)
            while self.player.busy and not cancel.is_set():
                time.sleep(0.05)
            self._end_turn(cancel, follow_up=False)

    def _turn(self, audio, spoken_frames, cancel):
        """`audio` is 16 kHz, mono (injected) or (n, 2) with the reSpeaker's left and right channels."""
        timing = {"end_of_speech": time.time()}
        angles, self.heard_doa = self.heard_doa, []
        wav = time.strftime("%H%M%S") + "_heard.wav"
        self._save_debug(wav, audio)
        mono = audio if audio.ndim == 1 else audio[:, 0 if config.STT_CHANNEL == "left" else 1]
        level = 20 * np.log10(np.sqrt(np.mean(mono ** 2)) + 1e-9)
        if level < config.SILENT_DBFS:
            log("ignored", text="", level_dbfs=round(level, 1))
            self._end_turn(cancel, follow_up=False)
            return
        # The command spotter listens to the same audio on the CPU while Whisper runs on the HAT, so it costs no time.
        spot = {}

        def run_spotter():
            try:
                spotter = self._command_spotter()
                spot["hit"] = spotter.spot(np.ascontiguousarray(mono, dtype=np.float32)) if spotter else None
            except Exception as e:
                log("spotter_error", error=repr(e))
        spot_thread = threading.Thread(target=run_spotter, daemon=True)
        spot_thread.start()
        text = self.hat.transcribe(np.ascontiguousarray(mono))
        spot_thread.join(timeout=1.5)
        timing["transcript"] = time.time()
        doa = round(float(np.median(angles))) if angles else None
        log("heard", text=text, audio_s=round(len(mono) / 16000, 2), wav=wav, doa=doa, spotted=spot.get("hit"))
        self.face.publish("heard", text=WAKE_PREFIX.sub("", text).strip())
        if cancel.is_set():
            return
        heard, text = text, WAKE_PREFIX.sub("", text).strip()
        stripped_to_nothing = heard != text and not text
        wake_only = stripped_to_nothing or (is_wake_phrase(heard) and sentence_key(heard) not in NOISE)
        noise = not wake_only and text.lower().strip(" .!?") in NOISE
        if spoken_frames < 2 or wake_only or noise:
            log("ignored", text=heard, wake_phrase=wake_only)
            if not wake_only:
                self.face.publish("emotion", name="sad", seconds=1.5)
            self.relisten = wake_only  # it was just "hey Jarvis": the user is about to ask something
            self._end_turn(cancel, follow_up=False)
            return

        routed = self.router.route(text, spot.get("hit"))
        if routed:
            reply = self._speak_tool(routed, cancel, timing)
        else:
            reply = self._speak_llm(text, cancel, timing)
        self.speech_q.put((cancel, None, timing))
        timing["reply_done"] = time.time()
        log("said", text=reply, tool=self.router.last, context=self.hat.context_used)

        # The HAT is idle while the reply plays, so re-prime now if the context is nearly full.
        # A new memory is re-primed in too, so the very next answer can use it.
        if self.router.last == "goodnight":
            self.face.publish("emotion", name="sleepy", seconds=12)

        if not cancel.is_set() and (self.hat.needs_reset() or self.router.memory_changed):
            self.router.memory_changed = False
            self.hat.prime()
            log("reprimed", context=self.hat.context_used)

        while not cancel.is_set() and ("voice_done" not in timing or self.player.busy):
            time.sleep(0.03)
        if cancel.is_set():
            return
        t0 = timing["end_of_speech"]
        log("latency", **{k: round(v - t0, 2) for k, v in timing.items() if k != "end_of_speech"})
        self._end_turn(cancel, follow_up=True)

    def _speak_tool(self, routed, cancel, timing):
        opener, answer, card = routed
        timing["first_token"] = time.time()
        if card:
            self.face.publish("card", **card)
        chunker = Chunker()
        for piece in [opener] + chunker.feed(answer) + chunker.flush():
            self.speech_q.put((cancel, piece, timing))
        return f"{opener} {answer}"

    def _speak_llm(self, text, cancel, timing):
        """Stream Qwen3's reply into the voice. Stops at a repeated sentence or after MAX_SENTENCES:
        a 1.7B model at 4 bits drifts into loops, and a voice reply should be short anyway."""
        stop = threading.Event()
        chunker = Chunker()
        seen, sentences, spoken = set(), 0, []
        head = ""  # the reply's first word, to check it starts with a cached opener

        def emit(piece):
            nonlocal sentences
            if "[" in piece:  # a command in the middle of a reply: run it, never read it out
                for cmd in re.findall(r"\[([^\]]{1,60})\]", piece):
                    self._run_command(f"[{cmd}]", cancel, stop, timing)
                piece = re.sub(r"\[[^\]]*\]?", "", piece).strip()
                if not piece:
                    return
            key = sentence_key(piece)
            if key in seen or sentences >= config.MAX_SENTENCES:
                stop.set()
                return
            seen.add(key)
            if piece[-1:] in ".!?":
                sentences += 1
            spoken.append(piece)
            self.speech_q.put((cancel, piece, timing))

        self.acted = False
        command = ""  # "[on: fan]" at the start of a reply: the model acting (persona.SYSTEM)
        for token in self.hat.reply(text, stop):
            if cancel.is_set():
                stop.set()
            if "first_token" not in timing:
                timing["first_token"] = time.time()
            if command is not None:
                command += token
                lead = command.lstrip()
                if lead.startswith("[") and head is not None:
                    self.speech_q.put((cancel, "Okay,", timing))  # cached; covers the second or two the command takes
                    head = None  # that was the opener
                if not lead or (lead.startswith("[") and "]" not in lead and len(lead) < 80):
                    continue
                token, command = self._run_command(command, cancel, stop, timing), None
                if stop.is_set() or not token:
                    continue
            if head is not None:
                head += token
                if re.search(r"[\s,.!?]", head.lstrip()):
                    # No cached opener means the first sound waits for a whole sentence to be
                    # synthesised (4 s or more), so say a cached filler now.
                    if re.sub(r"[^a-z]", "", head.split()[0].lower()) not in OPENER_WORDS:
                        self.speech_q.put((cancel, random.choice(FILLERS), timing))
                    head = None
            for piece in chunker.feed(token):
                if not stop.is_set():
                    emit(piece)
        if not stop.is_set():
            for piece in chunker.flush():
                emit(piece)
        if not self.acted and not cancel.is_set():
            self._keep_promise(" ".join(spoken))
        return " ".join(spoken)

    def _keep_promise(self, reply):
        """The 1.7B model sometimes says it will act without writing the command ("Let's turn the fan on.").
        When it promises something Jansky can do, do it, so what it said is true."""
        for sentence in re.split(r"(?<=[.!?])\s+", reply):
            low = sentence.lower()
            if re.search(r"\b(can'?t|cannot|won'?t|don'?t|unable|not able|no need|shouldn'?t|if you)\b", low):
                continue
            out, what = None, None
            m = re.search(r"\bi'?ll remind you (to|about) (.+?)[.!?]*$", low)
            if m:
                what = f"remind me {m.group(1)} {m.group(2)}"
                out = self.router.reminders.handle(what)
            elif config.HA_TOKEN and not low.startswith(("is ", "are ")) and (
                    Home.claims(low.strip(" .!?")) or re.search(r"\b(keep|leave|get|have|put)\b.*\b(on|off)\b", low)):
                state = re.findall(r"\b(on|off)\b", low)[-1]
                keys, _ = self.router.home.match(low)
                if len(keys) == 1:  # only when it named one device clearly
                    what = f"turn {state} the {keys[0]}"
                    out = self.router.home.handle(what)
            if out:
                log("tool_call", command=what, result=out[1], source="reply_words")
                if len(out) == 3 and out[2] and out[0] not in ("Hmm,", "Oh dear,"):
                    self.face.publish("card", **out[2])
                return

    def _run_command(self, raw, cancel, stop, timing):
        """Execute the model's command and return the rest of what it wrote. If the tool fails, say what the
        tool said instead of the model's claim, so Jansky never says it did something it didn't."""
        raw = raw.lstrip()
        if not raw.startswith("["):
            return raw
        cmd, _, rest = raw[1:].partition("]")
        out = self.router.act(cmd)
        log("tool_call", command=cmd, result=out[1] if out else None)
        self.acted = self.acted or out is not None
        if out is None:
            return rest.lstrip()
        opener, answer, card = out
        if card:
            self.face.publish("card", **card)
        if opener in ("Hmm,", "Oh dear,"):
            stop.set()
            chunker = Chunker()
            for piece in chunker.feed(answer) + chunker.flush():
                self.speech_q.put((cancel, piece, timing))
            return ""
        return rest.lstrip()

    def _flush_reply_mic(self):
        frames, self.reply_mic = self.reply_mic, []
        if frames:
            wav = time.strftime("%H%M%S") + "_during_reply.wav"
            self._save_debug(wav, np.concatenate(frames))
            log("mic_during_reply", wav=wav, vad_peak=round(self.reply_vad_peak, 2))
        self.reply_vad_peak = 0.0

    def _end_turn(self, cancel, follow_up):
        if self.turn is cancel:
            self._flush_reply_mic()
            self.turn = None
            self.follow_up_until = time.time() + config.FOLLOW_UP_SECONDS if follow_up else 0.0
            self.vad.reset()
            self.set_state(IDLE)

    def interrupt(self):
        if self.turn:
            self.turn.set()
            self.turn = None
        self.player.clear()
        log("barge_in")
        self._flush_reply_mic()

    # ---- the mic loop ----

    def run(self):
        self.set_state(IDLE)
        self.say(f"{self.hat.greeting} Say {config.WAKE_PHRASE} to wake me.")
        self.mic.start()
        log("listening_for_wake_word", phrase=config.WAKE_PHRASE)

        preroll = collections.deque(maxlen=config.PREROLL_FRAMES)  # ((left, right), speech prob)
        utterance, spoken, silence, waited = [], 0, 0, 0
        trigger_run = 0

        def begin_listening(frames=(), already_spoken=0):
            nonlocal utterance, spoken, silence, waited
            utterance, spoken, silence, waited = [f for f, _ in frames], already_spoken, 0, 0
            self.set_state(LISTENING)

        last_wake = 0.0

        def woke():
            nonlocal last_wake
            last_wake = time.time()
            # Capture starts at the trigger, with no pre-roll: the spotter fires right at the end of
            # the phrase, so earlier audio is only the wake phrase, which Whisper mangles into a question.
            self.wake.reset()
            self.vad.reset()  # its state still holds the wake phrase, which read as speech
            self.player.enqueue(chime(rising=True))
            self.face.publish("wake")
            begin_listening()

        for frame in self.mic.frames():
            if self.quit_requested.is_set():
                log("quit", source="close button")
                self.player.clear()
                return
            left, _ = frame
            p = self.vad.prob(left)
            self.mic_level = float(np.sqrt(np.mean(left * left)))

            if self.tap_pending:
                self.tap_pending = False
                log("tap", state=self.state)
                if self.state in (THINKING, SPEAKING):
                    self.interrupt()
                if self.state != LISTENING:
                    woke()
                    continue

            if self.state == IDLE:
                preroll.append((frame, p))
                if self.relisten:
                    self.relisten = False
                    self.player.enqueue(chime(rising=True))
                    begin_listening()
                    continue
                if time.time() < self.follow_up_until:
                    trigger_run = trigger_run + 1 if p >= config.SPEECH_PROB else 0
                    if trigger_run >= config.FOLLOW_UP_FRAMES:
                        trigger_run = 0
                        log("follow_up")
                        begin_listening(preroll, config.FOLLOW_UP_FRAMES)
                        continue
                phrase = self.wake.detect(left)
                if phrase and self.muted:
                    log("wake_muted", phrase=phrase)
                elif phrase:
                    log("wake", phrase=phrase)
                    woke()
                continue

            if self.state == LISTENING:
                # "Hey Jarvis" said during a follow-up: start again from here instead of
                # sending the wake phrase to Whisper as the question.
                if self.wake.detect(left) and time.time() - last_wake > config.WAKE_REFRACTORY_S:
                    log("wake_in_turn")
                    woke()
                    continue
                utterance.append(frame)
                if p >= config.SPEECH_PROB:
                    spoken += 1
                    silence = 0
                elif spoken and p < config.SILENCE_PROB:
                    silence += 1
                else:
                    waited += 1
                if 0 < spoken < config.MIN_SPEECH_FRAMES and silence >= config.END_SILENCE_FRAMES:
                    spoken = silence = 0  # a blip, not a request: keep waiting
                done = spoken >= config.MIN_SPEECH_FRAMES and silence >= config.END_SILENCE_FRAMES
                if done or len(utterance) >= config.MAX_UTTERANCE_FRAMES:
                    audio = np.stack([np.concatenate([l for l, _ in utterance]),
                                      np.concatenate([r for _, r in utterance])], axis=1)
                    self.start_turn(audio, spoken)
                elif not spoken and waited >= config.NO_SPEECH_FRAMES:
                    log("no_speech")
                    self.player.enqueue(chime(rising=False))
                    self.set_state(IDLE)
                continue

            # THINKING or SPEAKING: listen for the user talking over Jansky.
            preroll.append((frame, p))
            self.reply_mic.append(left)
            self.reply_vad_peak = max(self.reply_vad_peak, p)
            if config.BARGE_IN:
                # Speech that the array places at Jansky's own speaker is echo leaking past the AEC, not the user.
                echo = self.doa.speaking and self.doa.angle is not None and self.from_speaker(self.doa.angle)
                trigger_run = trigger_run + 1 if p >= config.BARGE_IN_PROB and not echo else 0
                if trigger_run >= config.BARGE_IN_FRAMES:
                    trigger_run = 0
                    self.interrupt()
                    begin_listening(preroll, config.BARGE_IN_FRAMES)

    def run_injected(self, paths):
        """Test mode: each WAV is treated as one finished utterance, played turn by turn."""
        for path in paths:
            audio, rate = sf.read(path, dtype="float32")
            if audio.ndim > 1:
                audio = audio[:, 0]
            if rate != 16000:
                audio = resample_poly(audio, 16000, rate).astype(np.float32)
            log("inject", file=os.path.basename(path))
            self.start_turn(audio, spoken_frames=99)
            while self.turn is not None:
                time.sleep(0.05)


def _on_sigterm(signum, frame):
    # SIGTERM (systemd, kill) takes the same clean-up path as Ctrl+C.
    raise KeyboardInterrupt


def main():
    signal.signal(signal.SIGTERM, _on_sigterm)
    ap = argparse.ArgumentParser()
    ap.add_argument("--inject", nargs="*", help="WAV files to use as utterances instead of the mic")
    args = ap.parse_args()
    jansky = None
    try:
        jansky = Jansky()
        if args.inject:
            jansky.run_injected(args.inject)
        else:
            jansky.run()
    except KeyboardInterrupt:
        pass
    finally:
        # os._exit kills lingering audio threads (v1 lesson) but skips multiprocessing cleanup,
        # so take the HAT process down explicitly or it outlives us holding the NPU.
        if jansky is not None and hasattr(jansky, "hat"):
            jansky.hat.process.kill()
        # Without Jansky the full-screen face is useless: close it so the desktop comes back.
        subprocess.run(["pkill", "-f", "--", "--user-data-dir=.*jansky-face"], check=False)
        os._exit(0)


if __name__ == "__main__":
    main()
