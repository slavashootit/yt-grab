"""Video -> MP4 conversion on top of ffmpeg.

Presets:
  h264  - H.264 + AAC, plays everywhere and opens in Premiere. Capped at 4K; HDR is tone-mapped to SDR.
  hevc  - H.265 + AAC tagged hvc1, keeps 4K/8K and HDR (10-bit PQ/HLG).
  remux - container change only (H.264/HEVC sources), takes seconds.

On macOS the Apple VideoToolbox hardware encoders are used when ffmpeg has them,
with an automatic retry on x264/x265 if the hardware encoder rejects the job.
"""
from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
from pathlib import Path

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
REMUX_VIDEO = {"h264", "hevc"}
MP4_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac"}
H264_MAX_SHORT_SIDE = 2160
PRESETS = ("h264", "hevc", "remux")
# ffmpeg-full is keg-only in Homebrew; prefer it when present because it ships zscale for tone mapping.
FFMPEG_CANDIDATES = (
    "/opt/homebrew/opt/ffmpeg-full/bin",
    "/usr/local/opt/ffmpeg-full/bin",
)


@functools.lru_cache(maxsize=1)
def ffmpeg_bin() -> tuple[str, str] | None:
    for folder in FFMPEG_CANDIDATES:
        ffmpeg, ffprobe = os.path.join(folder, "ffmpeg"), os.path.join(folder, "ffprobe")
        if os.access(ffmpeg, os.X_OK) and os.access(ffprobe, os.X_OK):
            return ffmpeg, ffprobe
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    return (ffmpeg, ffprobe) if ffmpeg and ffprobe else None


def _list_names(flag: str) -> set[str]:
    tools = ffmpeg_bin()
    if not tools:
        return set()
    out = subprocess.run([tools[0], "-hide_banner", flag], capture_output=True, text=True).stdout
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith(" ="):
            names.add(parts[1])
    return names


@functools.lru_cache(maxsize=1)
def capabilities() -> dict:
    encoders, filters = _list_names("-encoders"), _list_names("-filters")
    tools = ffmpeg_bin()
    return {
        "ffmpeg": tools[0] if tools else None,
        "h264_hw": "h264_videotoolbox" in encoders,
        "hevc_hw": "hevc_videotoolbox" in encoders,
        "x264": "libx264" in encoders,
        "x265": "libx265" in encoders,
        "tonemap": "zscale" in filters and "tonemap" in filters,
    }


