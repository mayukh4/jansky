"""Everything on the AI HAT+ 2: Whisper (ears) and Qwen3 (brain) on one shared VDevice.

They run in their own process. HailoRT's Python binding holds the GIL while it waits for each token,
which starved the voice and the mic loop when it shared a process with them (Kokoro went from 2 s to
7 s per sentence). The main process talks to it through `HatClient`.
"""
import multiprocessing as mp
import re
import threading

from . import config

NAME_FIXES = re.compile(r"\b(jan ?ski|janskie|janskye|jensky|yansky)\b", re.I)
EMOJI = re.compile("[%s-%s%s-%s%s%s]" % tuple(map(chr, (0x1F000, 0x1FAFF, 0x2600, 0x27BF, 0xFE0F, 0x200D))))
STOP_TOKENS = ("<|im_end|>", "<|endoftext|>")


class Hat:
    """Lives inside the HAT process."""

    def __init__(self, prime_messages):
        from hailo_platform import VDevice
        from hailo_platform.genai import LLM, Speech2Text

        self.vdevice = VDevice()
        self.stt = Speech2Text(self.vdevice, str(config.WHISPER_HEF))
        self.llm = LLM(self.vdevice, str(config.LLM_HEF))
        self.prime_messages = prime_messages

    def transcribe(self, audio16k):
        from hailo_platform.genai import Speech2TextTask

        # The penalty stops Whisper's "5, 5, 5, 5..." decoding loops on short or noisy audio.
        text = self.stt.generate_all_text(audio16k, task=Speech2TextTask.TRANSCRIBE, language="en",
                                          repetition_penalty=config.WHISPER_REPETITION_PENALTY)
        return NAME_FIXES.sub("Jansky", text.strip())

    def prime(self):
        """Load the persona into a fresh context. Each turn then appends only the user's sentence,
        which keeps the first token at ~0.6 s instead of re-reading the whole prompt.
        The prime ends by asking for a greeting, which is returned for Jansky to say at boot."""
        self.llm.clear_context()
        greeting = ""
        with self.llm.generate(prompt=self.prime_messages(), max_generated_tokens=40, **config.SAMPLING) as gen:
            for token in gen:
                greeting += token
        for stop in STOP_TOKENS:
            greeting = greeting.replace(stop, "")
        return EMOJI.sub("", greeting).replace("*", "").strip()

    def reply(self, text, cancel):
        prompt = [{"role": "user", "content": text}]
        with self.llm.generate(prompt=prompt, max_generated_tokens=config.MAX_REPLY_TOKENS,
                               **config.SAMPLING) as gen:
            in_think = False
            for token in gen:
                if cancel.is_set():
                    gen.release()
                    return
                if "<think>" in token:
                    in_think = True
                if in_think:
                    in_think = "</think>" not in token
                    continue
                for stop in STOP_TOKENS:
                    token = token.replace(stop, "")
                token = EMOJI.sub("", token).replace("*", "")
                if token:
                    yield token


def _serve(requests, results, cancel):
    from .persona import prime_messages

    hat = Hat(prime_messages)
    greeting = hat.prime()
    results.put(("ready", (hat.llm.get_context_usage_size(), greeting)))
    while True:
        cmd, arg = requests.get()
        if cmd == "transcribe":
            results.put(("text", hat.transcribe(arg)))
        elif cmd == "prime":
            hat.prime()
            results.put(("primed", hat.llm.get_context_usage_size()))
        elif cmd == "reply":
            for piece in hat.reply(arg, cancel):
                results.put(("token", piece))
            results.put(("end", hat.llm.get_context_usage_size()))


class HatClient:
    """Main-process handle on the HAT process. One request at a time."""

    def __init__(self):
        ctx = mp.get_context("spawn")
        self.requests, self.results, self.cancel = ctx.Queue(), ctx.Queue(), ctx.Event()
        self.process = ctx.Process(target=_serve, args=(self.requests, self.results, self.cancel), daemon=True)
        self.process.start()
        self.lock = threading.Lock()
        _, (self.context_used, self.greeting) = self.results.get()

    def transcribe(self, audio16k):
        with self.lock:
            self.requests.put(("transcribe", audio16k))
            return self.results.get()[1]

    def prime(self):
        with self.lock:
            self.requests.put(("prime", None))
            self.context_used = self.results.get()[1]

    def needs_reset(self):
        return self.context_used > config.CONTEXT_RESET_TOKENS

    def reply(self, text, cancel):
        """Yield reply text as it is generated. Setting `cancel` stops generation on the HAT."""
        with self.lock:
            self.cancel.clear()
            self.requests.put(("reply", text))
            while True:
                if cancel.is_set():
                    self.cancel.set()
                kind, value = self.results.get()
                if kind == "end":
                    self.context_used = value
                    return
                if not cancel.is_set():
                    yield value
