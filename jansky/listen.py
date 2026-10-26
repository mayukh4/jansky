"""Wake word ("Hey Jansky", sherpa-onnx keyword spotting) and voice activity (Silero VAD v4)."""
import os

import numpy as np
import onnxruntime as ort
import sentencepiece as spm
import sherpa_onnx

from . import config



def silero_path():
    """Silero VAD v4, the copy openWakeWord ships. install.sh puts it in models/; older setups only have it
    inside the openwakeword package."""
    path = config.MODELS / "silero_vad.onnx"
    if path.exists():
        return str(path)
    import openwakeword
    return os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models", "silero_vad.onnx")


class WakeWord:
    """Open-vocabulary keyword spotting: the wake phrase is spelled out, not trained.

    v1's trained "hey_jansky" model never worked (scores < 0.02). This 3.3M-parameter streaming
    zipformer caught 8/8 of Mayukh's real "Hey Jansky"s once his pronunciations were listed
    (he runs it together: "Hejanski", "Hey Jensky"), using ~4 % of one core.
    """

    def __init__(self):
        d = config.KWS_DIR
        sp = spm.SentencePieceProcessor(model_file=str(d / "bpe.model"))
        lines = []
        for phrases, (score, thr) in ((config.WAKE_PHRASES, config.WAKE_SENSITIVITY),
                                      (config.WAKE_NAMES, config.WAKE_NAME_SENSITIVITY)):
            for p in phrases:
                lines.append(" ".join(sp.encode(p, out_type=str)) + f" :{score} #{thr} @{p.replace(' ', '_')}")
        keywords = config.HOME / "cache" / "keywords.txt"
        keywords.parent.mkdir(parents=True, exist_ok=True)
        keywords.write_text("\n".join(lines) + "\n")
        model = "epoch-12-avg-2-chunk-16-left-64.int8.onnx"
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=str(d / "tokens.txt"), encoder=str(d / f"encoder-{model}"), decoder=str(d / f"decoder-{model}"),
            joiner=str(d / f"joiner-{model}"), num_threads=1, keywords_file=str(keywords), provider="cpu")
        self.stream = self.kws.create_stream()

    def detect(self, frame16k):
        """Feed one frame; return the matched phrase, or None."""
        self.stream.accept_waveform(16000, frame16k)
        while self.kws.is_ready(self.stream):
            self.kws.decode_stream(self.stream)
            hit = self.kws.get_result(self.stream)
            if hit:
                self.kws.reset_stream(self.stream)
                return hit
        return None

    def reset(self):
        self.kws.reset_stream(self.stream)


class CommandSpotter:
    """A second ear for home commands, run over the finished utterance while Whisper transcribes it.

    Whisper-Small on the HAT hears short commands badly ("Turn on, send." for "turn on fan", "Turn on Disclate."
    for "turn on desk light"), and the HAT's Whisper takes no vocabulary prompt. The keyword spotter is told the
    exact phrases ("TURN ON THE FAN" ...), so it only has to decide which of a few it heard. On Mayukh's real
    recordings it caught both misheard commands with no false hits on 24 other requests, in 0.1-0.4 s of CPU.
    """

    def __init__(self, names):
        d = config.KWS_DIR
        self.sp = spm.SentencePieceProcessor(model_file=str(d / "bpe.model"))
        self.names = sorted(names)
        phrases = []
        for n in self.names:
            n = n.upper()
            for on in ("ON", "OFF"):
                phrases += [f"TURN {on} THE {n}", f"TURN {on} {n}", f"SWITCH {on} THE {n}", f"TURN THE {n} {on}"]
        score, thr = config.COMMAND_SENSITIVITY
        lines = [" ".join(self.sp.encode(p, out_type=str)) + f" :{score} #{thr} @{p.replace(' ', '_')}" for p in phrases]
        keywords = config.HOME / "cache" / "home_keywords.txt"
        keywords.parent.mkdir(parents=True, exist_ok=True)
        keywords.write_text("\n".join(lines) + "\n")
        model = "epoch-12-avg-2-chunk-16-left-64.int8.onnx"
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=str(d / "tokens.txt"), encoder=str(d / f"encoder-{model}"), decoder=str(d / f"decoder-{model}"),
            joiner=str(d / f"joiner-{model}"), num_threads=2, keywords_file=str(keywords), provider="cpu")

    def spot(self, audio16k):
        """('fan', True) for "turn on the fan", or None."""
        s = self.kws.create_stream()
        s.accept_waveform(16000, np.concatenate([audio16k, np.zeros(8000, np.float32)]))
        s.input_finished()
        hit = None
        while self.kws.is_ready(s):
            self.kws.decode_stream(s)
            r = self.kws.get_result(s)
            if r:
                hit = r  # the last phrase heard wins ("turn on the fan... no, turn it off" is rare; last is right)
                self.kws.reset_stream(s)
        if not hit:
            return None
        words = hit.lower().split("_")
        on = "on" in words
        name = " ".join(w for w in words if w not in ("turn", "switch", "the", "on", "off"))
        return name, on


class VAD:
    """Silero VAD v4 on 512-sample windows. prob() takes any frame length and returns the max."""

    WINDOW = 512

    def __init__(self):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        self.session = ort.InferenceSession(silero_path(), opts)
        self.sr = np.array(16000, dtype=np.int64)
        self.carry = np.zeros(0, dtype=np.float32)
        self.reset()

    def reset(self):
        self.h = np.zeros((2, 1, 64), dtype=np.float32)
        self.c = np.zeros((2, 1, 64), dtype=np.float32)

    def prob(self, frame16k):
        x = np.concatenate([self.carry, frame16k])
        best = 0.0
        while len(x) >= self.WINDOW:
            win, x = x[:self.WINDOW], x[self.WINDOW:]
            out, self.h, self.c = self.session.run(
                None, {"input": win[None, :], "sr": self.sr, "h": self.h, "c": self.c})
            best = max(best, float(out[0][0]))
        self.carry = x
        return best
