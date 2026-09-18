"""Deterministic Russian/English natural-language intent parser."""

from __future__ import annotations

import re
from collections.abc import Iterable
from os import PathLike
from urllib.parse import parse_qs, urlsplit

from .models import DownloadIntent, IntentValidationError, RankingMode


class IntentParseError(IntentValidationError):
    """Raised when text contains neither YouTube URLs nor a usable topic."""


_NUMBER = r"(\d+(?:[.,]\d+)?)"
_UNIT = (
    r"(секунд(?:а|ы)?|сек\.?|seconds?|secs?|s|"
    r"минут(?:а|ы)?|мин\.?|minutes?|mins?|m|"
    r"час(?:а|ов)?|ч\.?|hours?|hrs?|h)"
)
_DURATION = _NUMBER + r"\s*" + _UNIT

_RANGE_PATTERNS = (
    re.compile(
        rf"\bот\s+{_NUMBER}\s*{_UNIT}?\s+до\s+{_NUMBER}\s*{_UNIT}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bbetween\s+{_NUMBER}\s*{_UNIT}?\s+and\s+{_NUMBER}\s*{_UNIT}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bfrom\s+{_NUMBER}\s*{_UNIT}?\s+to\s+{_NUMBER}\s*{_UNIT}\b",
        re.IGNORECASE,
    ),
)

_MIN_PATTERNS = (
    re.compile(
        rf"\b(?:не\s+короче|не\s+менее|как\s+минимум|минимум|"
        rf"(?<!не\s)дольше|(?<!не\s)длиннее|(?<!не\s)более|от)"
        rf"\s+{_DURATION}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:at\s+least|no\s+shorter\s+than|no\s+less\s+than|minimum|min\.?|"
        rf"(?<!no\s)longer\s+than|(?<!no\s)more\s+than|over)\s+{_DURATION}\b",
        re.IGNORECASE,
    ),
)

_MAX_PATTERNS = (
    re.compile(
        rf"\b(?:не\s+длиннее|не\s+дольше|не\s+более|"
        rf"как\s+максимум|"
        rf"максимум|(?<!не\s)короче|(?<!не\s)менее|до)\s+{_DURATION}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:at\s+most|no\s+longer\s+than|maximum|max\.?|"
        rf"(?<!no\s)shorter\s+than|(?<!no\s)less\s+than|under|up\s+to)\s+{_DURATION}\b",
        re.IGNORECASE,
    ),
)

_COUNT_PATTERNS = (
    re.compile(r"\b(?:top|топ)[-\s]*(\d+)\b", re.IGNORECASE),
    re.compile(
        r"\b(\d+)\s*(?:(?:(?:самых\s+)?(?:лучших|популярных|новых)|"
        r"best|most\s+viewed|newest)\s+)?"
        r"(?:видео|ролик(?:а|ов|и)?|videos?|clips?)\b",
        re.IGNORECASE,
    ),
)

_YOUTUBE_URL_RE = re.compile(
    r"(?<![\w@])(?:https?://)?(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?[^\s<>\"']+|shorts/[^\s<>\"']+|"
    r"live/[^\s<>\"']+|embed/[^\s<>\"']+)|youtu\.be/[^\s<>\"']+)",
    re.IGNORECASE,
)

_QUOTED_RE = re.compile(r'"([^"\n]+)"|\'([^\'\n]+)\'|«([^»\n]+)»')
_PATH_TOKEN = r'(?:"[^"\n]+"|\'[^\'\n]+\'|(?:~|\.{1,2}|/)[^\s,;]+|[A-Za-z]:[\\/][^\s,;]+)'
_OUTPUT_PATTERNS = (
    re.compile(
        rf"(?:--(?:output|to)|output(?:[_ -](?:path|folder|directory))?\s*[:=]?|"
        rf"путь\s*[:=])\s*"
        rf"(?P<path>{_PATH_TOKEN})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:в\s+папку|(?:положи|сохрани|запиши)(?:\s+\w+){{0,4}}\s+в|"
        rf"save(?:\s+them|\s+it)?\s+(?:to|in)|into|to)\s+"
        rf"(?P<path>{_PATH_TOKEN})",
        re.IGNORECASE,
    ),
)

_OUTPUT_ACTION_RE = re.compile(
    r"\s+(?:и\s+)?(?:положи|сохрани|запиши)\b.*$|"
    r"\s+(?:and\s+)?save(?:\s+them|\s+it)?\b.*$",
    re.IGNORECASE,
)

_TOPIC_MARKERS = (
    re.compile(r"\b(?:на\s+тему|про|об|о)\s+(.+)$", re.IGNORECASE),
    re.compile(
        r"\b(?:about|regarding|on\s+the\s+topic\s+of|featuring|on|of)\s+(.+)$",
        re.IGNORECASE,
    ),
    # Russian requests commonly say "видео с интерьерами...".  Requiring a
    # video noun before "с" avoids treating an arbitrary instrumental phrase
    # as the topic.
    re.compile(
        r"\b(?:видео|ролик(?:а|ов|и)?)\b.*?\sс\s+(.+)$",
        re.IGNORECASE,
    ),
)


