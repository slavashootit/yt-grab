"""Threads posts for yt-dlp, which has no Threads extractor of its own.

Logged-out browsers get a login wall, but search crawlers are served the full post page with the
media JSON embedded (video_versions, DASH manifest, carousel). Photo-only posts raise the usual
"no video" error, so YT Grab treats them like any other photo post.
"""
import json
import re

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError, int_or_none, parse_duration, traverse_obj

CRAWLER_UA = "Googlebot/2.1 (+http://www.google.com/bot.html)"


class ThreadsPostIE(InfoExtractor):
    IE_NAME = "threads"
    _VALID_URL = r"https?://(?:www\.)?threads\.(?:net|com)/(?:@(?P<user>[\w.]+)/)?(?:post|t)/(?P<id>[\w-]+)"

    def _find_post(self, html, code):
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

    def _formats(self, media, video_id):
        formats = []
        manifest = media.get("video_dash_manifest")
        if manifest:
            formats.extend(self._parse_mpd_formats(self._parse_xml(manifest, video_id), mpd_id="dash"))
        for i, v in enumerate(media.get("video_versions") or []):
            if not v.get("url"):
                continue
            formats.append({
                "url": v["url"], "format_id": f"{v.get('type') or i}", "ext": "mp4",
                "width": int_or_none(v.get("width")), "height": int_or_none(v.get("height")),
                # video_versions are progressive MP4s with sound; listed best-first.
                "quality": -i,
            })
        if formats and not any(f.get("width") for f in formats if not f.get("format_id", "").startswith("dash")):
            # Progressive files carry no size: assume the first is the original resolution.
            formats[-len(media["video_versions"])].update(
                width=int_or_none(media.get("original_width")), height=int_or_none(media.get("original_height")))
        return formats

    def _real_extract(self, url):
        code, user = self._match_valid_url(url).group("id", "user")
        page = f"https://www.threads.com/@{user}/post/{code}" if user else f"https://www.threads.com/t/{code}"
        html = self._download_webpage(page, code, headers={"User-Agent": CRAWLER_UA})
        post = self._find_post(html, code)
        if not post:
            raise ExtractorError("Unable to find the post: it may be private or deleted", expected=True)

        text = traverse_obj(post, ("caption", "text")) or "".join(
            traverse_obj(post, ("text_post_app_info", "text_fragments", "fragments", ..., "plaintext")) or [])
        text = (text or "").strip()
        common = {
            "uploader": traverse_obj(post, ("user", "username")) or user,
            "uploader_id": traverse_obj(post, ("user", "username")) or user,
            "channel": traverse_obj(post, ("user", "username")) or user,
            "timestamp": int_or_none(post.get("taken_at")),
            "description": text or None,
            "like_count": int_or_none(post.get("like_count")),
            "webpage_url": page,
        }
        title = text.split("\n")[0][:100] or f"Threads post by {common['uploader']}"

        items = post.get("carousel_media") or [post]
        entries = []
        for n, media in enumerate(items, 1):
            if not media.get("video_versions") and not media.get("video_dash_manifest"):
                continue
            media_id = str(media.get("pk") or media.get("id") or code).split("_")[0]
            entries.append({
                **common,
                "id": code if len(items) == 1 else f"{code}_{n}",
                "display_id": media_id,
                "title": title if len(items) == 1 else f"{title} ({n})",
                "formats": self._formats(media, code),
                "thumbnail": traverse_obj(media, ("image_versions2", "candidates", 0, "url")),
                "duration": media.get("video_duration") or parse_duration(self._search_regex(
                    r'mediaPresentationDuration="([^"]+)"', media.get("video_dash_manifest") or "", "duration", default=None)),
            })
        if not entries:
            raise ExtractorError("There's no video in this post", expected=True)
        if len(entries) == 1:
            return entries[0]
        return self.playlist_result(entries, code, title, **common, multi_video=True)
