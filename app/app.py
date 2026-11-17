#!/usr/bin/env python3
"""YT Grab: a local GUI for yt-dlp.

Starts a small HTTP server on 127.0.0.1 and opens the UI in the default browser.
Standard library only; needs yt-dlp, ffmpeg and (for YouTube) deno on PATH.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import date, timedelta
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse
import urllib.request

import converter
import photos
import sites
import summarize
import summary_prompts
import transcribe

APP_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".ytgrab"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
# The queue and its history survive a restart. (The override keeps test runs away from the real file.)
JOBS_FILE = Path(os.environ.get("YTGRAB_JOBS_FILE") or CONFIG_DIR / "jobs.json")
STATE_FILE = CONFIG_DIR / "state.json"
HISTORY_LIMIT = 200
# Job kinds that are saved to disk and can be started again from their own parameters.
RETRY_KINDS = {"download", "photos", "profile", "convert", "transcribe"}
EVENT_PREFIX = "[ytgrab-event]"
AUDIO_EXT = {".m4a", ".mp3", ".opus", ".ogg", ".flac", ".wav"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".avif"}
MEDIA_EXT = {".mp4", ".mkv", ".webm", ".mov"} | AUDIO_EXT | IMAGE_EXT
VENV_DIR = CONFIG_DIR / "venv"
BIN_DIR = CONFIG_DIR / "bin"
YTDLP_PACKAGE = "yt-dlp[default,curl-cffi]"
COMPONENTS = APP_DIR / "components.py"
# Our own yt-dlp extractors for sites yt-dlp lacks (Threads).
PLUGIN_ARGS = ["--plugin-dirs", str(APP_DIR / "ytdlp_plugins")]
# Tools YT Grab installs itself (no Homebrew needed) win over system copies.
os.environ["PATH"] = os.pathsep.join([str(BIN_DIR), str(VENV_DIR / "bin"), os.environ.get("PATH", "")])
PROGRESS_PREFIX = "[ytgrab]"
PROGRESS_TEMPLATE = (
    "download:" + PROGRESS_PREFIX
    + " %(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s"
    + "|%(info.playlist_index)s|%(info.n_entries)s|%(info.title)s"
)
COOKIE_BROWSERS = {"", "chrome", "safari", "firefox", "brave", "edge", "chromium", "opera", "vivaldi"}
MAX_PARALLEL_JOBS = 2
# How many latest videos of a non-YouTube channel/profile are checked against a date range.
DATE_SCAN_LIMIT = 150

DEFAULT_SETTINGS = {
    "out_dir": str(Path.home() / "Downloads" / "YouTube"),
    "cookies_browser": "",
    "groq_api_key": "",
    "asr_engine": "parakeet" if transcribe.is_apple_silicon() else "whisper",
    "asr_language": "auto",
    "asr_txt": True,
    "auto_mp4": True,  # most users want an editor-friendly MP4 right away
    "mp4_codec": "auto",
    "keep_original": False,
    "summary": False,
    "summary_lang": "uk",
}

settings_lock = threading.Lock()
jobs_lock = threading.Lock()
jobs: dict[str, "Job"] = {}
job_slots = threading.Semaphore(MAX_PARALLEL_JOBS)
# Encoding saturates the CPU/GPU, so conversions run one at a time.
convert_slots = threading.Semaphore(1)
transcribe_slots = threading.Semaphore(1)


# ---------- settings ----------

def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    try:
        data.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
    except (OSError, ValueError):
        pass
    return data


def save_settings(new: dict) -> dict:
    with settings_lock:
        data = load_settings()
        if "out_dir" in new:
            out_dir = os.path.expanduser(str(new["out_dir"]).strip())
            if not out_dir:
                raise ValueError("Вкажи папку для завантажень")
            data["out_dir"] = str(Path(out_dir).resolve())
        if "cookies_browser" in new:
            browser = str(new["cookies_browser"])
            if browser not in COOKIE_BROWSERS:
                raise ValueError("Невідомий браузер")
            data["cookies_browser"] = browser
        if "groq_api_key" in new:
            data["groq_api_key"] = str(new["groq_api_key"]).strip()
        if "asr_engine" in new:
            if new["asr_engine"] not in transcribe.ENGINES:
                raise ValueError("Невідомий рушій")
            data["asr_engine"] = new["asr_engine"]
        if "asr_language" in new:
            if new["asr_language"] not in dict(transcribe.LANGUAGES):
                raise ValueError("Невідома мова")
            data["asr_language"] = new["asr_language"]
        if "asr_txt" in new:
            data["asr_txt"] = bool(new["asr_txt"])
        if "auto_mp4" in new:
            data["auto_mp4"] = bool(new["auto_mp4"])
        if "mp4_codec" in new:
            if new["mp4_codec"] not in converter.MP4_CODECS:
                raise ValueError("Невідомий кодек")
            data["mp4_codec"] = new["mp4_codec"]
        if "keep_original" in new:
            data["keep_original"] = bool(new["keep_original"])
        if "summary" in new:
            data["summary"] = bool(new["summary"])
        if "site_quality" in new:
            # {"youtube": "1080", "tiktok": "best", …}: the last quality chosen for each site.
            raw = new["site_quality"] if isinstance(new["site_quality"], dict) else {}
            merged = dict(data.get("site_quality") or {})
            for site, quality in raw.items():
                site, quality = str(site)[:40], str(quality)
                if quality in ("best", "audio") or quality.isdigit():
                    merged[site] = quality
            data["site_quality"] = merged
        if "summary_lang" in new:
            if new["summary_lang"] not in dict(summarize.LANGUAGES):
                raise ValueError("Невідома мова підсумку")
            data["summary_lang"] = new["summary_lang"]
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        return data


def public_settings(data: dict | None = None) -> dict:
    # The Groq key never leaves the server; the UI only learns whether one is stored.
    data = dict(data or load_settings())
    data["has_groq_key"] = bool(data.pop("groq_api_key", ""))
    browser = data.get("cookies_browser") or ""
    data["cookies_access"] = cookies_readable(browser) if browser else None
    return data


# System Settings → Privacy & Security → Full Disk Access: lets YT Grab read the browser's cookies.
FULL_DISK_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"


# ---------- yt-dlp helpers ----------

def own_ytdlp() -> Path:
    scripts = "Scripts" if sys.platform.startswith("win") else "bin"
    return VENV_DIR / scripts / ("yt-dlp.exe" if sys.platform.startswith("win") else "yt-dlp")


def ytdlp_bin() -> str:
    # A private venv (installed from Settings) wins: it carries curl_cffi, which Homebrew's build lacks.
    own = own_ytdlp()
    if own.exists():
        return str(own)
    return shutil.which("yt-dlp") or "yt-dlp"


_ytdlp_info_cache: dict = {}


def ytdlp_info() -> dict:
    path = ytdlp_bin()
    if _ytdlp_info_cache.get("path") == path:
        return _ytdlp_info_cache
    info = {"path": path, "own": path == str(own_ytdlp()), "version": None, "curl_cffi": False}
    try:
        info["version"] = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30).stdout.strip() or None
        targets = subprocess.run([path, "--list-impersonate-targets"], capture_output=True, text=True, timeout=30).stdout
        info["curl_cffi"] = any("curl_cffi" in line and "unavailable" not in line for line in targets.splitlines())
    except (OSError, subprocess.TimeoutExpired):
        pass
    _ytdlp_info_cache.clear()
    _ytdlp_info_cache.update(info)
    return info


def tool_status() -> dict:
    yt = ytdlp_info()
    return {
        "yt-dlp": bool(yt["version"]),
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "deno": shutil.which("deno") is not None,
        "curl_cffi": yt["curl_cffi"],
        "gallery-dl": photos.gallery_bin(VENV_DIR) is not None,
    }


def normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("Встав посилання, що починається з http:// або https://")
    # A bare channel URL lists its tabs (Videos, Shorts, Live); default to the uploads tab.
    if re.match(r"^https?://(www\.|m\.)?youtube\.com/(@[^/?#]+|channel/[^/?#]+|c/[^/?#]+)/?$", url, re.I):
        url = url.rstrip("/") + "/videos"
    return url


def parse_dates(data: dict) -> tuple[str, str]:
    """Validate the optional "date_from"/"date_to" (YYYY-MM-DD, both inclusive) of a request."""
    values = []
    for key in ("date_from", "date_to"):
        value = str(data.get(key) or "").strip()
        if value:
            try:
                value = date.fromisoformat(value).isoformat()
            except ValueError:
                raise ValueError("Дата має бути у форматі РРРР-ММ-ДД")
        values.append(value)
    if values[0] and values[1] and values[0] > values[1]:
        raise ValueError("Початкова дата пізніша за кінцеву")
    return values[0], values[1]


def date_filter_args(url: str, date_from: str, date_to: str) -> list[str]:
    """yt-dlp options that keep only videos uploaded within [date_from, date_to]."""
    if not (date_from or date_to):
        return []
    lo, hi = date_from.replace("-", ""), date_to.replace("-", "")
    rule = " & ".join(p for p in (lo and f"upload_date>={lo}", hi and f"upload_date<={hi}") if p)
    args = ["--match-filters", rule]
    if lo:
        platform = sites.detect(url)
        if platform and platform[0] == "youtube":
            # Channel tabs are newest first: stop at the first older video instead of reading the whole
            # channel. Videos without a date never stop the scan.
            args += ["--lazy-playlist", "--break-match-filters", f"upload_date>={lo}",
                     "--break-match-filters", "!upload_date"]
        else:
            # TikTok and others pin old videos on top, so a break would end too early; cap the scan instead.
            args += ["--playlist-end", str(DATE_SCAN_LIMIT)]
    return args


VIMEO_PAGE = re.compile(r"^https?://(?:www\.)?vimeo\.com/(\d+)(?:/([0-9a-f]{6,}))?/?(?:[?#].*)?$")


# TikTok profile URL -> secUid seen when the profile was read. TikTok hides the secUid from about one
# request in five, so downloads go by "tiktokuser:<secUid>" instead of reading the profile page again.
TIKTOK_SEC_UIDS: dict[str, str] = {}
TIKTOK_PREVIEW = 30


def url_args(url: str) -> list[str]:
    """Final URL arguments for yt-dlp. vimeo.com pages now demand a login ("web client only works
    when logged-in"), while the embed player still serves the same video, so single videos go there."""
    if url in TIKTOK_SEC_UIDS:
        return ["--", f"tiktokuser:{TIKTOK_SEC_UIDS[url]}"]
    m = VIMEO_PAGE.match(url)
    if m:
        player = f"https://player.vimeo.com/video/{m.group(1)}" + (f"?h={m.group(2)}" if m.group(2) else "")
        return ["--referer", "https://vimeo.com/", "--", player]
    return ["--", url]


COOKIE_DIRS = {
    "chrome": "Google/Chrome", "brave": "BraveSoftware/Brave-Browser",
    "edge": "Microsoft Edge", "chromium": "Chromium", "vivaldi": "Vivaldi",
}


def cookies_readable(browser: str) -> bool:
    """macOS may hide another app's data from YT Grab (privacy protection) even though the file
    exists; yt-dlp then fails every request with "could not find ... cookies database"."""
    sub = COOKIE_DIRS.get(browser.split(":")[0].lower())
    if not sub:
        return True
    root = Path.home() / "Library/Application Support" / sub
    try:
        for d in [root, *[root / n for n in os.listdir(root)]]:
            f = d / "Cookies"
            if f.is_file():
                with open(f, "rb"):
                    return True
    except OSError:
        pass
    return False


def cookie_args(cfg: dict, url: str = "") -> list[str]:
    browser = cfg.get("cookies_browser")
    if not browser or sites.TIKTOK_PROFILE.search(url):
        return []
    if not cookies_readable(browser):
        print(f"cookies: no access to {browser} data — downloading without cookies", flush=True)
        return []
    return ["--cookies-from-browser", browser]


def best_thumbnail(info: dict) -> str | None:
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = [t for t in info.get("thumbnails") or [] if t.get("url")]
    return thumbs[-1]["url"] if thumbs else None


def format_size(fmt: dict) -> int:
    return int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)


