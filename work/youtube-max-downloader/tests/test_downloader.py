from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ytmax.downloader import (
    HDR_TO_SDR_FILTER,
    NATIVE_H264_FORMAT,
    TRUE_MAX_FORMAT,
    DownloadEngineError,
    JobCancelled,
    YouTubeEngine,
    _candidate_from_entry,
    _selected_video_is_hdr,
    _source_path_from_info,
)
from ytmax.media import MediaProbe
from ytmax.models import DownloadIntent


class _FakeYDL:
    search_entries: list[dict[str, object]] = []
    details: dict[str, dict[str, object]] = {}

    def __init__(self, options: dict[str, object]) -> None:
        self.options = options

    def __enter__(self) -> _FakeYDL:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, url: str, download: bool) -> dict[str, object]:
        if url.startswith("ytsearch"):
            return {"entries": self.search_entries}
        video_id = url.rsplit("=", 1)[-1]
        return self.details[video_id]


class _InspectYDL:
    responses: dict[str, object] = {}
    calls: list[tuple[str, bool]] = []
    created_options: list[dict[str, object]] = []

    def __init__(self, options: dict[str, object]) -> None:
        self.options = options
        type(self).created_options.append(options)

    def __enter__(self) -> _InspectYDL:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, url: str, download: bool) -> object:
        type(self).calls.append((url, download))
        response = type(self).responses[url]
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response()
        return response


