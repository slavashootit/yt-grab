"""Turn a timed transcript into a Markdown summary with any chat model.

Standard library only: shared by the app (Groq) and llm_runner.py (local MLX model).
Long talks are summarised chunk by chunk, then the notes are merged into one summary.
"""
from __future__ import annotations

import re
from typing import Callable

CHUNK_CHARS = 12000
CHUNK_MAX_TOKENS = 700
FINAL_MAX_TOKENS = 1400

LANGUAGE_RULES = {
    "uk": "українською мовою",
    "en": "in English",
    "same": "тією ж мовою, якою йде розмова",
}

FINAL_FORMAT = """## Коротко
2–4 речення: про що розмова і до чого дійшли.

## Головні думки
- 5–10 пунктів, кожен — одна конкретна думка або факт.

## Ключові моменти
- [хв:с] що відбувається — 5–10 пунктів; таймкоди бери лише з тексту.

## Домовленості та наступні кроки
- пункти; якщо їх немає, напиши: «Не прозвучало»."""

RULES = ("Правила: не вигадуй фактів, імен, чисел і таймкодів, яких немає в тексті. "
         "Без вступу й без власних коментарів — лише підсумок у вказаній структурі.")

ChatFn = Callable[[list, int], str]


def timestamp(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def transcript_text(result: dict) -> str:
    """One line per segment, prefixed with its start time: "[12:34] text"."""
    segments = [s for s in result.get("segments") or [] if str(s.get("text", "")).strip()]
    if not segments:
        words = result.get("words") or []
        return " ".join(str(w.get("text", "")) for w in words).strip()
    return "\n".join(f"[{timestamp(s['start'])}] {str(s['text']).strip()}" for s in segments)


def split_chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    lines, chunks, current = text.splitlines() or [text], [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > size:
            chunks.append(current)
            current = ""
        while len(line) > size:  # a single huge line (no segments): hard split
            chunks.append(line[:size])
            line = line[size:]
        current = f"{current}\n{line}" if current else line
    if current.strip():
        chunks.append(current)
    return chunks


def _system(language: str) -> dict:
    return {"role": "system", "content": f"Ти уважний редактор, який стисло й точно підсумовує розмови. Пиши {LANGUAGE_RULES.get(language, LANGUAGE_RULES['uk'])}."}


def final_messages(material: str, language: str, title: str, from_notes: bool) -> list:
    source = ("Нижче нотатки до частин однієї розмови (з таймкодами)." if from_notes
              else "Нижче розшифровка розмови з таймкодами [хв:с].")
    about = f" Назва: «{title}»." if title else ""
    return [_system(language), {"role": "user", "content": (
        f"{source}{about}\nСклади підсумок {LANGUAGE_RULES.get(language, LANGUAGE_RULES['uk'])} "
        f"у Markdown точно за структурою:\n\n{FINAL_FORMAT}\n\n{RULES}\n\n---\n{material}")}]


def chunk_messages(chunk: str, language: str, index: int, total: int) -> list:
    return [_system(language), {"role": "user", "content": (
        f"Це частина {index} з {total} розшифровки розмови з таймкодами [хв:с]. "
        f"Випиши {LANGUAGE_RULES.get(language, LANGUAGE_RULES['uk'])} 5–12 стислих пунктів: ключові думки, факти, "
        f"домовленості — кожен з таймкодом на початку, як у тексті. {RULES}\n\n---\n{chunk}")}]


def clean(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    return text.strip()


def summarize(chat: ChatFn, transcript: str, language: str = "uk", title: str = "",
              progress: Callable[[float], None] = lambda p: None) -> str:
    chunks = split_chunks(transcript)
    if not chunks:
        raise ValueError("Порожня розшифровка — нема що підсумовувати")
    if len(chunks) == 1:
        progress(10)
        summary = clean(chat(final_messages(chunks[0], language, title, False), FINAL_MAX_TOKENS))
        progress(100)
        return summary
    notes = []
    for n, chunk in enumerate(chunks, 1):
        notes.append(f"### Частина {n}\n" + clean(chat(chunk_messages(chunk, language, n, len(chunks)), CHUNK_MAX_TOKENS)))
        progress(n / (len(chunks) + 1) * 100)
    summary = clean(chat(final_messages("\n\n".join(notes), language, title, True), FINAL_MAX_TOKENS))
    progress(100)
    return summary


def document(summary: str, title: str, source_name: str, model: str, date: str) -> str:
    head = f"# Підсумок: {title or source_name}\n\n_{source_name} · {model} · {date}_\n\n"
    return head + summary.strip() + "\n"