def describe_video(info: dict) -> dict:
    formats = info.get("formats") or []
    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    audio_size = max((format_size(f) for f in audio), default=0)
    by_height: dict[int, dict] = {}
    for f in formats:
        height = f.get("height")
        if not height or f.get("vcodec") in (None, "none"):
            continue
        current = by_height.get(height)
        if current is None or (f.get("tbr") or 0) > (current.get("tbr") or 0):
            by_height[height] = f
    qualities = []
    for height in sorted(by_height, reverse=True):
        f = by_height[height]
        size = format_size(f)
        if f.get("acodec") in (None, "none"):
            size += audio_size
        qualities.append({"height": height, "fps": f.get("fps"), "size": size or None})
    return {
        "kind": "video",
        "title": info.get("title"),
        "uploader": info.get("channel") or info.get("uploader"),
        "duration": info.get("duration"),
        "thumbnail": best_thumbnail(info),
        "qualities": qualities,
        "audio_size": audio_size or None,
    }


def describe_playlist(info: dict) -> dict:
    entries = [e for e in info.get("entries") or [] if e]
    return {
        "kind": "playlist",
        "title": info.get("title"),
        "uploader": info.get("channel") or info.get("uploader"),
        "thumbnail": best_thumbnail(info),
        "count": info.get("playlist_count") or len(entries),
        "entries": [
            {"title": e.get("title"), "duration": e.get("duration")} for e in entries[:30]
        ],
    }


def fetch_photos(url: str, cfg: dict) -> dict | None:
    binary = photos.gallery_bin(VENV_DIR)
    if not binary:
        return None
    try:
        return photos.probe(binary, url, cfg.get("cookies_browser", ""))
    except subprocess.TimeoutExpired:
        raise ValueError("Сайт надто довго не відповідає, спробуй ще раз")
    except ValueError as exc:
        raise ValueError(sites.friendly_error(str(exc), url, cfg.get("cookies_browser", "")))


def fetch_info(url: str) -> dict:
    cfg = load_settings()
    platform = sites.detect(url)
    platform_name = platform[1] if platform else None
    profile = sites.instagram_profile(url)
    if profile:
        # Reading a profile needs a login and is slow; the date range is chosen first, the posts come later.
        return {"kind": "profile", "platform": "Instagram", "uploader": profile[0], "title": "@" + profile[0],
                "tab": profile[1], "max_posts": photos.PROFILE_MAX_POSTS,
                "has_cookies": bool(cfg.get("cookies_browser")),
                "cookies_access": cookies_readable(cfg.get("cookies_browser") or ""),
                "has_gallery": photos.gallery_bin(VENV_DIR) is not None}
    if sites.PHOTO_POST.search(url):
        result = fetch_photos(url, cfg)
        if result:
            result["platform"] = platform_name
            return result
    # A whole TikTok profile pages in 30 videos at a time (~40 s for 800 videos); the preview needs only the first page.
    preview = ["--playlist-end", str(TIKTOK_PREVIEW)] if sites.TIKTOK_PROFILE.search(url) else []
    cmd = [ytdlp_bin(), *PLUGIN_ARGS, "-J", "--flat-playlist", "--no-warnings", *preview, *cookie_args(cfg, url),
           *url_args(url)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        for _ in range(5):
            if proc.returncode == 0 or "secondary user ID" not in proc.stderr:
                break
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise ValueError("Сайт надто довго не відповідає, спробуй ще раз")
    if proc.returncode != 0:
        error = last_error_line(proc.stderr)
        if photos.NO_VIDEO.search(error):
            result = fetch_photos(url, cfg)
            if result:
                result["platform"] = platform_name
                return result
            if photos.gallery_bin(VENV_DIR) is None and platform_name in ("Instagram", "TikTok", "Threads", "X"):
                raise ValueError("Схоже, це пост із фото. Щоб качати фото, відкрий Налаштування → «Інструменти» "
                                 "і натисни «Встановити yt-dlp з curl_cffi» (заодно поставиться gallery-dl).")
        raise ValueError(sites.friendly_error(error, url, cfg.get("cookies_browser", ""))
                         or "yt-dlp не зміг прочитати посилання")
    info = json.loads(proc.stdout)
    if sites.TIKTOK_PROFILE.search(url) and str(info.get("id", "")).startswith("MS4w"):
        TIKTOK_SEC_UIDS[url] = info["id"]
    result = describe_playlist(info) if info.get("_type") in ("playlist", "multi_video") else describe_video(info)
    if preview and result["kind"] == "playlist":
        result["more"] = result["count"] >= TIKTOK_PREVIEW
    result["platform"] = platform_name or info.get("extractor_key")
    # Instagram carousels can mix photos and videos; yt-dlp sees only the videos.
    result["can_photos"] = (platform_name == "Instagram" and result["kind"] == "playlist"
                            and photos.gallery_bin(VENV_DIR) is not None)
    return result


def last_error_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("ERROR")]
    return (errors or lines or [""])[-1][:400]


