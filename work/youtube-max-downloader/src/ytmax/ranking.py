"""Deterministic ranking of video candidates."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timezone
from typing import Iterable

from .models import RANKING_MODES, Candidate, RankingMode


def rank_candidates(
    candidates: Iterable[Candidate],
    mode: RankingMode = "balanced",
) -> list[Candidate]:
    """Return candidates ordered by the requested deterministic strategy.

    Missing metadata is always supported.  In a single-signal strategy it is
    placed after candidates with that signal.  In ``balanced`` mode a missing
    signal contributes zero; if every candidate lacks a signal, that signal has
    no effect.  Final ties use title and video ID, never input order.
    """

    if mode not in RANKING_MODES:
        choices = ", ".join(sorted(RANKING_MODES))
        raise ValueError(f"Unknown ranking mode {mode!r}; expected one of: {choices}")

    items = list(candidates)
    if not items:
        return []

    relevance_raw = {candidate: _relevance(candidate) for candidate in items}
    views_raw = {
        candidate: math.log1p(candidate.view_count) if candidate.view_count is not None else None
        for candidate in items
    }
    newest_raw = {candidate: _published_timestamp(candidate.published_at) for candidate in items}

    if mode == "relevance":
        return sorted(
            items,
            key=lambda candidate: _single_signal_key(
                relevance_raw[candidate],
                views_raw[candidate],
                newest_raw[candidate],
                candidate,
            ),
        )
    if mode == "views":
        return sorted(
            items,
            key=lambda candidate: _single_signal_key(
                views_raw[candidate],
                relevance_raw[candidate],
                newest_raw[candidate],
                candidate,
            ),
        )
    if mode == "newest":
        return sorted(
            items,
            key=lambda candidate: _single_signal_key(
                newest_raw[candidate],
                relevance_raw[candidate],
                views_raw[candidate],
                candidate,
            ),
        )

    relevance = _normalise(relevance_raw)
    views = _normalise(views_raw)
    newest = _normalise(newest_raw)
    scores = {
        candidate: (
            0.50 * relevance[candidate] + 0.30 * views[candidate] + 0.20 * newest[candidate]
        )
        for candidate in items
    }
    return sorted(items, key=lambda candidate: (-scores[candidate], *_tie_key(candidate)))


def balanced_score(candidate: Candidate, candidates: Iterable[Candidate]) -> float:
    """Return the same 0..1 score used by balanced ranking.

    This helper is primarily for an explainable preview UI.  The supplied
    population defines min/max normalisation and must include ``candidate``.
    """

    items = list(candidates)
    if candidate not in items:
        items.append(candidate)
    relevance = _normalise({item: _relevance(item) for item in items})
    views = _normalise(
        {
            item: math.log1p(item.view_count) if item.view_count is not None else None
            for item in items
        }
    )
    newest = _normalise({item: _published_timestamp(item.published_at) for item in items})
    return 0.50 * relevance[candidate] + 0.30 * views[candidate] + 0.20 * newest[candidate]


def _relevance(candidate: Candidate) -> float | None:
    if candidate.relevance_score is not None:
        return candidate.relevance_score
    if candidate.query_rank is not None:
        # Works for either zero- or one-based ranks and is already bounded.
        return 1.0 / (1.0 + candidate.query_rank)
    return None


def _normalise(values: dict[Candidate, float | None]) -> dict[Candidate, float]:
    known = [value for value in values.values() if value is not None and math.isfinite(value)]
    if not known:
        return {candidate: 0.0 for candidate in values}
    low = min(known)
    high = max(known)
    if math.isclose(low, high):
        return {
            candidate: 1.0 if value is not None and math.isfinite(value) else 0.0
            for candidate, value in values.items()
        }
    return {
        candidate: (value - low) / (high - low)
        if value is not None and math.isfinite(value)
        else 0.0
        for candidate, value in values.items()
    }


def _single_signal_key(
    primary: float | None,
    secondary: float | None,
    tertiary: float | None,
    candidate: Candidate,
) -> tuple[float | int | str, ...]:
    return (
        primary is None,
        -(primary if primary is not None else 0.0),
        secondary is None,
        -(secondary if secondary is not None else 0.0),
        tertiary is None,
        -(tertiary if tertiary is not None else 0.0),
        *_tie_key(candidate),
    )


def _tie_key(candidate: Candidate) -> tuple[str, str]:
    return candidate.title.casefold(), candidate.video_id.casefold()


def _published_timestamp(
    value: datetime | date | str | int | float | None,
) -> float | None:
    if value is None:
        return None
    parsed: datetime
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        return timestamp if math.isfinite(timestamp) else None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            try:
                parsed = datetime.strptime(raw, "%Y%m%d")
            except ValueError:
                return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()