def parse_intent(
    text: str,
    *,
    output_path: str | PathLike[str] | None = None,
    default_count: int = 20,
) -> DownloadIntent:
    """Parse a small, explicit subset of Russian or English download prose.

    The parser is intentionally deterministic.  It extracts URLs, count,
    duration bounds, ranking mode, topic and a path; it never interprets the
    text as code or a shell command.  ``output_path`` is an explicit trusted UI
    or CLI choice and therefore overrides a path mentioned in ``text``.
    """

    if not isinstance(text, str) or not text.strip():
        raise IntentParseError("Request cannot be empty")
    if isinstance(default_count, bool) or not isinstance(default_count, int):
        raise IntentValidationError("default_count must be an integer")
    if not 1 <= default_count <= 100:
        raise IntentValidationError("default_count must be between 1 and 100")

    source = " ".join(text.replace("\x00", " ").split())
    urls = _extract_urls(source)
    explicit_count = _extract_count(source)
    min_seconds, max_seconds, duration_spans = _extract_duration_bounds(source)
    ranking = _extract_ranking(source)
    parsed_output, output_span = _extract_output_path(source)

    selected_output = str(output_path).strip() if output_path is not None else parsed_output
    selected_output = selected_output or None

    if urls:
        # An explicit URL list is already a complete selection.  A number in
        # surrounding prose should not cause only part of that list to run.
        count = len(urls)
        topic = None
    else:
        count = explicit_count if explicit_count is not None else default_count
        topic = _extract_topic(
            source,
            duration_spans=duration_spans,
            output_span=output_span,
        )
        if not topic:
            raise IntentParseError("Could not find a topic or YouTube URL in the request")

    return DownloadIntent(
        topic=topic,
        urls=urls,
        count=count,
        min_duration_seconds=min_seconds,
        max_duration_seconds=max_seconds,
        output_path=selected_output,
        ranking=ranking,
    )


def _extract_count(text: str) -> int | None:
    for pattern in _COUNT_PATTERNS:
        match = pattern.search(text)
        if match:
            count = int(match.group(1))
            if not 1 <= count <= 100:
                raise IntentValidationError("Video count must be between 1 and 100")
            return count
    return None


def _extract_duration_bounds(
    text: str,
) -> tuple[int | None, int | None, tuple[tuple[int, int], ...]]:
    min_seconds: int | None = None
    max_seconds: int | None = None
    spans: list[tuple[int, int]] = []

    masked = text
    for pattern in _RANGE_PATTERNS:
        match = pattern.search(masked)
        if not match:
            continue
        low_value, low_unit, high_value, high_unit = match.groups()
        low_unit = low_unit or high_unit
        high_unit = high_unit or low_unit
        min_seconds = _to_seconds(low_value, low_unit)
        max_seconds = _to_seconds(high_value, high_unit)
        spans.append(match.span())
        masked = _mask(masked, match.span())
        break

    for pattern in _MIN_PATTERNS:
        for match in pattern.finditer(masked):
            value = _to_seconds(match.group(1), match.group(2))
            min_seconds = value if min_seconds is None else max(min_seconds, value)
            spans.append(match.span())

    for pattern in _MAX_PATTERNS:
        for match in pattern.finditer(masked):
            value = _to_seconds(match.group(1), match.group(2))
            max_seconds = value if max_seconds is None else min(max_seconds, value)
            spans.append(match.span())

    if min_seconds is not None and max_seconds is not None and min_seconds > max_seconds:
        raise IntentValidationError("Minimum duration cannot be greater than maximum duration")

    return min_seconds, max_seconds, tuple(sorted(spans))


def _to_seconds(value: str, unit: str) -> int:
    amount = float(value.replace(",", "."))
    normalised = unit.lower().rstrip(".")
    if normalised.startswith(("ч", "h")):
        multiplier = 3600
    elif normalised.startswith(("м", "m")):
        multiplier = 60
    else:
        multiplier = 1
    return int(round(amount * multiplier))


def _extract_ranking(text: str) -> RankingMode:
    lower = text.casefold()
    if re.search(
        r"\b(?:нов(?:ый|ая|ое|ые|ых|ейший|ейшая|ейшие|ейших)|"
        r"свеж\w*|newest|latest|recent)\b",
        lower,
    ):
        return "newest"
    if re.search(
        r"\b(?:по\s+просмотрам|самые\s+просматриваемые|"
        r"популярн\w*|"
        r"most\s+viewed|by\s+views|popular)\b",
        lower,
    ):
        return "views"
    if re.search(
        r"\b(?:по\s+релевантности|релевантн\w*|"
        r"relevance|most\s+relevant)\b",
        lower,
    ):
        return "relevance"
    return "balanced"


