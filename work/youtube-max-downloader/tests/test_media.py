from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from ytmax.media import probe_media


class MediaTests(unittest.TestCase):
    def test_probe_parses_h264_aac_and_resolution(self) -> None:
        stderr = """
Input #0, mov,mp4,m4a, from 'video.mp4':
  Stream #0:0: Video: h264 (High) (avc1), yuv420p, 3840x2160, 30 fps
  Stream #0:1: Audio: aac (LC) (mp4a), 48000 Hz, stereo
"""
        completed = subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)
        with patch("ytmax.media.subprocess.run", return_value=completed):
            media = probe_media(Path("video.mp4"), "ffmpeg")

        self.assertEqual(media.video_codec, "h264")
        self.assertEqual(media.audio_codec, "aac")
        self.assertEqual((media.width, media.height), (3840, 2160))
        self.assertTrue(media.is_h264_mp4_compatible)

    def test_probe_reports_incompatible_codecs(self) -> None:
        stderr = """
  Stream #0:0: Video: av1 (Main), yuv420p10le, 1920x1080
  Stream #0:1: Audio: opus, 48000 Hz, stereo
"""
        completed = subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)
        with patch("ytmax.media.subprocess.run", return_value=completed):
            media = probe_media(Path("video.mp4"), "ffmpeg")

        self.assertEqual((media.video_codec, media.audio_codec), ("av1", "opus"))
        self.assertFalse(media.is_h264_mp4_compatible)


if __name__ == "__main__":
    unittest.main()
