"""Per-platform knowledge: URL detection, output folders and human-readable yt-dlp errors."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# (key, display name, host suffixes). Order matters only for display.
PLATFORMS = (
    ("youtube", "YouTube", ("youtube.com", "youtu.be", "youtube-nocookie.com")),
    ("instagram", "Instagram", ("instagram.com",)),
    ("tiktok", "TikTok", ("tiktok.com",)),
    ("facebook", "Facebook", ("facebook.com", "fb.watch", "fb.com")),
    ("x", "X", ("x.com", "twitter.com")),
    ("threads", "Threads", ("threads.net", "threads.com")),
    ("vimeo", "Vimeo", ("vimeo.com",)),
    ("reddit", "Reddit", ("reddit.com", "redd.it")),
    ("pinterest", "Pinterest", ("pinterest.com", "pin.it")),
    ("linkedin", "LinkedIn", ("linkedin.com",)),
    ("twitch", "Twitch", ("twitch.tv",)),
    ("soundcloud", "SoundCloud", ("soundcloud.com",)),
)

# Keep folder and file names well below the 255-byte limit: TikTok/X titles are whole post texts.
FILE_PART = "%(upload_date>%Y-%m-%d,timestamp>%Y-%m-%d|undated)s - %(title,description,id).150B [%(id)s].%(height&{}p.|)s%(ext)s"
UPLOADER = "%(channel,uploader,uploader_id|Unknown).80B"


def detect(url: str) -> tuple[str, str] | None:
    host = (urlparse(url).hostname or "").lower()
    for key, name, suffixes in PLATFORMS:
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return key, name
    return None


# TikTok slideshows are photo posts that yt-dlp does not support at all.
PHOTO_POST = re.compile(r"tiktok\.com/@[^/]+/photo/", re.I)

# Share/tracking parameters: dropping them lets the same post pasted twice be recognised as one.
_TRACKING = re.compile(r"^(utm_\w+|igsh|igshid|si|fbclid|gclid|feature|is_from_webapp|sender_device|"
                       r"share_\w+|_r|_t|ref|ref_src|ref_url)$", re.I)

# First path segments on instagram.com that are not usernames.
_IG_RESERVED = {"p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct", "about",
                "developer", "legal", "web", "challenge", "s", "share"}


def clean_url(url: str) -> str:
    parts = urlparse(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)]
    return urlunparse(parts._replace(query=urlencode(query), fragment=""))


def instagram_profile(url: str) -> tuple[str, str] | None:
    """Return (username, tab) for an Instagram profile link; tab is "reels" (default) or "posts"."""
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if host != "instagram.com" and not host.endswith(".instagram.com"):
        return None
    match = re.match(r"^/([A-Za-z0-9._]{1,30})/?(?:(reels|posts)/?)?$", parts.path)
    if not match or match.group(1).lower() in _IG_RESERVED:
        return None
    return match.group(1), match.group(2) or "reels"


def output_template(url: str) -> str:
    platform = detect(url)
    # YouTube keeps the original <channel>/... layout so existing archives and folders stay valid.
    if platform and platform[0] == "youtube":
        return f"{UPLOADER}/{FILE_PART}"
    folder = platform[1] if platform else "%(extractor_key|Other)s"
    return f"{folder}/{UPLOADER}/{FILE_PART}"


def platform_list() -> list[dict]:
    return [{"key": k, "name": n, "hosts": list(h)} for k, n, h in PLATFORMS]


_LOGIN = re.compile(
    r"login required|logged-in|log in|sign in to confirm|use --cookies|requires authentication|"
    r"only works when logged|account credentials|this content isn't available to everyone|rate-limit reached",
    re.I,
)


def friendly_error(raw: str, url: str, cookies_browser: str = "") -> str:
    """Translate a yt-dlp error line into a short Ukrainian hint; keeps the original for reference."""
    text = (raw or "").strip()
    platform = detect(url)
    name = platform[1] if platform else "Сайт"
    hint = ""
    if _LOGIN.search(text):
        if cookies_browser:
            hint = (f"{name} не пускає навіть з cookies з {cookies_browser}. Перевір, що в цьому браузері "
                    f"ти залогінений на {name}, і спробуй ще раз.")
        else:
            hint = (f"{name} вимагає вхід. Відкрий Налаштування → «Cookies з браузера» "
                    f"і обери браузер, у якому ти залогінений на {name}.")
    elif re.search(r"ip address is blocked|not available in your country|geo.?restrict", text, re.I):
        hint = f"{name} блокує цей пост для твого IP чи країни. Спробуй через VPN."
    elif re.search(r"impersonat|unexpected response from webpage|cloudflare|captcha", text, re.I):
        hint = (f"{name} не пускає звичайні запити. Відкрий Налаштування → «Інструменти» "
                "і натисни «Встановити yt-dlp з curl_cffi».")
    elif re.search(r"no video could be found|there'?s no video|no video formats found|no video in this", text, re.I):
        hint = "У цьому пості немає відео: лише фото або текст."
    elif re.search(r"unsupported url", text, re.I):
        hint = "Цей сайт або тип посилання yt-dlp не підтримує."
    elif re.search(r"private|members.only|subscriber", text, re.I):
        hint = (f"Пост приватний або лише для підписників. Якщо доступ є, увімкни cookies з браузера, "
                f"де ти залогінений на {name}.")
    elif re.search(r"http error 403|forbidden", text, re.I):
        hint = ("Сервер відмовив (403). Онови yt-dlp у Налаштуваннях, а якщо не допоможе, "
                "увімкни cookies з браузера.")
    elif re.search(r"http error 429|too many requests", text, re.I):
        hint = f"{name} тимчасово обмежив запити. Зачекай кілька хвилин або увімкни cookies."
    elif re.search(r"video unavailable|has been removed|does not exist|not found", text, re.I):
        hint = "Відео видалене або недоступне."
    if not hint:
        return text
    original = re.sub(r"^ERROR:\s*", "", text)
    return f"{hint} ({original[:200]})"
