from __future__ import annotations

import hashlib
import math
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .media import ffmpeg_executable, hidden_process_kwargs, probe_media
from .models import Candidate, DownloadIntent
from .ranking import rank_candidates
from .settings import prepare_output_directory, safe_filename, unique_path

EventCallback = Callable[[dict[str, Any]], None]

NATIVE_H264_FORMAT = "bv[vcodec^=avc1]+ba[acodec^=mp4a]/b[vcodec^=avc1][acodec^=mp4a]"
TRUE_MAX_FORMAT = "bv+ba/b"
_SOURCE_SIDECAR_SUFFIXES = frozenset(
    {".part", ".ytdl", ".json", ".jpg", ".jpeg", ".webp"}
)

HDR_TO_SDR_FILTER = (
    "zscale=transfer=linear:npl=100,format=gbrpf32le,"
    "zscale=primaries=bt709,tonemap=tonemap=mobius:desat=0,"
    "zscale=transfer=bt709:matrix=bt709:range=limited,format=yuv420p"
)


class DownloadEngineError(RuntimeError):
    pass


class JobCancelled(DownloadEngineError):
    pass


class _QuietLogger:
    def __init__(self, emit: EventCallback) -> None:
        self.emit = emit

    def debug(self, message: str) -> None:
        if message.startswith("[download] Destination"):
            self.emit({"kind": "log", "message": message})

    def warning(self, message: str) -> None:
        self.emit({"kind": "log", "level": "warning", "message": message})

    def error(self, message: str) -> None:
        self.emit({"kind": "log", "level": "error", "message": message})


def _human_bytes_per_second(value: float | int | None) -> str:
    if not value:
        return ""
    units = ["Б/с", "КБ/с", "МБ/с", "ГБ/с"]
    number = float(value)
    unit = units[0]
    for unit in units:
        if number < 1024 or unit == units[-1]:
            break
        number /= 1024
    return f"{number:.1f} {unit}"


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _selected_video_is_hdr(info: dict[str, Any]) -> bool:
    values: list[object] = [info.get("dynamic_range")]
    for key in ("requested_formats", "requested_downloads"):
        items = info.get(key) or []
        if isinstance(items, list):
            values.extend(item.get("dynamic_range") for item in items if isinstance(item, dict))
    known = {str(value).strip().upper() for value in values if value}
    return any(value not in {"SDR", "NONE", "UNKNOWN"} for value in known)


def _valid_http_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    url = value.strip()
    return url if url.startswith(("http://", "https://")) else ""


def _thumbnail_from_entry(entry: dict[str, Any]) -> str:
    direct = _valid_http_url(entry.get("thumbnail"))
    if direct:
        return direct

    choices: list[tuple[float, float, int, str]] = []
    thumbnails = entry.get("thumbnails")
    if not isinstance(thumbnails, list):
        return ""
    for order, item in enumerate(thumbnails):
        if not isinstance(item, dict):
            continue
        url = _valid_http_url(item.get("url"))
        if not url:
            continue
        width = _optional_float(item.get("width")) or 0.0
        height = _optional_float(item.get("height")) or 0.0
        preference = _optional_float(item.get("preference")) or 0.0
        choices.append((preference, width * height, order, url))
    return max(choices)[-1] if choices else ""


def _candidate_from_entry(
    entry: dict[str, Any],
    rank: int,
    *,
    fallback_thumbnail_url: str = "",
) -> Candidate | None:
    video_id = str(entry.get("id") or "").strip()
    if not video_id:
        return None
    url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
    if not url or not str(url).startswith(("http://", "https://")):
        url = f"https://www.youtube.com/watch?v={video_id}"
    published = _optional_float(entry.get("timestamp") or entry.get("release_timestamp"))
    if published is None and entry.get("upload_date"):
        try:
            published = time.mktime(time.strptime(str(entry["upload_date"]), "%Y%m%d"))
        except (ValueError, OverflowError, OSError):
            published = None
    return Candidate(
        video_id=video_id,
        title=str(entry.get("title") or video_id),
        url=str(url),
        thumbnail_url=_thumbnail_from_entry(entry) or fallback_thumbnail_url,
        channel=str(entry.get("channel") or entry.get("uploader") or ""),
        duration_seconds=_optional_int(entry.get("duration")),
        view_count=_optional_int(entry.get("view_count")),
        like_count=_optional_int(entry.get("like_count")),
        published_at=published,
        relevance_score=max(0.0, 1.0 - (rank / 100.0)),
        query_rank=rank,
    )