def build_download_cmd(url: str, quality: str, use_archive: bool, cfg: dict, files_log: str,
                       date_from: str = "", date_to: str = "") -> list[str]:
    out_dir = cfg["out_dir"]
    cmd = [
        ytdlp_bin(), *PLUGIN_ARGS, "--newline", "--no-colors", "--ignore-errors", "--no-overwrites",
        "--embed-metadata", "--embed-chapters", "--concurrent-fragments", "4",
        "--paths", out_dir, "--output", sites.output_template(url),
        "--progress-template", PROGRESS_TEMPLATE,
        "--print-to-file", "after_move:filepath", files_log,
        *cookie_args(cfg, url),
        *date_filter_args(url, date_from, date_to),
    ]
    if use_archive:
        # Playlists/channels skip videos fetched before, so re-runs only grab new uploads.
        # Audio keeps its own archive so it never blocks a video download of the same item.
        name = ".archive-audio.txt" if quality == "audio" else ".archive.txt"
        cmd += ["--download-archive", os.path.join(out_dir, name)]
    if quality == "audio":
        # Best available source (usually Opus) -> MP3 320 kbps, which every player and editor accepts.
        cmd += ["--format", "ba/b", "--extract-audio", "--audio-format", "mp3", "--audio-quality", "320K"]
    else:
        if quality == "best":
            cmd += ["--format", "bv*+ba/b"]
        else:
            height = int(quality)
            cmd += ["--format", f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"]
        cmd += ["--merge-output-format", "mkv"]
    return cmd + url_args(url)


# ---------- jobs ----------

def clean_thumbnail(value) -> str:
    """A preview URL from the page or the saved queue: only plain https links are kept."""
    url = str(value or "").strip()
    return url if url.startswith("https://") and len(url) < 2000 else ""


class Job:
    def __init__(self, url: str, title: str, quality: str, use_archive: bool, kind: str = "download"):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.url = url
        self.title = title or url
        self.quality = quality
        self.use_archive = use_archive
        self.mp4 = False
        self.replace = False
        self.status = "queued"
        self.percent = 0.0
        self.speed = ""
        self.eta = ""
        self.item = ""
        self.current = ""
        self.error = ""
        self.note = ""
        self.files: list[str] = []
        self.created = time.time()
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self.parent: Job | None = None  # the download an automatic MP4 conversion belongs to
        self.converting = 0  # automatic MP4 conversions still running for this download
        self.date_from = ""
        self.date_to = ""
        self.interrupted = False  # stopped because YT Grab was closed, not by the user
        self.thumbnail = ""  # preview image for the queue card
        self.phase = ""  # what is happening right now, in words: "читаю сторінку", "зводжу відео й звук"…
        self.streams = 1  # separate files yt-dlp fetches for one video (video + audio = 2)
        self.stream = 0  # which of them is downloading now

    def to_disk(self) -> dict:
        """What is needed to show the job after a restart and to start it again."""
        finished = self.status in ("done", "error", "cancelled")
        data = {
            "id": self.id, "kind": self.kind, "url": self.url, "title": self.title, "quality": self.quality,
            "use_archive": self.use_archive, "mp4": self.mp4, "replace": self.replace, "status": self.status,
            "percent": self.percent if finished else 0.0, "current": self.current if finished else "",
            "error": self.error, "note": self.note, "files": list(self.files), "created": self.created,
            "date_from": self.date_from, "date_to": self.date_to, "interrupted": self.interrupted,
            "thumbnail": self.thumbnail,
        }
        for key in ("preset", "res", "engine", "language", "want_txt", "want_summary", "platform"):
            if hasattr(self, key):
                data[key] = getattr(self, key)
        if self.kind in ("photos", "profile") and isinstance(getattr(self, "info", None), dict):
            data["info"] = self.info
        return data

    @classmethod
    def from_disk(cls, data: dict) -> "Job":
        job = cls(str(data["url"]), str(data.get("title") or ""), str(data.get("quality") or ""),
                  bool(data.get("use_archive")), kind=str(data.get("kind") or "download"))
        job.id = str(data.get("id") or job.id)
        job.mp4, job.replace = bool(data.get("mp4")), bool(data.get("replace"))
        job.status = str(data.get("status") or "cancelled")
        job.percent = float(data.get("percent") or 0.0)
        job.current, job.error, job.note = (str(data.get(k) or "") for k in ("current", "error", "note"))
        job.files = [str(f) for f in data.get("files") or []]
        job.created = float(data.get("created") or job.created)
        job.date_from, job.date_to = str(data.get("date_from") or ""), str(data.get("date_to") or "")
        job.thumbnail = clean_thumbnail(data.get("thumbnail"))
        for key in ("preset", "res", "engine", "language", "want_txt", "want_summary", "platform", "info"):
            if key in data:
                setattr(job, key, data[key])
        if job.status in ("queued", "running") or data.get("interrupted"):
            job.status, job.percent, job.current = "cancelled", 0.0, ""
            job.note = ("Перервано: YT Grab закрили. «Повторити» докачає з того ж місця." if job.kind == "download"
                        else "Перервано: YT Grab закрили.")
        return job

    def can_retry(self) -> bool:
        if self.kind not in RETRY_KINDS or self.status not in ("error", "cancelled"):
            return False
        if self.kind == "convert":
            return hasattr(self, "preset") and Path(self.url).is_file()
        if self.kind == "transcribe":
            return hasattr(self, "engine") and Path(self.url).is_file()
        if self.kind == "profile":
            return isinstance(getattr(self, "info", None), dict)
        return True

    def to_json(self) -> dict:
        return {
            "retry": self.can_retry(), "thumbnail": self.thumbnail, "phase": self.phase,
            "id": self.id, "kind": self.kind, "note": self.note, "url": self.url, "mp4": self.mp4, "title": self.title, "quality": self.quality,
            "status": self.status, "percent": self.percent, "speed": self.speed,
            "eta": self.eta, "item": self.item, "current": self.current, "error": self.error,
            "files": self.files, "created": self.created, "converting": self.converting,
            "date_from": self.date_from, "date_to": self.date_to,
        }


def parse_progress(job: Job, line: str) -> None:
    parts = line[len(PROGRESS_PREFIX):].strip().split("|", 5)
    if len(parts) < 6:
        return
    percent, speed, eta, index, total, title = (p.strip() for p in parts)
    try:
        part = float(percent.rstrip("%"))
    except ValueError:
        part = None
    if part is not None:
        # One bar for the whole job: video and audio streams count as halves of one video,
        # and a playlist moves through its videos instead of restarting at 0 % for each.
        video = (min(job.stream, job.streams - 1) + part / 100) / max(1, job.streams)
        if index.isdigit() and total.isdigit() and int(total) > 0:
            job.percent = min(100.0, (int(index) - 1 + video) / int(total) * 100)
        else:
            job.percent = video * 100
    job.speed = "" if speed in ("NA", "Unknown B/s") else speed
    job.eta = "" if eta in ("NA", "Unknown") else eta
    job.item = f"{index}/{total}" if index not in ("NA", "") and total not in ("NA", "") else ""
    job.phase = "качаю звук" if job.streams > 1 and job.stream >= 1 else "качаю"
    if title and title != "NA":
        job.current = title


FORMATS_LINE = re.compile(r"Downloading \d+ format\(s\): (\S+)")


def track_phase(job: Job, line: str) -> None:
    """Turn yt-dlp's log lines into a short state for the queue card."""
    formats = FORMATS_LINE.search(line)
    if formats:
        job.streams, job.stream = formats.group(1).count("+") + 1, -1
    elif line.startswith("[download] Destination:"):
        job.stream += 1
    elif line.startswith("[Merger]"):
        job.phase, job.speed, job.eta = "зводжу відео й звук", "", ""
    elif line.startswith("[ExtractAudio]"):
        job.phase, job.speed, job.eta = "роблю MP3", "", ""
    elif line.startswith(("[EmbedSubtitle]", "[Metadata]", "[EmbedThumbnail]")):
        job.phase, job.speed, job.eta = "дописую дані у файл", "", ""
    elif "has already been recorded in the archive" in line or "has already been downloaded" in line:
        job.phase = "пропускаю вже скачане"
    elif line.startswith("[") and not job.percent and "Extracting URL" in line:
        job.phase = "читаю сторінку"


def run_job(job: Job) -> None:
    with job_slots:
        if job.cancelled:
            job.status = "cancelled"
            return
        cfg = load_settings()
        Path(cfg["out_dir"]).mkdir(parents=True, exist_ok=True)
        fd, files_log = tempfile.mkstemp(prefix="ytgrab-", suffix=".txt")
        os.close(fd)
        tail: list[str] = []
        try:
            if sites.PHOTO_POST.search(job.url) and photos.gallery_bin(VENV_DIR):
                return switch_to_photos(job, cfg)
            cmd = build_download_cmd(job.url, job.quality, job.use_archive, cfg, files_log, job.date_from, job.date_to)
            job.status, job.phase = "running", "читаю сторінку"
            job.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1,
            )
            assert job.proc.stdout is not None
            for raw in job.proc.stdout:
                line = raw.rstrip()
                if line.startswith(PROGRESS_PREFIX):
                    parse_progress(job, line)
                elif line:
                    tail = (tail + [line])[-20:]
                    track_phase(job, line)
            code = job.proc.wait()
            job.files = [l for l in Path(files_log).read_text("utf-8").splitlines() if l.strip()]
            if job.mp4 and job.files and not job.cancelled:
                queue_mp4(job, cfg)
            if job.cancelled:
                job.status = "cancelled"
            elif code == 0:
                job.status, job.percent, job.eta = "done", 100.0, ""
                if not job.files:
                    job.error = ("За цей період нових відео немає" if job.date_from or job.date_to
                                 else "Нових відео немає: усе вже завантажено раніше")
            elif job.files:
                # --ignore-errors: some playlist items failed but others downloaded.
                job.status, job.percent = "done", 100.0
                job.error = "Частину відео пропущено: " + sites.friendly_error(
                    last_error_line("\n".join(tail)), job.url, cfg.get("cookies_browser", ""))
            elif photos.NO_VIDEO.search(last_error_line("\n".join(tail))) and photos.gallery_bin(VENV_DIR):
                # A post with photos only (or a carousel yt-dlp cannot read): let gallery-dl take it.
                job.proc = None
                switch_to_photos(job, cfg, last_error_line("\n".join(tail)))
            else:
                job.status = "error"
                job.error = (sites.friendly_error(last_error_line("\n".join(tail)), job.url, cfg.get("cookies_browser", ""))
                             or f"yt-dlp завершився з кодом {code}")
        except Exception as exc:  # surface any launcher failure in the UI
            job.status, job.error = "error", str(exc)
        finally:
            job.proc = None
            try:
                os.remove(files_log)
            except OSError:
                pass


def queue_mp4(job: Job, cfg: dict) -> None:
    """After a download, turn every fetched video into an MP4 (remux when possible)."""
    queued, skipped = 0, 0
    for path in job.files:
        if Path(path).suffix.lower() in AUDIO_EXT | IMAGE_EXT:
            continue
        try:
            info = converter.probe(path)
            preset = converter.auto_preset(info, cfg.get("mp4_codec", "auto"))
            if Path(path).suffix.lower() == ".mp4" and preset == "remux":
                skipped += 1  # already an MP4 in H.264/HEVC
                continue
            start_convert(path, preset, "source", replace=True, parent=job)
            queued += 1
        except ValueError as exc:
            job.note = f"Не вдалося поставити в MP4: {exc}"
    if queued:
        job.note = f"Конвертую в MP4: {queued}" + (f" · вже MP4: {skipped}" if skipped else "")


