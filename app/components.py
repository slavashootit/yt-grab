"""Download self-contained command-line tools into ~/.ytgrab/bin (macOS).

Usage: python components.py ffmpeg|deno
Prints "PROGRESS <0-100>" lines. Lets YT Grab run on a Mac without Homebrew.
"""
from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

BIN_DIR = Path.home() / ".ytgrab" / "bin"


def _arch() -> str:
    return "arm64" if platform.machine() in ("arm64", "aarch64") else "x86_64"


def sources(name: str) -> list[tuple[str, str]]:
    """(url, binary inside the zip) pairs for a component."""
    if name == "ffmpeg":
        arch = "arm64" if _arch() == "arm64" else "amd64"
        base = f"https://ffmpeg.martin-riedl.de/redirect/latest/macos/{arch}/release"
        return [(f"{base}/ffmpeg.zip", "ffmpeg"), (f"{base}/ffprobe.zip", "ffprobe")]
    if name == "deno":
        arch = "aarch64" if _arch() == "arm64" else "x86_64"
        return [(f"https://github.com/denoland/deno/releases/latest/download/deno-{arch}-apple-darwin.zip", "deno")]
    raise SystemExit(f"unknown component: {name}")


def download(url: str, dest: Path, start: float, span: float) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "YTGrab/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total:
                print(f"PROGRESS {start + span * done / total:.1f}", flush=True)


def ensure_runnable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if sys.platform != "darwin":
        return
    # Apple Silicon refuses unsigned binaries; keep the vendor signature, else sign ad hoc.
    if subprocess.run(["codesign", "-v", str(path)], capture_output=True).returncode != 0:
        subprocess.run(["codesign", "--force", "--sign", "-", str(path)], capture_output=True)


def install(name: str) -> None:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    items = sources(name)
    with tempfile.TemporaryDirectory(prefix="ytgrab-dl-") as tmp:
        for n, (url, binary) in enumerate(items):
            archive = Path(tmp) / f"{binary}.zip"
            download(url, archive, n * 100 / len(items), 100 / len(items))
            with zipfile.ZipFile(archive) as zf:
                member = next((m for m in zf.namelist() if os.path.basename(m) == binary), None)
                if member is None:
                    raise SystemExit(f"{binary} not found in {url}")
                target = BIN_DIR / binary
                partial = BIN_DIR / f".{binary}.part"
                with zf.open(member) as src, open(partial, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                partial.replace(target)
            ensure_runnable(target)
    print("PROGRESS 100", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    install(sys.argv[1])
