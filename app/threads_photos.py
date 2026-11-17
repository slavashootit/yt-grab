"""Threads photo posts and mixed carousels.

yt-dlp's Threads plugin (ytdlp_plugins/) takes videos only and gallery-dl has no Threads support, so
photos are read here from the same crawler-facing post page and saved like any other photo post.
"""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

CRAWLER_UA = "Googlebot/2.1 (+http://www.google.com/bot.html)"
POST_URL = re.compile(r"https?://(?:www\.)?threads\.(?:net|com)/(?:@(?P<user>[\w.]+)/)?(?:post|t)/(?P<id>[\w-]+)", re.I)
MAX_BYTES = 300 * 1024 * 1024


def _get(url: str, timeout: int = 30) -> urllib.request.addinfourl:
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": CRAWLER_UA}), timeout=timeout)


def _find_post(html: str, code: str) -> dict | None:
    for blob in re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S):
        if code not in blob:
            continue
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get("code") == code and ("video_versions" in node or "image_versions2" in node):
                    return node
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return None


def _largest_image(media: dict) -> str | None:
    candidates = [c for c in ((media.get("image_versions2") or {}).get("candidates") or []) if c.get("url")]
    if not candidates:
        return None
    return max(candidates, key=lambda c: int(c.get("width") or 0))["url"]


def _image_ext(url: str) -> str:
    match = re.search(r"\.(jpe?g|png|webp|heic)$", urlparse(url).path, re.I)
    return (match.group(1).lower() if match else "jpg").replace("jpeg", "jpg")


def _files(post: dict) -> list[tuple[str, str]]:
    """(url, extension) for every photo and video of the post, in carousel order."""
    files = []
    for media in post.get("carousel_media") or [post]:
        videos = [v for v in media.get("video_versions") or [] if v.get("url")]
        if videos:
            files.append((videos[0]["url"], "mp4"))  # listed best-first, progressive MP4 with sound
            continue
        image = _largest_image(media)
        if image:
            files.append((image, _image_ext(image)))
    return files


def _read_post(url: str) -> tuple[dict, str, str]:
    match = POST_URL.match(url)
    if not match:
        raise ValueError("Це не посилання на пост Threads")
    code, user = match.group("id"), match.group("user")
    page = f"https://www.threads.com/@{user}/post/{code}" if user else f"https://www.threads.com/t/{code}"
    with _get(page) as resp:
        html = resp.read().decode("utf-8", "replace")
    post = _find_post(html, code)
    if not post:
        raise ValueError("Пост не знайдено: він приватний або видалений")
    return post, code, user or ""


def probe(url: str) -> dict | None:
    """A photo-post description in the same shape as photos.probe(), or None for a post without media."""
    post, code, user = _read_post(url)
    files = _files(post)
    if not files:
        return None
    images = [u for u, ext in files if ext != "mp4"]
    text = ((post.get("caption") or {}).get("text") or "").strip()
    taken = post.get("taken_at")
    date = datetime.fromtimestamp(int(taken), timezone.utc).strftime("%Y-%m-%d") if str(taken or "").isdigit() else ""
    return {
        "kind": "photos",
        "title": text.split("\n")[0][:120] or "Фото-пост",
        "uploader": ((post.get("user") or {}).get("username")) or user or "Unknown",
        "date": date,
        "post_id": code,
        "images": len(images),
        "videos": len(files) - len(images),
        "audio": 0,
        "count": len(files),
        "previews": images[:8],
        "thumbnail": images[0] if images else None,
    }


def download(url: str, folder: Path, on_file: Callable[[str, int], None], cancelled: Callable[[], bool]) -> int:
    """Save every photo/video as 01.jpg, 02.mp4… Returns how many files the post has."""
    post, _, _ = _read_post(url)
    files = _files(post)
    folder.mkdir(parents=True, exist_ok=True)
    for n, (src, ext) in enumerate(files, 1):
        if cancelled():
            break
        target = folder / f"{n:02}.{ext}"
        if not target.exists():
            part = target.with_name(target.name + ".part")
            try:
                with _get(src, timeout=60) as resp, open(part, "wb") as out:
                    size = 0
                    while chunk := resp.read(1 << 16):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError("Файл завеликий")
                        out.write(chunk)
                part.replace(target)
            finally:
                part.unlink(missing_ok=True)
        on_file(str(target), len(files))
    return len(files)
