from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ytmax.queue_store import (
    InvalidQueueTransition,
    QueueState,
    QueueStore,
    QueueValidationError,
    canonicalize_youtube_video,
)


class QueueStoreTests(unittest.TestCase):
    def test_canonicalizes_supported_youtube_aliases(self) -> None:
        expected = "https://www.youtube.com/watch?v=abc_123"
        for source in (
            "https://youtu.be/abc_123?t=4",
            "https://www.youtube.com/watch?v=abc_123&list=x",
            "https://youtube.com/shorts/abc_123",
            "https://youtube-nocookie.com/embed/abc_123",
        ):
            with self.subTest(source=source):
                video = canonicalize_youtube_video(source)
                self.assertEqual(video.video_id, "abc_123")
                self.assertEqual(video.url, expected)

    def test_replace_transitions_and_recovers_active_item_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = QueueStore(directory)
            store.replace(
                [
                    {
                        "video_id": "first",
                        "url": "https://youtu.be/first",
                        "metadata": {"title": "Первое", "view_count": 12},
                    },
                    {
                        "video_id": "second",
                        "url": "https://youtu.be/second",
                        "metadata": {"title": "Второе"},
                    },
                ],
                settings={"output_dir": "/tmp/video", "quality_mode": "native_h264"},
            )
            store.mark_downloading("first")
            store.mark_transcoding("first")

            restored = QueueStore(directory).snapshot()

            self.assertEqual(restored.load_report.active_requeued, 1)
            self.assertEqual(restored.items[0].state, QueueState.PENDING)
            self.assertEqual(restored.items[0].attempts, 1)
            self.assertEqual(restored.items[0].metadata["title"], "Первое")
            self.assertEqual(restored.settings["quality_mode"], "native_h264")

    def test_failed_items_can_be_retried_without_touching_done_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = QueueStore(directory)
            store.replace(
                [
                    {"video_id": "done", "url": "https://youtu.be/done"},
                    {"video_id": "failed", "url": "https://youtu.be/failed"},
                ]
            )
            store.mark_downloading("done")
            store.mark_done("done", str(Path(directory) / "done.mp4"))
            store.mark_downloading("failed")
            store.mark_failed("failed", "network")

            retried = store.retry_failed()

            self.assertEqual([item.video_id for item in retried], ["failed"])
            self.assertEqual(store.get("failed").state, QueueState.PENDING)  # type: ignore[union-attr]
            self.assertEqual(store.get("done").state, QueueState.DONE)  # type: ignore[union-attr]
            with self.assertRaises(InvalidQueueTransition):
                store.mark_downloading("done")

    def test_invalid_replace_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = QueueStore(directory)
            store.replace([{"video_id": "kept", "url": "https://youtu.be/kept"}])

            with self.assertRaises(QueueValidationError):
                store.replace(
                    [
                        {"video_id": "duplicate", "url": "https://youtu.be/duplicate"},
                        {"video_id": "duplicate", "url": "https://youtu.be/duplicate"},
                    ]
                )

            self.assertEqual([item.video_id for item in store.items()], ["kept"])

    def test_corrupt_manifest_is_quarantined_and_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "download-queue.json"
            path.write_text("{ definitely not json", encoding="utf-8")

            store = QueueStore(directory)

            report = store.last_load_report
            self.assertIsNotNone(report.corrupt_backup)
            self.assertTrue(report.corrupt_backup.is_file())  # type: ignore[union-attr]
            self.assertEqual(store.items(), ())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["items"], [])


if __name__ == "__main__":
    unittest.main()