def _validated_url_list(urls: Iterable[str], *, empty_message: str) -> list[str]:
    raw_urls: list[str] = []
    for raw_url in urls:
        url = str(raw_url).strip()
        if url:
            raw_urls.append(url)
    if not raw_urls:
        raise DownloadEngineError(empty_message)
    try:
        validated = DownloadIntent(urls=tuple(raw_urls), count=1)
    except ValueError as exc:
        raise DownloadEngineError(str(exc)) from exc
    return list(validated.urls)


def _source_path_from_info(info: dict[str, Any], temp_dir: Path) -> Path:
    """Return the exact media path produced by yt-dlp for this invocation."""
    temp_root = temp_dir.resolve()
    reported_paths: list[object] = [info.get("filepath"), info.get("_filename")]
    requested_downloads = info.get("requested_downloads")
    if isinstance(requested_downloads, list):
        reported_paths.extend(
            item.get("filepath") for item in requested_downloads if isinstance(item, dict)
        )

    for value in reported_paths:
        if not isinstance(value, (str, os.PathLike)) or not str(value):
            continue
        candidate = Path(value)
        try:
            candidate.resolve().relative_to(temp_root)
        except (OSError, ValueError):
            continue
        if candidate.is_file() and candidate.suffix.lower() not in _SOURCE_SIDECAR_SUFFIXES:
            return candidate

    fallback = [
        path
        for path in temp_dir.glob("source.*")
        if path.is_file() and path.suffix.lower() not in _SOURCE_SIDECAR_SUFFIXES
    ]
    if len(fallback) == 1:
        return fallback[0]
    if fallback:
        raise DownloadEngineError(
            "yt-dlp не сообщил итоговый исходник, а во временной папке найдено несколько "
            f"файлов; удалите папку и повторите: {temp_dir}"
        )
    raise DownloadEngineError(f"Исходный файл не найден; временная папка: {temp_dir}")