def start_job(url: str, title: str, quality: str, use_archive: bool, mp4: bool = False,
              date_from: str = "", date_to: str = "") -> Job:
    if quality not in ("best", "audio") and not str(quality).isdigit():
        raise ValueError("Невідома якість")
    job = Job(url, title, str(quality), use_archive)
    job.mp4 = mp4 and quality != "audio"
    job.date_from, job.date_to = date_from, date_to
    with jobs_lock:
        # Two identical jobs would race for the same output file.
        for other in jobs.values():
            if (other.status in ("queued", "running") and other.url == url
                    and other.quality == job.quality and other.kind in ("download", "photos")):
                raise ValueError("Це вже качається")
        jobs[job.id] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job


# ---------- conversion jobs ----------

def run_ffmpeg(job: Job, cmd: list[str], duration: float) -> tuple[int, str]:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err:
        job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True,
                                    encoding="utf-8", errors="replace", bufsize=1)
        assert job.proc.stdout is not None
        speed = 0.0
        for raw in job.proc.stdout:
            key, _, value = raw.strip().partition("=")
            if key == "out_time_us" and value.isdigit() and duration > 0:
                done = int(value) / 1_000_000
                job.percent = min(99.9, done / duration * 100)
                if speed > 0:
                    left = int((duration - done) / speed)
                    job.eta = f"{left // 60}:{left % 60:02d}"
            elif key == "speed" and value.endswith("x"):
                try:
                    speed = float(value[:-1])
                    job.speed = f"{speed:.2f}x"
                except ValueError:
                    pass
        code = job.proc.wait()
        err.seek(0)
        return code, last_error_line(err.read())


def run_convert(job: Job) -> None:
    with convert_slots:
        if job.cancelled:
            job.status = "cancelled"
            report_to_parent(job, Path(job.info["path"]), Path(job.info["path"]))
            return
        info = job.info
        src = Path(info["path"])
        dst = converter.output_path(info["path"], job.quality)
        keep_original = bool(load_settings().get("keep_original"))
        if job.replace and src.suffix.lower() != ".mp4" and not src.with_suffix(".mp4").exists():
            dst = src.with_suffix(".mp4")  # "title [id].2160p.mkv" -> "title [id].2160p.mp4"
        elif job.replace and src.suffix.lower() == ".mp4" and not keep_original:
            dst = src  # an AV1/VP9 .mp4 is re-encoded and takes the original's place
        part = dst.with_name(dst.name[: -len(".mp4")] + ".part.mp4")
        hardware = True
        try:
            job.status = "running"
            while True:
                cmd, notes = converter.build_command(info, job.preset, job.res, str(part), hardware)
                job.note = " ".join(notes)
                code, err = run_ffmpeg(job, cmd, info["duration"])
                if code == 0 or job.cancelled or not converter.is_hardware(cmd):
                    break
                # VideoToolbox rejects some sizes/profiles; redo the job with x264/x265.
                hardware, job.percent, job.current = False, 0.0, "апаратне кодування не впоралось, кодую програмно"
                part.unlink(missing_ok=True)
            if job.cancelled:
                job.status = "cancelled"
            elif code == 0:
                part.rename(dst)
                job.files, job.status, job.percent, job.eta, job.speed = [str(dst)], "done", 100.0, "", ""
                job.current = dst.name
                if job.replace and not keep_original and src != dst:
                    src.unlink(missing_ok=True)
            else:
                job.status, job.error = "error", err or f"ffmpeg завершився з кодом {code}"
        except Exception as exc:  # surface any launcher failure in the UI
            job.status, job.error = "error", str(exc)
        finally:
            job.proc = None
            if job.status != "done":
                part.unlink(missing_ok=True)
            report_to_parent(job, src, dst)


def report_to_parent(job: Job, src: Path, dst: Path) -> None:
    """Point the download's card at the new MP4 so its buttons (→ SRT, Показати) use it."""
    parent = job.parent
    if parent is None:
        return
    with jobs_lock:
        parent.converting = max(0, parent.converting - 1)
        if job.status == "done":
            parent.files = [str(dst) if Path(f).resolve() == src.resolve() else f for f in parent.files]
            if str(dst) not in parent.files:
                parent.files.append(str(dst))
        failed = job.status == "error"
        if failed:
            parent.note = f"MP4 не вдався: {job.error[:160]}"
        elif parent.converting == 0 and not parent.note.startswith("MP4 не вдався"):
            parent.note = "Готово в MP4" if job.status == "done" else ""


def start_convert(path: str, preset: str, res: str, replace: bool = False, parent: Job | None = None) -> Job:
    src = Path(os.path.expanduser(path.strip())).resolve()
    if not src.is_file():
        raise ValueError(f"Файл не знайдено: {path}")
    if preset not in converter.PRESETS:
        raise ValueError("Невідомий пресет")
    if res != "source" and not res.isdigit():
        raise ValueError("Невідома роздільність")
    info = converter.probe(str(src))
    label = converter.label_for(info, preset, res)
    job = Job(str(src), src.name, label, False, kind="convert")
    job.info, job.preset, job.res, job.replace = info, preset, res, replace
    job.parent = parent
    if preset == "remux":
        converter.build_command(info, preset, res, "/dev/null", False)  # validate codecs up front
    with jobs_lock:
        for other in jobs.values():
            if (other.kind == "convert" and other.status in ("queued", "running")
                    and other.url == job.url and other.quality == label):
                raise ValueError("Цей файл уже конвертується")
        jobs[job.id] = job
        if parent is not None:
            parent.converting += 1  # counted before the thread starts, so a fast finish can't race it
    threading.Thread(target=run_convert, args=(job,), daemon=True).start()
    return job


# ---------- private yt-dlp install ----------

def find_python() -> str | None:
    # yt-dlp needs Python 3.10+; macOS's bundled python3 can be 3.9, Homebrew's yt-dlp pulls a newer one.
    if sys.version_info >= (3, 10):
        return sys.executable
    for name in ("python3.14", "python3.13", "python3.12", "python3.11", "python3.10"):
        found = shutil.which(name) or next((p for p in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}")
                                            if os.access(p, os.X_OK)), None)
        if found:
            return found
    return None


def run_steps(job: Job, steps: list[tuple[str, list[str]]], finish: Callable[[Job], None]) -> None:
    job.status = "running"
    tail: list[str] = []
    try:
        for n, (label, cmd) in enumerate(steps, 1):
            if job.cancelled:
                break
            job.current = label
            job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace", bufsize=1)
            assert job.proc.stdout is not None
            for raw in job.proc.stdout:
                if raw.strip():
                    tail = (tail + [raw.strip()])[-20:]
            if job.proc.wait() != 0:
                if job.cancelled:
                    break
                raise RuntimeError(last_error_line("\n".join(tail)) or f"крок «{label}» не вдався")
            job.percent = n / len(steps) * 100
        if job.cancelled:
            job.status = "cancelled"
        else:
            finish(job)
            job.status, job.percent = "done", 100.0
    except Exception as exc:  # surface any launcher failure in the UI
        job.status, job.error = "error", str(exc)
    finally:
        job.proc = None


def start_steps(title: str, steps_factory: Callable[[], list[tuple[str, list[str]]]],
                finish: Callable[[Job], None]) -> Job:
    steps = steps_factory()  # raises ValueError for unsupported setups before a job is queued
    with jobs_lock:
        if any(j.kind == "setup" and j.status in ("queued", "running") for j in jobs.values()):
            raise ValueError("Встановлення вже йде, дочекайся його в Черзі")
        job = Job("setup", title, "setup", False, kind="setup")
        jobs[job.id] = job
    threading.Thread(target=run_steps, args=(job, steps, finish), daemon=True).start()
    return job


def can_self_install() -> bool:
    """macOS can fetch every tool itself; elsewhere ffmpeg/deno come from the system package manager."""
    return sys.platform == "darwin"


def missing_components() -> list[str]:
    status = tool_status()
    return [name for name, ok in status.items() if not ok]


def components_setup_steps() -> list[tuple[str, list[str]]]:
    steps = ytdlp_setup_steps()
    if can_self_install():
        if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            steps.append(("завантажую ffmpeg і ffprobe (~55 МБ)", [sys.executable, str(COMPONENTS), "ffmpeg"]))
        if not shutil.which("deno"):
            steps.append(("завантажую deno (~45 МБ)", [sys.executable, str(COMPONENTS), "deno"]))
    return steps


def ytdlp_setup_steps() -> list[tuple[str, list[str]]]:
    uv = transcribe.uv_bin()
    if uv:
        venv_python = VENV_DIR / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
        return [
            ("готую середовище", [uv, "venv", "--allow-existing", "--python", "3.12", str(VENV_DIR)]),
            ("ставлю yt-dlp, curl_cffi і gallery-dl", [uv, "pip", "install", "--python", str(venv_python),
                                                       "--upgrade", YTDLP_PACKAGE, "gallery-dl"]),
        ]
    python = find_python()
    if not python:
        raise ValueError("Потрібен Python 3.10+: brew install python")
    steps = []
    if not own_ytdlp().exists():
        steps.append(("створюю окреме середовище", [python, "-m", "venv", str(VENV_DIR)]))
    venv_python = VENV_DIR / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    steps.append(("встановлюю yt-dlp і curl_cffi", [str(venv_python), "-m", "pip", "install", "--upgrade",
                                                   "--disable-pip-version-check", YTDLP_PACKAGE, "gallery-dl"]))
    return steps


