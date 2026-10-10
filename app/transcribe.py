"""Video/audio -> SRT transcription.

Engines:
  parakeet - NVIDIA Parakeet TDT 0.6B v3 via parakeet-mlx; local, Apple Silicon only, 25 European languages.
  whisper  - Whisper large-v3-turbo via faster-whisper; local, any computer (CPU or CUDA), 99 languages.
  groq     - Whisper large-v3-turbo on Groq's API; free key, very fast, audio leaves the computer.

Local engines live in their own venvs under ~/.ytgrab/asr/<engine>, created with uv so the right
Python is fetched automatically. They are driven through asr_runner.py.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

APP_DIR = Path(__file__).resolve().parent
YTGRAB_DIR = Path.home() / ".ytgrab"
ASR_DIR = YTGRAB_DIR / "asr"
TOOLS_DIR = YTGRAB_DIR / "tools"
RUNNER = APP_DIR / "asr_runner.py"
GROQ_URL = os.environ.get("YTGRAB_GROQ_URL", "https://api.groq.com/openai/v1/audio/transcriptions")
GROQ_MODEL = "whisper-large-v3-turbo"
GROQ_MAX_BYTES = 24 * 1024 * 1024
GROQ_CHUNK_SECONDS = 1800

MAX_LINE = 42
MAX_CUE_CHARS = 84
MAX_CUE_SECONDS = 6.0
MIN_CUE_SECONDS = 1.0
SPLIT_PAUSE = 0.8

LANGUAGES = [
    ("auto", "Автоматично"), ("uk", "Українська"), ("en", "English"), ("ru", "Русский"), ("pl", "Polski"),
    ("de", "Deutsch"), ("fr", "Français"), ("es", "Español"), ("it", "Italiano"), ("pt", "Português"),
]
MEDIA_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".opus", ".ogg", ".flac", ".wav", ".m4v", ".avi"}

ENGINES = {
    "parakeet": {
        "name": "Parakeet v3",
        "packages": ["parakeet-mlx"],
        "python": "3.12",
        "local": True,
    },
    "whisper": {
        "name": "Whisper turbo",
        "packages": ["faster-whisper"],
        "python": "3.12",
        "local": True,
    },
    "groq": {"name": "Groq (Whisper)", "local": False},
}


def is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def engine_dir(engine: str) -> Path:
    return ASR_DIR / engine


def engine_python(engine: str) -> Path:
    if sys.platform.startswith("win"):
        return engine_dir(engine) / "Scripts" / "python.exe"
    return engine_dir(engine) / "bin" / "python"


def engine_ready(engine: str) -> bool:
    return (engine_dir(engine) / ".ready").exists() and engine_python(engine).exists()


def engines_status(settings: dict) -> list[dict]:
    return [
        {"key": "parakeet", "name": ENGINES["parakeet"]["name"], "installed": engine_ready("parakeet"),
         "supported": is_apple_silicon()},
        {"key": "whisper", "name": ENGINES["whisper"]["name"], "installed": engine_ready("whisper"),
         "supported": True},
        {"key": "groq", "name": ENGINES["groq"]["name"], "installed": bool(settings.get("groq_api_key")),
         "supported": True},
    ]


# ---------- setup ----------

def uv_bin() -> str | None:
    bundled = os.environ.get("YTGRAB_UV")  # the macOS app ships its own uv
    if bundled and os.access(bundled, os.X_OK):
        return bundled
    found = shutil.which("uv")
    if found:
        return found
    own = TOOLS_DIR / ("Scripts/uv.exe" if sys.platform.startswith("win") else "bin/uv")
    return str(own) if own.exists() else None


def setup_steps(engine: str, python_for_bootstrap: str) -> list[tuple[str, list[str]]]:
    """Commands that install a local engine end to end, including the model download."""
    spec = ENGINES[engine]
    if not spec.get("local"):
        raise ValueError("Цей рушій не потребує встановлення")
    if engine == "parakeet" and not is_apple_silicon():
        raise ValueError("Parakeet працює лише на Маках з чипом Apple (M1 і новіші)")
    steps: list[tuple[str, list[str]]] = []
    uv = uv_bin()
    if not uv:
        tools_python = TOOLS_DIR / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
        steps.append(("готую інсталятор", [python_for_bootstrap, "-m", "venv", str(TOOLS_DIR)]))
        steps.append(("ставлю uv", [str(tools_python), "-m", "pip", "install", "--disable-pip-version-check", "-q", "uv"]))
        uv = str(TOOLS_DIR / ("Scripts/uv.exe" if sys.platform.startswith("win") else "bin/uv"))
    target = engine_dir(engine)
    steps.append((f"готую Python {spec['python']}", [uv, "venv", "--allow-existing", "--python", spec["python"], str(target)]))
    steps.append((f"ставлю {', '.join(spec['packages'])}",
                  [uv, "pip", "install", "--python", str(engine_python(engine)), "--upgrade", *spec["packages"]]))
    steps.append(("завантажую модель (один раз, може зайняти кілька хвилин)",
                  [str(engine_python(engine)), str(RUNNER), "--engine", engine, "--download-only"]))
    return steps


def mark_ready(engine: str) -> None:
    (engine_dir(engine) / ".ready").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), "utf-8")


# ---------- media helpers ----------

def media_probe(path: str) -> dict:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ValueError("ffprobe не знайдено: brew install ffmpeg")
    proc = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise ValueError("Не вдалося прочитати файл")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise ValueError("У файлі немає звуку")
    return {
        "path": path,
        "duration": float((data.get("format") or {}).get("duration") or 0),
        "acodec": audio.get("codec_name"),
        "has_video": any(s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
                         for s in streams),
    }


def extract_audio(src: str, dst: str, start: float | None = None, end: float | None = None, codec: str = "wav") -> None:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-to", f"{end:.3f}"]
    cmd += ["-i", src, "-vn", "-ac", "1", "-ar", "16000"]
    cmd += ["-c:a", "libopus", "-b:a", "24k"] if codec == "opus" else ["-c:a", "pcm_s16le"]
    proc = subprocess.run(cmd + [dst], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg не зміг дістати звук: " + (proc.stderr.strip().splitlines() or ["?"])[-1])


def silence_points(src: str) -> list[float]:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", src, "-vn", "-ac", "1",
                           "-af", "silencedetect=n=-35dB:d=0.4", "-f", "null", "-"],
                          capture_output=True, text=True)
    starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", proc.stderr)]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", proc.stderr)]
    return [(s + e) / 2 for s, e in zip(starts, ends)]


def chunk_bounds(duration: float, chunk: float, silences: list[float]) -> list[tuple[float, float]]:
    """Split [0, duration] near every `chunk` seconds, preferring a silence within 90 s of the target."""
    bounds, start = [], 0.0
    while duration - start > chunk:
        target = start + chunk
        near = [s for s in silences if abs(s - target) <= 90 and s > start + 60]
        cut = min(near, key=lambda s: abs(s - target)) if near else target
        bounds.append((start, cut))
        start = cut
    bounds.append((start, duration))
    return bounds


# ---------- engines ----------

def run_local(engine: str, wav: str, language: str, duration: float, on_progress: Callable[[float], None],
              register_proc: Callable[[subprocess.Popen], None]) -> dict:
    if not engine_ready(engine):
        raise RuntimeError(f"{ENGINES[engine]['name']} ще не встановлено: натисни «Встановити» в розділі Субтитри")
    fd, out = tempfile.mkstemp(prefix="ytgrab-asr-", suffix=".json")
    os.close(fd)
    try:
        cmd = [str(engine_python(engine)), str(RUNNER), "--engine", engine, "--audio", wav, "--out", out,
               "--language", language, "--duration", str(duration)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)
        register_proc(proc)
        error, tail = "", []
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.strip()
            if line.startswith("PROGRESS "):
                try:
                    on_progress(float(line.split()[1]))
                except ValueError:
                    pass
            elif line.startswith("ERROR "):
                error = line[6:]
            elif line:
                tail = (tail + [line])[-5:]
        if proc.wait() != 0:
            raise RuntimeError(error or (tail[-1] if tail else "розпізнавання не вдалося"))
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def _multipart(fields: list[tuple[str, str]], file_field: str, filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = "----ytgrab" + uuid.uuid4().hex
    parts = []
    for name, value in fields:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                  "Content-Type: audio/ogg\r\n\r\n").encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def groq_request(api_key: str, audio_path: str, language: str) -> dict:
    fields = [("model", GROQ_MODEL), ("response_format", "verbose_json"), ("temperature", "0"),
              ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment")]
    if language not in ("", "auto"):
        fields.append(("language", language))
    body, content_type = _multipart(fields, "file", os.path.basename(audio_path), Path(audio_path).read_bytes())
    for attempt in range(6):
        req = urllib.request.Request(GROQ_URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": content_type, "User-Agent": "YTGrab/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code == 401:
                raise RuntimeError("Groq не прийняв ключ: перевір його в розділі Субтитри")
            if exc.code == 413:
                raise RuntimeError("Groq: файл завеликий для безкоштовного тарифу")
            if exc.code in (429, 500, 502, 503) and attempt < 5:
                wait = float(exc.headers.get("retry-after") or 0) or min(60, 5 * (attempt + 1))
                time.sleep(min(wait, 120))
                continue
            raise RuntimeError(f"Groq відповів {exc.code}: {detail[:200]}")
        except urllib.error.URLError as exc:
            if attempt < 5:
                time.sleep(5)
                continue
            raise RuntimeError(f"Немає зв'язку з Groq: {exc.reason}")
    raise RuntimeError("Groq не відповідає, спробуй пізніше")


def run_groq(src: str, language: str, duration: float, api_key: str, workdir: str,
             on_progress: Callable[[float], None], is_cancelled: Callable[[], bool]) -> dict:
    if not api_key:
        raise RuntimeError("Додай безкоштовний ключ Groq у розділі Субтитри")
    whole = os.path.join(workdir, "audio.ogg")
    extract_audio(src, whole, codec="opus")
    if os.path.getsize(whole) <= GROQ_MAX_BYTES:
        bounds = [(0.0, duration)]
    else:
        bounds = chunk_bounds(duration, GROQ_CHUNK_SECONDS, silence_points(whole))
    words, segments, language_seen = [], [], None
    for n, (start, end) in enumerate(bounds):
        if is_cancelled():
            raise RuntimeError("cancelled")
        piece = whole
        if len(bounds) > 1:
            piece = os.path.join(workdir, f"chunk{n}.ogg")
            extract_audio(src, piece, start, end, codec="opus")
        data = groq_request(api_key, piece, language)
        language_seen = language_seen or data.get("language")
        for w in data.get("words") or []:
            text = str(w.get("word", "")).strip()
            if text:
                words.append({"text": text, "start": start + float(w["start"]), "end": start + float(w["end"])})
        for s in data.get("segments") or []:
            segments.append({"text": str(s.get("text", "")).strip(), "start": start + float(s["start"]),
                             "end": start + float(s["end"])})
        on_progress((n + 1) / len(bounds) * 100)
    return {"words": words, "segments": segments, "language": language_seen}


# ---------- subtitles ----------

def _words_from_segments(segments: list[dict]) -> list[dict]:
    words = []
    for seg in segments:
        tokens = seg["text"].split()
        if not tokens:
            continue
        total = sum(len(t) for t in tokens) or 1
        span, t = max(0.01, seg["end"] - seg["start"]), seg["start"]
        for token in tokens:
            step = span * len(token) / total
            words.append({"text": token, "start": t, "end": t + step})
            t += step
    return words


def build_cues(result: dict) -> list[tuple[float, float, str]]:
    words = [w for w in result.get("words") or [] if w.get("text")]
    if not words:
        words = _words_from_segments(result.get("segments") or [])
    words.sort(key=lambda w: w["start"])
    cues: list[tuple[float, float, str]] = []
    current: list[dict] = []

    def flush() -> None:
        if current:
            cues.append((current[0]["start"], current[-1]["end"], " ".join(w["text"] for w in current)))
            current.clear()

    for w in words:
        if current:
            text = " ".join(x["text"] for x in current)
            last = current[-1]["text"]
            if (len(text) + 1 + len(w["text"]) > MAX_CUE_CHARS
                    or w["end"] - current[0]["start"] > MAX_CUE_SECONDS
                    or w["start"] - current[-1]["end"] > SPLIT_PAUSE
                    or (last[-1:] in ".?!…" and len(text) >= 15)
                    or (last[-1:] in ",;:—" and len(text) >= 60)):
                flush()
        current.append(w)
    flush()

    fixed = []
    for i, (start, end, text) in enumerate(cues):
        next_start = cues[i + 1][0] if i + 1 < len(cues) else None
        end = max(end, start + MIN_CUE_SECONDS)
        if next_start is not None:
            end = min(end, max(start + 0.2, next_start - 0.04))
        fixed.append((start, end, text))
    return fixed


DASHES = ("—", "–", "-")
# Short words that should not hang at the end of a subtitle line (UA/EN prepositions and conjunctions).
HANGING = {"і", "й", "та", "а", "в", "у", "з", "із", "зі", "до", "на", "за", "по", "від", "для", "не", "що", "як",
           "a", "an", "the", "to", "of", "in", "on", "and", "or", "for", "with"}


def wrap(text: str) -> str:
    if len(text) <= MAX_LINE:
        return text
    spaces = [i for i, ch in enumerate(text) if ch == " "]
    if not spaces:
        return text

    def cost(i: int) -> float:
        first, second = text[:i], text[i + 1:]
        score = float(max(len(first), len(second)))  # balanced lines, both within MAX_LINE if possible
        if max(len(first), len(second)) > MAX_LINE:
            score += 100
        if second[:1] in DASHES:  # never start a line with a dash
            score += 50
        if first.rsplit(" ", 1)[-1].lower() in HANGING:  # keep short prepositions with the next word
            score += 12
        if first[-1:] in ",.;:!?…":  # prefer breaking after punctuation
            score -= 6
        return score

    cut = min(spaces, key=cost)
    return text[:cut] + "\n" + text[cut + 1:]


def _timestamp(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(cues: list[tuple[float, float, str]]) -> str:
    blocks = [f"{n}\n{_timestamp(a)} --> {_timestamp(b)}\n{wrap(t)}\n" for n, (a, b, t) in enumerate(cues, 1)]
    return "\n".join(blocks)


def to_text(result: dict) -> str:
    segments = result.get("segments") or []
    paragraphs, current, last_end = [], [], None
    for seg in segments:
        if last_end is not None and seg["start"] - last_end > 2.0 and current:
            paragraphs.append(" ".join(current))
            current = []
        current.append(seg["text"])
        last_end = seg["end"]
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(p.strip() for p in paragraphs if p.strip()) + "\n"


def unique_path(src: str, suffix: str) -> Path:
    source = Path(src)
    candidate, n = source.with_name(source.stem + suffix), 2
    while candidate.exists():
        candidate, n = source.with_name(f"{source.stem} ({n}){suffix}"), n + 1
    return candidate
