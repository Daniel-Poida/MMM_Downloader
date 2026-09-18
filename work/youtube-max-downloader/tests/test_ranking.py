from __future__ import annotations

import unittest
from datetime import datetime, timezone

from ytmax.models import Candidate
from ytmax.ranking import balanced_score, rank_candidates


class RankingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.relevant = Candidate(
            "relevant",
            "Exact match",
            view_count=1_000,
            published_at="2023-01-01T00:00:00Z",
            relevance_score=0.99,
        )
        self.viewed = Candidate(
            "viewed",
            "Popular",
            view_count=1_000_000,
            published_at="2022-01-01T00:00:00Z",
            relevance_score=0.40,
        )
        self.newest = Candidate(
            "newest",
            "Fresh",
            view_count=10_000,
            published_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            relevance_score=0.60,
        )
        self.missing = Candidate("missing", "Unknown")
        self.items = [self.missing, self.viewed, self.relevant, self.newest]

    def test_relevance_ranking(self) -> None:
        ranked = rank_candidates(self.items, "relevance")
        self.assertEqual(
            [item.video_id for item in ranked],
            ["relevant", "newest", "viewed", "missing"],
        )

    def test_views_ranking_puts_missing_last(self) -> None:
        ranked = rank_candidates(self.items, "views")
        self.assertEqual(
            [item.video_id for item in ranked],
            ["viewed", "newest", "relevant", "missing"],
        )

    def test_newest_ranking_accepts_iso_and_datetime(self) -> None:
        ranked = rank_candidates(self.items, "newest")
        self.assertEqual(
            [item.video_id for item in ranked],
            ["newest", "relevant", "viewed", "missing"],
        )

    def test_newest_ranking_accepts_unix_timestamp_from_ytdlp(self) -> None:
        timestamped = Candidate("timestamped", "Timestamped", published_at=1_800_000_000.0)
        ranked = rank_candidates([self.newest, timestamped], "newest")
        self.assertEqual([item.video_id for item in ranked], ["timestamped", "newest"])

    def test_balanced_ranking_is_deterministic(self) -> None:
        forward = rank_candidates(self.items, "balanced")
        reverse = rank_candidates(reversed(self.items), "balanced")
        self.assertEqual(
            [item.video_id for item in forward],
            [item.video_id for item in reverse],
        )
        self.assertEqual(forward[-1].video_id, "missing")

    def test_query_rank_is_relevance_fallback(self) -> None:
        first = Candidate("first", "First", query_rank=1)
        tenth = Candidate("tenth", "Tenth", query_rank=10)
        absent = Candidate("absent", "Absent")
        self.assertEqual(
            rank_candidates([tenth, absent, first], "relevance"),
            [first, tenth, absent],
        )

    def test_ties_do_not_depend_on_input_order(self) -> None:
        alpha = Candidate("b-id", "Alpha")
        beta = Candidate("a-id", "Beta")
        self.assertEqual(
            rank_candidates([beta, alpha], "balanced"),
            rank_candidates([alpha, beta], "balanced"),
        )

    def test_balanced_score_is_bounded(self) -> None:
        score = balanced_score(self.newest, self.items)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_invalid_mode_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rank_candidates(self.items, "magic")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
