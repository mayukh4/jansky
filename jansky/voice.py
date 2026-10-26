"""Jansky's voice: Kokoro-82M int8 on the CPU (Hailo ships no TTS model), spoken a piece at a time.

Kokoro needs ~1 s even for two words on the Pi 5, so the short openers Jansky starts replies with
("Right,", "Ah,", "Sure thing.") are synthesised once and cached on disk. The first sound then plays
as soon as the LLM's first tokens arrive, while the rest of the sentence is still being synthesised.
"""
import re

import numpy as np
import onnxruntime as ort
from kokoro_onnx import Kokoro

from . import config
from .audio import to_device_rate
from .persona import OPENERS

CACHE = config.HOME / "cache" / f"openers_{config.VOICE}_{config.VOICE_SPEED}.npz"


def cache_key(text):
    return re.sub(r"[^a-z ]", "", text.lower()).strip()


class Voice:
    def __init__(self):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 3  # leave a core for the mic loop
        session = ort.InferenceSession(str(config.KOKORO_MODEL), opts, providers=["CPUExecutionProvider"])
        self.kokoro = Kokoro.from_session(session, str(config.KOKORO_VOICES))
        self.cache = self._load_openers()

    def _load_openers(self):
        if CACHE.exists():
            data = np.load(CACHE)
            if sorted(data.files) == sorted(cache_key(o) for o in OPENERS):
                return {k: data[k] for k in data.files}
        cache = {cache_key(o): self._synth(o) for o in OPENERS}
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        np.savez(CACHE, **cache)
        return cache

    def _synth(self, text):
        audio, rate = self.kokoro.create(text, voice=config.VOICE, speed=config.VOICE_SPEED, lang=config.VOICE_LANG)
        return to_device_rate(audio, rate)

    def synth(self, text):
        cached = self.cache.get(cache_key(text))
        return cached if cached is not None else self._synth(text)


SENTENCE_END = re.compile(r"[.!?;:](?=\s|$)")


class Chunker:
    """Turns a token stream into speakable pieces.

    The first piece ends at the first punctuation mark, so a cached opener plays at once. After that,
    whole sentences, with long ones split at a comma.
    """

    MIN_LEN = 12
    LONG = 60  # split long sentences at a comma: Kokoro needs ~1 s per 6 words before any sound

    def __init__(self):
        self.buf = ""
        self.first = True

    def _cut(self):
        if self.first:
            m = re.search(r"[,.!?;:](?=\s)", self.buf)
            return m.end() if m else None
        for m in SENTENCE_END.finditer(self.buf):
            if m.end() >= self.MIN_LEN:
                return m.end()
        if len(self.buf) > self.LONG:
            i = self.buf.rfind(", ", 0, self.LONG)
            if i > self.MIN_LEN:
                return i + 1
        return None

    def feed(self, text):
        self.buf += text
        out = []
        while (cut := self._cut()) is not None:
            piece, self.buf = self.buf[:cut].strip(), self.buf[cut:]
            self.first = False
            if piece:
                out.append(piece)
        return out

    def flush(self):
        piece, self.buf = self.buf.strip(), ""
        return [piece] if piece else []
