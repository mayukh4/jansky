"""reSpeaker audio in and out.

Jansky's voice must play through the reSpeaker itself: that stream is the echo reference the
XVF3800 cancels, which is what makes barge-in possible.
"""
import collections
import queue
import threading

import numpy as np
import sounddevice as sd
from scipy.signal import firwin, lfilter, resample_poly

from . import config


def find_device():
    for i, d in enumerate(sd.query_devices()):
        if config.AUDIO_DEVICE in d["name"]:
            return i
    raise RuntimeError(
        f"reSpeaker '{config.AUDIO_DEVICE}' not found. Check it is on a USB-A port and on the "
        f"c48k2ch firmware: arecord -l should list card {config.AUDIO_DEVICE}."
    )


class Decimator:
    """48 kHz -> 16 kHz with filter state carried across blocks, so 80 ms frames join cleanly."""

    def __init__(self):
        self.taps = firwin(63, 7200, fs=config.DEVICE_RATE)
        self.zi = np.zeros(len(self.taps) - 1)

    def __call__(self, x48):
        y, self.zi = lfilter(self.taps, 1.0, x48, zi=self.zi)
        return y[::3].astype(np.float32)


class Mic:
    """Captures both reSpeaker channels and yields 80 ms frames at 16 kHz as (left, right).

    Left is the processed, auto-selected beam (for wake word and VAD). Right is the XVF3800's ASR beam,
    the output it tunes for speech recognition.
    """

    def __init__(self, device):
        self.q = queue.Queue()
        self.decimate_left, self.decimate_right = Decimator(), Decimator()
        self.stream = sd.InputStream(
            device=device, samplerate=config.DEVICE_RATE, channels=2, dtype="int16",
            blocksize=config.BLOCK, latency="high", callback=self._callback,
        )

    def _callback(self, indata, frames, time_info, status):
        # Only queue here: any real work in the callback causes input overflows.
        self.q.put(bytes(indata))

    def start(self):
        self.stream.start()

    def stop(self):
        self.stream.stop()

    def frames(self):
        while True:
            raw = self.q.get()
            if raw is None:
                return
            pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            yield self.decimate_left(pcm[0::2]), self.decimate_right(pcm[1::2])


class Player:
    """Always-open output stream. enqueue() adds mono 48 kHz audio, clear() cuts it within ~20 ms."""

    def __init__(self, device):
        self.chunks = collections.deque()
        self.offset = 0
        self.lock = threading.Lock()
        self.level = 0.0  # output RMS, for the face
        self.stream = sd.OutputStream(
            device=device, samplerate=config.DEVICE_RATE, channels=2, dtype="float32",
            blocksize=960, callback=self._callback,
        )
        self.stream.start()

    def _callback(self, outdata, frames, time_info, status):
        buf = np.zeros(frames, dtype=np.float32)
        pos = 0
        with self.lock:
            while pos < frames and self.chunks:
                chunk = self.chunks[0]
                take = min(len(chunk) - self.offset, frames - pos)
                buf[pos:pos + take] = chunk[self.offset:self.offset + take]
                pos += take
                self.offset += take
                if self.offset >= len(chunk):
                    self.chunks.popleft()
                    self.offset = 0
        # Left carries the AEC reference; the DAC plays left on both outputs anyway.
        outdata[:, 0] = buf
        outdata[:, 1] = buf
        self.level = float(np.sqrt(np.mean(buf * buf)))

    def enqueue(self, audio48):
        with self.lock:
            self.chunks.append(np.asarray(audio48, dtype=np.float32) * config.OUTPUT_GAIN)

    def clear(self):
        with self.lock:
            self.chunks.clear()
            self.offset = 0

    @property
    def busy(self):
        with self.lock:
            return bool(self.chunks)


def to_device_rate(audio, rate):
    if rate == config.DEVICE_RATE:
        return audio.astype(np.float32)
    return resample_poly(audio, config.DEVICE_RATE, rate).astype(np.float32)


def chime(rising=True):
    """Short two-note earcon: rising when Jansky starts listening, falling when it gives up."""
    notes = (660, 990) if rising else (660, 440)
    out = []
    for f in notes:
        t = np.arange(int(0.07 * config.DEVICE_RATE)) / config.DEVICE_RATE
        tone = 0.08 * np.sin(2 * np.pi * f * t) * np.hanning(len(t))
        out.append(tone)
    return np.concatenate(out).astype(np.float32)