def finish_ytdlp_setup(job: Job) -> None:
    _ytdlp_info_cache.clear()
    missing = missing_components()
    job.current = "усе встановлено" if not missing else "ще бракує: " + ", ".join(missing)


def start_setup(title: str = "Встановлення компонентів") -> Job:
    return start_steps(title, components_setup_steps, finish_ytdlp_setup)


# ---------- keeping yt-dlp fresh ----------

def read_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(**changes) -> None:
    data = read_state()
    data.update(changes)
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    except OSError:
        pass


def version_tuple(version: str | None) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version or ""))


def latest_ytdlp(force: bool = False) -> str | None:
    """Newest yt-dlp on PyPI; asked at most once in 20 hours."""
    state = read_state()
    if not force and time.time() - float(state.get("ytdlp_checked") or 0) < 20 * 3600:
        return state.get("ytdlp_latest")
    try:
        with urllib.request.urlopen("https://pypi.org/pypi/yt-dlp/json", timeout=15) as resp:
            latest = str(json.load(resp)["info"]["version"])
    except Exception:  # offline or PyPI is down: try again on the next round
        return state.get("ytdlp_latest")
    write_state(ytdlp_checked=time.time(), ytdlp_latest=latest)
    return latest


def ytdlp_outdated() -> str | None:
    """The newer version, when YT Grab's own yt-dlp is behind PyPI."""
    info = ytdlp_info()
    if not (info["own"] and info["version"]):
        return None
    latest = latest_ytdlp()
    return latest if latest and version_tuple(latest) > version_tuple(info["version"]) else None


def active_jobs() -> int:
    return sum(1 for j in list(jobs.values()) if j.status in ("queued", "running"))


def watch_ytdlp() -> None:
    """Sites change weekly and an old yt-dlp is the usual reason a download fails,
    so a newer release is installed quietly as soon as the queue is idle."""
    time.sleep(20)
    while True:
        try:
            latest = ytdlp_outdated() if can_self_install() else None
            if latest:
                while active_jobs():
                    time.sleep(30)
                job = start_setup(f"Оновлення yt-dlp до {latest}")
                while job.status in ("queued", "running"):
                    time.sleep(2)
                if job.status != "done":
                    time.sleep(6 * 3600)  # no luck (offline?): do not hammer
        except Exception as exc:
            print(f"yt-dlp update check failed: {exc}", flush=True)
        time.sleep(3600)


def start_asr_setup(engine: str) -> Job:
    if engine not in transcribe.ENGINES:
        raise ValueError("Невідомий рушій")
    python = find_python() or sys.executable

    def finish(job: Job) -> None:
        transcribe.mark_ready(engine)
        job.current = f"{transcribe.ENGINES[engine]['name']} готовий"

    return start_steps(f"Встановлення {transcribe.ENGINES[engine]['name']}",
                       lambda: transcribe.setup_steps(engine, python), finish)


# ---------- photo posts ----------

def run_gallery(job: Job, cmd: list[str], cfg: dict, total: int = 0) -> tuple[int, list[str], int]:
    """Run gallery-dl, collecting new files into job.files. Returns (exit code, output tail, skipped)."""
    tail: list[str] = []
    skipped = 0
    job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)
    assert job.proc.stdout is not None
    for raw in job.proc.stdout:
        line = raw.strip()
        existed = line.startswith("# ")  # "# " marks files that were already there (or in the archive)
        path = line[2:] if existed else line
        if path and os.path.isfile(path):
            if existed and total == 0:
                skipped += 1
                continue
            job.files.append(path)
            job.current = Path(path).name
            # Profiles have no known total: let the bar creep towards the end as files arrive.
            job.percent = min(99.0, len(job.files) / total * 100) if total else 100 * (1 - 0.92 ** len(job.files))
        elif line:
            tail = (tail + [line])[-10:]
    return job.proc.wait(), tail, skipped


def download_photos(job: Job, cfg: dict, binary: str) -> None:
    """Fetch a photo post (job.info from photos.probe) into its own folder. Runs inside a job slot."""
    folder = photos.target_dir(cfg["out_dir"], job.platform, job.info)
    folder.mkdir(parents=True, exist_ok=True)
    total = max(1, int(job.info.get("count") or 1))
    job.status, job.current = "running", folder.name
    code, tail, _ = run_gallery(job, photos.download_cmd(binary, job.url, folder, cfg.get("cookies_browser", "")),
                                cfg, total)
    if job.cancelled:
        job.status = "cancelled"
    elif job.files:
        job.status, job.percent = "done", 100.0
        if code != 0:
            job.error = "Частину файлів пропущено: " + sites.friendly_error(
                last_error_line("\n".join(tail)), job.url, cfg.get("cookies_browser", ""))
    else:
        job.status = "error"
        job.error = (sites.friendly_error(last_error_line("\n".join(tail)), job.url, cfg.get("cookies_browser", ""))
                     or "gallery-dl нічого не скачав")


def set_photo_info(job: Job, info: dict) -> None:
    platform = sites.detect(job.url)
    clean = {k: str(info.get(k) or "") for k in ("title", "uploader", "date", "post_id")}
    clean["count"] = int(info.get("count") or 0)
    job.info, job.platform = clean, (platform[1] if platform else "Other")


def switch_to_photos(job: Job, cfg: dict, ytdlp_error: str = "") -> None:
    """Turn a download that found no video into a photo-post download (used by pasted links and batches)."""
    binary = photos.gallery_bin(VENV_DIR)
    job.status, job.current, job.error = "running", "шукаю фото в пості", ""
    try:
        info = photos.probe(binary, job.url, cfg.get("cookies_browser", ""))
    except subprocess.TimeoutExpired:
        raise ValueError("Сайт надто довго не відповідає, спробуй ще раз")
    except ValueError as exc:
        raise ValueError(sites.friendly_error(str(exc), job.url, cfg.get("cookies_browser", "")))
    if not info:
        # Nothing for gallery-dl either: yt-dlp's own message (e.g. "unsupported URL") says more.
        raise ValueError(sites.friendly_error(ytdlp_error, job.url, cfg.get("cookies_browser", ""))
                         or "У цьому пості немає ні відео, ні фото")
    job.kind, job.quality, job.mp4 = "photos", "photos", False
    set_photo_info(job, info)
    if job.title == job.url and info.get("title"):
        job.title = info["title"]
    download_photos(job, cfg, binary)


def run_photos(job: Job) -> None:
    with job_slots:
        if job.cancelled:
            job.status = "cancelled"
            return
        cfg = load_settings()
        binary = photos.gallery_bin(VENV_DIR)
        try:
            if not binary:
                raise RuntimeError("gallery-dl не встановлено: Налаштування → «Інструменти»")
            download_photos(job, cfg, binary)
        except Exception as exc:  # surface any launcher failure in the UI
            job.status, job.error = "error", str(exc)
        finally:
            job.proc = None


# ---------- Instagram profiles ----------

PROFILE_TABS = {"reels": "рілси", "posts": "пости"}


def run_profile(job: Job) -> None:
    with job_slots:
        if job.cancelled:
            job.status = "cancelled"
            return
        cfg = load_settings()
        binary = photos.gallery_bin(VENV_DIR)
        username, tab = job.info["username"], job.info["tab"]
        try:
            if not binary:
                raise RuntimeError("gallery-dl не встановлено: Налаштування → «Оновити компоненти»")
            folder = photos.profile_dir(cfg["out_dir"], username)
            folder.mkdir(parents=True, exist_ok=True)
            # gallery-dl's --date-before is exclusive; the UI's "to" date is inclusive.
            before = (date.fromisoformat(job.date_to) + timedelta(days=1)).isoformat() if job.date_to else ""
            cmd = photos.profile_cmd(binary, username, tab, folder, Path(cfg["out_dir"]) / ".archive-gallery.sqlite3",
                                     cfg.get("cookies_browser", ""), job.date_from, before)
            job.status, job.current = "running", "читаю профіль"
            code, tail, skipped = run_gallery(job, cmd, cfg)
            if job.cancelled:
                job.status = "cancelled"
            elif job.files or code == 0:
                job.status, job.percent, job.current = "done", 100.0, ""
                if skipped:
                    job.note = f"Уже були завантажені раніше: {skipped}"
                if code != 0:
                    job.error = "Частину постів пропущено: " + sites.friendly_error(
                        last_error_line("\n".join(tail)), job.url, cfg.get("cookies_browser", ""))
                elif not job.files:
                    job.error = "За цей період нових постів немає" if skipped == 0 else "Нових постів немає"
                if job.mp4 and job.files:
                    queue_mp4(job, cfg)
            else:
                job.status = "error"
                browser = cfg.get("cookies_browser", "")
                if browser and not cookies_readable(browser):
                    job.error = ("Instagram пускає до профілю лише після входу, а YT Grab не бачить cookies браузера. "
                                 "Налаштування → «Дати доступ», потім перезапусти YT Grab")
                else:
                    job.error = (sites.friendly_error(last_error_line("\n".join(tail)), job.url, browser)
                                 or "gallery-dl нічого не скачав")
        except Exception as exc:  # surface any launcher failure in the UI
            job.status, job.error = "error", str(exc)
        finally:
            job.proc = None


