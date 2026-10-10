"""Photo posts (Instagram carousels, TikTok slideshows, ...) via gallery-dl.

yt-dlp only extracts video, so photo-only posts fall back to gallery-dl. It is installed into the
same private venv as yt-dlp from Settings -> Tools.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "heic", "gif", "avif"}
VIDEO_EXT = {"mp4", "mov", "webm", "mkv"}
AUDIO_EXT = {"mp3", "m4a", "aac", "opus", "ogg"}
# yt-dlp errors that mean "there is a post here, just no video in it".
NO_VIDEO = re.compile(r"no video|unsupported url|there'?s no video|no media found|no video formats", re.I)


def gallery_bin(own_dir: Path) -> str | None:
    own = own_dir / "bin" / "gallery-dl"
    if own.exists():
        return str(own)
    return shutil.which("gallery-dl")


def _cookies(browser: str) -> list[str]:
    return ["--cookies-from-browser", browser] if browser else []


def probe(binary: str, url: str, cookies_browser: str = "") -> dict | None:
    """Return a photo-post description, or None when gallery-dl finds no files."""
    proc = subprocess.run([binary, "-j", *_cookies(cookies_browser), url], capture_output=True, text=True, timeout=120)
    try:
        messages = json.loads(proc.stdout or "[]")
    except ValueError:
        messages = []
    files = [(m[1], m[2]) for m in messages if isinstance(m, list) and len(m) >= 3 and m[0] == 3]
    if not files:
        err = [l for l in (proc.stderr or "").splitlines() if "[error]" in l]
        if err:
            raise ValueError(re.sub(r"^\[[^\]]+\]\[error\]\s*", "", err[-1])[:300])
        return None
    meta = files[0][1]
    images = [u for u, m in files if str(m.get("extension", "")).lower() in IMAGE_EXT]
    videos = [u for u, m in files if str(m.get("extension", "")).lower() in VIDEO_EXT]
    audio = [u for u, m in files if str(m.get("extension", "")).lower() in AUDIO_EXT]
    author = meta.get("author")
    uploader = (author.get("uniqueId") or author.get("nickname")) if isinstance(author, dict) else author
    uploader = uploader or meta.get("username") or meta.get("owner_username") or meta.get("fullname") or "Unknown"
    title = (meta.get("desc") or meta.get("description") or meta.get("caption") or "").strip()
    date = str(meta.get("date") or meta.get("post_date") or "")[:10]
    post_id = str(meta.get("post_shortcode") or meta.get("shortcode") or meta.get("id") or meta.get("post_id") or "")
    return {
        "kind": "photos",
        "title": title or "Фото-пост",
        "uploader": str(uploader),
        "date": date if re.match(r"\d{4}-\d{2}-\d{2}$", date) else "",
        "post_id": post_id,
        "images": len(images),
        "videos": len(videos),
        "audio": len(audio),
        "count": len(files),
        "previews": images[:8],
        "thumbnail": images[0] if images else None,
    }


def _clean(text: str, limit: int) -> str:
    text = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", text).strip().strip(".")
    encoded = text.encode("utf-8")[:limit]
    return encoded.decode("utf-8", "ignore").strip() or "post"


def target_dir(out_dir: str, platform: str, info: dict) -> Path:
    name = f"{info.get('date') or 'undated'} - {_clean(info.get('title', ''), 120)}"
    if info.get("post_id"):
        name += f" [{_clean(info['post_id'], 40)}]"
    return Path(out_dir) / _clean(platform or "Other", 40) / _clean(info.get("uploader", "Unknown"), 80) / name


def download_cmd(binary: str, url: str, folder: Path, cookies_browser: str = "") -> list[str]:
    # Numbered files keep the carousel order; "00" is TikTok's slideshow soundtrack.
    return [binary, "-D", str(folder), "-f", "{num:>02}.{extension}", *_cookies(cookies_browser), url]
