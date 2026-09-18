"""Core, dependency-free data models used by ytmax.

The models deliberately contain no downloader or filesystem behaviour.  This
keeps intent parsing and ranking deterministic and easy to exercise in tests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from os import PathLike
from typing import Literal, TypeAlias
from urllib.parse import parse_qs, quote, urlsplit

RankingMode: TypeAlias = Literal["balanced", "relevance", "views", "newest"]
RANKING_MODES = frozenset({"balanced", "relevance", "views", "newest"})


class IntentValidationError(ValueError):
    """Raised when a parsed or manually-created intent is contradictory."""


@dataclass(frozen=True, slots=True)
class DownloadIntent:
    """A validated request to find videos or download explicit URLs.

    Exactly one of ``topic`` and ``urls`` must be present.  Paths remain plain
    strings here: resolving and authorising them belongs to the filesystem
    boundary, not to a natural-language parser.
    """

    topic: str | None = None
    urls: tuple[str, ...] = ()
    count: int = 20
    min_duration_seconds: int | None = None
    max_duration_seconds: int | None = None
    output_path: str | None = None
    ranking: RankingMode = "balanced"

    def __post_init__(self) -> None:
        topic = self.topic.strip() if self.topic is not None else None
        topic = topic or None

        # Be friendly to callers passing a list while keeping the public model
        # immutable and serialisation-friendly.
        normalised_urls: list[str] = []
        seen_url_keys: set[str] = set()
        for raw_url in self.urls:
            url = str(raw_url).strip()
            if not url:
                continue
            if not _is_youtube_url(url):
                raise IntentValidationError("Only YouTube URLs are supported")
            key = youtube_url_key(url)
            if key in seen_url_keys:
                continue
            normalised_urls.append(url)
            seen_url_keys.add(key)
        urls = tuple(normalised_urls)

        output_path = (
            str(self.output_path).strip()
            if isinstance(self.output_path, (str, PathLike))
            else self.output_path
        )
        output_path = output_path or None

        ranking = str(self.ranking).lower()

        object.__setattr__(self, "topic", topic)
        object.__setattr__(self, "urls", urls)
        object.__setattr__(self, "output_path", output_path)
        object.__setattr__(self, "ranking", ranking)

        if bool(topic) == bool(urls):
            raise IntentValidationError(
                "Specify exactly one source: a search topic or one or more URLs"
            )
        if isinstance(self.count, bool) or not isinstance(self.count, int):
            raise IntentValidationError("Video count must be an integer")
        if not 1 <= self.count <= 100:
            raise IntentValidationError("Video count must be between 1 and 100")
        if len(urls) > 100:
            raise IntentValidationError("At most 100 URLs can be downloaded at once")
        if ranking not in RANKING_MODES:
            choices = ", ".join(sorted(RANKING_MODES))
            raise IntentValidationError(f"Ranking must be one of: {choices}")

        for field_name, value in (
            ("min_duration_seconds", self.min_duration_seconds),
            ("max_duration_seconds", self.max_duration_seconds),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise IntentValidationError(f"{field_name} must be an integer")
            if value < 0:
                raise IntentValidationError(f"{field_name} cannot be negative")

        if (
            self.min_duration_seconds is not None
            and self.max_duration_seconds is not None
            and self.min_duration_seconds > self.max_duration_seconds
        ):
            raise IntentValidationError("Minimum duration cannot be greater than maximum duration")


@dataclass(frozen=True, slots=True)
class Candidate:
    """Metadata used to rank one YouTube video candidate.

    ``published_at`` accepts a datetime/date or an ISO-8601 string because both
    the YouTube API and yt-dlp naturally expose ISO strings.  Ranking handles an
    absent or malformed date as missing metadata rather than failing the batch.
    """

    video_id: str
    title: str
    url: str = ""
    thumbnail_url: str = ""
    channel: str | None = None
    duration_seconds: int | None = None
    view_count: int | None = None
    like_count: int | None = None
    published_at: datetime | date | str | int | float | None = None
    relevance_score: float | None = None
    query_rank: int | None = None

    def __post_init__(self) -> None:
        video_id = self.video_id.strip()
        title = self.title.strip()
        url = self.url.strip()
        thumbnail_url = self.thumbnail_url.strip()
        channel = self.channel.strip() if self.channel is not None else None

        if thumbnail_url:
            try:
                thumbnail_parts = urlsplit(thumbnail_url)
            except ValueError:
                thumbnail_url = ""
            else:
                if (
                    thumbnail_parts.scheme not in {"http", "https"}
                    or not thumbnail_parts.hostname
                ):
                    thumbnail_url = ""

        object.__setattr__(self, "video_id", video_id)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "thumbnail_url", thumbnail_url)
        object.__setattr__(self, "channel", channel or None)

        if not video_id:
            raise ValueError("Candidate.video_id cannot be empty")
        if not title:
            raise ValueError("Candidate.title cannot be empty")

        for field_name, value in (
            ("duration_seconds", self.duration_seconds),
            ("view_count", self.view_count),
            ("like_count", self.like_count),
            ("query_rank", self.query_rank),
        ):
            if value is not None and (isinstance(value, bool) or value < 0):
                raise ValueError(f"Candidate.{field_name} cannot be negative")

        if self.relevance_score is not None:
            try:
                score = float(self.relevance_score)
            except (TypeError, ValueError) as exc:
                raise ValueError("Candidate.relevance_score must be numeric") from exc
            if not math.isfinite(score):
                raise ValueError("Candidate.relevance_score must be finite")
            object.__setattr__(self, "relevance_score", score)

        if isinstance(self.published_at, (int, float)) and not isinstance(self.published_at, bool):
            if not math.isfinite(float(self.published_at)):
                raise ValueError("Candidate.published_at timestamp must be finite")

    @property
    def watch_url(self) -> str:
        """Return the supplied URL or a canonical watch URL."""

        return self.url or f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def preview_image_url(self) -> str:
        """Return a supplied thumbnail or YouTube's lightweight 16:9 fallback."""

        if self.thumbnail_url:
            return self.thumbnail_url
        video_id = quote(self.video_id, safe="")
        return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def _is_youtube_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.casefold().rstrip(".")
    return (
        host == "youtu.be"
        or host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtube-nocookie.com"
        or host.endswith(".youtube-nocookie.com")
    )


def youtube_url_key(value: str) -> str:
    """Return a stable key for equivalent URLs pointing at one YouTube video.

    Known watch, short, live and embed URL variants are keyed by their video
    ID.  Other URLs retain their stripped text, so playlists and channel URLs
    are never accidentally merged.
    """

    url = str(value).strip()
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url

    host = parsed.hostname.casefold().rstrip(".") if parsed.hostname else ""
    path = parsed.path.strip("/")
    video_id = ""
    if host == "youtu.be":
        video_id = path.split("/", 1)[0]
    elif (
        host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtube-nocookie.com"
        or host.endswith(".youtube-nocookie.com")
    ):
        if path.casefold() == "watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        else:
            first, separator, remainder = path.partition("/")
            if separator and first.casefold() in {"shorts", "live", "embed"}:
                video_id = remainder.split("/", 1)[0]

    return f"video:{video_id}" if video_id else url