def profile_title(username: str, tab: str, date_from: str, date_to: str) -> str:
    date_from, date_to = (f"{d[8:10]}.{d[5:7]}.{d[:4]}" if d else "" for d in (date_from, date_to))
    if date_from and date_to:
        period = f"{date_from} – {date_to}"
    elif date_from:
        period = f"з {date_from}"
    elif date_to:
        period = f"до {date_to}"
    else:
        period = f"останні {photos.PROFILE_MAX_POSTS}"
    return f"@{username} · {PROFILE_TABS[tab]} · {period}"


def start_profile(url: str, tab: str, date_from: str, date_to: str, mp4: bool) -> Job:
    profile = sites.instagram_profile(url)
    if not profile:
        raise ValueError("Це не посилання на профіль Instagram")
    if tab not in PROFILE_TABS:
        raise ValueError("Обери рілси або пости")
    if not photos.gallery_bin(VENV_DIR):
        raise ValueError("Для профілів Instagram потрібен gallery-dl: Налаштування → «Оновити компоненти»")
    username = profile[0]
    job = Job(f"https://www.instagram.com/{username}/{tab}/", profile_title(username, tab, date_from, date_to),
              tab, True, kind="profile")
    job.info = {"username": username, "tab": tab}
    job.date_from, job.date_to, job.mp4 = date_from, date_to, mp4
    with jobs_lock:
        for other in jobs.values():
            if other.kind == "profile" and other.status in ("queued", "running") and other.url == job.url:
                raise ValueError("Цей профіль уже качається")
        jobs[job.id] = job
    threading.Thread(target=run_profile, args=(job,), daemon=True).start()
    return job


# ---------- batches ----------

def start_batch(urls: list, quality: str, mp4: bool) -> dict:
    """Queue many links from any sites at once; photo posts are routed to gallery-dl by run_job."""
    added, errors, seen = [], [], set()
    duplicates = 0
    for raw in urls[:200]:
        raw = str(raw).strip()
        if not raw:
            continue
        try:
            url = normalize_url(sites.clean_url(raw))
            if url in seen:
                duplicates += 1
                continue
            seen.add(url)
            if sites.instagram_profile(url):
                raise ValueError("Це профіль: встав його окремо на головній, щоб обрати період")
            job = start_job(url, "", quality, use_archive=True, mp4=mp4)
            added.append({"url": url, "id": job.id})
        except ValueError as exc:
            if str(exc) == "Це вже качається":
                duplicates += 1
            else:
                errors.append({"url": raw, "error": str(exc)})
    return {"added": added, "errors": errors, "duplicates": duplicates}


def start_photos(url: str, info: dict) -> Job:
    job = Job(url, str(info.get("title") or "") or url, "photos", False, kind="photos")
    set_photo_info(job, info)
    with jobs_lock:
        for other in jobs.values():
            if other.kind == "photos" and other.status in ("queued", "running") and other.url == url:
                raise ValueError("Цей пост уже качається")
        jobs[job.id] = job
    threading.Thread(target=run_photos, args=(job,), daemon=True).start()
    return job


# ---------- transcription jobs ----------

def run_transcription(job: Job) -> None:
    with transcribe_slots:
        if job.cancelled:
            job.status = "cancelled"
            return
        cfg = load_settings()
        workdir = tempfile.mkdtemp(prefix="ytgrab-asr-")
        try:
            job.status, job.current = "running", "дістаю звук"
            src, info = job.url, job.info
            started = time.time()

            def progress(p: float) -> None:
                job.percent = p
                elapsed = time.time() - started
                if p > 2:
                    left = int(elapsed / p * (100 - p))
                    job.eta = f"{left // 60}:{left % 60:02d}"

            if job.engine == "groq":
                job.current = "надсилаю в Groq"
                result = transcribe.run_groq(src, job.language, info["duration"], cfg.get("groq_api_key", ""),
                                             workdir, progress, lambda: job.cancelled)
            else:
                wav = os.path.join(workdir, "audio.wav")
                transcribe.extract_audio(src, wav)
                if job.cancelled:
                    raise RuntimeError("cancelled")
                job.current = "розпізнаю мову" if job.engine == "parakeet" else "розпізнаю"

                def register(proc: subprocess.Popen) -> None:
                    job.proc = proc
                    if job.cancelled:
                        proc.terminate()

                result = transcribe.run_local(job.engine, wav, job.language, info["duration"], progress, register)
            if job.cancelled:
                raise RuntimeError("cancelled")
            cues = transcribe.build_cues(result)
            if not cues:
                raise RuntimeError("Мови не розпізнано: у файлі тиша або музика")
            srt = transcribe.unique_path(src, ".srt")
            srt.write_text(transcribe.to_srt(cues), "utf-8")
            job.files = [str(srt)]
            if job.want_txt:
                txt = transcribe.unique_path(src, ".txt")
                txt.write_text(transcribe.to_text(result), "utf-8")
                job.files.append(str(txt))
            lang = result.get("language")
            done_text = f"{len(cues)} титрів" + (f" · мова: {lang}" if lang else "")
            if job.want_summary and not job.cancelled:
                write_summary(job, result, cfg, src)
            job.status, job.percent, job.eta = "done", 100.0, ""
            job.current = done_text + (" · є підсумок" if any(f.endswith("підсумок.md") for f in job.files) else "")
        except Exception as exc:  # surface any engine failure in the UI
            if job.cancelled:
                job.status = "cancelled"
            else:
                job.status, job.error = "error", str(exc)
        finally:
            job.proc = None
            shutil.rmtree(workdir, ignore_errors=True)


def write_summary(job: Job, result: dict, cfg: dict, path: str) -> None:
    """Add "<name> — підсумок.md"; a failed summary never discards the subtitles."""
    src = Path(path)
    try:
        engine = summarize.pick_engine(cfg)
        job.current, job.percent, job.eta = "пишу підсумок", 0.0, ""
        transcript = summary_prompts.transcript_text(result)
        language = cfg.get("summary_lang", "uk")

        def progress(p: float) -> None:
            job.percent = p

        if engine == "local":
            def register(proc: subprocess.Popen) -> None:
                job.proc = proc
                if job.cancelled:
                    proc.terminate()

            body = summarize.run_local(transcript, language, src.stem, progress, register)
            model = summarize.LOCAL_MODEL_NAME
        else:
            body = summarize.run_groq(transcript, language, src.stem, cfg.get("groq_api_key", ""), progress)
            model = f"Groq · {summarize.GROQ_MODEL}"
        out = transcribe.unique_path(str(src), " — підсумок.md")
        out.write_text(summary_prompts.document(body, src.stem, src.name, model, time.strftime("%Y-%m-%d")), "utf-8")
        job.files.append(str(out))
    except Exception as exc:  # keep the finished subtitles; explain what went wrong with the summary
        if not job.cancelled:
            job.note = f"Субтитри готові, але підсумок не вдався: {exc}"


def start_transcription(path: str, engine: str, language: str, want_txt: bool, want_summary: bool = False) -> Job:
    src = Path(os.path.expanduser(path.strip())).resolve()
    if not src.is_file():
        raise ValueError(f"Файл не знайдено: {path}")
    if engine not in transcribe.ENGINES:
        raise ValueError("Невідомий рушій")
    if engine == "parakeet" and not transcribe.is_apple_silicon():
        raise ValueError("Parakeet працює лише на Маках з чипом Apple")
    if language not in dict(transcribe.LANGUAGES):
        raise ValueError("Невідома мова")
    info = transcribe.media_probe(str(src))
    label = transcribe.ENGINES[engine]["name"] + (" + підсумок" if want_summary else "")
    job = Job(str(src), src.name, label, False, kind="transcribe")
    job.info, job.engine, job.language, job.want_txt = info, engine, language, want_txt
    job.want_summary = want_summary
    with jobs_lock:
        for other in jobs.values():
            if other.kind == "transcribe" and other.status in ("queued", "running") and other.url == job.url:
                raise ValueError("Цей файл уже розпізнається")
        jobs[job.id] = job
    threading.Thread(target=run_transcription, args=(job,), daemon=True).start()
    return job


# ---------- locating dropped files and native file dialogs ----------

SEARCH_ROOTS = ("Downloads", "Desktop", "Movies", "Documents", "Music", "Pictures")


