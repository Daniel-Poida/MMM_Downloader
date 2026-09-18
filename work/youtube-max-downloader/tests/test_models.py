from __future__ import annotations

import unittest

from ytmax.models import DownloadIntent, IntentValidationError, youtube_url_key


class ModelTests(unittest.TestCase):
    def test_youtube_video_url_variants_share_a_stable_key(self) -> None:
        urls = (
            "https://youtu.be/AbC_123?t=10",
            "https://www.youtube.com/watch?v=AbC_123&feature=share",
            "https://m.youtube.com/shorts/AbC_123",
            "https://www.youtube-nocookie.com/embed/AbC_123?autoplay=1",
        )

        self.assertEqual({youtube_url_key(url) for url in urls}, {"video:AbC_123"})

    def test_download_intent_deduplicates_aliases_and_preserves_first_seen_order(self) -> None:
        first = "https://youtu.be/first"
        first_alias = "https://www.youtube.com/watch?v=first&t=30"
        second = "https://youtube.com/shorts/second"
        second_alias = "https://youtube.com/embed/second"

        intent = DownloadIntent(
            urls=(first, first_alias, second, second_alias, first),
            count=5,
        )

        self.assertEqual(intent.urls, (first, second))

    def test_non_video_youtube_urls_are_not_merged_by_path_agnostic_rules(self) -> None:
        playlist = "https://www.youtube.com/playlist?list=PL-one"
        another_playlist = "https://www.youtube.com/playlist?list=PL-two"

        intent = DownloadIntent(urls=(playlist, another_playlist), count=2)

        self.assertEqual(intent.urls, (playlist, another_playlist))

    def test_invalid_alias_cannot_hide_behind_a_valid_video_url(self) -> None:
        with self.assertRaises(IntentValidationError):
            DownloadIntent(
                urls=("https://youtu.be/first", "ftp://youtu.be/first"),
                count=2,
            )


if __name__ == "__main__":
    unittest.main()
