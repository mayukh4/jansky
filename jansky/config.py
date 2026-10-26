"""Paths and tuning for Jansky v2. Everything here is read at startup."""
import os
from pathlib import Path

HOME = Path.home() / "jansky-v2"
MODELS = HOME / "models"


def _env(path):
    """KEY=value lines from ~/jansky-v2/.env (secrets live there, never in the code)."""
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"\'')
    return out


ENV = {**_env(HOME / ".env"), **os.environ}

# AI HAT+ 2 (Hailo-10H, HailoRT 5.3)
LLM_HEF = MODELS / "Qwen3-1.7B-Instruct.hef"
# Small, not Base: on Mayukh's live recordings Base misheard ("Once the weather now") and gave different
# text for the same audio run to run; Small got every request right for ~0.5 s more.
WHISPER_HEF = MODELS / "Whisper-Small.hef"

# Voice (CPU)
KOKORO_MODEL = MODELS / "kokoro-v1.0.int8.onnx"
KOKORO_VOICES = MODELS / "voices-v1.0.bin"
VOICE = "bm_george"
VOICE_LANG = "en-gb"
VOICE_SPEED = 1.05
OUTPUT_GAIN = 0.9

# reSpeaker XVF3800 on the c48k2ch firmware. Matched by name, never by card number.
AUDIO_DEVICE = "C48K2Ch"
DEVICE_RATE = 48000
BLOCK = 3840  # 80 ms at 48 kHz -> 1280 samples at 16 kHz per frame

# The face: served on this port, shown full screen by Chromium on the 800x480 panel.
FACE_PORT = 8765
CAPTIONS = True
# Direction of arrival, as the reSpeaker reports it. Calibrated 2026-10-21 with Mayukh at the desk:
# front 92-93, his left 147-149, his right 29-31. Angles grow toward the user's left, which is the left of the
# screen as he sees it, so the gaze is mirrored (sign -1).
DOA_FRONT_DEG = 91
DOA_SIGN = -1
# Jansky's own speaker sits at ~230 deg and the array reports it as speech after replies. Ignore that sector.
SPEAKER_DOA_DEG = 230
SPEAKER_DOA_HALF_WIDTH = 35
WAKE_REFRACTORY_S = 1.5  # ignore a second trigger on the tail of the same phrase
RESPEAKER_PIDS = (0x001D, 0x001C, 0x001A)  # c48k2ch, c16k2ch, other USB firmware

# Wake word: "Hey Jansky", by sherpa-onnx keyword spotting (listen.py). Spellings cover how Mayukh says it
# (run together, closer to "Jensky"). The bare name is stricter, so a passing mention is less likely to wake it.
KWS_DIR = MODELS / "kws" / "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
WAKE_PHRASE = "hey Jansky"
WAKE_PHRASES = ["HEY JANSKY", "HEY JENSKY", "HEY JANSKI", "HEJANSKI", "HEJENSKI", "HI JANSKY", "HI JUNSKI",
                "HEY JUN SKI", "HEY JUNSKY"]
# Tuned on Mayukh's real recording streamed exactly as the mic loop does: 8/8 wakes. False wakes only on
# contrived near-misses ("Hey, Jan, ski season", "Jen's key is on the table").
WAKE_SENSITIVITY = (2.0, 0.1)        # (boost, threshold) for the phrases
WAKE_NAMES = ["JANSKY", "JENSKY", "JANSKI"]
WAKE_NAME_SENSITIVITY = (1.5, 0.25)  # the bare name alone
# Home commands, spotted in the finished utterance (listen.CommandSpotter). Tuned 2026-10-22 on 28 real recordings:
# both of Mayukh's misheard commands caught, no false hits. (1.0, 0.35) missed "turn on desk light".
COMMAND_SENSITIVITY = (1.5, 0.25)

# Turn-taking, in 80 ms frames
SPEECH_PROB = 0.5        # Silero probability that counts as speech
SILENCE_PROB = 0.35      # below this counts as silence once speech has started
END_SILENCE_FRAMES = 9   # ~0.7 s of silence ends the utterance
NO_SPEECH_FRAMES = 62    # ~5 s after the wake word with no speech: give up
MAX_UTTERANCE_FRAMES = 190  # ~15 s
FOLLOW_UP_SECONDS = 8.0  # after an answer, talk again without the wake word
FOLLOW_UP_FRAMES = 3     # ~0.25 s of speech opens a follow-up turn
PREROLL_FRAMES = 5       # ~0.4 s kept from before a VAD-triggered start
MIN_SPEECH_FRAMES = 3    # ~0.25 s of speech before an utterance counts; shorter blips are ignored
SILENT_DBFS = -50        # utterances quieter than this skip Whisper (it invents text on silence)

# Barge-in: talking over Jansky stops it. Relies on the reSpeaker's AEC removing Jansky's own voice.
BARGE_IN = True
BARGE_IN_PROB = 0.7
BARGE_IN_FRAMES = 4      # ~0.3 s

# Which reSpeaker channel Whisper hears: "left" (processed beam) or "right" (the XVF3800's ASR beam).
STT_CHANNEL = "left"  # the right (ASR beam) was 22 dB quieter and no more accurate

# Home, for weather (Open-Meteo, no key)
HOME_CITY = ENV.get("HOME_CITY", "Kingston")
HOME_LAT, HOME_LON = float(ENV.get("HOME_LAT", 44.2312)), float(ENV.get("HOME_LON", -76.4860))

# What Jansky calls you (JANSKY_USER in ~/jansky-v2/.env).
USER = ENV.get("JANSKY_USER", "friend")

# Memory: what Jansky knows about you, read into the prompt (memory.py). Editable by hand.
MEMORY_FILE = HOME / "memory.md"
MEMORY_IN_PROMPT = 15  # newest facts that go into the 2048-token context

# Reminders and to-dos: a Markdown checklist (todo.py), shown on the screen until closed by touch.
TODO_FILE = HOME / "todo.md"

# Home Assistant (home.py): REST API with a long-lived token. Both go in ~/jansky-v2/.env:
#   HA_URL=http://homeassistant.local:8123
#   HA_TOKEN=...
# No token: the home tools say so instead of failing.
HA_URL = ENV.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
HA_TOKEN = ENV.get("HA_TOKEN", "")
HA_DOMAINS = ("light", "switch", "fan", "input_boolean")
HOME_DEVICES_FILE = HOME / "cache" / "home_devices.txt"  # device names for the persona, written on each refresh
# Entities that are settings of a device, not things to switch on and off.
HA_SKIP = ("child lock", "indicator", "power-on behavior", "sensor")

# Brain
MAX_SENTENCES = 3  # voice replies stop after this many sentences, or at the first repeated one
CONTEXT_RESET_TOKENS = 1500  # Qwen3 on the HAT has 2048; re-prime before it fills
MAX_REPLY_TOKENS = 160
# Qwen's recommended non-thinking sampling plus a strong frequency penalty. Without the penalty the
# 4-bit model loops ("super, super, super..."); at 0.5 it collapsed into "I! I! I!". 1.0 was clean.
SAMPLING = dict(temperature=0.7, top_p=0.8, top_k=20, frequency_penalty=1.0)
WHISPER_REPETITION_PENALTY = 1.2

# Every utterance (and what the mic heard while Jansky spoke) is saved here for debugging.
DEBUG_DIR = HOME / "debug"
DEBUG_KEEP = 60