def probe(path: str) -> dict:
    tools = ffmpeg_bin()
    if not tools:
        raise ValueError("ffmpeg не знайдено: brew install ffmpeg")
    proc = subprocess.run(
        [tools[1], "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise ValueError("Не вдалося прочитати файл: " + (proc.stderr.strip().splitlines() or ["?"])[-1])
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ValueError("У файлі немає відео")
    duration = float((data.get("format") or {}).get("duration") or video.get("duration") or 0)
    return {
        "path": path,
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "vcodec": video.get("codec_name"),
        "pix_fmt": video.get("pix_fmt"),
        "transfer": video.get("color_transfer"),
        "hdr": video.get("color_transfer") in HDR_TRANSFERS,
        "fps": _fps(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        "acodec": audio.get("codec_name") if audio else None,
        "channels": int(audio.get("channels") or 0) if audio else 0,
    }


def _fps(rate: str | None) -> float | None:
    try:
        num, den = (rate or "0/0").split("/")
        return round(int(num) / int(den), 2) if int(den) else None
    except ValueError:
        return None


def _target_short_side(info: dict, preset: str, res: str) -> int:
    short = min(info["width"], info["height"]) or info["height"]
    target = short if res == "source" else min(int(res), short)
    if preset == "h264":
        target = min(target, H264_MAX_SHORT_SIDE)
    return target


def _scale_filter(info: dict, target: int) -> list[str]:
    short = min(info["width"], info["height"])
    if not short or target >= short:
        return []
    size = f"-2:{target}" if info["width"] >= info["height"] else f"{target}:-2"
    return [f"scale={size}:flags=lanczos"]


def _bitrate(short_side: int, hevc: bool) -> int:
    for limit, mbps in ((720, 10), (1080, 20), (1440, 35), (2160, 60)):
        if short_side <= limit:
            break
    else:
        mbps = 120
    return int(mbps * (0.7 if hevc else 1.0) * 1_000_000)


def _audio_args(info: dict) -> list[str]:
    if not info["acodec"]:
        return []
    if info["acodec"] in MP4_AUDIO:
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "512k" if info["channels"] > 2 else "320k"]


def output_path(src: str, label: str) -> Path:
    # Build names as strings: labels like "H.264" contain dots that Path.with_suffix would eat.
    source = Path(src)
    base = f"{source.stem} [{label}]"
    candidate, n = source.with_name(base + ".mp4"), 2
    while candidate.exists():
        candidate, n = source.with_name(f"{base} ({n}).mp4"), n + 1
    return candidate


def label_for(info: dict, preset: str, res: str) -> str:
    if preset == "remux":
        return "MP4"
    target = _target_short_side(info, preset, res)
    name = "H.264" if preset == "h264" else ("HEVC HDR" if info["hdr"] else "HEVC")
    return f"{name} {target}p"


def build_command(info: dict, preset: str, res: str, dst: str, hardware: bool) -> tuple[list[str], list[str]]:
    """Return (ffmpeg argv, user-facing notes)."""
    if preset not in PRESETS:
        raise ValueError("Невідомий пресет")
    caps = capabilities()
    tools = ffmpeg_bin()
    if not tools:
        raise ValueError("ffmpeg не знайдено: brew install ffmpeg")
    notes: list[str] = []
    cmd = [tools[0], "-hide_banner", "-nostdin", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
           "-i", info["path"], "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0", "-map_chapters", "0"]

    if preset == "remux":
        if info["vcodec"] not in REMUX_VIDEO:
            raise ValueError(f"Перепакувати можна лише H.264 або HEVC, а тут {info['vcodec']}. Обери H.264 або HEVC.")
        cmd += ["-c:v", "copy"]
        if info["vcodec"] == "hevc":
            cmd += ["-tag:v", "hvc1"]
        cmd += _audio_args(info)
        return cmd + ["-movflags", "+faststart", dst], notes

    target = _target_short_side(info, preset, res)
    short = min(info["width"], info["height"])
    filters = _scale_filter(info, target)
    rate = _bitrate(target, hevc=preset == "hevc")

    if preset == "h264":
        if short > H264_MAX_SHORT_SIDE and res == "source":
            notes.append("H.264 обмежено 4K. Для 8K обери HEVC.")
        if info["hdr"]:
            if caps["tonemap"]:
                filters += ["zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709",
                            "tonemap=tonemap=hable:desat=0", "zscale=t=bt709:m=bt709:r=tv"]
            else:
                notes.append("HDR перетворено в SDR без тонмапінгу, кольори можуть бути бляклі. "
                             "Для HDR обери HEVC або встанови ffmpeg-full.")
        filters.append("format=yuv420p")
        cmd += ["-vf", ",".join(filters)]
        if hardware and caps["h264_hw"]:
            cmd += ["-c:v", "h264_videotoolbox", "-profile:v", "high", "-b:v", str(rate)]
        elif caps["x264"]:
            cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high"]
        else:
            raise ValueError("У ffmpeg немає кодувальника H.264")
        if info["hdr"]:
            cmd += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    else:  # hevc
        ten_bit = info["hdr"]
        if hardware and caps["hevc_hw"]:
            filters.append("format=p010le" if ten_bit else "format=yuv420p")
            cmd += ["-vf", ",".join(filters), "-c:v", "hevc_videotoolbox",
                    "-profile:v", "main10" if ten_bit else "main", "-b:v", str(rate)]
        elif caps["x265"]:
            filters.append("format=yuv420p10le" if ten_bit else "format=yuv420p")
            cmd += ["-vf", ",".join(filters), "-c:v", "libx265", "-preset", "medium", "-crf", "18" if ten_bit else "20"]
            if ten_bit:
                params = f"colorprim=bt2020:transfer={info['transfer']}:colormatrix=bt2020nc:repeat-headers=1"
                if info["transfer"] == "smpte2084":
                    params += ":hdr10=1"
                cmd += ["-x265-params", params]
        else:
            raise ValueError("У ffmpeg немає кодувальника HEVC")
        if ten_bit:
            cmd += ["-color_primaries", "bt2020", "-color_trc", info["transfer"], "-colorspace", "bt2020nc"]
        cmd += ["-tag:v", "hvc1"]

    cmd += _audio_args(info)
    return cmd + ["-movflags", "+faststart", dst], notes


def is_hardware(cmd: list[str]) -> bool:
    return any(arg.endswith("_videotoolbox") for arg in cmd)