def _candidates(name: str) -> list[Path]:
    found: list[Path] = []
    if shutil.which("mdfind"):
        try:
            out = subprocess.run(["mdfind", "-name", name], capture_output=True, text=True, timeout=15).stdout
            found = [Path(line) for line in out.splitlines() if os.path.basename(line) == name]
        except (OSError, subprocess.TimeoutExpired):
            found = []
    if found:
        return found
    # Spotlight unavailable or the volume is not indexed: walk the usual folders (bounded).
    roots = [Path(load_settings()["out_dir"])] + [Path.home() / r for r in SEARCH_ROOTS]
    seen = 0
    for root in roots:
        if not root.is_dir():
            continue
        if root.name == name:
            found.append(root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            seen += len(filenames) + len(dirnames)
            if name in filenames or name in dirnames:
                found.append(Path(dirpath) / name)
            if seen > 200_000:
                return found
    return found


def locate(item: dict) -> Path | None:
    name = os.path.basename(str(item.get("name", "")))
    if not name:
        return None
    want_dir = bool(item.get("dir"))
    size, mtime = item.get("size"), item.get("mtime")
    matches = []
    for path in dict.fromkeys(_candidates(name)):
        try:
            st = path.stat()
        except OSError:
            continue
        if want_dir != path.is_dir():
            continue
        if not want_dir and size is not None and st.st_size != int(size):
            continue
        matches.append((path, st))
    if len(matches) > 1 and mtime:
        close = [m for m in matches if abs(m[1].st_mtime - float(mtime) / 1000) < 3]
        matches = close or matches
    if not matches:
        return None
    matches.sort(key=lambda m: m[1].st_mtime, reverse=True)
    return matches[0][0]


def expand_media(paths: list[Path], limit: int = 500) -> list[str]:
    out: list[str] = []
    for path in paths:
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if (child.is_file() and child.suffix.lower() in transcribe.MEDIA_EXT
                        and not child.name.startswith(".") and ".part." not in child.name):
                    out.append(str(child))
                    if len(out) >= limit:
                        return out
        elif path.is_file():
            out.append(str(path))
    return out


def choose_files(prompt: str) -> list[str]:
    if sys.platform == "darwin":
        script = (
            f'set picked to choose file with prompt "{prompt}" with multiple selections allowed\n'
            'set out to ""\n'
            'repeat with f in picked\n'
            '  set out to out & POSIX path of f & linefeed\n'
            'end repeat\n'
            'return out'
        )
        proc = subprocess.run(["osascript", "-e", "activate", "-e", script], capture_output=True, text=True)
        if proc.returncode != 0:
            if "-128" in proc.stderr:  # user pressed Cancel
                return []
            raise ValueError("Не вдалося відкрити вікно вибору: " + proc.stderr.strip()[:200])
        return [line for line in proc.stdout.splitlines() if line.strip()]
    if shutil.which("zenity"):
        proc = subprocess.run(["zenity", "--file-selection", "--multiple", "--separator=\n", f"--title={prompt}"],
                              capture_output=True, text=True)
        return [line for line in proc.stdout.splitlines() if line.strip()]
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        picked = filedialog.askopenfilenames(title=prompt)
        root.destroy()
        return list(picked)
    except Exception as exc:
        raise ValueError(f"Вікно вибору файлів недоступне ({exc}). Встав шлях вручну.")


def cancel_job(job_id: str) -> None:
    job = jobs.get(job_id)
    if not job:
        raise ValueError("Завдання не знайдено")
    job.cancelled = True
    proc = job.proc
    if proc and proc.poll() is None:
        proc.terminate()
    elif job.status == "queued":
        job.status = "cancelled"


# ---------- retry ----------

def retry_job(job_id: str) -> Job:
    """Start a failed or cancelled job again with the same parameters; the old card goes away."""
    job = jobs.get(job_id)
    if not job:
        raise ValueError("Завдання не знайдено")
    if not job.can_retry():
        raise ValueError("Це завдання не можна повторити")
    if job.kind == "download":
        new = start_job(job.url, "" if job.title == job.url else job.title, job.quality, job.use_archive,
                        job.mp4, job.date_from, job.date_to)
    elif job.kind == "photos":
        new = start_photos(job.url, {**(getattr(job, "info", None) or {}), "title": job.title})
    elif job.kind == "profile":
        new = start_profile(job.url, str(job.info.get("tab") or "reels"), job.date_from, job.date_to, job.mp4)
    elif job.kind == "convert":
        new = start_convert(job.url, job.preset, job.res, replace=job.replace)
    else:
        new = start_transcription(job.url, job.engine, job.language, bool(job.want_txt),
                                  bool(getattr(job, "want_summary", False)))
    new.thumbnail = job.thumbnail
    with jobs_lock:
        jobs.pop(job_id, None)
    return new


def retry_after_update(job_id: str) -> Job:
    """Update yt-dlp first, then retry: the cure for most "403" and "unable to extract" failures."""
    job = jobs.get(job_id)
    if not job or not job.can_retry():
        raise ValueError("Це завдання не можна повторити")
    latest_ytdlp(force=True)
    setup = start_setup("Оновлення yt-dlp")

    def wait() -> None:
        while setup.status in ("queued", "running"):
            time.sleep(1)
        if setup.status == "done":
            try:
                retry_job(job_id)
            except ValueError as exc:
                print(f"retry after update: {exc}", flush=True)

    threading.Thread(target=wait, daemon=True).start()
    return setup


# ---------- queue on disk, events for the macOS shell ----------

def load_jobs() -> None:
    try:
        items = json.loads(JOBS_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return
    with jobs_lock:
        for item in items if isinstance(items, list) else []:
            try:
                job = Job.from_disk(item)
            except (KeyError, TypeError, ValueError):
                continue
            jobs[job.id] = job


def jobs_snapshot() -> str:
    with jobs_lock:
        kept = sorted((j for j in jobs.values() if j.kind in RETRY_KINDS), key=lambda j: j.created, reverse=True)
        return json.dumps([j.to_disk() for j in kept[:HISTORY_LIMIT]], ensure_ascii=False, indent=1)


def save_jobs(snapshot: str) -> None:
    try:
        JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = JOBS_FILE.with_suffix(".tmp")
        tmp.write_text(snapshot, "utf-8")
        tmp.replace(JOBS_FILE)
    except OSError as exc:
        print(f"queue not saved: {exc}", flush=True)


def emit_event(**event) -> None:
    """One line on stdout; the macOS app turns it into a notification or a Dock badge."""
    print(EVENT_PREFIX + " " + json.dumps(event, ensure_ascii=False), flush=True)


def watch_jobs() -> None:
    saved = jobs_snapshot()
    seen = {j.id for j in list(jobs.values())}  # restored history never notifies
    last_active = -1
    recent: list[float] = []  # when the latest notifications went out
    held = {"done": 0, "error": 0}  # finished during a burst, reported once the queue is empty
    while True:
        time.sleep(1)
        try:
            snapshot = jobs_snapshot()
            if snapshot != saved:
                save_jobs(snapshot)
                saved = snapshot
            active = active_jobs()
            if active != last_active:
                emit_event(type="active", count=active)
                last_active = active
            now = time.time()
            recent = [t for t in recent if now - t < 60]
            for job in list(jobs.values()):
                if job.id in seen or job.status in ("queued", "running") or job.converting:
                    continue
                seen.add(job.id)
                # Automatic MP4 conversions report through their download; setup and cancels stay quiet.
                if job.parent is not None or job.kind == "setup" or job.status == "cancelled":
                    continue
                if len(recent) >= 3:
                    held[job.status] += 1  # a batch: one summary instead of a wall of banners
                    continue
                recent.append(now)
                if job.status == "done":
                    detail = job.error or ("Готово" + (f" · файлів: {len(job.files)}" if len(job.files) > 1 else ""))
                else:
                    detail = "Помилка: " + job.error[:140]
                emit_event(type=job.status, title=job.title[:120], detail=detail)
            if not active and (held["done"] or held["error"]):
                detail = f"Готово: {held['done']}" + (f" · з помилкою: {held['error']}" if held["error"] else "")
                emit_event(type="error" if held["error"] and not held["done"] else "done",
                           title="Черга завершена", detail=detail)
                held = {"done": 0, "error": 0}
        except Exception as exc:
            print(f"queue watcher: {exc}", flush=True)


# ---------- downloaded files ----------

def list_downloads(limit: int = 500) -> list[dict]:
    root = Path(load_settings()["out_dir"])
    if not root.is_dir():
        return []
    items = []
    for path in root.rglob("*"):
        if (path.suffix.lower() in MEDIA_EXT and path.is_file() and not path.name.startswith(".")
                and ".part." not in path.name):
            stat = path.stat()
            items.append({
                "path": str(path), "name": path.name, "folder": path.parent.name,
                "size": stat.st_size, "mtime": stat.st_mtime,
                "video": path.suffix.lower() not in AUDIO_EXT | IMAGE_EXT,
                "media": path.suffix.lower() not in IMAGE_EXT,
                "image": path.suffix.lower() in IMAGE_EXT,
            })
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return items[:limit]


def open_path(path_str: str, reveal: bool) -> None:
    root = Path(load_settings()["out_dir"]).resolve()
    path = Path(path_str).resolve()
    if root not in path.parents and path != root:
        raise ValueError("Файл поза папкою завантажень")
    if not path.exists():
        raise ValueError("Файл не знайдено")
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)] if reveal else ["open", str(path)])
    elif sys.platform.startswith("win"):
        if reveal:
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path.parent if reveal else path)])


# ---------- image previews ----------

# Social CDNs refuse hot-linked images in the browser, so previews are fetched here and relayed.
THUMB_HOSTS = ("tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com", "cdninstagram.com", "fbcdn.net",
               "twimg.com", "ytimg.com", "pinimg.com", "redd.it", "redditmedia.com", "threads.net")
THUMB_MAX_BYTES = 20 * 1024 * 1024