class YouTubeEngine:
    def __init__(
        self,
        *,
        output_dir: str = "",
        quality_mode: str = "native_h264",
        cookie_browser: str = "",
        emit: EventCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        if quality_mode not in {"native_h264", "true_max_h264"}:
            raise ValueError(f"Неизвестный режим качества: {quality_mode}")
        self.output_dir = prepare_output_directory(output_dir) if output_dir else None
        self.quality_mode = quality_mode
        self.cookie_browser = cookie_browser
        self.emit = emit or (lambda event: None)
        self.cancel_event = cancel_event or threading.Event()
        self._ffmpeg: str | None = None
        self._current_item = 0
        self._total_items = 0

    @staticmethod
    def _yt_dlp() -> Any:
        try:
            import yt_dlp  # type: ignore[import-not-found]

            return yt_dlp
        except ImportError as exc:
            raise DownloadEngineError(
                "yt-dlp не установлен. Запустите приложение через START_MMM_DOWNLOADER.bat."
            ) from exc

    @property
    def ffmpeg(self) -> str:
        if self._ffmpeg is None:
            self._ffmpeg = ffmpeg_executable()
        return self._ffmpeg

    def _require_output_dir(self) -> Path:
        if self.output_dir is None:
            raise DownloadEngineError("Выберите папку для сохранения")
        return self.output_dir

    def _common_options(self, *, require_ffmpeg: bool = False) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "logger": _QuietLogger(self.emit),
            "socket_timeout": 30,
            "retries": 5,
            "fragment_retries": 5,
            "file_access_retries": 3,
            "noplaylist": True,
        }
        if require_ffmpeg:
            options["ffmpeg_location"] = self.ffmpeg
        if self.cookie_browser:
            options["cookiesfrombrowser"] = (self.cookie_browser,)
        return options

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled("Операция отменена")

    def search(self, intent: DownloadIntent) -> list[Candidate]:
        if not intent.topic:
            return []
        self._check_cancelled()
        pool_size = min(100, max(30, intent.count * 5))
        self.emit(
            {
                "kind": "phase",
                "message": f"Ищу кандидатов: {intent.topic}",
                "phase": "search",
            }
        )
        options = self._common_options()
        options.update({"extract_flat": "in_playlist", "skip_download": True})
        yt_dlp = self._yt_dlp()
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                result = ydl.extract_info(f"ytsearch{pool_size}:{intent.topic}", download=False)
        except Exception as exc:
            raise DownloadEngineError(f"Поиск YouTube не удался: {exc}") from exc
        if not isinstance(result, dict):
            raise DownloadEngineError("YouTube вернул результат поиска неизвестного формата")

        candidates: list[Candidate] = []
        seen: set[str] = set()
        unresolved: list[tuple[int, Candidate]] = []
        for rank, entry in enumerate((result or {}).get("entries") or []):
            self._check_cancelled()
            if not isinstance(entry, dict):
                continue
            if entry.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
                continue
            candidate = _candidate_from_entry(entry, rank)
            if candidate is None or candidate.video_id in seen:
                continue
            seen.add(candidate.video_id)
            needs_duration = candidate.duration_seconds is None and (
                intent.min_duration_seconds is not None or intent.max_duration_seconds is not None
            )
            needs_views = intent.ranking == "views" and candidate.view_count is None
            needs_date = intent.ranking == "newest" and candidate.published_at is None
            if needs_duration or needs_views or needs_date:
                unresolved.append((rank, candidate))
                continue
            candidates.append(candidate)

        if unresolved:
            detail_options = self._common_options()
            detail_options["skip_download"] = True
            with yt_dlp.YoutubeDL(detail_options) as ydl:
                for position, (rank, candidate) in enumerate(unresolved, start=1):
                    self._check_cancelled()
                    self.emit(
                        {
                            "kind": "phase",
                            "phase": "search",
                            "message": (f"Уточняю метаданные: {position} из {len(unresolved)}"),
                        }
                    )
                    try:
                        details = ydl.extract_info(candidate.watch_url, download=False)
                    except Exception as exc:
                        self.emit(
                            {
                                "kind": "log",
                                "level": "warning",
                                "message": (
                                    f"Пропускаю {candidate.title}: "
                                    f"не удалось уточнить метаданные ({exc})"
                                ),
                            }
                        )
                        continue
                    if not isinstance(details, dict) or details.get("live_status") in {
                        "is_live",
                        "is_upcoming",
                        "post_live",
                    }:
                        continue
                    resolved = _candidate_from_entry(
                        details,
                        rank,
                        fallback_thumbnail_url=candidate.thumbnail_url,
                    )
                    if resolved is not None:
                        candidates.append(resolved)

        eligible: list[Candidate] = []
        for candidate in candidates:
            duration = candidate.duration_seconds
            if intent.min_duration_seconds is not None and (
                duration is None or duration < intent.min_duration_seconds
            ):
                continue
            if intent.max_duration_seconds is not None and (
                duration is None or duration > intent.max_duration_seconds
            ):
                continue
            eligible.append(candidate)

        ranked = rank_candidates(eligible, mode=intent.ranking)
        selected = ranked[: intent.count]
        self.emit(
            {
                "kind": "candidates",
                "items": selected,
                "message": f"Подходят {len(selected)} из запрошенных {intent.count}",
            }
        )
        return selected

    def inspect_urls(self, urls: Iterable[str]) -> list[Candidate]:
        """Resolve explicit YouTube URLs into preview candidates.

        URLs are inspected independently so one unavailable video does not
        discard metadata already obtained for the rest of the batch.  The
        successful candidates retain input order and the first occurrence of
        each resolved video ID wins.
        """

        url_list = _validated_url_list(urls, empty_message="Нет ссылок для предпросмотра")
        self._check_cancelled()
        total = len(url_list)
        options = self._common_options()
        options["skip_download"] = True
        yt_dlp = self._yt_dlp()

        candidates: list[Candidate] = []
        seen_video_ids: set[str] = set()
        failed = 0
        skipped = 0

        try:
            context = yt_dlp.YoutubeDL(options)
            with context as ydl:
                for index, url in enumerate(url_list, start=1):
                    self._check_cancelled()
                    self.emit(
                        {
                            "kind": "phase",
                            "phase": "inspect",
                            "item": index,
                            "total": total,
                            "url": url,
                            "message": f"Проверяю ссылку {index} из {total}",
                        }
                    )
                    self._check_cancelled()
                    try:
                        details = ydl.extract_info(url, download=False)
                    except JobCancelled:
                        raise
                    except Exception as exc:
                        failed += 1
                        self.emit(
                            {
                                "kind": "log",
                                "level": "error",
                                "phase": "inspect",
                                "reason": "extract",
                                "item": index,
                                "total": total,
                                "url": url,
                                "message": f"Не удалось проверить ссылку {index}: {exc}",
                            }
                        )
                        continue

                    # Metadata extraction is a blocking yt-dlp call.  Checking
                    # immediately afterwards ensures a concurrently requested
                    # cancellation never leaks a stale candidate into the UI.
                    self._check_cancelled()
                    if not isinstance(details, dict):
                        skipped += 1
                        self.emit(
                            {
                                "kind": "log",
                                "level": "warning",
                                "phase": "inspect",
                                "reason": "invalid_metadata",
                                "item": index,
                                "total": total,
                                "url": url,
                                "message": (
                                    f"Ссылка {index} пропущена: YouTube не вернул "
                                    "метаданные видео"
                                ),
                            }
                        )
                        continue

                    if details.get("entries") is not None or details.get("_type") in {
                        "playlist",
                        "multi_video",
                    }:
                        skipped += 1
                        self.emit(
                            {
                                "kind": "log",
                                "level": "warning",
                                "phase": "inspect",
                                "reason": "collection",
                                "item": index,
                                "total": total,
                                "url": url,
                                "message": (
                                    f"Ссылка {index} ведёт на подборку; вставьте ссылку "
                                    "на отдельное видео"
                                ),
                            }
                        )
                        continue

                    if details.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
                        skipped += 1
                        self.emit(
                            {
                                "kind": "log",
                                "level": "warning",
                                "phase": "inspect",
                                "reason": "live",
                                "item": index,
                                "total": total,
                                "url": url,
                                "message": (
                                    f"Ссылка {index} пропущена: прямые трансляции "
                                    "и премьеры не поддерживаются"
                                ),
                            }
                        )
                        continue

                    try:
                        candidate = _candidate_from_entry(details, index - 1)
                    except (TypeError, ValueError) as exc:
                        candidate = None
                        metadata_error = str(exc)
                    else:
                        metadata_error = "не найден идентификатор видео"
                    if candidate is None:
                        skipped += 1
                        self.emit(
                            {
                                "kind": "log",
                                "level": "warning",
                                "phase": "inspect",
                                "reason": "invalid_metadata",
                                "item": index,
                                "total": total,
                                "url": url,
                                "message": f"Ссылка {index} пропущена: {metadata_error}",
                            }
                        )
                        continue
                    if candidate.video_id in seen_video_ids:
                        skipped += 1
                        self.emit(
                            {
                                "kind": "log",
                                "level": "warning",
                                "phase": "inspect",
                                "reason": "duplicate",
                                "item": index,
                                "total": total,
                                "url": url,
                                "video_id": candidate.video_id,
                                "message": (
                                    f"Ссылка {index} пропущена: видео уже есть "
                                    "в предпросмотре"
                                ),
                            }
                        )
                        continue

                    seen_video_ids.add(candidate.video_id)
                    candidates.append(candidate)
        except JobCancelled:
            raise
        except Exception as exc:
            raise DownloadEngineError(f"Не удалось подготовить предпросмотр: {exc}") from exc

        self.emit(
            {
                "kind": "candidates",
                "source": "urls",
                "items": candidates,
                "total": total,
                "failed": failed,
                "skipped": skipped,
                "message": f"Получены данные: {len(candidates)} из {total}",
            }
        )
        return candidates

    def download_urls(self, urls: Iterable[str]) -> list[Path]:
        self._require_output_dir()
        url_list = _validated_url_list(urls, empty_message="Нет ссылок для скачивания")
        self._total_items = len(url_list)
        completed: list[Path] = []
        errors: list[str] = []
        for index, url in enumerate(url_list, start=1):
            self._check_cancelled()
            self._current_item = index
            self.emit(
                {
                    "kind": "item_start",
                    "item": index,
                    "total": self._total_items,
                    "message": f"Видео {index} из {self._total_items}",
                }
            )
            try:
                if self.quality_mode == "native_h264":
                    path = self._download_native_h264(url)
                else:
                    path = self._download_true_max_h264(url)
                completed.append(path)
                self.emit(
                    {
                        "kind": "item_done",
                        "item": index,
                        "total": self._total_items,
                        "path": str(path),
                        "message": f"Готово: {path.name}",
                    }
                )
            except JobCancelled:
                raise
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                self.emit(
                    {
                        "kind": "item_error",
                        "item": index,
                        "total": self._total_items,
                        "message": str(exc),
                    }
                )
        if errors and not completed:
            raise DownloadEngineError("\n".join(errors))
        self.emit(
            {
                "kind": "batch_done",
                "paths": [str(path) for path in completed],
                "failed": len(errors),
                "message": f"Скачано: {len(completed)}, ошибок: {len(errors)}",
            }
        )
        return completed

    def _progress_hook(self, data: dict[str, Any]) -> None:
        self._check_cancelled()
        if data.get("status") != "downloading":
            return
        downloaded = data.get("downloaded_bytes") or 0
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        percent = (float(downloaded) / float(total) * 100.0) if total else None
        speed = _human_bytes_per_second(data.get("speed"))
        eta = data.get("eta")
        parts = []
        if percent is not None:
            parts.append(f"{percent:.1f}%")
        if speed:
            parts.append(speed)
        if eta is not None:
            parts.append(f"осталось ~{int(eta)} с")
        self.emit(
            {
                "kind": "progress",
                "phase": "download",
                "item": self._current_item,
                "total": self._total_items,
                "percent": percent,
                "message": " · ".join(parts) or "Скачивание…",
            }
        )

    def _download_native_h264(self, url: str) -> Path:
        output_dir = self._require_output_dir()
        options = self._common_options(require_ffmpeg=True)
        before = {path.resolve() for path in output_dir.glob("*.mp4")}
        options.update(
            {
                "format": NATIVE_H264_FORMAT,
                "merge_output_format": "mp4",
                "outtmpl": str(output_dir / "%(title).160B [%(id)s].%(ext)s"),
                "windowsfilenames": True,
                "continuedl": True,
                "overwrites": False,
                "concurrent_fragment_downloads": 4,
                "progress_hooks": [self._progress_hook],
            }
        )
        yt_dlp = self._yt_dlp()
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
        except JobCancelled:
            raise
        except Exception as exc:
            raise DownloadEngineError(
                "Не удалось получить строгую пару H.264 + AAC. "
                "Попробуйте режим «Абсолютный максимум → H.264» или cookies. "
                f"Детали: {exc}"
            ) from exc

        video_id = str((info or {}).get("id") or "")
        matching = sorted(
            (
                path
                for path in output_dir.glob("*.mp4")
                if path.resolve() not in before or (video_id and f"[{video_id}]" in path.name)
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not matching:
            raise DownloadEngineError("yt-dlp завершился, но итоговый MP4 не найден")
        result = matching[0]
        media = probe_media(result, self.ffmpeg)
        if media.video_codec != "h264" or media.audio_codec != "aac":
            raise DownloadEngineError(
                "Проверка кодеков не пройдена: "
                f"video={media.video_codec or 'unknown'}, "
                f"audio={media.audio_codec or 'unknown'}"
            )
        return result

    def _download_true_max_h264(self, url: str) -> Path:
        output_dir = self._require_output_dir()
        token = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        temp_dir = output_dir / ".mmm-downloader-temp" / token
        temp_dir.mkdir(parents=True, exist_ok=True)
        options = self._common_options(require_ffmpeg=True)
        options.update(
            {
                "format": TRUE_MAX_FORMAT,
                "merge_output_format": "mkv",
                "outtmpl": str(temp_dir / "source.%(ext)s"),
                "continuedl": True,
                "overwrites": False,
                "concurrent_fragment_downloads": 4,
                "progress_hooks": [self._progress_hook],
            }
        )
        yt_dlp = self._yt_dlp()
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True) or {}
        except JobCancelled:
            raise
        except Exception as exc:
            raise DownloadEngineError(f"Скачивание исходника не удалось: {exc}") from exc

        source = _source_path_from_info(info, temp_dir)
        title = str(info.get("title") or info.get("id") or "video")
        video_id = str(info.get("id") or token)
        final_path = unique_path(output_dir, safe_filename(title, video_id))
        converted = temp_dir / "converted.mp4"
        self._transcode(
            source,
            converted,
            float(info.get("duration") or 0),
            hdr_to_sdr=_selected_video_is_hdr(info),
        )
        media = probe_media(converted, self.ffmpeg)
        if media.video_codec != "h264" or media.audio_codec not in {"aac", None}:
            raise DownloadEngineError(
                "Проверка перекодированного файла не пройдена; "
                f"временная папка сохранена: {temp_dir}"
            )
        os.replace(converted, final_path)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return final_path

    def _transcode(
        self,
        source: Path,
        destination: Path,
        duration: float,
        *,
        hdr_to_sdr: bool = False,
    ) -> None:
        self.emit(
            {
                "kind": "phase",
                "phase": "transcode",
                "message": "Перекодирую в H.264/AAC — это может занять время…",
            }
        )
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
        ]
        if hdr_to_sdr:
            command.extend(["-vf", HDR_TO_SDR_FILTER])
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "19",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                "-progress",
                "pipe:1",
                "-nostats",
                str(destination),
            ]
        )
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_process_kwargs(),
        )
        watcher_stop = threading.Event()

        def cancel_watcher() -> None:
            while not watcher_stop.wait(0.2):
                if self.cancel_event.is_set():
                    if process.poll() is None:
                        process.terminate()
                    return

        threading.Thread(target=cancel_watcher, daemon=True).start()
        output_lines: list[str] = []
        assert process.stdout is not None
        try:
            for raw_line in process.stdout:
                self._check_cancelled()
                line = raw_line.strip()
                output_lines.append(line)
                if len(output_lines) > 30:
                    output_lines.pop(0)
                if duration and line.startswith(("out_time_ms=", "out_time_us=")):
                    try:
                        micros = int(line.split("=", 1)[1])
                        percent = min(100.0, micros / 1_000_000 / duration * 100)
                    except ValueError:
                        continue
                    self.emit(
                        {
                            "kind": "progress",
                            "phase": "transcode",
                            "item": self._current_item,
                            "total": self._total_items,
                            "percent": percent,
                            "message": f"Перекодирование: {percent:.1f}%",
                        }
                    )
            return_code = process.wait()
        except JobCancelled:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise
        finally:
            watcher_stop.set()
        self._check_cancelled()
        if return_code != 0:
            details = "\n".join(output_lines[-10:])
            raise DownloadEngineError(f"FFmpeg завершился с ошибкой:\n{details}")
