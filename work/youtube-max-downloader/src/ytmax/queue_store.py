"""Durable, dependency-free download queue storage.

The module intentionally has no GUI or downloader imports.  A typical GUI
integration creates one :class:`QueueStore`, saves a plan before starting a
worker, and then records state changes emitted by that worker::

    queue.replace(
        [
            {
                "video_id": candidate.video_id,
                "url": candidate.watch_url,
                "metadata": {
                    "title": candidate.title,
                    "thumbnail_url": candidate.thumbnail_url,
                },
            }
            for candidate in candidates
        ],
        settings={
            "output_dir": output_dir,
            "quality_mode": quality_mode,
            "cookie_browser": cookie_browser,
        },
    )

All mutating operations write a complete JSON manifest to a temporary file in
the settings directory and atomically replace the previous manifest.  Queue
items returned to callers are snapshots; mutating their ``metadata`` mapping
does not mutate the store.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, TypeAlias
from urllib.parse import parse_qs, unquote, urlsplit

MANIFEST_FILENAME = "download-queue.json"
MANIFEST_VERSION = 1
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_ACTIVE_STATES = frozenset({"downloading", "transcoding"})

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class QueueState(str, Enum):
    """Persistent lifecycle states for one download."""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    TRANSCODING = "transcoding"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STATES = frozenset({QueueState.DOWNLOADING, QueueState.TRANSCODING})
TERMINAL_STATES = frozenset({QueueState.DONE, QueueState.FAILED, QueueState.CANCELLED})


class QueueStoreError(RuntimeError):
    """Base class for queue storage errors."""


class QueueValidationError(QueueStoreError, ValueError):
    """Raised when a queue item, URL, or manifest value is invalid."""


class QueuePersistenceError(QueueStoreError, OSError):
    """Raised when a manifest cannot be safely persisted."""


class QueueItemNotFoundError(QueueStoreError, KeyError):
    """Raised when a requested video is not in the queue."""


class InvalidQueueTransition(QueueStoreError, ValueError):
    """Raised for a lifecycle transition that would violate the state machine."""


@dataclass(frozen=True, slots=True)
class CanonicalVideo:
    """A validated YouTube video identity and its canonical watch URL."""

    video_id: str
    url: str


def canonicalize_youtube_video(
    url: str | None = None,
    *,
    video_id: str | None = None,
) -> CanonicalVideo:
    """Return a canonical video id and ``youtube.com/watch`` URL.

    ``youtu.be`` links and YouTube ``watch``, ``shorts``, ``live`` and ``embed``
    URLs are accepted.  When both values are supplied they must identify the
    same video.  Supplying only ``video_id`` is useful for candidates returned
    directly by yt-dlp.
    """

    explicit_id = _validate_video_id(video_id) if video_id is not None else None
    url_id: str | None = None
    if url is not None and str(url).strip():
        url_id = _video_id_from_url(str(url).strip())
    elif url is not None and explicit_id is None:
        raise QueueValidationError("A YouTube URL or video_id is required")

    if explicit_id is not None and url_id is not None and explicit_id != url_id:
        raise QueueValidationError("video_id does not match the supplied YouTube URL")
    canonical_id = explicit_id or url_id
    if canonical_id is None:
        raise QueueValidationError("A YouTube URL or video_id is required")
    return CanonicalVideo(
        video_id=canonical_id,
        url=f"https://www.youtube.com/watch?v={canonical_id}",
    )


def _validate_video_id(value: object) -> str:
    video_id = str(value).strip() if value is not None else ""
    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise QueueValidationError("Invalid YouTube video_id")
    return video_id


def _video_id_from_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise QueueValidationError("Invalid YouTube URL") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise QueueValidationError("YouTube URL must use http or https")

    host = parsed.hostname.casefold().rstrip(".")
    is_youtube = host == "youtube.com" or host.endswith(".youtube.com")
    is_nocookie = host == "youtube-nocookie.com" or host.endswith(
        ".youtube-nocookie.com"
    )
    path = unquote(parsed.path).strip("/")
    if host == "youtu.be":
        candidate = path.split("/", 1)[0]
    elif is_youtube or is_nocookie:
        if path.casefold() == "watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        else:
            parts = path.split("/", 1)
            candidate = (
                parts[1].split("/", 1)[0]
                if len(parts) == 2
                and parts[0].casefold() in {"shorts", "live", "embed", "v"}
                else ""
            )
    else:
        raise QueueValidationError("Only YouTube video URLs are supported")
    return _validate_video_id(candidate)


@dataclass(frozen=True, slots=True)
class QueueSeed:
    """Convenient input for adding a fresh pending item."""

    video_id: str
    url: str = ""
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        canonical = canonicalize_youtube_video(self.url or None, video_id=self.video_id)
        object.__setattr__(self, "video_id", canonical.video_id)
        object.__setattr__(self, "url", canonical.url)
        object.__setattr__(self, "metadata", _copy_json_object(self.metadata, "metadata"))


@dataclass(frozen=True, slots=True)
class QueueItem:
    """Serializable snapshot of one queued video."""

    video_id: str
    url: str = ""
    state: QueueState = QueueState.PENDING
    output_path: str | None = None
    error: str | None = None
    attempts: int = 0
    metadata: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        canonical = canonicalize_youtube_video(self.url or None, video_id=self.video_id)
        object.__setattr__(self, "video_id", canonical.video_id)
        object.__setattr__(self, "url", canonical.url)
        try:
            state = self.state if isinstance(self.state, QueueState) else QueueState(self.state)
        except (TypeError, ValueError) as exc:
            raise QueueValidationError(f"Unknown queue state: {self.state!r}") from exc
        object.__setattr__(self, "state", state)

        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int):
            raise QueueValidationError("attempts must be a non-negative integer")
        if self.attempts < 0:
            raise QueueValidationError("attempts must be a non-negative integer")

        output_path = _optional_text(self.output_path)
        error = _optional_text(self.error)
        if state is QueueState.DONE and output_path is None:
            raise QueueValidationError("A done item must have an output_path")
        if state is QueueState.FAILED and error is None:
            raise QueueValidationError("A failed item must have an error")
        if state not in {QueueState.DONE, QueueState.FAILED} and (
            output_path is not None or error is not None
        ):
            raise QueueValidationError(
                "Only done/failed items may contain output_path or error details"
            )
        if state is QueueState.DONE and error is not None:
            raise QueueValidationError("A done item cannot contain an error")
        if state is QueueState.FAILED and output_path is not None:
            raise QueueValidationError("A failed item cannot contain an output_path")
        object.__setattr__(self, "output_path", output_path)
        object.__setattr__(self, "error", error)
        object.__setattr__(self, "metadata", _copy_json_object(self.metadata, "metadata"))

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def can_retry(self) -> bool:
        return self.state is QueueState.FAILED


@dataclass(frozen=True, slots=True)
class QueueLoadReport:
    """Non-fatal repairs performed by the most recent :meth:`QueueStore.load`."""

    active_requeued: int = 0
    invalid_items_skipped: int = 0
    corrupt_backup: Path | None = None
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    """GUI-friendly point-in-time view of the manifest."""

    items: tuple[QueueItem, ...]
    settings: dict[str, JsonValue]
    load_report: QueueLoadReport

    @property
    def resumable(self) -> tuple[QueueItem, ...]:
        """Items ready to be downloaded or offered for restoration."""

        return tuple(item for item in self.items if item.state is QueueState.PENDING)


_ALLOWED_TRANSITIONS: dict[QueueState, frozenset[QueueState]] = {
    QueueState.PENDING: frozenset(
        {QueueState.DOWNLOADING, QueueState.FAILED, QueueState.CANCELLED}
    ),
    QueueState.DOWNLOADING: frozenset(
        {
            QueueState.TRANSCODING,
            QueueState.DONE,
            QueueState.FAILED,
            QueueState.CANCELLED,
        }
    ),
    QueueState.TRANSCODING: frozenset(
        {QueueState.DONE, QueueState.FAILED, QueueState.CANCELLED}
    ),
    QueueState.DONE: frozenset(),
    QueueState.FAILED: frozenset({QueueState.PENDING, QueueState.CANCELLED}),
    QueueState.CANCELLED: frozenset({QueueState.PENDING}),
}


class QueueStore:
    """Thread-safe durable queue backed by an atomic JSON manifest.

    ``directory`` defaults to :func:`ytmax.settings.settings_dir`.  The class
    protects concurrent threads in one process; atomic replacement protects
    readers from partial JSON, but separate writer processes should not share a
    manifest concurrently.
    """

    def __init__(
        self,
        directory: str | os.PathLike[str] | None = None,
        *,
        filename: str = MANIFEST_FILENAME,
    ) -> None:
        if directory is None:
            # Import lazily so tests and embedders can override settings_dir.
            from . import settings as app_settings

            directory = app_settings.settings_dir()
        if not filename or Path(filename).name != filename:
            raise QueueValidationError("filename must be a plain file name")
        self.directory = Path(directory)
        self.path = self.directory / filename
        self._lock = threading.RLock()
        self._items: dict[str, QueueItem] = {}
        self._settings: dict[str, JsonValue] = {}
        self._last_load_report = QueueLoadReport()
        self._writes_blocked_reason: str | None = None
        self.load()

    @property
    def last_load_report(self) -> QueueLoadReport:
        with self._lock:
            return self._last_load_report

    def snapshot(self) -> QueueSnapshot:
        """Return a detached in-memory snapshot without reading the disk."""

        with self._lock:
            return self._snapshot_locked()

    def load(self) -> QueueSnapshot:
        """Reload, validate, and recover the manifest from disk.

        Items left in ``downloading`` or ``transcoding`` are changed to
        ``pending`` and the repaired manifest is immediately persisted.  A
        malformed manifest is moved aside as ``*.corrupt-<timestamp>.json``;
        valid items from a partially malformed manifest are retained.
        """

        with self._lock:
            self._load_locked()
            return self._snapshot_locked()

    def items(
        self,
        states: Iterable[QueueState | str] | QueueState | str | None = None,
    ) -> tuple[QueueItem, ...]:
        """Return detached items in stable insertion order, optionally filtered."""

        with self._lock:
            wanted = _normalise_states(states)
            return tuple(
                _clone_item(item)
                for item in self._items.values()
                if wanted is None or item.state in wanted
            )

    def resumable(self) -> tuple[QueueItem, ...]:
        """Return the pending items that can be offered for restoration."""

        return self.items(QueueState.PENDING)

    def get(self, video_id: str) -> QueueItem | None:
        with self._lock:
            item = self._items.get(_validate_video_id(video_id))
            return _clone_item(item) if item is not None else None

    def replace(
        self,
        entries: Iterable[QueueItem | QueueSeed | Mapping[str, object]],
        *,
        settings: Mapping[str, object] | None = None,
    ) -> tuple[QueueItem, ...]:
        """Atomically replace the complete queue and its launch settings.

        Each entry may be a :class:`QueueItem`, :class:`QueueSeed`, or a mapping
        with ``video_id``, optional ``url``, and optional ``metadata`` keys.
        Mappings may also specify ``state``, ``output_path``, ``error`` and
        ``attempts`` when restoring an already materialised queue.  Omitting
        ``state`` creates a pending item.  Duplicate video ids reject the whole
        operation, so an invalid batch never partly replaces the old queue.

        ``settings`` is any JSON-safe object with string keys.  It is intended
        for values needed to resume a batch, such as ``output_dir``,
        ``quality_mode`` and ``cookie_browser``.
        """

        new_items: dict[str, QueueItem] = {}
        for entry in entries:
            item = _coerce_item(entry)
            if item.video_id in new_items:
                raise QueueValidationError(f"Duplicate video_id: {item.video_id}")
            new_items[item.video_id] = item
        new_settings = _copy_json_object(settings or {}, "settings")
        with self._lock:
            self._commit_locked(new_items, new_settings)
            return tuple(_clone_item(item) for item in self._items.values())

    def enqueue(
        self,
        url: str | None = None,
        *,
        video_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> QueueItem:
        """Add one pending video, returning the existing item on duplicate id."""

        canonical = canonicalize_youtube_video(url, video_id=video_id)
        item = QueueItem(
            video_id=canonical.video_id,
            url=canonical.url,
            metadata=_copy_json_object(metadata or {}, "metadata"),
        )
        with self._lock:
            existing = self._items.get(item.video_id)
            if existing is not None:
                return _clone_item(existing)
            new_items = dict(self._items)
            new_items[item.video_id] = item
            self._commit_locked(new_items, self._settings)
            return _clone_item(item)

    def set_settings(self, settings: Mapping[str, object]) -> QueueSnapshot:
        """Atomically update the resumable launch settings only."""

        new_settings = _copy_json_object(settings, "settings")
        with self._lock:
            self._commit_locked(self._items, new_settings)
            return self._snapshot_locked()

    def claim_next(self) -> QueueItem | None:
        """Atomically claim the first pending item and increment its attempts."""

        with self._lock:
            for item in self._items.values():
                if item.state is QueueState.PENDING:
                    return self._transition_locked(item.video_id, QueueState.DOWNLOADING)
            return None

    def transition(
        self,
        video_id: str,
        state: QueueState | str,
        *,
        output_path: str | os.PathLike[str] | None = None,
        error: str | None = None,
    ) -> QueueItem:
        """Persist a validated lifecycle transition for one video."""

        try:
            target = state if isinstance(state, QueueState) else QueueState(state)
        except (TypeError, ValueError) as exc:
            raise QueueValidationError(f"Unknown queue state: {state!r}") from exc
        with self._lock:
            return self._transition_locked(
                _validate_video_id(video_id),
                target,
                output_path=output_path,
                error=error,
            )

    def mark_downloading(self, video_id: str) -> QueueItem:
        return self.transition(video_id, QueueState.DOWNLOADING)

    def mark_transcoding(self, video_id: str) -> QueueItem:
        return self.transition(video_id, QueueState.TRANSCODING)

    def mark_done(
        self,
        video_id: str,
        output_path: str | os.PathLike[str],
    ) -> QueueItem:
        return self.transition(video_id, QueueState.DONE, output_path=output_path)

    def mark_failed(self, video_id: str, error: str) -> QueueItem:
        return self.transition(video_id, QueueState.FAILED, error=error)

    def mark_cancelled(self, video_id: str) -> QueueItem:
        return self.transition(video_id, QueueState.CANCELLED)

    def retry_failed(self, video_id: str | None = None) -> tuple[QueueItem, ...]:
        """Move one or all failed items to pending and return the retried entries."""

        with self._lock:
            if video_id is None:
                selected = [
                    item for item in self._items.values() if item.state is QueueState.FAILED
                ]
            else:
                item = self._require_item_locked(_validate_video_id(video_id))
                if item.state is not QueueState.FAILED:
                    raise InvalidQueueTransition(
                        f"Cannot retry {item.video_id} from state {item.state.value}"
                    )
                selected = [item]
            if not selected:
                return ()

            new_items = dict(self._items)
            retried: list[QueueItem] = []
            for item in selected:
                pending = replace(
                    item,
                    state=QueueState.PENDING,
                    output_path=None,
                    error=None,
                )
                new_items[item.video_id] = pending
                retried.append(pending)
            self._commit_locked(new_items, self._settings)
            return tuple(_clone_item(item) for item in retried)

    def resume_cancelled(self, video_id: str) -> QueueItem:
        """Move a deliberately cancelled item back to pending."""

        return self.transition(video_id, QueueState.PENDING)

    def clear_completed(self) -> int:
        """Remove done and cancelled entries, retaining pending and failed work."""

        with self._lock:
            new_items = {
                video_id: item
                for video_id, item in self._items.items()
                if item.state not in {QueueState.DONE, QueueState.CANCELLED}
            }
            removed = len(self._items) - len(new_items)
            if removed:
                self._commit_locked(new_items, self._settings)
            return removed

    def _transition_locked(
        self,
        video_id: str,
        target: QueueState,
        *,
        output_path: str | os.PathLike[str] | None = None,
        error: str | None = None,
    ) -> QueueItem:
        item = self._require_item_locked(video_id)
        if item.state is target:
            return _clone_item(item)
        if target not in _ALLOWED_TRANSITIONS[item.state]:
            raise InvalidQueueTransition(
                f"Cannot transition {video_id} from {item.state.value} to {target.value}"
            )

        attempts = item.attempts + 1 if target is QueueState.DOWNLOADING else item.attempts
        new_output: str | None = None
        new_error: str | None = None
        if target is QueueState.DONE:
            new_output = _optional_text(output_path)
            if new_output is None:
                raise QueueValidationError("mark_done requires a non-empty output_path")
        elif target is QueueState.FAILED:
            new_error = _optional_text(error)
            if new_error is None:
                raise QueueValidationError("mark_failed requires a non-empty error")
        elif output_path is not None or error is not None:
            raise QueueValidationError("output_path/error only apply to done or failed states")

        updated = replace(
            item,
            state=target,
            output_path=new_output,
            error=new_error,
            attempts=attempts,
        )
        new_items = dict(self._items)
        new_items[video_id] = updated
        self._commit_locked(new_items, self._settings)
        return _clone_item(updated)

    def _require_item_locked(self, video_id: str) -> QueueItem:
        try:
            return self._items[video_id]
        except KeyError as exc:
            raise QueueItemNotFoundError(video_id) from exc

    def _snapshot_locked(self) -> QueueSnapshot:
        return QueueSnapshot(
            items=tuple(_clone_item(item) for item in self._items.values()),
            settings=_copy_json_object(self._settings, "settings"),
            load_report=self._last_load_report,
        )

    def _load_locked(self) -> None:
        if not self.path.exists():
            self._items = {}
            self._settings = {}
            self._last_load_report = QueueLoadReport()
            self._writes_blocked_reason = None
            return

        try:
            if self.path.stat().st_size > _MAX_MANIFEST_BYTES:
                raise QueueValidationError("Queue manifest is unexpectedly large")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeError, QueueValidationError) as exc:
            self._recover_corrupt_locked(str(exc))
            return
        except OSError as exc:
            warning = f"Could not read queue manifest: {exc}"
            self._last_load_report = QueueLoadReport(warning=warning)
            self._writes_blocked_reason = warning
            return

        try:
            if not isinstance(payload, dict):
                raise QueueValidationError("Queue manifest root must be an object")
            if payload.get("version") != MANIFEST_VERSION:
                raise QueueValidationError("Unsupported queue manifest version")
            raw_settings = payload.get("settings", {})
            settings = _copy_json_object(raw_settings, "settings")
            raw_items = payload.get("items")
            if not isinstance(raw_items, list):
                raise QueueValidationError("Queue manifest items must be a list")
        except (QueueValidationError, TypeError) as exc:
            self._recover_corrupt_locked(str(exc))
            return

        items: dict[str, QueueItem] = {}
        invalid_count = 0
        active_count = 0
        for raw_item in raw_items:
            try:
                if not isinstance(raw_item, Mapping):
                    raise QueueValidationError("Queue item must be an object")
                item = _item_from_mapping(raw_item)
                if item.video_id in items:
                    raise QueueValidationError("Duplicate queue video_id")
            except (QueueValidationError, TypeError, ValueError):
                invalid_count += 1
                continue
            if item.state in ACTIVE_STATES:
                item = replace(
                    item,
                    state=QueueState.PENDING,
                    output_path=None,
                    error=None,
                )
                active_count += 1
            items[item.video_id] = item

        backup: Path | None = None
        warning: str | None = None
        if invalid_count:
            backup, warning = self._quarantine_locked(
                f"Skipped {invalid_count} invalid queue item(s)"
            )
            if backup is None:
                self._last_load_report = QueueLoadReport(
                    active_requeued=active_count,
                    invalid_items_skipped=invalid_count,
                    warning=warning,
                )
                self._writes_blocked_reason = warning or "Could not preserve corrupt manifest"
                return

        self._items = items
        self._settings = settings
        self._writes_blocked_reason = None
        self._last_load_report = QueueLoadReport(
            active_requeued=active_count,
            invalid_items_skipped=invalid_count,
            corrupt_backup=backup,
            warning=warning,
        )
        if active_count or invalid_count:
            try:
                self._atomic_write_locked(items, settings)
            except QueuePersistenceError as exc:
                self._last_load_report = replace(
                    self._last_load_report,
                    warning=f"Queue was recovered in memory but not persisted: {exc}",
                )

    def _recover_corrupt_locked(self, reason: str) -> None:
        backup, warning = self._quarantine_locked(reason)
        if backup is None:
            self._last_load_report = QueueLoadReport(warning=warning)
            self._writes_blocked_reason = warning or "Could not preserve corrupt manifest"
            return
        self._items = {}
        self._settings = {}
        self._writes_blocked_reason = None
        self._last_load_report = QueueLoadReport(
            corrupt_backup=backup,
            warning=f"Corrupt queue manifest was preserved: {reason}",
        )
        try:
            self._atomic_write_locked({}, {})
        except QueuePersistenceError as exc:
            self._last_load_report = replace(
                self._last_load_report,
                warning=f"Corrupt manifest was preserved but a clean one was not written: {exc}",
            )

    def _quarantine_locked(self, reason: str) -> tuple[Path | None, str | None]:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = self.path.with_name(f"{self.path.stem}.corrupt-{timestamp}{self.path.suffix}")
        try:
            os.replace(self.path, backup)
        except OSError as exc:
            return None, f"Could not preserve corrupt queue manifest ({reason}): {exc}"
        return backup, f"Queue manifest required repair: {reason}"

    def _commit_locked(
        self,
        items: Mapping[str, QueueItem],
        settings: Mapping[str, JsonValue],
    ) -> None:
        if self._writes_blocked_reason:
            raise QueuePersistenceError(self._writes_blocked_reason)
        self._atomic_write_locked(items, settings)
        self._items = dict(items)
        self._settings = _copy_json_object(settings, "settings")

    def _atomic_write_locked(
        self,
        items: Mapping[str, QueueItem],
        settings: Mapping[str, JsonValue],
    ) -> None:
        payload = {
            "version": MANIFEST_VERSION,
            "settings": _copy_json_object(settings, "settings"),
            "items": [_item_to_mapping(item) for item in items.values()],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        temporary_path: Path | None = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.directory,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                temporary_path.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary_path, self.path)
            temporary_path = None
            _fsync_directory(self.directory)
        except OSError as exc:
            raise QueuePersistenceError(f"Could not save queue manifest: {exc}") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass


def _normalise_states(
    states: Iterable[QueueState | str] | QueueState | str | None,
) -> frozenset[QueueState] | None:
    if states is None:
        return None
    values: Iterable[QueueState | str]
    if isinstance(states, (QueueState, str)):
        values = (states,)
    else:
        values = states
    try:
        return frozenset(
            value if isinstance(value, QueueState) else QueueState(value) for value in values
        )
    except (TypeError, ValueError) as exc:
        raise QueueValidationError("Unknown queue state filter") from exc


def _coerce_item(entry: QueueItem | QueueSeed | Mapping[str, object]) -> QueueItem:
    if isinstance(entry, QueueItem):
        return _clone_item(entry)
    if isinstance(entry, QueueSeed):
        return QueueItem(
            video_id=entry.video_id,
            url=entry.url,
            metadata=entry.metadata,
        )
    if isinstance(entry, Mapping):
        return _item_from_mapping(entry)
    raise QueueValidationError("Queue entries must be QueueItem, QueueSeed, or mappings")


def _item_from_mapping(raw: Mapping[str, object]) -> QueueItem:
    video_id = raw.get("video_id")
    if not isinstance(video_id, str):
        raise QueueValidationError("Queue item video_id must be a string")
    url = raw.get("url", "")
    if not isinstance(url, str):
        raise QueueValidationError("Queue item url must be a string")
    state = raw.get("state", QueueState.PENDING)
    output_path = raw.get("output_path")
    error = raw.get("error")
    attempts = raw.get("attempts", 0)
    metadata = raw.get("metadata", {})
    if output_path is not None and not isinstance(output_path, str):
        raise QueueValidationError("Queue item output_path must be a string or null")
    if error is not None and not isinstance(error, str):
        raise QueueValidationError("Queue item error must be a string or null")
    if not isinstance(attempts, int) or isinstance(attempts, bool):
        raise QueueValidationError("Queue item attempts must be an integer")
    if not isinstance(metadata, Mapping):
        raise QueueValidationError("Queue item metadata must be an object")
    return QueueItem(
        video_id=video_id,
        url=url,
        state=state,  # type: ignore[arg-type]
        output_path=output_path,
        error=error,
        attempts=attempts,
        metadata=_copy_json_object(metadata, "metadata"),
    )


def _item_to_mapping(item: QueueItem) -> dict[str, object]:
    return {
        "video_id": item.video_id,
        "url": item.url,
        "state": item.state.value,
        "output_path": item.output_path,
        "error": item.error,
        "attempts": item.attempts,
        "metadata": _copy_json_object(item.metadata, "metadata"),
    }


def _clone_item(item: QueueItem) -> QueueItem:
    return QueueItem(
        video_id=item.video_id,
        url=item.url,
        state=item.state,
        output_path=item.output_path,
        error=item.error,
        attempts=item.attempts,
        metadata=item.metadata,
    )


def _copy_json_object(value: object, name: str) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise QueueValidationError(f"{name} must be an object")
    copied = _copy_json_value(value, name=name, depth=0)
    if not isinstance(copied, dict):  # pragma: no cover - guaranteed by Mapping check
        raise QueueValidationError(f"{name} must be an object")
    return copied


def _copy_json_value(value: object, *, name: str, depth: int) -> JsonValue:
    if depth > 32:
        raise QueueValidationError(f"{name} is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise QueueValidationError(f"{name} cannot contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, JsonValue] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise QueueValidationError(f"{name} keys must be strings")
            copied[key] = _copy_json_value(child, name=name, depth=depth + 1)
        return copied
    if isinstance(value, (list, tuple)):
        return [
            _copy_json_value(child, name=name, depth=depth + 1) for child in value
        ]
    raise QueueValidationError(f"{name} contains a non-JSON value: {type(value).__name__}")


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = [
    "ACTIVE_STATES",
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "TERMINAL_STATES",
    "CanonicalVideo",
    "InvalidQueueTransition",
    "QueueItem",
    "QueueItemNotFoundError",
    "QueueLoadReport",
    "QueuePersistenceError",
    "QueueSeed",
    "QueueSnapshot",
    "QueueState",
    "QueueStore",
    "QueueStoreError",
    "QueueValidationError",
    "canonicalize_youtube_video",
]
