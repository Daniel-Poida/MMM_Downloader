from __future__ import annotations

import unittest

from ytmax.intent import IntentParseError, parse_intent
from ytmax.models import DownloadIntent, IntentValidationError


class ParseIntentTests(unittest.TestCase):
    def test_russian_request(self) -> None:
        intent = parse_intent(
            "скачай 20 лучших видео длительностью от 10 минут "
            "с интерьерами в стиле Ар-Деко и положи их "
            'в "/tmp/Art Deco"'
        )

        self.assertEqual(intent.count, 20)
        self.assertEqual(intent.min_duration_seconds, 600)
        self.assertIsNone(intent.max_duration_seconds)
        self.assertEqual(intent.topic, "интерьерами в стиле Ар-Деко")
        self.assertEqual(intent.output_path, "/tmp/Art Deco")
        self.assertEqual(intent.ranking, "balanced")

    def test_english_quoted_topic_and_range(self) -> None:
        intent = parse_intent(
            'download top 12 videos about "Art Deco interiors" '
            "between 10 and 45 minutes to ./downloads"
        )

        self.assertEqual(intent.topic, "Art Deco interiors")
        self.assertEqual(intent.count, 12)
        self.assertEqual(intent.min_duration_seconds, 600)
        self.assertEqual(intent.max_duration_seconds, 2700)
        self.assertEqual(intent.output_path, "./downloads")

    def test_plain_english_topic_and_ranking(self) -> None:
        intent = parse_intent(
            "find 8 videos about brutalist houses at least 1.5 hours, most viewed"
        )

        self.assertEqual(intent.topic, "brutalist houses")
        self.assertEqual(intent.min_duration_seconds, 5400)
        self.assertEqual(intent.ranking, "views")

    def test_english_plain_topic_after_of(self) -> None:
        intent = parse_intent("download 20 best videos of Art Deco interiors under 1 hour")
        self.assertEqual(intent.count, 20)
        self.assertEqual(intent.topic, "Art Deco interiors")
        self.assertEqual(intent.max_duration_seconds, 3600)

    def test_separate_russian_min_and_max_do_not_overlap(self) -> None:
        intent = parse_intent("скачай 10 роликов про лофт не менее 10 минут и не более 1 часа")
        self.assertEqual(intent.min_duration_seconds, 600)
        self.assertEqual(intent.max_duration_seconds, 3600)

    def test_list_of_urls_is_deduplicated_by_video_id(self) -> None:
        intent = parse_intent(
            "download https://youtu.be/abc123 and "
            "https://www.youtube.com/watch?v=abc123 plus "
            "youtube.com/shorts/xyz789"
        )

        self.assertIsNone(intent.topic)
        self.assertEqual(intent.count, 2)
        self.assertEqual(
            intent.urls,
            (
                "https://youtu.be/abc123",
                "https://youtube.com/shorts/xyz789",
            ),
        )

    def test_explicit_output_path_overrides_prose(self) -> None:
        intent = parse_intent(
            "скачай 5 видео про японские интерьеры в папку /tmp/wrong",
            output_path="/tmp/right",
        )
        self.assertEqual(intent.output_path, "/tmp/right")

    def test_quoted_relative_output_is_not_mistaken_for_topic(self) -> None:
        intent = parse_intent('download 4 videos about Art Deco lighting to "rendered videos"')
        self.assertEqual(intent.topic, "Art Deco lighting")
        self.assertEqual(intent.output_path, "rendered videos")

    def test_output_keyword_without_equals(self) -> None:
        intent = parse_intent("find 3 videos about Bauhaus output /tmp/bauhaus")
        self.assertEqual(intent.topic, "Bauhaus")
        self.assertEqual(intent.output_path, "/tmp/bauhaus")

    def test_newest_and_max_duration(self) -> None:
        intent = parse_intent("найди 7 новых видео про баухаус не длиннее 30 минут")
        self.assertEqual(intent.ranking, "newest")
        self.assertEqual(intent.max_duration_seconds, 1800)
        self.assertEqual(intent.topic, "баухаус")

    def test_count_outside_cap_is_rejected(self) -> None:
        with self.assertRaises(IntentValidationError):
            parse_intent("download 101 videos about architecture")

    def test_contradictory_duration_is_rejected(self) -> None:
        with self.assertRaises(IntentValidationError):
            parse_intent("скачай 5 видео про ар-деко от 30 минут до 10 минут")

    def test_missing_topic_and_urls_is_rejected(self) -> None:
        with self.assertRaises(IntentParseError):
            parse_intent("скачай пожалуйста")

    def test_model_validates_exclusive_source(self) -> None:
        with self.assertRaises(IntentValidationError):
            DownloadIntent(topic="topic", urls=("https://youtu.be/id",))


if __name__ == "__main__":
    unittest.main()
