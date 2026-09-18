from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class MediaToolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MediaProbe:
    video_codec: str | None
    audio_codec: str | None
    width: int | None = None
    height: int | None = None

    @property
    def is_h264_mp4_compatible(self) -> bool:
        return self.video_codec == "h264" and self.audio_codec in {"aac", None}


def ffmpeg_executable() -> str:
    configured = os.environ.get("MMM_FFMPEG") or os.environ.get("YTMAX_FFMPEG")
    if configured and Path(configured).is_file():
        return configured

    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and Path(bundled).is_file():
            return bundled
    except (ImportError, RuntimeError, OSError):
        pass

    system = shutil.which("ffmpeg")
    if system:
        return system
    raise MediaToolError("FFmpeg не найден. Переустановите приложение или задайте MMM_FFMPEG.")


def hidden_process_kwargs() -> dict[str, object]:
    if os.name != "nt":
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]


def probe_media(path: Path, ffmpeg: str | None = None) -> MediaProbe:
    executable = ffmpeg or ffmpeg_executable()
    process = subprocess.run(
        [executable, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        **hidden_process_kwargs(),
    )
    output = f"{process.stdout}\n{process.stderr}"
    video = re.search(
        r"Stream #.*?Video:\s*([a-zA-Z0-9_]+).*?(\d{2,5})x(\d{2,5})",
        output,
    )
    audio = re.search(r"Stream #.*?Audio:\s*([a-zA-Z0-9_]+)", output)
    return MediaProbe(
        video_codec=video.group(1).lower() if video else None,
        audio_codec=audio.group(1).lower() if audio else None,
        width=int(video.group(2)) if video else None,
        height=int(video.group(3)) if video else None,
    )
