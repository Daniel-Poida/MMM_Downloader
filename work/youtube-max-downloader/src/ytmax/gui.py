from __future__ import annotations

import os
import queue
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QObject,
    QRect,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QIcon,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .ai_intent import (
    AIIntentError,
    ApiKeyCheck,
    parse_intent_with_ai,
    verify_api_key,
)
from .api_keys import (
    ApiKeyStore,
    ApiKeyStoreError,
    normalize_secret,
    provider_for_secret,
)
from .downloader import JobCancelled, YouTubeEngine
from .intent import IntentParseError, parse_intent
from .models import Candidate, DownloadIntent
from .queue_store import QueueItem, QueueState, QueueStore, QueueStoreError
from .settings import (
    DEFAULT_PROVIDER,
    ApiKeyProfile,
    AppSettings,
    ai_provider,
    load_settings,
    save_settings,
)

QUALITY_LABELS = {
    "Лучший нативный H.264 (быстро)": "native_h264",
    "Абсолютный максимум → H.264 (медленно)": "true_max_h264",
}
COOKIE_LABELS = {
    "Без cookies": "",
    "Chrome": "chrome",
    "Microsoft Edge": "edge",
    "Firefox": "firefox",
}

_STATUS_COLORS = {
    "muted": QColor("#9E9CA6"),
    "working": QColor("#E960A8"),
    "success": QColor("#63D69A"),
    "error": QColor("#FF6675"),
}


