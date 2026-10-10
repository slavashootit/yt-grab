"""Conversation summaries: local Qwen3-4B on Apple Silicon (MLX) or Groq with the free key."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

import summary_prompts
import transcribe

APP_DIR = Path(__file__).resolve().parent
RUNNER = APP_DIR / "llm_runner.py"
LOCAL_DIR = transcribe.ASR_DIR / "llm"
LOCAL_MODEL_NAME = "Qwen3-4B (локально)"
GROQ_CHAT_URL = os.environ.get("YTGRAB_GROQ_CHAT_URL", "https://api.groq.com/openai/v1/chat/completions")
GROQ_MODEL = "llama-3.3-70b-versatile"
LANGUAGES = [("uk", "Українською"), ("en", "English"), ("same", "Мовою розмови")]


def local_python() -> Path:
    return LOCAL_DIR / "bin" / "python"


def local_ready() -> bool:
    return (LOCAL_DIR / ".ready").exists() and local_python().exists()


def status(settings: dict) -> dict:
    return {
        "local_supported": transcribe.is_apple_silicon(),
        "local_installed": local_ready(),
        "groq": bool(settings.get("groq_api_key")),
        "languages": [{"code": c, "name": n} for c, n in LANGUAGES],
    }


def setup_steps(python_for_bootstrap: str) -> list[tuple[str, list[str]]]:
    if not transcribe.is_apple_silicon():
        raise ValueError("Локальний підсумок працює лише на Маках з чипом Apple. Скористайся Groq.")
    steps: list[tuple[str, list[str]]] = []
    uv = transcribe.uv_bin()
    if not uv:
        tools_python = transcribe.TOOLS_DIR / "bin" / "python"
        steps.append(("готую інсталятор", [python_for_bootstrap, "-m", "venv", str(transcribe.TOOLS_DIR)]))
        steps.append(("ставлю uv", [str(tools_python), "-m", "pip", "install", "--disable-pip-version-check", "-q", "uv"]))
        uv = str(transcribe.TOOLS_DIR / "bin" / "uv")
    steps.append(("готую Python 3.12", [uv, "venv", "--allow-existing", "--python", "3.12", str(LOCAL_DIR)]))
    steps.append(("ставлю mlx-lm", [uv, "pip", "install", "--python", str(local_python()), "--upgrade", "mlx-lm"]))
    steps.append(("завантажую модель Qwen3-4B (≈2,3 ГБ, один раз)", [str(local_python()), str(RUNNER), "--download-only"]))
    return steps


def mark_ready() -> None:
    (LOCAL_DIR / ".ready").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), "utf-8")


def pick_engine(settings: dict) -> str:
    if local_ready():
        return "local"
    if settings.get("groq_api_key"):
        return "groq"
    raise ValueError("Для підсумку встанови Qwen3 у розділі Субтитри або збережи ключ Groq")


def run_local(transcript: str, language: str, title: str, on_progress: Callable[[float], None],
              register_proc: Callable[[subprocess.Popen], None]) -> str:
    with tempfile.TemporaryDirectory(prefix="ytgrab-llm-") as tmp:
        job_file, out_file = os.path.join(tmp, "job.json"), os.path.join(tmp, "summary.md")
        with open(job_file, "w", encoding="utf-8") as fh:
            json.dump({"transcript": transcript, "language": language, "title": title}, fh, ensure_ascii=False)
        proc = subprocess.Popen([str(local_python()), str(RUNNER), "--input", job_file, "--out", out_file],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
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
            raise RuntimeError(error or (tail[-1] if tail else "підсумок не вдався"))
        return Path(out_file).read_text("utf-8")


def groq_chat(api_key: str) -> summary_prompts.ChatFn:
    def chat(messages: list, max_tokens: int) -> str:
        body = json.dumps({"model": GROQ_MODEL, "messages": messages, "temperature": 0.3,
                           "max_tokens": max_tokens}).encode("utf-8")
        for attempt in range(6):
            req = urllib.request.Request(GROQ_CHAT_URL, data=body, method="POST", headers={
                "Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": "YTGrab/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                if exc.code == 401:
                    raise RuntimeError("Groq не прийняв ключ: перевір його в розділі Субтитри")
                if exc.code in (429, 500, 502, 503) and attempt < 5:
                    wait = float(exc.headers.get("retry-after") or 0) or min(60, 10 * (attempt + 1))
                    time.sleep(min(wait, 120))
                    continue
                raise RuntimeError(f"Groq відповів {exc.code}: {detail[:200]}")
            except urllib.error.URLError as exc:
                if attempt < 5:
                    time.sleep(5)
                    continue
                raise RuntimeError(f"Немає зв'язку з Groq: {exc.reason}")
        raise RuntimeError("Groq не відповідає, спробуй пізніше")
    return chat


def run_groq(transcript: str, language: str, title: str, api_key: str, on_progress: Callable[[float], None]) -> str:
    return summary_prompts.summarize(groq_chat(api_key), transcript, language, title, on_progress)