class DownloaderTests(unittest.TestCase):
    def _engine(self, directory: str, events: list[dict[str, object]]) -> YouTubeEngine:
        with patch("ytmax.downloader.ffmpeg_executable", return_value="fake-ffmpeg"):
            return YouTubeEngine(output_dir=directory, emit=events.append)

    def test_native_selector_is_strict_h264_aac(self) -> None:
        self.assertIn("vcodec^=avc1", NATIVE_H264_FORMAT)
        self.assertIn("acodec^=mp4a", NATIVE_H264_FORMAT)
        self.assertNotIn("/best", NATIVE_H264_FORMAT)

    def test_candidate_uses_best_available_thumbnail_and_canonical_fallback(self) -> None:
        candidate = _candidate_from_entry(
            {
                "id": "abc",
                "title": "Preview",
                "thumbnails": [
                    {
                        "url": "https://i.ytimg.com/vi/abc/small.jpg",
                        "width": 120,
                        "height": 90,
                    },
                    {
                        "url": "https://i.ytimg.com/vi/abc/large.jpg",
                        "width": 480,
                        "height": 360,
                    },
                ],
            },
            0,
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(
            candidate.thumbnail_url,
            "https://i.ytimg.com/vi/abc/large.jpg",
        )
        self.assertEqual(candidate.watch_url, "https://www.youtube.com/watch?v=abc")

        fallback = _candidate_from_entry(
            {"id": "fallback", "title": "Fallback", "thumbnail": "not-a-url"},
            1,
        )
        self.assertIsNotNone(fallback)
        self.assertEqual(fallback.thumbnail_url, "")
        self.assertEqual(
            fallback.preview_image_url,
            "https://i.ytimg.com/vi/fallback/mqdefault.jpg",
        )

    def test_true_max_selector_does_not_cap_to_sdr(self) -> None:
        self.assertEqual(TRUE_MAX_FORMAT, "bv+ba/b")
        self.assertNotIn("dynamic_range", TRUE_MAX_FORMAT)
        self.assertTrue(
            _selected_video_is_hdr(
                {"requested_formats": [{"dynamic_range": "HDR10"}, {"acodec": "opus"}]}
            )
        )
        self.assertFalse(_selected_video_is_hdr({"dynamic_range": "SDR"}))

    def test_search_filters_duration_and_resolves_missing_metadata(self) -> None:
        _FakeYDL.search_entries = [
            {
                "id": "short",
                "title": "Short",
                "duration": "599",
                "view_count": 1_000_000,
            },
            {
                "id": "unknown",
                "title": "Unknown duration",
                "duration": None,
                "view_count": 5_000,
                "thumbnail": "https://i.ytimg.com/vi/unknown/search.jpg",
            },
            {
                "id": "long",
                "title": "Long",
                "duration": 900,
                "view_count": 20_000,
            },
            {"id": "live", "title": "Live", "duration": 1000, "live_status": "is_live"},
            {"id": "broken", "title": "Broken", "duration": "NA", "view_count": "NA"},
        ]
        _FakeYDL.details = {
            "unknown": {
                "id": "unknown",
                "title": "Unknown duration",
                "duration": 700,
                "view_count": 5_000,
            },
            "broken": {
                "id": "broken",
                "title": "Broken",
                "duration": 601,
                "view_count": None,
            },
        }
        fake_module = SimpleNamespace(YoutubeDL=_FakeYDL)
        events: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as directory:
            engine = self._engine(directory, events)
            intent = DownloadIntent(
                topic="Art Deco",
                count=10,
                min_duration_seconds=600,
                ranking="relevance",
            )
            with patch.object(YouTubeEngine, "_yt_dlp", return_value=fake_module):
                results = engine.search(intent)

        self.assertEqual([item.video_id for item in results], ["unknown", "long", "broken"])
        self.assertEqual(
            results[0].thumbnail_url,
            "https://i.ytimg.com/vi/unknown/search.jpg",
        )
        self.assertTrue(any(event.get("kind") == "candidates" for event in events))

    def test_inspect_urls_preserves_order_and_returns_full_metadata(self) -> None:
        first = "https://youtu.be/request-first"
        second = "https://www.youtube.com/watch?v=request-second"
        _InspectYDL.responses = {
            first: {
                "id": "resolved-first",
                "title": " First video ",
                "webpage_url": "https://www.youtube.com/watch?v=resolved-first",
                "channel": "Channel one",
                "duration": "125",
                "view_count": "1000",
                "like_count": 25,
                "timestamp": 1_700_000_000,
                "thumbnails": [
                    {"url": "https://img.test/small.jpg", "width": 120, "height": 90},
                    {"url": "https://img.test/large.jpg", "width": 1280, "height": 720},
                ],
            },
            second: {
                "id": "resolved-second",
                "title": "Second video",
                "uploader": "Channel two",
                "duration": 300,
                "thumbnail": "https://img.test/second.jpg",
            },
        }
        _InspectYDL.calls = []
        _InspectYDL.created_options = []
        events: list[dict[str, object]] = []

        with tempfile.TemporaryDirectory() as directory:
            engine = self._engine(directory, events)
            with patch.object(
                YouTubeEngine,
                "_yt_dlp",
                return_value=SimpleNamespace(YoutubeDL=_InspectYDL),
            ):
                results = engine.inspect_urls([first, second])

        self.assertEqual([item.video_id for item in results], ["resolved-first", "resolved-second"])
        self.assertEqual([item.query_rank for item in results], [0, 1])
        self.assertEqual(results[0].title, "First video")
        self.assertEqual(results[0].duration_seconds, 125)
        self.assertEqual(results[0].view_count, 1000)
        self.assertEqual(results[0].like_count, 25)
        self.assertEqual(results[0].channel, "Channel one")
        self.assertEqual(results[0].thumbnail_url, "https://img.test/large.jpg")
        self.assertEqual(
            results[1].watch_url,
            "https://www.youtube.com/watch?v=resolved-second",
        )
        self.assertEqual(_InspectYDL.calls, [(first, False), (second, False)])
        self.assertTrue(_InspectYDL.created_options[0]["skip_download"])
        final = [event for event in events if event.get("kind") == "candidates"]
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0]["items"], results)
        self.assertEqual(final[0]["failed"], 0)
        self.assertEqual(final[0]["skipped"], 0)

    def test_url_preview_needs_neither_output_folder_nor_ffmpeg(self) -> None:
        url = "https://youtu.be/preview"
        _InspectYDL.responses = {
            url: {"id": "preview", "title": "Preview", "duration": 42}
        }
        _InspectYDL.calls = []
        _InspectYDL.created_options = []
        with (
            patch("ytmax.downloader.ffmpeg_executable", side_effect=AssertionError),
            patch.object(
                YouTubeEngine,
                "_yt_dlp",
                return_value=SimpleNamespace(YoutubeDL=_InspectYDL),
            ),
        ):
            engine = YouTubeEngine(output_dir="")
            results = engine.inspect_urls([url])

        self.assertEqual([candidate.video_id for candidate in results], ["preview"])
        self.assertNotIn("ffmpeg_location", _InspectYDL.created_options[0])

    def test_inspect_urls_continues_after_errors_and_warns_for_skipped_items(self) -> None:
        bad = "https://youtu.be/bad-request"
        good = "https://youtu.be/good-request"
        collection = "https://www.youtube.com/playlist?list=PL-test"
        live = "https://youtu.be/live-request"
        duplicate = "https://youtu.be/duplicate-request"
        _InspectYDL.responses = {
            bad: RuntimeError("private video"),
            good: {"id": "same-video", "title": "Available"},
            collection: {
                "_type": "playlist",
                "id": "PL-test",
                "title": "A playlist",
                "entries": [],
            },
            live: {"id": "live-video", "title": "Live", "live_status": "is_live"},
            duplicate: {"id": "same-video", "title": "Duplicate alias"},
        }
        _InspectYDL.calls = []
        _InspectYDL.created_options = []
        events: list[dict[str, object]] = []

        with tempfile.TemporaryDirectory() as directory:
            engine = self._engine(directory, events)
            with patch.object(
                YouTubeEngine,
                "_yt_dlp",
                return_value=SimpleNamespace(YoutubeDL=_InspectYDL),
            ):
                results = engine.inspect_urls([bad, good, collection, live, duplicate])

        self.assertEqual([item.video_id for item in results], ["same-video"])
        logs = [event for event in events if event.get("kind") == "log"]
        self.assertEqual(
            [(event["reason"], event["level"]) for event in logs],
            [
                ("extract", "error"),
                ("collection", "warning"),
                ("live", "warning"),
                ("duplicate", "warning"),
            ],
        )
        final = next(event for event in events if event.get("kind") == "candidates")
        self.assertEqual(final["failed"], 1)
        self.assertEqual(final["skipped"], 3)

    def test_inspect_urls_all_fail_returns_empty_candidates_event(self) -> None:
        url = "https://youtu.be/unavailable"
        _InspectYDL.responses = {url: RuntimeError("unavailable")}
        _InspectYDL.calls = []
        _InspectYDL.created_options = []
        events: list[dict[str, object]] = []

        with tempfile.TemporaryDirectory() as directory:
            engine = self._engine(directory, events)
            with patch.object(
                YouTubeEngine,
                "_yt_dlp",
                return_value=SimpleNamespace(YoutubeDL=_InspectYDL),
            ):
                results = engine.inspect_urls([url])

        self.assertEqual(results, [])
        final = next(event for event in events if event.get("kind") == "candidates")
        self.assertEqual(final["items"], [])
        self.assertEqual(final["failed"], 1)

    def test_inspect_urls_honours_cancellation_before_and_after_extraction(self) -> None:
        url = "https://youtu.be/cancelled"
        events: list[dict[str, object]] = []
        cancel_event = threading.Event()

        with tempfile.TemporaryDirectory() as directory:
            with patch("ytmax.downloader.ffmpeg_executable", return_value="fake-ffmpeg"):
                engine = YouTubeEngine(
                    output_dir=directory,
                    emit=events.append,
                    cancel_event=cancel_event,
                )
            cancel_event.set()
            with self.assertRaisesRegex(JobCancelled, "Операция отменена"):
                engine.inspect_urls([url])

        self.assertFalse(any(event.get("kind") == "candidates" for event in events))

        events = []
        cancel_event.clear()

        def cancel_during_extraction() -> dict[str, str]:
            cancel_event.set()
            return {"id": "cancelled", "title": "Must not be returned"}

        _InspectYDL.responses = {url: cancel_during_extraction}
        _InspectYDL.calls = []
        _InspectYDL.created_options = []
        with tempfile.TemporaryDirectory() as directory:
            with patch("ytmax.downloader.ffmpeg_executable", return_value="fake-ffmpeg"):
                engine = YouTubeEngine(
                    output_dir=directory,
                    emit=events.append,
                    cancel_event=cancel_event,
                )
            with (
                patch.object(
                    YouTubeEngine,
                    "_yt_dlp",
                    return_value=SimpleNamespace(YoutubeDL=_InspectYDL),
                ),
                self.assertRaisesRegex(JobCancelled, "Операция отменена"),
            ):
                engine.inspect_urls([url])

        self.assertFalse(any(event.get("kind") == "candidates" for event in events))

    def test_batch_continues_after_one_video_fails(self) -> None:
        events: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as directory:
            engine = self._engine(directory, events)

            def download(url: str) -> Path:
                if url == "bad":
                    raise RuntimeError("network error")
                return Path(directory) / f"{url}.mp4"

            first = "https://youtu.be/first"
            bad = "https://youtu.be/bad"
            last = "https://youtu.be/last"

            def download_url(url: str) -> Path:
                return download(url.rsplit("/", 1)[-1])

            with patch.object(engine, "_download_native_h264", side_effect=download_url):
                paths = engine.download_urls([first, bad, last])

        self.assertEqual([path.name for path in paths], ["first.mp4", "last.mp4"])
        self.assertEqual(sum(event.get("kind") == "item_error" for event in events), 1)
        batch = next(event for event in events if event.get("kind") == "batch_done")
        self.assertEqual(batch["failed"], 1)

    def test_native_download_rejects_non_aac_audio(self) -> None:
        class FakeDownloadYDL:
            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> FakeDownloadYDL:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(self, _url: str, download: bool) -> dict[str, str]:
                self.test_case.assertTrue(download)
                (self.output / "Video [id].mp4").touch()
                return {"id": "id"}

        with tempfile.TemporaryDirectory() as directory:
            FakeDownloadYDL.output = Path(directory)  # type: ignore[attr-defined]
            FakeDownloadYDL.test_case = self  # type: ignore[attr-defined]
            engine = self._engine(directory, [])
            fake_module = SimpleNamespace(YoutubeDL=FakeDownloadYDL)
            with (
                patch.object(YouTubeEngine, "_yt_dlp", return_value=fake_module),
                patch(
                    "ytmax.downloader.probe_media",
                    return_value=MediaProbe(video_codec="h264", audio_codec="opus"),
                ),
                self.assertRaises(DownloadEngineError),
            ):
                engine._download_native_h264("https://youtu.be/id")

    def test_true_max_pipeline_transcodes_verifies_and_moves(self) -> None:
        class FakeDownloadYDL:
            def __init__(self, options: dict[str, object]) -> None:
                self.options = options

            def __enter__(self) -> FakeDownloadYDL:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def extract_info(self, _url: str, download: bool) -> dict[str, object]:
                self.test_case.assertTrue(download)
                self.test_case.assertEqual(self.options["format"], TRUE_MAX_FORMAT)
                parent = Path(str(self.options["outtmpl"])).parent
                (parent / "source.webm").write_bytes(b"source")
                return {
                    "id": "vid",
                    "title": "A: Test?",
                    "duration": 10,
                    "requested_formats": [{"dynamic_range": "HDR10"}],
                }

        events: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as directory:
            FakeDownloadYDL.test_case = self  # type: ignore[attr-defined]
            engine = self._engine(directory, events)
            fake_module = SimpleNamespace(YoutubeDL=FakeDownloadYDL)

            def transcode(
                _source: Path,
                destination: Path,
                _duration: float,
                *,
                hdr_to_sdr: bool,
            ) -> None:
                self.assertTrue(hdr_to_sdr)
                destination.write_bytes(b"converted")

            with (
                patch.object(YouTubeEngine, "_yt_dlp", return_value=fake_module),
                patch.object(engine, "_transcode", side_effect=transcode),
                patch(
                    "ytmax.downloader.probe_media",
                    return_value=MediaProbe(video_codec="h264", audio_codec="aac"),
                ),
            ):
                result = engine._download_true_max_h264("https://youtu.be/vid")

            self.assertEqual(result.name, "A_ Test_ [vid].mp4")
            self.assertEqual(result.read_bytes(), b"converted")
            token = hashlib.sha256(b"https://youtu.be/vid").hexdigest()[:12]
            self.assertFalse((Path(directory) / ".mmm-downloader-temp" / token).exists())

    def test_true_max_uses_filepath_reported_by_current_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp_dir = Path(directory)
            stale = temp_dir / "source.mkv"
            current = temp_dir / "source.webm"
            stale.write_bytes(b"old and deliberately larger")
            current.write_bytes(b"new")

            result = _source_path_from_info({"filepath": str(current)}, temp_dir)

        self.assertEqual(result, current)

    def test_true_max_rejects_ambiguous_unreported_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp_dir = Path(directory)
            (temp_dir / "source.mkv").touch()
            (temp_dir / "source.webm").touch()

            with self.assertRaises(DownloadEngineError):
                _source_path_from_info({}, temp_dir)

    def test_transcode_builds_h264_aac_command_and_reports_progress(self) -> None:
        class FakeProcess:
            command: list[str] = []

            def __init__(self, command: list[str], **_kwargs: object) -> None:
                type(self).command = command
                self.stdout = iter(["out_time_us=5000000\n", "progress=end\n"])

            def poll(self) -> int:
                return 0

            def wait(self, timeout: int | None = None) -> int:
                return 0

            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                return None

        events: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as directory:
            engine = self._engine(directory, events)
            with patch("ytmax.downloader.subprocess.Popen", FakeProcess):
                engine._transcode(
                    Path(directory) / "source.webm",
                    Path(directory) / "result.mp4",
                    10,
                    hdr_to_sdr=True,
                )

        command = FakeProcess.command
        self.assertIn("libx264", command)
        self.assertIn("aac", command)
        self.assertIn("192k", command)
        self.assertIn("+faststart", command)
        self.assertIn(HDR_TO_SDR_FILTER, command)
        progress = [event for event in events if event.get("kind") == "progress"]
        self.assertEqual(progress[0]["percent"], 50.0)


if __name__ == "__main__":
    unittest.main()