def fetch_thumb(url: str) -> tuple[bytes, str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(host == h or host.endswith("." + h) for h in THUMB_HOSTS):
        raise ValueError("Недозволене джерело зображення")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        ctype = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
        if not ctype.startswith("image/"):
            raise ValueError("Це не зображення")
        data = resp.read(THUMB_MAX_BYTES + 1)
    if len(data) > THUMB_MAX_BYTES:
        raise ValueError("Зображення завелике")
    return data, ctype


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "YTGrab/1.0"

    def log_message(self, fmt: str, *args) -> None:  # keep the terminal quiet
        pass

    def _host_allowed(self) -> bool:
        # Blocks DNS-rebinding: only answer requests addressed to our loopback host.
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        # Requiring application/json forces a CORS preflight for cross-site callers,
        # which we never approve, so other websites cannot drive this API.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise ValueError("Очікується JSON")
        length = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("Очікується JSON-об'єкт")
        return data

    def do_GET(self) -> None:
        if not self._host_allowed():
            return self._send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = (APP_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            state = read_state()
            self._send_json({"tools": tool_status(), "ytdlp": {**ytdlp_info(), "latest": state.get("ytdlp_latest")},
                             "settings": public_settings(),
                             "self_install": can_self_install(),
                             "setup_running": any(j.kind == "setup" and j.status in ("queued", "running")
                                                  for j in jobs.values()),
                             "platform": sys.platform, "platforms": sites.platform_list()})
        elif path == "/api/jobs":
            with jobs_lock:
                data = [j.to_json() for j in sorted(jobs.values(), key=lambda j: j.created, reverse=True)]
            self._send_json(data)
        elif path == "/api/downloads":
            self._send_json(list_downloads())
        elif path == "/api/thumb":
            try:
                data, ctype = fetch_thumb((parse_qs(urlparse(self.path).query).get("u") or [""])[0])
            except (ValueError, OSError) as exc:
                return self._send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/convert/caps":
            self._send_json(converter.capabilities())
        elif path == "/api/asr":
            cfg = load_settings()
            self._send_json({"engines": transcribe.engines_status(cfg), "apple_silicon": transcribe.is_apple_silicon(),
                             "languages": [{"code": c, "name": n} for c, n in transcribe.LANGUAGES],
                             "settings": public_settings(cfg), "summary": summarize.status(cfg)})
        else:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._host_allowed():
            return self._send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        path = urlparse(self.path).path
        try:
            data = self._read_json()
            if path == "/api/info":
                self._send_json(fetch_info(normalize_url(str(data.get("url", "")))))
            elif path == "/api/download":
                mp4 = bool(data.get("mp4", load_settings().get("auto_mp4")))
                date_from, date_to = parse_dates(data)
                job = start_job(normalize_url(str(data.get("url", ""))), str(data.get("title") or ""),
                                str(data.get("quality", "best")), use_archive=data.get("kind") != "video", mp4=mp4,
                                date_from=date_from, date_to=date_to)
                job.thumbnail = clean_thumbnail(data.get("thumbnail"))
                self._send_json(job.to_json())
            elif path == "/api/profile":
                date_from, date_to = parse_dates(data)
                mp4 = bool(data.get("mp4", load_settings().get("auto_mp4")))
                self._send_json(start_profile(normalize_url(str(data.get("url", ""))), str(data.get("tab") or "reels"),
                                              date_from, date_to, mp4).to_json())
            elif path == "/api/batch":
                urls = data.get("urls") or []
                if not isinstance(urls, list) or not urls:
                    raise ValueError("Встав хоча б одне посилання")
                mp4 = bool(data.get("mp4", load_settings().get("auto_mp4")))
                self._send_json(start_batch(urls, str(data.get("quality", "best")), mp4))
            elif path == "/api/probe":
                src = Path(os.path.expanduser(str(data.get("path", "")).strip())).resolve()
                if not src.is_file():
                    raise ValueError("Файл не знайдено")
                self._send_json(converter.probe(str(src)))
            elif path == "/api/convert":
                paths = data.get("paths") or []
                if not isinstance(paths, list) or not paths:
                    raise ValueError("Обери хоча б один файл")
                created, errors = [], []
                for item in paths:
                    try:
                        created.append(start_convert(str(item), str(data.get("preset", "h264")),
                                                     str(data.get("res", "source"))).to_json())
                    except ValueError as exc:
                        errors.append(f"{Path(str(item)).name}: {exc}")
                if not created and errors:
                    raise ValueError("; ".join(errors))
                self._send_json({"jobs": created, "errors": errors})
            elif path == "/api/tools/install":
                self._send_json(start_setup().to_json())
            elif path == "/api/photos":
                info = data.get("info") if isinstance(data.get("info"), dict) else {}
                job = start_photos(normalize_url(str(data.get("url", ""))), info)
                job.thumbnail = clean_thumbnail(data.get("thumbnail"))
                self._send_json(job.to_json())
            elif path == "/api/retry":
                job_id = str(data.get("id", ""))
                job = retry_after_update(job_id) if data.get("update") else retry_job(job_id)
                self._send_json(job.to_json())
            elif path == "/api/cancel":
                cancel_job(str(data.get("id", "")))
                self._send_json({"ok": True})
            elif path == "/api/clear":
                with jobs_lock:
                    for job_id in [k for k, j in jobs.items() if j.status in ("done", "error", "cancelled")]:
                        del jobs[job_id]
                self._send_json({"ok": True})
            elif path == "/api/settings":
                self._send_json(public_settings(save_settings(data)))
            elif path == "/api/asr/install":
                self._send_json(start_asr_setup(str(data.get("engine", ""))).to_json())
            elif path == "/api/summary/install":
                python = find_python() or sys.executable

                def finish_summary(job: Job) -> None:
                    summarize.mark_ready()
                    job.current = "Qwen3-4B готовий"

                self._send_json(start_steps("Встановлення Qwen3-4B для підсумків",
                                            lambda: summarize.setup_steps(python), finish_summary).to_json())
            elif path == "/api/media-probe":
                src = Path(os.path.expanduser(str(data.get("path", "")).strip())).resolve()
                if not src.is_file():
                    raise ValueError("Файл не знайдено")
                self._send_json(transcribe.media_probe(str(src)))
            elif path == "/api/transcribe":
                paths = data.get("paths") or []
                if not isinstance(paths, list) or not paths:
                    raise ValueError("Обери хоча б один файл")
                cfg = load_settings()
                engine = str(data.get("engine") or cfg["asr_engine"])
                language = str(data.get("language") or cfg["asr_language"])
                want_txt = bool(data.get("txt", cfg["asr_txt"]))
                want_summary = bool(data.get("summary", cfg.get("summary")))
                if want_summary:
                    summarize.pick_engine(cfg)  # fail fast with a clear message before any work starts
                save_settings({"asr_engine": engine, "asr_language": language, "asr_txt": want_txt,
                               "summary": want_summary})
                created, errors = [], []
                for item in paths:
                    try:
                        created.append(start_transcription(str(item), engine, language, want_txt, want_summary).to_json())
                    except ValueError as exc:
                        errors.append(f"{Path(str(item)).name}: {exc}")
                if not created and errors:
                    raise ValueError("; ".join(errors))
                self._send_json({"jobs": created, "errors": errors})
            elif path == "/api/locate":
                items = data.get("items") or []
                found, missing = [], []
                for item in items if isinstance(items, list) else []:
                    hit = locate(item if isinstance(item, dict) else {})
                    (found.append(hit) if hit else missing.append(str((item or {}).get("name", "?"))))
                self._send_json({"paths": expand_media(found), "missing": missing})
            elif path == "/api/expand":
                # The macOS app passes real paths of files/folders dropped from Finder.
                paths = [Path(os.path.expanduser(str(p))) for p in (data.get("paths") or []) if str(p).strip()]
                self._send_json({"paths": expand_media(paths)})
            elif path == "/api/read-links":
                urls: list[str] = []
                for item in data.get("paths") or []:
                    src = Path(os.path.expanduser(str(item)))
                    if src.suffix.lower() in (".txt", ".csv", ".md") and src.is_file() and src.stat().st_size < 2_000_000:
                        urls += re.findall(r"https?://[^\s\"'<>]+", src.read_text("utf-8", errors="replace"))
                self._send_json({"urls": list(dict.fromkeys(u.rstrip("),.;") for u in urls))})
            elif path == "/api/choose":
                picked = choose_files(str(data.get("prompt") or "Обери файли"))
                self._send_json({"paths": expand_media([Path(p) for p in picked])})
            elif path == "/api/open-privacy":
                subprocess.Popen(["open", FULL_DISK_PANE])
                self._send_json({"ok": True})
            elif path == "/api/open":
                open_path(str(data.get("path", "")), bool(data.get("reveal")))
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def stop_all_jobs() -> None:
    for job in list(jobs.values()):
        if job.status in ("queued", "running"):
            job.interrupted = True
        job.cancelled = True
        if job.proc and job.proc.poll() is None:
            job.proc.terminate()
    save_jobs(jobs_snapshot())


def watch_parent(pid: int) -> None:
    # The macOS app passes its PID: if it dies without stopping us, do not linger in the background.
    while True:
        time.sleep(2)
        try:
            os.kill(pid, 0)
        except OSError:
            stop_all_jobs()
            os._exit(0)


def _interrupt(*_args) -> None:
    raise KeyboardInterrupt


def main() -> None:
    signal.signal(signal.SIGTERM, _interrupt)
    parent = os.environ.get("YTGRAB_PARENT_PID", "")
    if parent.isdigit():
        threading.Thread(target=watch_parent, args=(int(parent),), daemon=True).start()
    port = int(os.environ.get("YTGRAB_PORT", "8765"))
    server = None
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            break
        except OSError:
            continue
    if server is None:
        sys.exit("Не вдалося знайти вільний порт")
    load_jobs()
    threading.Thread(target=watch_jobs, daemon=True).start()
    if os.environ.get("YTGRAB_NO_AUTOUPDATE") != "1":
        threading.Thread(target=watch_ytdlp, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    missing = missing_components()
    print(f"YT Grab працює: {url}", flush=True)
    if missing and can_self_install():
        # First launch: fetch everything in the background, progress shows up in the Queue.
        print("Встановлюю компоненти: " + ", ".join(missing), flush=True)
        try:
            start_setup()
        except ValueError as exc:
            print(exc, flush=True)
    elif missing:
        print("Не знайдено: " + ", ".join(missing), flush=True)
    print("Щоб зупинити, натисни Ctrl+C або закрий це вікно.")
    if os.environ.get("YTGRAB_NO_BROWSER") != "1":
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_all_jobs()


if __name__ == "__main__":
    main()
