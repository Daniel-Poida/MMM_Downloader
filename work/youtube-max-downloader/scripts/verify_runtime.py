from __future__ import annotations

import shutil
import subprocess

import imageio_ffmpeg
import yt_dlp
import yt_dlp_ejs  # noqa: F401
from PySide6.QtCore import qVersion
from PySide6.QtWidgets import QApplication  # noqa: F401


def main() -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    encoders = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout
    missing = [codec for codec in ("libx264", "aac") if codec not in encoders]
    if missing:
        raise RuntimeError(f"FFmpeg is missing required encoders: {', '.join(missing)}")

    deno = shutil.which("deno")
    if not deno:
        raise RuntimeError("Deno was not found on PATH")
    deno_version = subprocess.run(
        [deno, "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.splitlines()[0]

    print(f"Python/Qt {qVersion()}: OK; yt-dlp: {yt_dlp.version.__version__}")
    print(f"FFmpeg H.264/AAC: OK; {deno_version}")


if __name__ == "__main__":
    main()