def _extract_urls(text: str) -> tuple[str, ...]:
    urls: list[str] = []
    seen_keys: set[str] = set()
    for match in _YOUTUBE_URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?)]}»")
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        key = _youtube_url_key(url)
        if key not in seen_keys:
            urls.append(url)
            seen_keys.add(key)
    if len(urls) > 100:
        raise IntentValidationError("At most 100 URLs can be downloaded at once")
    return tuple(urls)


def _youtube_url_key(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname.casefold() if parsed.hostname else ""
    path = parsed.path.strip("/")
    if host.endswith("youtu.be"):
        video_id = path.split("/", 1)[0]
        return f"video:{video_id}" if video_id else url
    if host.endswith("youtube.com"):
        if path == "watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif path.startswith(("shorts/", "live/", "embed/")):
            video_id = path.split("/", 1)[1].split("/", 1)[0]
        else:
            video_id = ""
        return f"video:{video_id}" if video_id else url
    return url


def _extract_output_path(text: str) -> tuple[str | None, tuple[int, int] | None]:
    for pattern in _OUTPUT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        value = _unquote(match.group("path").strip()).strip()
        if value:
            return value, match.span()
    return None, None


def _extract_topic(
    text: str,
    *,
    duration_spans: Iterable[tuple[int, int]],
    output_span: tuple[int, int] | None,
) -> str | None:
    # A quoted non-path phrase is the least ambiguous topic form.
    for match in _QUOTED_RE.finditer(text):
        if output_span is not None and (
            output_span[0] <= match.start() and match.end() <= output_span[1]
        ):
            continue
        value = next(group for group in match.groups() if group is not None).strip()
        if not _looks_like_path(value) and not _YOUTUBE_URL_RE.fullmatch(value):
            return _clean_topic(value)

    working = text
    if output_span is not None:
        working = _mask(working, output_span)
    for span in duration_spans:
        working = _mask(working, span)

    for marker in _TOPIC_MARKERS:
        matches = list(marker.finditer(working))
        if matches:
            topic = _clean_topic(matches[-1].group(1))
            if topic:
                return topic

    # Fallback for compact commands such as "download 10 Art Deco videos".
    fallback = working
    fallback = _YOUTUBE_URL_RE.sub(" ", fallback)
    fallback = _OUTPUT_ACTION_RE.sub(" ", fallback)
    fallback = re.sub(
        r"^(?:please\s+)?(?:download|find|get|search(?:\s+for)?|"
        r"скачай|загрузи|найди|подбери)\b",
        " ",
        fallback,
        flags=re.IGNORECASE,
    )
    for pattern in _COUNT_PATTERNS:
        fallback = pattern.sub(" ", fallback)
    fallback = re.sub(
        r"\b(?:best|top|лучших|самых|популярных|balanced|"
        r"по\s+релевантности|by\s+views)\b",
        " ",
        fallback,
        flags=re.IGNORECASE,
    )
    fallback = re.sub(r"\b(?:please|пожалуйста)\b", " ", fallback, flags=re.IGNORECASE)
    fallback = re.sub(
        r"\b(?:videos?|clips?|видео|ролик(?:а|ов|и)?)\b",
        " ",
        fallback,
        flags=re.IGNORECASE,
    )
    return _clean_topic(fallback)


def _clean_topic(value: str) -> str | None:
    value = _OUTPUT_ACTION_RE.sub("", value)
    value = re.split(
        rf"\s+(?:at\s+least|at\s+most|no\s+longer\s+than|longer\s+than|"
        rf"under|over|не\s+короче|не\s+длиннее|от|до)\s+{_NUMBER}",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    value = re.sub(
        r"(?:[,;]\s*|\s+)(?:most\s+viewed|by\s+views|most\s+relevant|"
        r"newest|latest|по\s+просмотрам|по\s+релевантности)$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n,;:.!?—–-\"'«»")
    value = re.sub(r"^(?:about|on|of|про|об|о|на\s+тему)\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+(?:and|и)$", "", value, flags=re.IGNORECASE).strip()
    return value or None


def _looks_like_path(value: str) -> bool:
    return bool(
        value.startswith(("/", "~/", "./", "../", "\\\\")) or re.match(r"^[A-Za-z]:[\\/]", value)
    )


def _unquote(value: str) -> str:
    pairs = {'"': '"', "'": "'", "«": "»"}
    if len(value) >= 2 and value[0] in pairs and value[-1] == pairs[value[0]]:
        return value[1:-1]
    return value


def _mask(text: str, span: tuple[int, int]) -> str:
    start, end = span
    return text[:start] + (" " * (end - start)) + text[end:]
