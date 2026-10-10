"""Local speech-to-text runner, executed with an engine's private venv python.

Usage: python asr_runner.py --engine parakeet|whisper --audio a.wav --out result.json
       python asr_runner.py --engine ... --download-only

Prints "PROGRESS <0-100>" lines while working; writes {"words", "segments", "language"} JSON to --out.
Only the standard library plus the engine package are available here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

DEFAULT_MODELS = {
    "parakeet": "mlx-community/parakeet-tdt-0.6b-v3",
    "whisper": "turbo",
}


def emit(percent: float) -> None:
    print(f"PROGRESS {max(0.0, min(99.0, percent)):.1f}", flush=True)


def run_parakeet(args) -> dict:
    import inspect

    from parakeet_mlx import from_pretrained

    model = from_pretrained(args.model or DEFAULT_MODELS["parakeet"])
    if args.download_only:
        return {}
    kwargs = {"chunk_duration": 120.0, "overlap_duration": 15.0}
    if "chunk_callback" in inspect.signature(model.transcribe).parameters:
        def on_chunk(*cb):
            if len(cb) >= 2 and cb[1]:
                emit(cb[0] / cb[1] * 100)
        kwargs["chunk_callback"] = on_chunk
    result = model.transcribe(args.audio, **kwargs)
    words, segments = [], []
    for sentence in result.sentences:
        segments.append({"text": sentence.text.strip(), "start": sentence.start, "end": sentence.end})
        current = None
        # Tokens are sub-word pieces; a leading space marks the start of a new word.
        for token in sentence.tokens:
            if current is None or token.text.startswith(" "):
                if current and current["text"]:
                    words.append(current)
                current = {"text": token.text.strip(), "start": token.start, "end": token.end}
            else:
                current["text"] += token.text
                current["end"] = token.end
        if current and current["text"]:
            words.append(current)
    return {"words": words, "segments": segments, "language": None}


def _load_wav(path: str):
    """Read the 16 kHz mono PCM WAV prepared by the app into float32 samples.

    Passing an array skips faster-whisper's PyAV decoding, which breaks across PyAV releases.
    """
    import wave

    import numpy as np

    with wave.open(path, "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 1 or wav.getframerate() != 16000:
            raise ValueError("expected 16 kHz mono 16-bit WAV")
        frames = wav.readframes(wav.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def run_whisper(args) -> dict:
    from faster_whisper import WhisperModel

    device, compute = "cpu", "int8"
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            device, compute = "cuda", "float16"
    except Exception:
        pass
    model = WhisperModel(args.model or DEFAULT_MODELS["whisper"], device=device, compute_type=compute,
                         cpu_threads=os.cpu_count() or 4)
    if args.download_only:
        return {}
    segments_iter, info = model.transcribe(
        _load_wav(args.audio),
        language=None if args.language in ("", "auto") else args.language,
        word_timestamps=True,
        vad_filter=True,
        beam_size=5,
        condition_on_previous_text=False,
    )
    words, segments = [], []
    for seg in segments_iter:
        segments.append({"text": seg.text.strip(), "start": seg.start, "end": seg.end})
        for w in seg.words or []:
            if w.word.strip():
                words.append({"text": w.word.strip(), "start": w.start, "end": w.end})
        if args.duration > 0:
            emit(seg.end / args.duration * 100)
    return {"words": words, "segments": segments, "language": info.language}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True, choices=sorted(DEFAULT_MODELS))
    parser.add_argument("--audio", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--language", default="auto")
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--model", default=os.environ.get("YTGRAB_ASR_MODEL", ""))
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()
    try:
        result = (run_parakeet if args.engine == "parakeet" else run_whisper)(args)
    except Exception as exc:  # report to the parent process in one line
        print(f"ERROR {type(exc).__name__}: {exc}", flush=True)
        return 1
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False)
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