def _resource_path(relative_path: str) -> Path:
    """Resolve project assets both from source and a PyInstaller bundle."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / relative_path
    return Path(__file__).resolve().parents[2] / relative_path


def _load_stylesheet() -> str:
    try:
        stylesheet = _resource_path("assets/mmm_downloader.qss").read_text(encoding="utf-8")
        asset_dir = _resource_path("assets").resolve().as_posix()
        return stylesheet.replace("__ASSET_DIR__", asset_dir)
    except OSError:
        return "QWidget#AppRoot { background: #09090B; color: #F5F4F6; }"


def _duration(value: int | None) -> str:
    if value is None:
        return "—"
    hours, remainder = divmod(int(value), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _views(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value:,}".replace(",", " ")


def _queue_metadata(candidate: Candidate) -> dict[str, object]:
    published_at = candidate.published_at
    if hasattr(published_at, "isoformat"):
        published_at = published_at.isoformat()  # type: ignore[union-attr]
    elif published_at is not None and not isinstance(
        published_at, (str, int, float)
    ):
        published_at = str(published_at)
    return {
        "title": candidate.title,
        "thumbnail_url": candidate.thumbnail_url,
        "channel": candidate.channel,
        "duration_seconds": candidate.duration_seconds,
        "view_count": candidate.view_count,
        "like_count": candidate.like_count,
        "published_at": published_at,
        "relevance_score": candidate.relevance_score,
        "query_rank": candidate.query_rank,
    }


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _candidate_from_queue_item(item: QueueItem) -> Candidate:
    metadata = item.metadata
    title = metadata.get("title")
    channel = metadata.get("channel")
    thumbnail_url = metadata.get("thumbnail_url")
    published_at = metadata.get("published_at")
    if not isinstance(published_at, (str, int, float)):
        published_at = None
    return Candidate(
        video_id=item.video_id,
        title=title if isinstance(title, str) and title.strip() else f"Видео {item.video_id}",
        url=item.url,
        thumbnail_url=thumbnail_url if isinstance(thumbnail_url, str) else "",
        channel=channel if isinstance(channel, str) else None,
        duration_seconds=_optional_int(metadata.get("duration_seconds")),
        view_count=_optional_int(metadata.get("view_count")),
        like_count=_optional_int(metadata.get("like_count")),
        published_at=published_at,
        relevance_score=_optional_float(metadata.get("relevance_score")),
        query_rank=_optional_int(metadata.get("query_rank")),
    )


def _card() -> QFrame:
    card = QFrame()
    card.setProperty("card", True)
    return card


def _model_fits(provider: str, model: str) -> bool:
    """Похож ли идентификатор на модель этого провайдера.

    У OpenRouter идентификатор всегда вида «автор/модель», у OpenAI — без
    косой черты. Этого хватает, чтобы не затирать свою модель пользователя и
    не тащить чужую при смене ключа.
    """
    if not model:
        return False
    return ("/" in model) if provider == "openrouter" else ("/" not in model)


def _label(text: str, *, object_name: str = "", muted: bool = False) -> QLabel:
    label = QLabel(text)
    if object_name:
        label.setObjectName(object_name)
    if muted:
        label.setProperty("muted", True)
    return label


class CandidateTableModel(QAbstractTableModel):
    checked_changed = Signal(int, int)
    PREVIEW_COLUMN = 0
    DURATION_COLUMN = 1
    VIEWS_COLUMN = 2
    CHANNEL_COLUMN = 3
    STATUS_COLUMN = 4
    LINK_ROLE = int(Qt.ItemDataRole.UserRole) + 1
    THUMBNAIL_URL_ROLE = int(Qt.ItemDataRole.UserRole) + 2
    HEADERS = ("Превью, видео и ссылка", "Длительность", "Просмотры", "Канал", "Статус")

    def __init__(self) -> None:
        super().__init__()
        self.items: list[Candidate] = []
        self.statuses: dict[str, str] = {}
        self.thumbnails: dict[str, QPixmap] = {}
        self.checked_ids: set[str] = set()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(self.HEADERS)
        ):
            return self.HEADERS[section]
        return None

    def data(
        self,
        index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object | None:
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        candidate = self.items[index.row()]
        column = index.column()
        status = self.statuses.get(candidate.video_id, "Найдено")

        if role == Qt.ItemDataRole.DisplayRole:
            values = (
                candidate.title,
                _duration(candidate.duration_seconds),
                _views(candidate.view_count),
                candidate.channel or "—",
                status,
            )
            return values[column]
        if role == Qt.ItemDataRole.CheckStateRole and column == self.PREVIEW_COLUMN:
            return (
                Qt.CheckState.Checked
                if candidate.video_id in self.checked_ids
                else Qt.CheckState.Unchecked
            )
        if role == self.LINK_ROLE and column == self.PREVIEW_COLUMN:
            return candidate.watch_url
        if role == self.THUMBNAIL_URL_ROLE and column == self.PREVIEW_COLUMN:
            return candidate.preview_image_url
        if role == Qt.ItemDataRole.DecorationRole and column == self.PREVIEW_COLUMN:
            return self.thumbnails.get(candidate.video_id)
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == self.PREVIEW_COLUMN:
                return f"{candidate.title}\n{candidate.watch_url}"
            if column == self.STATUS_COLUMN:
                return status
        if role == Qt.ItemDataRole.AccessibleTextRole and column == self.PREVIEW_COLUMN:
            return f"{candidate.title}, {candidate.watch_url}"
        if role == Qt.ItemDataRole.TextAlignmentRole and column in {
            self.DURATION_COLUMN,
            self.VIEWS_COLUMN,
        }:
            return Qt.AlignmentFlag.AlignCenter
        if role == Qt.ItemDataRole.ForegroundRole:
            if column == self.STATUS_COLUMN:
                if status.startswith("Готово"):
                    return QBrush(_STATUS_COLORS["success"])
                if status.startswith("Ошибка"):
                    return QBrush(_STATUS_COLORS["error"])
                if status.startswith(("Скачивание", "Конвертация", "Подготовка")):
                    return QBrush(_STATUS_COLORS["working"])
            if column in {
                self.DURATION_COLUMN,
                self.VIEWS_COLUMN,
                self.CHANNEL_COLUMN,
            }:
                return QBrush(_STATUS_COLORS["muted"])
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:  # noqa: N802
        flags = super().flags(index)
        if index.isValid() and index.column() == self.PREVIEW_COLUMN:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(  # noqa: N802
        self,
        index: QModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if (
            not index.isValid()
            or index.column() != self.PREVIEW_COLUMN
            or role != Qt.ItemDataRole.CheckStateRole
            or not 0 <= index.row() < len(self.items)
        ):
            return False
        candidate = self.items[index.row()]
        checked = value in {Qt.CheckState.Checked, Qt.CheckState.Checked.value}
        changed = checked != (candidate.video_id in self.checked_ids)
        if not changed:
            return False
        if checked:
            self.checked_ids.add(candidate.video_id)
        else:
            self.checked_ids.discard(candidate.video_id)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        self.checked_changed.emit(len(self.checked_ids), len(self.items))
        return True

    def set_candidates(
        self,
        items: list[Candidate],
        *,
        checked_ids: set[str] | None = None,
    ) -> None:
        self.beginResetModel()
        self.items = list(items)
        self.statuses = {item.video_id: "Найдено" for item in self.items}
        active_ids = {item.video_id for item in self.items}
        self.checked_ids = (
            active_ids
            if checked_ids is None
            else active_ids.intersection(checked_ids)
        )
        self.thumbnails = {
            video_id: pixmap
            for video_id, pixmap in self.thumbnails.items()
            if video_id in active_ids
        }
        self.endResetModel()
        self.checked_changed.emit(len(self.checked_ids), len(self.items))

    def set_all_checked(self, checked: bool) -> None:
        next_ids = {item.video_id for item in self.items} if checked else set()
        if next_ids == self.checked_ids:
            return
        self.checked_ids = next_ids
        if self.items:
            self.dataChanged.emit(
                self.index(0, self.PREVIEW_COLUMN),
                self.index(len(self.items) - 1, self.PREVIEW_COLUMN),
                [Qt.ItemDataRole.CheckStateRole],
            )
        self.checked_changed.emit(len(self.checked_ids), len(self.items))

    def checked_candidates(self) -> list[Candidate]:
        return [item for item in self.items if item.video_id in self.checked_ids]

    def set_thumbnail(self, video_id: str, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            return
        self.thumbnails[video_id] = pixmap
        for row, candidate in enumerate(self.items):
            if candidate.video_id != video_id:
                continue
            cell = self.index(row, self.PREVIEW_COLUMN)
            self.dataChanged.emit(cell, cell, [Qt.ItemDataRole.DecorationRole])
            break

    def set_item_status(self, item_number: int | None, status: str) -> None:
        if item_number is None:
            return
        row = item_number - 1
        if not 0 <= row < len(self.items):
            return
        candidate = self.items[row]
        self.statuses[candidate.video_id] = status
        cell = self.index(row, self.STATUS_COLUMN)
        self.dataChanged.emit(cell, cell, [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ForegroundRole])

    def set_candidate_status(self, video_id: str, status: str) -> None:
        for row, candidate in enumerate(self.items):
            if candidate.video_id != video_id:
                continue
            self.statuses[video_id] = status
            cell = self.index(row, self.STATUS_COLUMN)
            self.dataChanged.emit(
                cell,
                cell,
                [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ForegroundRole],
            )
            return


class VideoPreviewDelegate(QStyledItemDelegate):
    link_activated = Signal(int)
    THUMBNAIL_SIZE = QSize(112, 63)
    ROW_HEIGHT = 86
    CHECKBOX_SIZE = 20
    CHECKBOX_GAP = 10

    @classmethod
    def _content_rects(cls, rect: QRect) -> tuple[QRect, QRect, QRect, QRect]:
        content = rect.adjusted(9, 7, -9, -7)
        checkbox_rect = QRect(
            content.left(),
            content.top() + max(0, (content.height() - cls.CHECKBOX_SIZE) // 2),
            cls.CHECKBOX_SIZE,
            cls.CHECKBOX_SIZE,
        )
        thumbnail_rect = QRect(
            checkbox_rect.right() + cls.CHECKBOX_GAP,
            content.top() + max(0, (content.height() - cls.THUMBNAIL_SIZE.height()) // 2),
            cls.THUMBNAIL_SIZE.width(),
            cls.THUMBNAIL_SIZE.height(),
        )
        text_left = thumbnail_rect.right() + 13
        text_width = max(0, content.right() - text_left)
        title_rect = QRect(text_left, content.top() + 9, text_width, 24)
        link_rect = QRect(text_left, content.bottom() - 28, text_width, 22)
        return checkbox_rect, thumbnail_rect, title_rect, link_rect

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        base = QStyleOptionViewItem(option)
        self.initStyleOption(base, index)
        base.text = ""
        base.icon = QIcon()
        base.features &= ~QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        style = base.widget.style() if base.widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, base, painter, base.widget)

        checkbox_rect, thumbnail_rect, title_rect, link_rect = self._content_rects(option.rect)
        pixmap = index.data(Qt.ItemDataRole.DecorationRole)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        checked = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        painter.setPen(QPen(QColor("#E960A8") if checked else QColor("#55535E"), 1))
        painter.setBrush(QColor("#E960A8") if checked else QColor("#0E0E11"))
        painter.drawRoundedRect(checkbox_rect, 5, 5)
        if checked:
            check_path = QPainterPath()
            check_path.moveTo(checkbox_rect.left() + 5, checkbox_rect.center().y())
            check_path.lineTo(checkbox_rect.left() + 9, checkbox_rect.bottom() - 5)
            check_path.lineTo(checkbox_rect.right() - 4, checkbox_rect.top() + 5)
            painter.setPen(QPen(QColor("#160D12"), 2.2))
            painter.drawPath(check_path)

        painter.setPen(QPen(QColor("#303038"), 1))
        painter.setBrush(QColor("#0E0E11"))
        painter.drawRoundedRect(thumbnail_rect, 7, 7)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            scaled = pixmap.scaled(
                self.THUMBNAIL_SIZE,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            source = QRect(
                max(0, (scaled.width() - self.THUMBNAIL_SIZE.width()) // 2),
                max(0, (scaled.height() - self.THUMBNAIL_SIZE.height()) // 2),
                self.THUMBNAIL_SIZE.width(),
                self.THUMBNAIL_SIZE.height(),
            )
            clip = QPainterPath()
            clip.addRoundedRect(thumbnail_rect, 7, 7)
            painter.setClipPath(clip)
            painter.drawPixmap(thumbnail_rect, scaled, source)
            painter.setClipping(False)
        else:
            placeholder_font = QFont(option.font)
            placeholder_font.setPointSize(19)
            painter.setFont(placeholder_font)
            painter.setPen(QColor("#696771"))
            painter.drawText(thumbnail_rect, Qt.AlignmentFlag.AlignCenter, "▶")

        text_width = title_rect.width()

        title_font = QFont(option.font)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(
            option.palette.highlightedText().color()
            if option.state & QStyle.StateFlag.State_Selected
            else QColor("#F5F4F6")
        )
        title = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        title = painter.fontMetrics().elidedText(
            title,
            Qt.TextElideMode.ElideRight,
            text_width,
        )
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            title,
        )

        link_font = QFont(option.font)
        link_font.setUnderline(True)
        link_font.setPointSizeF(max(8.0, link_font.pointSizeF() - 1.0))
        painter.setFont(link_font)
        painter.setPen(QColor("#F3A6CB") if option.state & QStyle.StateFlag.State_Selected else QColor("#E960A8"))
        link = str(index.data(CandidateTableModel.LINK_ROLE) or "")
        link = painter.fontMetrics().elidedText(
            link,
            Qt.TextElideMode.ElideMiddle,
            text_width,
        )
        painter.drawText(
            link_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            link,
        )
        painter.restore()

    def editorEvent(  # noqa: N802
        self,
        event: QEvent,
        model: QAbstractTableModel,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> bool:
        if (
            index.column() != CandidateTableModel.PREVIEW_COLUMN
            or event.type() != QEvent.Type.MouseButtonRelease
            or not isinstance(event, QMouseEvent)
            or event.button() != Qt.MouseButton.LeftButton
        ):
            return super().editorEvent(event, model, option, index)
        checkbox_rect, _thumbnail_rect, _title_rect, link_rect = self._content_rects(
            option.rect
        )
        position = event.position().toPoint()
        if checkbox_rect.contains(position):
            current = index.data(Qt.ItemDataRole.CheckStateRole)
            next_state = (
                Qt.CheckState.Unchecked
                if current == Qt.CheckState.Checked
                else Qt.CheckState.Checked
            )
            return model.setData(index, next_state, Qt.ItemDataRole.CheckStateRole)
        if link_rect.contains(position):
            self.link_activated.emit(index.row())
            return True
        return super().editorEvent(event, model, option, index)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        base = super().sizeHint(option, index)
        return QSize(max(330, base.width()), self.ROW_HEIGHT)


class ThumbnailLoader(QObject):
    thumbnail_ready = Signal(str, QPixmap)
    MAX_DOWNLOAD_BYTES = 3 * 1024 * 1024
    MAX_CACHE_ITEMS = 128

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.network = QNetworkAccessManager(self)
        self.cache: OrderedDict[str, QPixmap] = OrderedDict()
        self.pending: dict[str, set[str]] = {}

    @staticmethod
    def _allowed_url(url: QUrl) -> bool:
        host = url.host().casefold().rstrip(".")
        return (
            url.isValid()
            and url.scheme().casefold() in {"http", "https"}
            and (host == "ytimg.com" or host.endswith(".ytimg.com"))
        )

    def load(self, candidate: Candidate) -> None:
        url_text = candidate.preview_image_url
        url = QUrl(url_text)
        if not self._allowed_url(url):
            return
        cached = self.cache.get(url_text)
        if cached is not None:
            self.cache.move_to_end(url_text)
            self.thumbnail_ready.emit(candidate.video_id, cached)
            return
        if url_text in self.pending:
            self.pending[url_text].add(candidate.video_id)
            return

        self.pending[url_text] = {candidate.video_id}
        request = QNetworkRequest(url)
        request.setTransferTimeout(12_000)
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        request.setRawHeader(b"User-Agent", b"MMM Downloader/0.2.1")
        reply = self.network.get(request)
        reply.finished.connect(
            lambda reply=reply, url_text=url_text: self._finish(reply, url_text)
        )

    def _finish(self, reply: QNetworkReply, url_text: str) -> None:
        video_ids = self.pending.pop(url_text, set())
        pixmap = QPixmap()
        if (
            reply.error() == QNetworkReply.NetworkError.NoError
            and self._allowed_url(reply.url())
        ):
            payload = bytes(reply.readAll())
            if 0 < len(payload) <= self.MAX_DOWNLOAD_BYTES:
                pixmap.loadFromData(payload)
        reply.deleteLater()
        if pixmap.isNull():
            return

        self.cache[url_text] = pixmap
        self.cache.move_to_end(url_text)
        while len(self.cache) > self.MAX_CACHE_ITEMS:
            self.cache.popitem(last=False)
        for video_id in video_ids:
            self.thumbnail_ready.emit(video_id, pixmap)


class ApiKeyDialog(QDialog):
    """Добавление ключа с проверкой у провайдера до сохранения.

    Провайдер опознаётся по самому ключу, поэтому выбирать его вручную не нужно.
    Проверка идёт в отдельном потоке: обращение к сети в потоке интерфейса
    подвесило бы окно на всё время ожидания. Результат возвращается сигналом,
    который Qt доставляет уже в потоке интерфейса.
    """

    check_finished = Signal(object)

    def __init__(
        self,
        suggested_name: str,
        model: str = "",
        parent: QWidget | None = None,
        *,
        verifier: Callable[..., ApiKeyCheck] = verify_api_key,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Добавить API-ключ")
        self.setModal(True)
        self.setMinimumWidth(460)

        self._model = model
        self._verifier = verifier
        self._check_thread: threading.Thread | None = None
        self.check: ApiKeyCheck | None = None
        self.unverified_reason = ""
        self.provider = DEFAULT_PROVIDER

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 16)
        layout.setSpacing(12)
        note = _label(
            "Подойдёт ключ OpenAI (sk-…) или OpenRouter (sk-or-v1-…) — провайдер "
            "определяется по ключу. Он проверяется запросом, который не тратит "
            "токены, и сохраняется в системном хранилище паролей.",
            muted=True,
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        form.setSpacing(9)
        self.name_edit = QLineEdit(suggested_name)
        self.name_edit.setPlaceholderText("Например, Рабочий")
        form.addRow("Название", self.name_edit)
        self.secret_edit = QLineEdit()
        self.secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_edit.setPlaceholderText("sk-… или sk-or-v1-…")
        form.addRow("API-ключ", self.secret_edit)
        layout.addLayout(form)

        self.status_label = _label("", object_name="KeyCheckStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setProperty("tone", "neutral")
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("Проверить и сохранить")
        self.save_button.setObjectName("PrimaryButton")
        self.save_button.setEnabled(False)
        self.cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setText("Отмена")
        # Сохранение без проверки нужно только тогда, когда проверке помешала
        # сеть или лимит: сам ключ при этом ещё ничем не опорочен.
        self.save_anyway_button = buttons.addButton(
            "Сохранить без проверки",
            QDialogButtonBox.ButtonRole.ActionRole,
        )
        self.save_anyway_button.setVisible(False)
        self.save_anyway_button.clicked.connect(self._accept_without_check)
        buttons.accepted.connect(self._verify_and_accept)
        buttons.rejected.connect(self.reject)
        self.name_edit.textChanged.connect(self._update_save_button)
        self.secret_edit.textChanged.connect(self._on_secret_changed)
        self.check_finished.connect(self._on_check_finished)
        layout.addWidget(buttons)
        self.secret_edit.setFocus()

    def _update_save_button(self) -> None:
        self.save_button.setEnabled(
            bool(self.name_edit.text().strip() and self.secret_edit.text().strip())
            and self._check_thread is None
        )

    def _on_secret_changed(self) -> None:
        # Правка ключа обесценивает прошлый вердикт.
        self.check = None
        self.save_anyway_button.setVisible(False)
        self._set_status("", tone="neutral")
        self._update_save_button()

    def _set_status(self, message: str, *, tone: str) -> None:
        self.status_label.setText(message)
        self.status_label.setVisible(bool(message))
        self.status_label.setProperty("tone", tone)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        # Ответ провайдера занимает несколько строк: без пересчёта размера окно
        # осталось бы прежним, и текст лёг бы поверх кнопок.
        self.layout().activate()
        self.adjustSize()

    def _set_inputs_enabled(self, enabled: bool) -> None:
        self.name_edit.setEnabled(enabled)
        self.secret_edit.setEnabled(enabled)
        self.save_button.setEnabled(enabled and bool(self.secret()))
        self.cancel_button.setEnabled(enabled)

    def _verify_and_accept(self) -> None:
        if self._check_thread is not None:
            return
        try:
            secret = normalize_secret(self.secret_edit.text())
        except ValueError as exc:
            self._set_status(str(exc), tone="error")
            return
        if not self.profile_name():
            self._set_status("Укажите название ключа.", tone="error")
            return

        self.provider = provider_for_secret(secret)
        target = ai_provider(self.provider)
        # Модель из главного окна годится только своему провайдеру; для чужого
        # ключа проверяем модель по умолчанию этого провайдера.
        model = self._model if self._model in target.models else target.default_model

        self.save_anyway_button.setVisible(False)
        self._set_status(f"Проверяем ключ в {target.title}…", tone="neutral")
        self._set_inputs_enabled(False)
        self._check_thread = threading.Thread(
            target=self._run_check,
            args=(secret, self.provider, model),
            daemon=True,
        )
        self._check_thread.start()

    def _run_check(self, secret: str, provider: str, model: str) -> None:
        try:
            check = self._verifier(secret, provider=provider, model=model)
        except Exception as exc:  # верификатор не должен ронять окно
            check = ApiKeyCheck(
                ok=False,
                message=f"Не удалось проверить ключ: {exc}",
                reason="network",
                provider=provider,
            )
        self.check_finished.emit(check)

    def _on_check_finished(self, check: ApiKeyCheck) -> None:
        self._check_thread = None
        self.check = check
        self._set_inputs_enabled(True)
        if check.ok:
            self.accept()
            return
        self._set_status(check.message, tone="error")
        self.save_anyway_button.setVisible(check.transient)

    def _accept_without_check(self) -> None:
        if self._check_thread is not None:
            return
        try:
            normalize_secret(self.secret_edit.text())
        except ValueError as exc:
            self._set_status(str(exc), tone="error")
            return
        self.provider = provider_for_secret(self.secret_edit.text())
        self.unverified_reason = self.check.message if self.check else ""
        self.check = None
        self.accept()

    def wait_for_check(self, timeout: float = 20.0) -> None:
        """Дождаться фоновой проверки. Нужно тестам и закрытию окна."""
        thread = self._check_thread
        if thread is not None:
            thread.join(timeout)

    def check_note(self) -> tuple[str, str]:
        """Текст и тон для постоянной подписи в главном окне."""
        check = self.check
        title = ai_provider(self.provider).title
        if check is None:
            reason = f" ({self.unverified_reason})" if self.unverified_reason else ""
            return (f"Ключ сохранён без проверки в {title}{reason}.", "warning")
        if check.model_available is False:
            return (check.message, "warning")
        if check.ok:
            return (check.message, "success")
        return (check.message, "error")

    def profile_name(self) -> str:
        return self.name_edit.text().strip()

    def secret(self) -> str:
        return self.secret_edit.text().strip()


class DownloaderApp(QMainWindow):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("MMM Downloader")
        self.resize(1180, 800)
        self.setMinimumSize(900, 720)

        icon_path = _resource_path("assets/mmm_downloader.png")
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.settings = load_settings()
        try:
            self.queue_store: QueueStore | None = QueueStore()
            self.queue_store_error = ""
        except QueueStoreError as exc:
            self.queue_store = None
            self.queue_store_error = str(exc)
        self.api_key_store = ApiKeyStore()
        self.api_key_verifier: Callable[..., ApiKeyCheck] = verify_api_key
        self.api_key_profiles: list[ApiKeyProfile] = list(self.settings.api_key_profiles)
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.preview_candidates: list[Candidate] = []
        self.active_batch_candidates: list[Candidate] = []
        self.failed_candidate_ids: set[str] = set()
        self.last_paths: list[str] = []
        self.closing = False
        self.force_close = False
        self.active_item_number: int | None = None
        self.active_item_in_progress = False
        self.last_ai_warning = ""
        self._model_provider = ""

        self._build_ui()
        self.thumbnail_loader = ThumbnailLoader(self)
        self.thumbnail_loader.thumbnail_ready.connect(self.candidate_model.set_thumbnail)
        self._toggle_ai_fields(self.ai_checkbox.isChecked())
        self._install_shortcuts()

        self.event_timer = QTimer(self)
        self.event_timer.setInterval(100)
        self.event_timer.timeout.connect(self._poll_events)
        self.event_timer.start()

        QTimer.singleShot(0, lambda: _apply_windows_dark_title_bar(self))
        QTimer.singleShot(250, self._offer_resume_queue)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("AppRoot")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(12)

        layout.addLayout(self._build_header())

        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(self._build_request_card(), 3)
        top.addWidget(self._build_settings_card(), 1)
        layout.addLayout(top)

        layout.addWidget(self._build_queue_card(), 1)
        layout.addWidget(self._build_status_card())

        self.input_text.setFocus()

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setSpacing(12)

        icon_label = QLabel()
        icon_label.setFixedSize(46, 46)
        pixmap = QPixmap(str(_resource_path("assets/mmm_downloader.png")))
        if not pixmap.isNull():
            icon_label.setPixmap(
                pixmap.scaled(
                    46,
                    46,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        header.addWidget(icon_label)

        header.addWidget(
            _label("MMM Downloader", object_name="AppTitle"),
            0,
            Qt.AlignmentFlag.AlignVCenter,
        )
        header.addStretch(1)

        for text in ("MP4", "H.264"):
            badge = _label(text, object_name="FormatBadge")
            badge.setFixedHeight(36)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.addWidget(badge)

        self.state_badge = _label("●  Готово", object_name="StateBadge")
        self.state_badge.setProperty("tone", "neutral")
        self.state_badge.setFixedHeight(36)
        self.state_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.state_badge)
        return header

    def _build_request_card(self) -> QFrame:
        card = _card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 15)
        layout.setSpacing(8)

        heading = QHBoxLayout()
        heading.addWidget(_label("Что скачать?", object_name="SectionTitle"))
        heading.addStretch(1)
        hint = _label("Ctrl + Enter — запустить", muted=True)
        hint.setToolTip("На macOS также работает Control + Enter")
        heading.addWidget(hint)
        layout.addLayout(heading)

        self.input_text = QPlainTextEdit()
        self.input_text.setObjectName("RequestInput")
        self.input_text.setPlaceholderText("Скачать весь метал с YT")
        self.input_text.setMinimumHeight(82)
        self.input_text.setMaximumHeight(98)
        layout.addWidget(self.input_text)

        folder_row = QHBoxLayout()
        folder_row.setSpacing(9)
        folder_row.addWidget(_label("Папка", muted=True))
        self.output_edit = QLineEdit(self.settings.output_dir)
        self.output_edit.setPlaceholderText("Выберите папку назначения")
        folder_row.addWidget(self.output_edit, 1)
        self.folder_button = QPushButton("Выбрать…")
        self.folder_button.clicked.connect(self._choose_folder)
        folder_row.addWidget(self.folder_button)
        layout.addLayout(folder_row)

        self.plan_label = _label(
            "Сначала найдите видео, затем отметьте нужные и скачайте.",
            muted=True,
        )
        self.plan_label.setWordWrap(True)
        layout.addWidget(self.plan_label)

        # Подпись живёт в широкой карточке запроса, а не в узкой колонке
        # настроек: длинный ответ OpenAI помещается здесь в одну-две строки и
        # читается рядом с разобранным планом.
        self.ai_status_label = _label("", object_name="AiStatus")
        self.ai_status_label.setWordWrap(True)
        self.ai_status_label.setProperty("tone", "neutral")
        self.ai_status_label.setVisible(False)
        layout.addWidget(self.ai_status_label)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.preview_button = QPushButton("Найти и выбрать  →")
        self.preview_button.setObjectName("PrimaryButton")
        self.preview_button.clicked.connect(lambda: self._start(preview_only=True))
        actions.addWidget(self.preview_button)
        self.download_button = QPushButton("Скачать без просмотра")
        self.download_button.setToolTip(
            "Скачать все найденные видео сразу, не выбирая их в таблице"
        )
        self.download_button.clicked.connect(lambda: self._start(preview_only=False))
        actions.addWidget(self.download_button)
        layout.addLayout(actions)
        return card

    def _build_settings_card(self) -> QFrame:
        card = _card()
        card.setMinimumWidth(340)
        card.setMaximumWidth(400)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(0)

        layout.addWidget(_label("Настройки", object_name="SectionTitle"))
        layout.addSpacing(4)
        layout.addWidget(_label("Качество", muted=True))
        layout.addSpacing(4)
        self.quality_combo = QComboBox()
        self.quality_combo.setObjectName("SettingsControl")
        self.quality_combo.setFixedHeight(38)
        for label, value in QUALITY_LABELS.items():
            self.quality_combo.addItem(label, value)
        quality_index = self.quality_combo.findData(self.settings.quality_mode)
        self.quality_combo.setCurrentIndex(max(0, quality_index))
        self.quality_combo.setToolTip(
            "Абсолютный максимум перекодирует лучший поток в H.264; нативный режим быстрее."
        )
        layout.addWidget(self.quality_combo)

        layout.addSpacing(8)
        layout.addWidget(_label("Cookies браузера", muted=True))
        layout.addSpacing(4)
        self.cookies_combo = QComboBox()
        self.cookies_combo.setObjectName("SettingsControl")
        self.cookies_combo.setFixedHeight(38)
        for label, value in COOKIE_LABELS.items():
            self.cookies_combo.addItem(label, value)
        cookies_index = self.cookies_combo.findData(self.settings.cookie_browser)
        self.cookies_combo.setCurrentIndex(max(0, cookies_index))
        layout.addWidget(self.cookies_combo)

        layout.addSpacing(8)
        self.ai_checkbox = QCheckBox("Нейро-разбор запроса")
        self.ai_checkbox.setChecked(self.settings.use_ai)
        self.ai_checkbox.toggled.connect(self._toggle_ai_fields)
        layout.addWidget(self.ai_checkbox)

        layout.addSpacing(8)
        self.ai_fields = QWidget()
        ai_layout = QVBoxLayout(self.ai_fields)
        ai_layout.setContentsMargins(0, 0, 0, 0)
        ai_layout.setSpacing(0)
        ai_layout.addWidget(_label("API-ключ OpenAI или OpenRouter", muted=True))
        ai_layout.addSpacing(4)
        keys_row = QHBoxLayout()
        keys_row.setSpacing(8)
        self.api_key_combo = QComboBox()
        self.api_key_combo.setObjectName("SettingsControl")
        self.api_key_combo.setFixedHeight(38)
        keys_row.addWidget(self.api_key_combo, 1)
        self.add_api_key_button = QPushButton()
        self.add_api_key_button.setObjectName("KeyIconButton")
        self.add_api_key_button.setFixedSize(38, 38)
        self.add_api_key_button.setIcon(
            QIcon(str(_resource_path("assets/plus.svg")))
        )
        self.add_api_key_button.setIconSize(QSize(16, 16))
        self.add_api_key_button.setToolTip("Добавить ключ")
        self.add_api_key_button.clicked.connect(self._add_api_key)
        keys_row.addWidget(self.add_api_key_button)
        self.delete_api_key_button = QPushButton()
        self.delete_api_key_button.setObjectName("KeyIconButton")
        self.delete_api_key_button.setProperty("tone", "danger")
        self.delete_api_key_button.setFixedSize(38, 38)
        self.delete_api_key_button.setIcon(
            QIcon(str(_resource_path("assets/minus.svg")))
        )
        self.delete_api_key_button.setIconSize(QSize(16, 16))
        self.delete_api_key_button.setToolTip("Удалить выбранный ключ")
        self.delete_api_key_button.clicked.connect(self._delete_api_key)
        keys_row.addWidget(self.delete_api_key_button)
        ai_layout.addLayout(keys_row)
        ai_layout.addSpacing(6)
        self.ai_model_combo = QComboBox()
        self.ai_model_combo.setObjectName("SettingsControl")
        self.ai_model_combo.setEditable(True)
        self.ai_model_combo.setFixedHeight(38)
        # Сохранённая модель ставится до наполнения списка: _refresh_model_choices
        # оставит её, если она подходит провайдеру выбранного ключа.
        self.ai_model_combo.setCurrentText(self.settings.ai_model)
        self.ai_model_combo.lineEdit().setPlaceholderText("Модель")
        self.ai_model_combo.setToolTip(
            "Идентификатор модели у провайдера выбранного ключа. Несуществующая "
            "модель отклоняется сервером, и запрос разбирается локально."
        )
        ai_layout.addWidget(self.ai_model_combo)
        self._refresh_api_key_combo(self.settings.selected_api_key_id)
        self.api_key_combo.currentIndexChanged.connect(self._remember_api_key_selection)
        layout.addWidget(self.ai_fields)
        layout.addStretch(1)
        return card

    def _build_queue_card(self) -> QFrame:
        card = _card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(9)

        heading = QHBoxLayout()
        heading.addWidget(_label("Подборка и очередь", object_name="SectionTitle"))
        self.queue_count_label = _label("0 видео", object_name="CountBadge")
        heading.addWidget(self.queue_count_label)
        heading.addStretch(1)
        self.select_all_button = QPushButton("Выбрать все")
        self.select_all_button.setObjectName("InlineButton")
        self.select_all_button.clicked.connect(self._select_all)
        self.select_all_button.setEnabled(False)
        heading.addWidget(self.select_all_button)
        self.deselect_all_button = QPushButton("Снять все")
        self.deselect_all_button.setObjectName("InlineButton")
        self.deselect_all_button.clicked.connect(self._deselect_all)
        self.deselect_all_button.setEnabled(False)
        heading.addWidget(self.deselect_all_button)
        self.retry_button = QPushButton("Повторить ошибки")
        self.retry_button.setObjectName("InlineButton")
        self.retry_button.clicked.connect(self._retry_failed)
        self.retry_button.setEnabled(False)
        self.retry_button.setVisible(False)
        heading.addWidget(self.retry_button)
        self.download_found_button = QPushButton("Скачать выбранные (0)")
        self.download_found_button.setObjectName("PrimaryButton")
        self.download_found_button.clicked.connect(self._download_found)
        self.download_found_button.setEnabled(False)
        heading.addWidget(self.download_found_button)
        layout.addLayout(heading)

        self.results_stack = QStackedWidget()
        self.results_stack.setMinimumHeight(125)

        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.setContentsMargins(12, 18, 12, 18)
        empty_layout.addStretch(1)
        empty_icon = QLabel()
        empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap(str(_resource_path("assets/mmm_downloader.png")))
        if not pixmap.isNull():
            empty_icon.setPixmap(
                pixmap.scaled(
                    62,
                    62,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        empty_layout.addWidget(empty_icon)
        empty_title = _label("Здесь появится найденная подборка", object_name="SectionTitle")
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_title)
        empty_hint = _label(
            "Ссылка, двойной клик или Enter откроют видео на YouTube",
            muted=True,
        )
        empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_hint)
        empty_layout.addStretch(1)
        self.results_stack.addWidget(empty)

        self.candidate_model = CandidateTableModel()
        self.candidate_model.checked_changed.connect(self._update_checked_actions)
        self.table = QTableView()
        self.table.setModel(self.candidate_model)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(VideoPreviewDelegate.ROW_HEIGHT)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionResizeMode(
            CandidateTableModel.PREVIEW_COLUMN,
            QHeaderView.ResizeMode.Stretch,
        )
        for column in range(1, self.candidate_model.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(CandidateTableModel.DURATION_COLUMN, 95)
        self.table.setColumnWidth(CandidateTableModel.VIEWS_COLUMN, 110)
        self.table.setColumnWidth(CandidateTableModel.CHANNEL_COLUMN, 145)
        self.table.setColumnWidth(CandidateTableModel.STATUS_COLUMN, 135)
        self.preview_delegate = VideoPreviewDelegate(self.table)
        self.table.setItemDelegateForColumn(
            CandidateTableModel.PREVIEW_COLUMN,
            self.preview_delegate,
        )
        self.preview_delegate.link_activated.connect(self._open_video_row)
        self.table.doubleClicked.connect(self._open_selected_video)
        self.results_stack.addWidget(self.table)
        layout.addWidget(self.results_stack, 1)
        return card

    def _build_status_card(self) -> QFrame:
        card = _card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 13, 18, 14)
        layout.setSpacing(9)

        row = QHBoxLayout()
        self.status_label = QLabel("Готов к работе")
        self.status_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(self.status_label, 1)
        self.details_button = QPushButton("Журнал  ›")
        self.details_button.setObjectName("InlineButton")
        self.details_button.clicked.connect(self._show_log)
        row.addWidget(self.details_button)
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setObjectName("DangerButton")
        self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.setEnabled(False)
        row.addWidget(self.cancel_button)
        self.open_output_button = QPushButton("Открыть папку")
        self.open_output_button.clicked.connect(self._open_output)
        row.addWidget(self.open_output_button)
        layout.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.log_dialog = QDialog(self)
        self.log_dialog.setWindowTitle("MMM Downloader — журнал")
        self.log_dialog.resize(760, 360)
        log_layout = QVBoxLayout(self.log_dialog)
        log_layout.setContentsMargins(14, 14, 14, 14)
        self.log = QPlainTextEdit(self.log_dialog)
        self.log.setObjectName("LogView")
        self.log.setReadOnly(True)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log.setMaximumBlockCount(1000)
        log_layout.addWidget(self.log)
        return card

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(
            lambda: self._start(preview_only=True)
        )
        QShortcut(QKeySequence("Ctrl+Enter"), self).activated.connect(
            lambda: self._start(preview_only=True)
        )
        QShortcut(QKeySequence.StandardKey.Find, self).activated.connect(self.input_text.setFocus)
        QShortcut(QKeySequence("Delete"), self.table).activated.connect(
            self._uncheck_selected_rows
        )
        QShortcut(QKeySequence("Return"), self.table).activated.connect(
            self._open_selected_video
        )
        QShortcut(QKeySequence("Escape"), self).activated.connect(self._cancel)

    def _choose_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Куда сохранять видео",
            self.output_edit.text().strip() or str(Path.home()),
        )
        if selected:
            self.output_edit.setText(selected)

    def _toggle_ai_fields(self, enabled: bool) -> None:
        self.ai_fields.setVisible(enabled)
        self._set_ai_status(*self._idle_ai_note())

    def _refresh_api_key_combo(self, selected_id: str = "") -> None:
        self.api_key_combo.blockSignals(True)
        self.api_key_combo.clear()
        for profile in self.api_key_profiles:
            suffix = f" · {profile.hint}" if profile.hint else ""
            self.api_key_combo.addItem(f"{profile.name}{suffix}", profile.key_id)
        if self.api_key_profiles:
            selected_index = self.api_key_combo.findData(selected_id)
            self.api_key_combo.setCurrentIndex(max(0, selected_index))
        else:
            self.api_key_combo.addItem("Нет сохранённых ключей", "")
            self.api_key_combo.setCurrentIndex(0)
        self.api_key_combo.blockSignals(False)
        busy = bool(self.worker and self.worker.is_alive())
        self.api_key_combo.setEnabled(bool(self.api_key_profiles) and not busy)
        self.delete_api_key_button.setEnabled(bool(self.api_key_profiles) and not busy)
        self._refresh_model_choices()
        self._set_ai_status(*self._idle_ai_note())

    def _selected_profile(self) -> ApiKeyProfile | None:
        key_id = str(self.api_key_combo.currentData() or "")
        return next(
            (item for item in self.api_key_profiles if item.key_id == key_id),
            None,
        )

    def _selected_provider(self) -> str:
        profile = self._selected_profile()
        return profile.provider if profile else DEFAULT_PROVIDER

    def _refresh_model_choices(self) -> None:
        """Держать список моделей в согласии с провайдером выбранного ключа."""
        provider = self._selected_provider()
        if provider == self._model_provider:
            return
        target = ai_provider(provider)
        current = self.ai_model_combo.currentText().strip()
        self.ai_model_combo.blockSignals(True)
        self.ai_model_combo.clear()
        self.ai_model_combo.addItems(target.models)
        # Модель чужого провайдера у нового ключа не заработает, поэтому при
        # смене провайдера берём модель по умолчанию. Свой, не из списка,
        # идентификатор при этом сохраняется.
        self.ai_model_combo.setCurrentText(
            current if _model_fits(provider, current) else target.default_model
        )
        self.ai_model_combo.blockSignals(False)
        self._model_provider = provider

    def _idle_ai_note(self) -> tuple[str, str]:
        if not self.ai_checkbox.isChecked():
            return ("", "neutral")
        if not self.api_key_profiles:
            return ("Нейро-разбор включён, но ключ не добавлен — запросы "
                    "разбираются локально.", "warning")
        return (
            f"Нейро-разбор идёт через {ai_provider(self._selected_provider()).title} "
            "и применяется к текстовым запросам; готовые ссылки разбираются локально.",
            "neutral",
        )

    def _set_ai_status(self, message: str, tone: str = "neutral") -> None:
        """Постоянная подпись о том, применялся ли ключ к последнему запросу."""
        self.ai_status_label.setText(message)
        self.ai_status_label.setVisible(bool(message))
        self.ai_status_label.setProperty("tone", tone)
        self.ai_status_label.style().unpolish(self.ai_status_label)
        self.ai_status_label.style().polish(self.ai_status_label)

    def _remember_api_key_selection(self, _index: int = -1) -> None:
        self._refresh_model_choices()
        self._set_ai_status(*self._idle_ai_note())
        try:
            save_settings(self._settings_from_ui())
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Не удалось сохранить выбор",
                f"Выбранный ключ будет действовать до закрытия приложения.\n\n{exc}",
            )

    def _add_api_key(self) -> None:
        dialog = ApiKeyDialog(
            f"Ключ {len(self.api_key_profiles) + 1}",
            self._current_ai_model(),
            self,
            verifier=self.api_key_verifier,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            profile = self.api_key_store.create(dialog.profile_name(), dialog.secret())
        except (ApiKeyStoreError, ValueError) as exc:
            QMessageBox.critical(self, "Не удалось сохранить ключ", str(exc))
            return

        previous_profiles = list(self.api_key_profiles)
        previous_selected = str(self.api_key_combo.currentData() or "")
        self.api_key_profiles.append(profile)
        self._refresh_api_key_combo(profile.key_id)
        note, tone = dialog.check_note()
        self._set_ai_status(note, tone)
        self._append_log(f"Ключ «{profile.name}»: {note}")
        try:
            save_settings(self._settings_from_ui())
        except OSError as exc:
            self.api_key_profiles = previous_profiles
            self._refresh_api_key_combo(previous_selected)
            try:
                self.api_key_store.delete(profile.key_id)
            except ApiKeyStoreError:
                pass
            QMessageBox.critical(
                self,
                "Не удалось сохранить список ключей",
                str(exc),
            )

    def _delete_api_key(self) -> None:
        key_id = str(self.api_key_combo.currentData() or "")
        profile = next(
            (item for item in self.api_key_profiles if item.key_id == key_id),
            None,
        )
        if profile is None:
            return
        answer = QMessageBox.question(
            self,
            "Удалить API-ключ?",
            f"Удалить ключ «{profile.name}» из системного хранилища?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        previous_profiles = list(self.api_key_profiles)
        previous_selected = key_id
        previous_index = self.api_key_combo.currentIndex()
        self.api_key_profiles = [
            item for item in self.api_key_profiles if item.key_id != key_id
        ]
        next_id = ""
        if self.api_key_profiles:
            next_index = min(previous_index, len(self.api_key_profiles) - 1)
            next_id = self.api_key_profiles[next_index].key_id
        self._refresh_api_key_combo(next_id)
        try:
            save_settings(self._settings_from_ui())
            self.api_key_store.delete(key_id)
        except (ApiKeyStoreError, OSError) as exc:
            self.api_key_profiles = previous_profiles
            self._refresh_api_key_combo(previous_selected)
            try:
                save_settings(self._settings_from_ui())
            except OSError:
                pass
            QMessageBox.critical(self, "Не удалось удалить ключ", str(exc))

    def _selected_api_key(self) -> str:
        key_id = str(self.api_key_combo.currentData() or "")
        if not key_id:
            raise ApiKeyStoreError("Добавьте и выберите API-ключ.")
        return self.api_key_store.get(key_id)

    def _show_log(self) -> None:
        self.log_dialog.show()
        self.log_dialog.raise_()
        self.log_dialog.activateWindow()

    def _current_ai_model(self) -> str:
        model = self.ai_model_combo.currentText().strip()
        return model or ai_provider(self._selected_provider()).default_model

    def _settings_from_ui(self) -> AppSettings:
        return AppSettings(
            output_dir=self.output_edit.text().strip(),
            quality_mode=str(self.quality_combo.currentData() or "native_h264"),
            cookie_browser=str(self.cookies_combo.currentData() or ""),
            use_ai=self.ai_checkbox.isChecked(),
            ai_model=self._current_ai_model(),
            api_key_profiles=list(self.api_key_profiles),
            selected_api_key_id=str(self.api_key_combo.currentData() or ""),
        )

    @staticmethod
    def _queue_launch_settings(settings: AppSettings) -> dict[str, object]:
        return {
            "output_dir": settings.output_dir,
            "quality_mode": settings.quality_mode,
            "cookie_browser": settings.cookie_browser,
        }

    def _persist_queue(
        self,
        candidates: list[Candidate],
        settings: AppSettings,
    ) -> None:
        if self.queue_store is None:
            return
        try:
            self.queue_store.replace(
                [
                    {
                        "video_id": candidate.video_id,
                        "url": candidate.watch_url,
                        "metadata": _queue_metadata(candidate),
                    }
                    for candidate in candidates
                ],
                settings=self._queue_launch_settings(settings),
            )
        except QueueStoreError as exc:
            self.events.put(
                {
                    "kind": "log",
                    "level": "warning",
                    "message": f"Не удалось сохранить очередь: {exc}",
                }
            )

    def _queue_transition(
        self,
        action: str,
        candidate: Candidate | None,
        *args: object,
    ) -> None:
        if self.queue_store is None or candidate is None:
            return
        try:
            method = getattr(self.queue_store, action)
            method(candidate.video_id, *args)
        except (QueueStoreError, AttributeError) as exc:
            self._append_log(f"Очередь: не удалось сохранить состояние ({exc})")

    def _offer_resume_queue(self) -> None:
        if self.queue_store is None:
            if self.queue_store_error:
                self._append_log(
                    f"Очередь недоступна; восстановление отключено: {self.queue_store_error}"
                )
            return
        snapshot = self.queue_store.snapshot()
        if snapshot.load_report.warning:
            self._append_log(snapshot.load_report.warning)
        restorable = [
            item
            for item in snapshot.items
            if item.state
            in {QueueState.PENDING, QueueState.FAILED, QueueState.CANCELLED}
        ]
        if not restorable:
            return
        answer = QMessageBox.question(
            self,
            "Восстановить очередь?",
            (
                f"С прошлого запуска осталось видео: {len(restorable)}.\n\n"
                "Вернуть их в таблицу? Скачивание не начнётся автоматически."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        saved = snapshot.settings
        output_dir = saved.get("output_dir")
        if isinstance(output_dir, str):
            self.output_edit.setText(output_dir)
        quality_mode = saved.get("quality_mode")
        if isinstance(quality_mode, str):
            quality_index = self.quality_combo.findData(quality_mode)
            if quality_index >= 0:
                self.quality_combo.setCurrentIndex(quality_index)
        cookie_browser = saved.get("cookie_browser")
        if isinstance(cookie_browser, str):
            cookie_index = self.cookies_combo.findData(cookie_browser)
            if cookie_index >= 0:
                self.cookies_combo.setCurrentIndex(cookie_index)

        candidates = [_candidate_from_queue_item(item) for item in restorable]
        self.failed_candidate_ids = {
            item.video_id for item in restorable if item.state is QueueState.FAILED
        }
        self._show_candidates(candidates)
        for item in restorable:
            status = {
                QueueState.PENDING: "Ожидает продолжения",
                QueueState.FAILED: "Ошибка — можно повторить",
                QueueState.CANCELLED: "Отменено — можно продолжить",
            }[item.state]
            self.candidate_model.set_candidate_status(item.video_id, status)
        self.retry_button.setVisible(bool(self.failed_candidate_ids))
        self.retry_button.setText(
            f"Повторить ошибки ({len(self.failed_candidate_ids)})"
        )
        self.plan_label.setText(f"Восстановлена очередь: {len(candidates)} видео")
        self._set_status("Очередь восстановлена", tone="success")

    def _parse(
        self,
        text: str,
        settings: AppSettings,
        api_key: str,
        provider: str = DEFAULT_PROVIDER,
    ) -> DownloadIntent:
        if not settings.use_ai:
            return parse_intent(text, output_path=settings.output_dir)
        if text.lstrip().startswith(("http://", "https://")):
            self.events.put(
                {
                    "kind": "ai",
                    "tone": "neutral",
                    "message": (
                        "Запрос — готовая ссылка, нейро-разбор для неё не нужен: "
                        "ключ не применялся."
                    ),
                }
            )
            return parse_intent(text, output_path=settings.output_dir)
        title = ai_provider(provider).title
        try:
            intent = parse_intent_with_ai(
                text,
                api_key=api_key,
                provider=provider,
                model=settings.ai_model,
                output_path=settings.output_dir,
            )
        except AIIntentError as exc:
            self.events.put(
                {
                    "kind": "ai",
                    "tone": "error" if exc.reason == "auth" else "warning",
                    "message": f"Нейро-разбор не сработал: {exc}",
                    "note": "Запрос разобран локально, ключ не применён.",
                }
            )
            return parse_intent(text, output_path=settings.output_dir)
        self.events.put(
            {
                "kind": "ai",
                "tone": "success",
                "message": (
                    f"Запрос разобран через {title}, модель «{settings.ai_model}»."
                ),
            }
        )
        return intent

    def _start(self, *, preview_only: bool) -> None:
        if self.worker and self.worker.is_alive():
            return
        text = self.input_text.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Нет запроса", "Вставьте ссылку или напишите запрос.")
            return
        settings = self._settings_from_ui()
        api_key = ""
        provider = self._selected_provider()
        if settings.use_ai:
            try:
                api_key = self._selected_api_key()
            except ApiKeyStoreError as exc:
                self._set_ai_status(str(exc), "error")
                QMessageBox.warning(self, "API-ключ", str(exc))
                return
        if not preview_only and not settings.output_dir:
            QMessageBox.warning(self, "Нет папки", "Выберите папку для сохранения.")
            return
        try:
            save_settings(settings)
        except OSError:
            pass
        self._clear_results()
        self._set_busy(True)
        self.cancel_event.clear()
        self.worker = threading.Thread(
            target=self._worker,
            args=(text, settings, api_key, preview_only, provider),
            daemon=True,
        )
        self.worker.start()

    def _worker(
        self,
        text: str,
        settings: AppSettings,
        api_key: str,
        preview_only: bool,
        provider: str = DEFAULT_PROVIDER,
    ) -> None:
        try:
            intent = self._parse(text, settings, api_key, provider)
            self.events.put({"kind": "plan", "intent": intent})
            engine = YouTubeEngine(
                output_dir=settings.output_dir,
                quality_mode=settings.quality_mode,
                cookie_browser=settings.cookie_browser,
                emit=self.events.put,
                cancel_event=self.cancel_event,
            )
            if intent.urls:
                candidates = engine.inspect_urls(intent.urls)
            else:
                candidates = engine.search(intent)
            urls = [candidate.watch_url for candidate in candidates]
            if not urls:
                raise RuntimeError("Не удалось получить данные подходящих видео")
            if preview_only:
                self._persist_queue(list(candidates), settings)
                self.events.put({"kind": "worker_done", "preview": True})
                return
            self.active_batch_candidates = list(candidates)
            self._persist_queue(self.active_batch_candidates, settings)
            engine.download_urls(urls)
            self.events.put({"kind": "worker_done", "preview": False})
        except JobCancelled:
            self.events.put({"kind": "cancelled", "message": "Операция отменена"})
            self.events.put({"kind": "worker_done", "preview": False})
        except (IntentParseError, ValueError, RuntimeError, OSError) as exc:
            self.events.put({"kind": "fatal", "message": str(exc)})
            self.events.put({"kind": "worker_done", "preview": False})
        except Exception as exc:
            self.events.put(
                {
                    "kind": "fatal",
                    "message": f"Непредвиденная ошибка: {type(exc).__name__}: {exc}",
                }
            )
            self.events.put({"kind": "worker_done", "preview": False})

    def _download_found(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        candidates = self.candidate_model.checked_candidates()
        if not candidates:
            return
        self._start_candidate_download(candidates)

    def _retry_failed(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        candidates = [
            candidate
            for candidate in self.preview_candidates
            if candidate.video_id in self.failed_candidate_ids
        ]
        if not candidates:
            return
        self.candidate_model.set_all_checked(False)
        for candidate in candidates:
            row = self.preview_candidates.index(candidate)
            self.candidate_model.setData(
                self.candidate_model.index(row, CandidateTableModel.PREVIEW_COLUMN),
                Qt.CheckState.Checked,
                Qt.ItemDataRole.CheckStateRole,
            )
        self._start_candidate_download(candidates)

    def _start_candidate_download(self, candidates: list[Candidate]) -> None:
        settings = self._settings_from_ui()
        if not settings.output_dir:
            QMessageBox.warning(self, "Нет папки", "Выберите папку для сохранения.")
            return
        try:
            save_settings(settings)
        except OSError:
            pass
        self.active_batch_candidates = list(candidates)
        self.failed_candidate_ids.difference_update(
            candidate.video_id for candidate in self.active_batch_candidates
        )
        self.retry_button.setVisible(bool(self.failed_candidate_ids))
        urls = [candidate.watch_url for candidate in self.active_batch_candidates]
        self._persist_queue(self.active_batch_candidates, settings)
        self._set_busy(True)
        self.cancel_event.clear()

        def run() -> None:
            try:
                engine = YouTubeEngine(
                    output_dir=settings.output_dir,
                    quality_mode=settings.quality_mode,
                    cookie_browser=settings.cookie_browser,
                    emit=self.events.put,
                    cancel_event=self.cancel_event,
                )
                engine.download_urls(urls)
            except JobCancelled:
                self.events.put({"kind": "cancelled", "message": "Операция отменена"})
            except (RuntimeError, ValueError, OSError) as exc:
                self.events.put({"kind": "fatal", "message": str(exc)})
            except Exception as exc:
                self.events.put(
                    {
                        "kind": "fatal",
                        "message": f"Непредвиденная ошибка: {type(exc).__name__}: {exc}",
                    }
                )
            finally:
                self.events.put({"kind": "worker_done", "preview": False})

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _cancel(self) -> None:
        if not self.worker or not self.worker.is_alive():
            return
        self.cancel_event.set()
        self._set_status("Останавливаю операцию…", tone="working")

    def _set_busy(self, busy: bool) -> None:
        for widget in (
            self.input_text,
            self.output_edit,
            self.folder_button,
            self.quality_combo,
            self.cookies_combo,
            self.ai_checkbox,
            self.ai_model_combo,
            self.api_key_combo,
            self.add_api_key_button,
            self.delete_api_key_button,
            self.preview_button,
            self.download_button,
        ):
            widget.setEnabled(not busy)
        self.api_key_combo.setEnabled(not busy and bool(self.api_key_profiles))
        self.delete_api_key_button.setEnabled(not busy and bool(self.api_key_profiles))
        checked = len(self.candidate_model.checked_ids)
        total = len(self.preview_candidates)
        self.download_found_button.setEnabled(not busy and checked > 0)
        self.select_all_button.setEnabled(not busy and total > 0 and checked < total)
        self.deselect_all_button.setEnabled(not busy and checked > 0)
        self.retry_button.setEnabled(not busy and bool(self.failed_candidate_ids))
        self.cancel_button.setEnabled(busy)
        if busy:
            self.progress.setRange(0, 0)
            self._set_status("Подготовка…", tone="working")
        else:
            self.progress.setRange(0, 100)

    def _clear_results(self) -> None:
        self.last_ai_warning = ""
        self.preview_candidates = []
        self.active_batch_candidates = []
        self.failed_candidate_ids.clear()
        self.last_paths = []
        self.active_item_number = None
        self.active_item_in_progress = False
        self.candidate_model.set_candidates([])
        self.results_stack.setCurrentIndex(0)
        self.queue_count_label.setText("0 видео")
        self.log.clear()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.download_found_button.setEnabled(False)
        self.select_all_button.setEnabled(False)
        self.deselect_all_button.setEnabled(False)
        self.retry_button.setEnabled(False)
        self.retry_button.setVisible(False)

    def _append_log(self, message: str) -> None:
        self.log.appendPlainText(message.rstrip())
        scrollbar = self.log.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _show_candidates(
        self,
        items: list[Candidate],
        *,
        checked_ids: set[str] | None = None,
    ) -> None:
        self.preview_candidates = list(items)
        self.candidate_model.set_candidates(
            self.preview_candidates,
            checked_ids=checked_ids,
        )
        for candidate in self.preview_candidates:
            self.thumbnail_loader.load(candidate)
        self.results_stack.setCurrentIndex(1 if items else 0)
        self._update_checked_actions(
            len(self.candidate_model.checked_ids),
            len(self.preview_candidates),
        )

    def _select_all(self) -> None:
        self.candidate_model.set_all_checked(True)

    def _deselect_all(self) -> None:
        self.candidate_model.set_all_checked(False)

    def _uncheck_selected_rows(self) -> None:
        selection = self.table.selectionModel()
        for index in selection.selectedRows():
            preview_index = self.candidate_model.index(
                index.row(),
                CandidateTableModel.PREVIEW_COLUMN,
            )
            self.candidate_model.setData(
                preview_index,
                Qt.CheckState.Unchecked,
                Qt.ItemDataRole.CheckStateRole,
            )

    def _update_checked_actions(self, checked: int, total: int) -> None:
        idle = not (self.worker and self.worker.is_alive())
        self.queue_count_label.setText(
            f"Выбрано {checked} из {total}" if total else "0 видео"
        )
        self.download_found_button.setText(f"Скачать выбранные ({checked})")
        self.download_found_button.setEnabled(idle and checked > 0)
        self.select_all_button.setEnabled(idle and total > 0 and checked < total)
        self.deselect_all_button.setEnabled(idle and checked > 0)

    def _open_selected_video(self, index: QModelIndex | None = None) -> None:
        if index is not None and index.isValid():
            self._open_video_row(index.row())
            return
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self._open_video_row(rows[0].row())

    def _open_video_row(self, row: int) -> None:
        if not 0 <= row < len(self.preview_candidates):
            return
        url = QUrl(self.preview_candidates[row].watch_url)
        host = url.host().casefold().rstrip(".")
        is_youtube = (
            host == "youtu.be"
            or host == "youtube.com"
            or host.endswith(".youtube.com")
            or host == "youtube-nocookie.com"
            or host.endswith(".youtube-nocookie.com")
        )
        if url.scheme().casefold() not in {"http", "https"} or not is_youtube:
            return
        QDesktopServices.openUrl(url)

    def _open_output(self) -> None:
        path = self.output_edit.text().strip()
        if not path:
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).expanduser()))):
            QMessageBox.critical(self, "Не удалось открыть папку", path)

    def _finish_status(self, message: str, tone: str) -> None:
        """Итог операции с напоминанием, если нейро-разбор так и не применился."""
        if self.last_ai_warning:
            message = f"{message} · нейро-разбор не применён"
            tone = "warning"
        self._set_status(message, tone=tone)

    def _set_status(self, message: str, *, tone: str = "neutral") -> None:
        self.status_label.setText(message)
        badge_text = {
            "working": "●  В работе",
            "success": "●  Готово",
            "warning": "●  Частично",
            "error": "●  Ошибка",
        }.get(tone, "●  Готово")
        self.state_badge.setText(badge_text)
        self.state_badge.setProperty("tone", tone)
        self.state_badge.style().unpolish(self.state_badge)
        self.state_badge.style().polish(self.state_badge)

    def _active_candidate(self) -> Candidate | None:
        if self.active_item_number is None:
            return None
        index = self.active_item_number - 1
        if not 0 <= index < len(self.active_batch_candidates):
            return None
        return self.active_batch_candidates[index]

    def _set_active_candidate_status(self, status: str) -> None:
        candidate = self._active_candidate()
        if candidate is not None:
            self.candidate_model.set_candidate_status(candidate.video_id, status)

    def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        message = str(event.get("message") or "")
        item_number = event.get("item")
        if isinstance(item_number, int):
            self.active_item_number = item_number

        if kind == "plan":
            intent: DownloadIntent = event["intent"]
            if intent.urls:
                summary = f"План: скачать {len(intent.urls)} ссылок"
            else:
                duration = []
                if intent.min_duration_seconds is not None:
                    duration.append(f"от {_duration(intent.min_duration_seconds)}")
                if intent.max_duration_seconds is not None:
                    duration.append(f"до {_duration(intent.max_duration_seconds)}")
                suffix = f" · {' '.join(duration)}" if duration else ""
                summary = f"План: {intent.count} видео · «{intent.topic}»{suffix}"
            self.plan_label.setText(summary)
        elif kind == "candidates":
            candidates = list(event.get("items") or [])
            self.active_batch_candidates = candidates
            self._show_candidates(candidates)
            self._set_status(message, tone="working")
            self._append_log(message)
        elif kind == "ai":
            tone = str(event.get("tone") or "neutral")
            note = str(event.get("note") or "")
            full = f"{message} {note}".strip()
            self._set_ai_status(full, tone)
            self._append_log(full)
            # Строка состояния переписывается следующими событиями, поэтому
            # причину запоминаем и повторяем в итоге операции.
            self.last_ai_warning = message if tone in {"warning", "error"} else ""
            if self.last_ai_warning:
                self._set_status(message, tone="warning")
        elif kind in {"phase", "item_start", "log"}:
            if message:
                self._set_status(message, tone="working")
                self._append_log(message)
            if kind == "item_start":
                self.active_item_in_progress = True
                self._set_active_candidate_status("Подготовка…")
                self._queue_transition("mark_downloading", self._active_candidate())
            elif event.get("phase") == "transcode":
                self._set_active_candidate_status("Конвертация…")
                self._queue_transition("mark_transcoding", self._active_candidate())
        elif kind == "progress":
            percent = event.get("percent")
            if percent is None:
                self.progress.setRange(0, 0)
            else:
                self.progress.setRange(0, 100)
                self.progress.setValue(max(0, min(100, round(float(percent)))))
            total = event.get("total")
            prefix = f"[{item_number}/{total}] " if item_number and total else ""
            self._set_status(prefix + message, tone="working")
            phase = "Конвертация" if event.get("phase") == "transcode" else "Скачивание"
            row_status = f"{phase} {float(percent):.0f}%" if percent is not None else f"{phase}…"
            self._set_active_candidate_status(row_status)
        elif kind == "item_done":
            path = str(event.get("path") or "")
            if path:
                self.last_paths.append(path)
            self._set_active_candidate_status("Готово")
            self._queue_transition("mark_done", self._active_candidate(), path)
            self.active_item_in_progress = False
            self._set_status(message, tone="success")
            self._append_log("✓ " + message)
        elif kind == "item_error":
            candidate = self._active_candidate()
            if candidate is not None:
                self.failed_candidate_ids.add(candidate.video_id)
                self.candidate_model.set_candidate_status(candidate.video_id, "Ошибка")
                self._queue_transition("mark_failed", candidate, message)
            self.active_item_in_progress = False
            self._set_status("Ошибка одного видео; очередь продолжается", tone="error")
            self._append_log("✗ " + message)
        elif kind == "batch_done":
            paths = list(event.get("paths") or [])
            self.last_paths = paths or self.last_paths
            self.progress.setRange(0, 100)
            self.progress.setValue(100 if self.last_paths else 0)
            failed = int(event.get("failed") or 0)
            self.retry_button.setVisible(failed > 0)
            self.retry_button.setText(f"Повторить ошибки ({failed})")
            self._finish_status(message, "warning" if failed else "success")
            self._append_log(message)
        elif kind == "fatal":
            if self.active_item_in_progress:
                candidate = self._active_candidate()
                if candidate is not None:
                    self.failed_candidate_ids.add(candidate.video_id)
                    self._queue_transition("mark_failed", candidate, message)
                self.active_item_in_progress = False
            self._set_status("Не удалось завершить операцию", tone="error")
            self._append_log("Ошибка: " + message)
            self._show_log()
            QMessageBox.critical(self, "MMM Downloader", message)
        elif kind == "cancelled":
            if self.active_item_in_progress:
                self._queue_transition("mark_cancelled", self._active_candidate())
                self.active_item_in_progress = False
            self._set_status(message, tone="neutral")
            self._append_log(message)
        elif kind == "worker_done":
            self.worker = None
            self._set_busy(False)
            self.retry_button.setVisible(bool(self.failed_candidate_ids))
            self.retry_button.setEnabled(bool(self.failed_candidate_ids))
            if event.get("preview"):
                self.progress.setValue(0)
                self._finish_status(
                    f"Предпросмотр готов: {len(self.preview_candidates)} видео",
                    "success",
                )
            if self.closing:
                self.force_close = True
                QTimer.singleShot(0, self.close)

    def _poll_events(self) -> None:
        for _ in range(300):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)

    def _close(self) -> bool:
        if not self.worker or not self.worker.is_alive():
            return True
        if self.closing:
            return False
        answer = QMessageBox.question(
            self,
            "Загрузка выполняется",
            "Остановить текущую операцию и закрыть приложение?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self.cancel_event.set()
        self.closing = True
        self._set_status("Останавливаю операцию перед закрытием…", tone="working")
        return False

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.force_close or self._close():
            event.accept()
        else:
            event.ignore()


def _preferred_font() -> QFont:
    available = set(QFontDatabase.families())
    for family in ("Segoe UI Variable", "Segoe UI", "Inter", ".AppleSystemUIFont"):
        if family in available:
            return QFont(family, 10)
    return QFont()


def _dark_palette() -> QPalette:
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: "#09090B",
        QPalette.ColorRole.WindowText: "#F5F4F6",
        QPalette.ColorRole.Base: "#0E0E11",
        QPalette.ColorRole.AlternateBase: "#16161A",
        QPalette.ColorRole.ToolTipBase: "#1A1A1F",
        QPalette.ColorRole.ToolTipText: "#F5F4F6",
        QPalette.ColorRole.Text: "#F5F4F6",
        QPalette.ColorRole.Button: "#1A1A1F",
        QPalette.ColorRole.ButtonText: "#F5F4F6",
        QPalette.ColorRole.BrightText: "#FFFFFF",
        QPalette.ColorRole.Link: "#E960A8",
        QPalette.ColorRole.Highlight: "#E960A8",
        QPalette.ColorRole.HighlightedText: "#09090B",
        QPalette.ColorRole.PlaceholderText: "#6F6C76",
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#696771"))
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ButtonText,
        QColor("#696771"),
    )
    return palette


def _apply_windows_dark_title_bar(window: QMainWindow) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        enabled = ctypes.c_int(1)
        hwnd = int(window.winId())
        dwmapi = ctypes.windll.dwmapi
        for attribute in (20, 19):
            result = dwmapi.DwmSetWindowAttribute(
                hwnd,
                attribute,
                ctypes.byref(enabled),
                ctypes.sizeof(enabled),
            )
            if result == 0:
                break
    except (AttributeError, OSError, TypeError, ValueError):
        pass


def run_gui() -> None:
    existing = QApplication.instance()
    app = existing or QApplication(sys.argv)
    app.setApplicationName("MMM Downloader")
    app.setOrganizationName("MMM Downloader")
    app.setStyle("Fusion")
    app.setFont(_preferred_font())
    app.setPalette(_dark_palette())
    app.setStyleSheet(_load_stylesheet())

    icon_path = _resource_path("assets/mmm_downloader.png")
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))

    window = DownloaderApp()
    window.show()
    if existing is None:
        app.exec()
