from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QLabel,
    QMessageBox,
    QStyle,
    QStyleOptionButton,
    QWidget,
)

from ytmax.ai_intent import AIIntentError, ApiKeyCheck  # noqa: E402
from ytmax.api_keys import ApiKeyStore  # noqa: E402
from ytmax.gui import (  # noqa: E402
    ApiKeyDialog,
    CandidateTableModel,
    DownloaderApp,
    _duration,
    _load_stylesheet,
    _resource_path,
    _views,
)
from ytmax.models import Candidate, DownloadIntent  # noqa: E402
from ytmax.queue_store import QueueState, QueueStore  # noqa: E402
from ytmax.settings import ApiKeyProfile, AppSettings  # noqa: E402


class FakeBackend:
    def __init__(self, passwords: dict[tuple[str, str], str] | None = None) -> None:
        self.passwords = passwords or {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.passwords.pop((service, username), None)


class GuiResourceTests(unittest.TestCase):
    def test_source_icon_is_available(self) -> None:
        icon_path = _resource_path("assets/mmm_downloader.png")

        self.assertTrue(icon_path.is_file())
        self.assertEqual(icon_path.name, "mmm_downloader.png")

    def test_frozen_resource_uses_pyinstaller_root(self) -> None:
        with patch.object(sys, "_MEIPASS", "/tmp/mmm-bundle", create=True):
            path = _resource_path("assets/mmm_downloader.png")

        self.assertEqual(path, Path("/tmp/mmm-bundle/assets/mmm_downloader.png"))

    def test_windows_icon_contains_common_sizes(self) -> None:
        icon_path = _resource_path("assets/mmm_downloader.ico")
        payload = icon_path.read_bytes()
        reserved, image_type, count = struct.unpack_from("<HHH", payload)
        sizes = []
        for index in range(count):
            width, height = struct.unpack_from("<BB", payload, 6 + (16 * index))
            sizes.append((width or 256, height or 256))

        self.assertEqual((reserved, image_type), (0, 1))
        self.assertEqual(
            sizes,
            [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )

    def test_brand_stylesheet_is_available(self) -> None:
        stylesheet = _load_stylesheet()

        self.assertIn("#E960A8", stylesheet)
        self.assertIn("QFrame[card=\"true\"]", stylesheet)


class GuiFormattingTests(unittest.TestCase):
    def test_duration_and_views_are_human_readable(self) -> None:
        self.assertEqual(_duration(65), "1:05")
        self.assertEqual(_duration(3_661), "1:01:01")
        self.assertEqual(_views(1_234_567), "1 234 567")

    def test_candidate_model_tracks_status(self) -> None:
        model = CandidateTableModel()
        model.set_candidates(
            [
                Candidate(
                    video_id="abc",
                    title="Art Deco",
                    thumbnail_url="https://i.ytimg.com/vi/abc/hqdefault.jpg",
                    channel="Studio",
                    duration_seconds=610,
                    view_count=1200,
                )
            ]
        )

        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.data(model.index(0, 0)), "Art Deco")
        self.assertEqual(
            model.data(model.index(0, 0), Qt.ItemDataRole.CheckStateRole),
            Qt.CheckState.Checked,
        )
        self.assertEqual(
            model.data(model.index(0, 0), CandidateTableModel.LINK_ROLE),
            "https://www.youtube.com/watch?v=abc",
        )
        self.assertEqual(
            model.data(model.index(0, 0), CandidateTableModel.THUMBNAIL_URL_ROLE),
            "https://i.ytimg.com/vi/abc/hqdefault.jpg",
        )
        self.assertEqual(model.data(model.index(0, 4)), "Найдено")

        model.set_item_status(1, "Скачивание 42%")

        self.assertEqual(model.data(model.index(0, 4)), "Скачивание 42%")
        self.assertEqual(
            model.data(model.index(0, 1), Qt.ItemDataRole.TextAlignmentRole),
            Qt.AlignmentFlag.AlignCenter,
        )

        self.assertTrue(
            model.setData(
                model.index(0, 0),
                Qt.CheckState.Unchecked,
                Qt.ItemDataRole.CheckStateRole,
            )
        )
        self.assertEqual(model.checked_candidates(), [])


class GuiWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_window_uses_modern_layout_and_native_h264_default(self) -> None:
        with patch("ytmax.gui.load_settings", return_value=AppSettings()):
            window = DownloaderApp()
        try:
            self.assertEqual(window.quality_combo.currentData(), "native_h264")
            self.assertEqual(window.centralWidget().objectName(), "AppRoot")
            self.assertEqual(window.input_text.placeholderText(), "Скачать весь метал с YT")
            self.assertFalse(window.log_dialog.isVisible())
            self.assertEqual(window.candidate_model.columnCount(), 5)
            self.assertEqual(
                window.candidate_model.headerData(
                    0,
                    Qt.Orientation.Horizontal,
                    Qt.ItemDataRole.DisplayRole,
                ),
                "Превью, видео и ссылка",
            )
            self.assertEqual(window.preview_button.objectName(), "PrimaryButton")
            self.assertNotEqual(window.download_button.objectName(), "PrimaryButton")
            labels = [label.text() for label in window.findChildren(QLabel)]
            self.assertNotIn(
                "Видео и умные подборки — в максимальном качестве",
                labels,
            )
        finally:
            window.event_timer.stop()
            window.force_close = True
            window.close()

    def test_settings_card_keeps_controls_separated_at_minimum_window_size(self) -> None:
        previous_stylesheet = self.app.styleSheet()
        self.app.setStyleSheet(_load_stylesheet())
        with (
            patch("ytmax.gui.load_settings", return_value=AppSettings(use_ai=True)),
            patch.object(DownloaderApp, "_offer_resume_queue"),
        ):
            window = DownloaderApp()
        try:
            window.resize(window.minimumSize())
            window.show()
            self.app.processEvents()

            labels = {label.text(): label for label in window.findChildren(QLabel)}
            quality_label = labels["Качество"]
            cookies_label = labels["Cookies браузера"]
            api_key_label = labels["API-ключ OpenAI или OpenRouter"]

            def top(widget: QWidget) -> int:
                return widget.mapTo(window, QPoint()).y()

            def bottom(widget: QWidget) -> int:
                return top(widget) + widget.height()

            # These are lower bounds, not pixel-perfect snapshots. They ensure that
            # a constrained window cannot compress adjacent controls into each other.
            minimum_readable_gap = 2
            vertical_pairs = (
                ("quality label/field", quality_label, window.quality_combo),
                ("quality/cookies label", window.quality_combo, cookies_label),
                ("cookies label/field", cookies_label, window.cookies_combo),
                ("cookies/AI checkbox", window.cookies_combo, window.ai_checkbox),
                ("AI checkbox/API label", window.ai_checkbox, api_key_label),
                ("API label/key field", api_key_label, window.api_key_combo),
            )
            for name, upper, lower in vertical_pairs:
                with self.subTest(pair=name):
                    self.assertGreaterEqual(
                        top(lower) - bottom(upper),
                        minimum_readable_gap,
                    )

            key_row_bottom = max(
                bottom(window.api_key_combo),
                bottom(window.add_api_key_button),
                bottom(window.delete_api_key_button),
            )
            self.assertGreaterEqual(
                top(window.ai_model_combo) - key_row_bottom,
                minimum_readable_gap,
            )

            label_to_field_gaps = (
                top(window.quality_combo) - bottom(quality_label),
                top(window.cookies_combo) - bottom(cookies_label),
                top(window.api_key_combo) - bottom(api_key_label),
            )
            self.assertLessEqual(
                max(label_to_field_gaps) - min(label_to_field_gaps),
                2,
            )

            settings_card = window.quality_combo.parentWidget()
            self.assertLessEqual(
                settings_card.minimumSizeHint().width(),
                settings_card.maximumWidth(),
            )
            self.assertGreaterEqual(
                settings_card.height(),
                settings_card.minimumSizeHint().height(),
            )
            self.assertGreaterEqual(
                window.ai_fields.height(),
                window.ai_fields.minimumSizeHint().height(),
            )
        finally:
            window.event_timer.stop()
            window.force_close = True
            window.close()
            self.app.setStyleSheet(previous_stylesheet)

    def test_api_key_action_buttons_have_one_square_geometry_and_typography(self) -> None:
        previous_stylesheet = self.app.styleSheet()
        self.app.setStyleSheet(_load_stylesheet())
        with (
            patch("ytmax.gui.load_settings", return_value=AppSettings(use_ai=True)),
            patch.object(DownloaderApp, "_offer_resume_queue"),
        ):
            window = DownloaderApp()
        try:
            window.resize(window.minimumSize())
            window.show()
            self.app.processEvents()

            add_button = window.add_api_key_button
            delete_button = window.delete_api_key_button
            self.assertEqual(add_button.size(), delete_button.size())
            self.assertEqual(add_button.width(), add_button.height())
            self.assertEqual(
                add_button.fontMetrics().height(),
                delete_button.fontMetrics().height(),
            )

            def window_rect(widget: QWidget) -> QRect:
                return QRect(
                    widget.mapTo(window, QPoint()),
                    widget.size(),
                )

            combo_rect = window_rect(window.api_key_combo)
            add_rect = window_rect(add_button)
            delete_rect = window_rect(delete_button)
            self.assertFalse(combo_rect.intersects(add_rect))
            self.assertFalse(add_rect.intersects(delete_rect))
            self.assertLessEqual(abs(add_rect.center().y() - delete_rect.center().y()), 1)

            combo_to_add_gap = add_rect.left() - (combo_rect.left() + combo_rect.width())
            add_to_delete_gap = delete_rect.left() - (add_rect.left() + add_rect.width())
            self.assertLessEqual(abs(combo_to_add_gap - add_to_delete_gap), 1)
        finally:
            window.event_timer.stop()
            window.force_close = True
            window.close()
            self.app.setStyleSheet(previous_stylesheet)

    def test_ai_checkbox_indicator_has_room_for_checkmark_and_label(self) -> None:
        previous_stylesheet = self.app.styleSheet()
        self.app.setStyleSheet(_load_stylesheet())
        with patch("ytmax.gui.load_settings", return_value=AppSettings(use_ai=True)):
            window = DownloaderApp()
        try:
            window.show()
            self.app.processEvents()

            option = QStyleOptionButton()
            window.ai_checkbox.initStyleOption(option)
            style = window.ai_checkbox.style()
            indicator = style.subElementRect(
                QStyle.SubElement.SE_CheckBoxIndicator,
                option,
                window.ai_checkbox,
            )
            contents = style.subElementRect(
                QStyle.SubElement.SE_CheckBoxContents,
                option,
                window.ai_checkbox,
            )
            self.assertTrue(window.ai_checkbox.rect().contains(indicator))
            self.assertTrue(window.ai_checkbox.rect().contains(contents))
            self.assertFalse(indicator.intersects(contents))
            self.assertEqual(indicator.width(), indicator.height())
            self.assertGreaterEqual(
                contents.left() - (indicator.left() + indicator.width()),
                4,
            )

            checkmark = QSvgRenderer(str(_resource_path("assets/check.svg")))
            self.assertTrue(checkmark.isValid())
            checkmark_size = checkmark.defaultSize()
            self.assertGreaterEqual(indicator.width() - checkmark_size.width(), 4)
            self.assertGreaterEqual(indicator.height() - checkmark_size.height(), 4)
        finally:
            window.event_timer.stop()
            window.force_close = True
            window.close()
            self.app.setStyleSheet(previous_stylesheet)

    def test_preview_cell_shows_thumbnail_and_explicit_link_opens_video(self) -> None:
        candidate = Candidate(
            video_id="preview-id",
            title="Интерьер",
            url="https://www.youtube.com/watch?v=preview-id",
        )
        with patch("ytmax.gui.load_settings", return_value=AppSettings()):
            window = DownloaderApp()
        try:
            with patch.object(window.thumbnail_loader, "load") as load_thumbnail:
                window._show_candidates([candidate])
            load_thumbnail.assert_called_once_with(candidate)

            pixmap = QPixmap(16, 9)
            pixmap.fill(QColor("#E960A8"))
            window.candidate_model.set_thumbnail(candidate.video_id, pixmap)
            stored = window.candidate_model.data(
                window.candidate_model.index(0, CandidateTableModel.PREVIEW_COLUMN),
                Qt.ItemDataRole.DecorationRole,
            )
            self.assertIsInstance(stored, QPixmap)
            self.assertFalse(stored.isNull())

            with patch("ytmax.gui.QDesktopServices.openUrl", return_value=True) as open_url:
                window.preview_delegate.link_activated.emit(0)

            open_url.assert_called_once()
            self.assertEqual(
                open_url.call_args.args[0].toString(),
                candidate.watch_url,
            )
        finally:
            window.event_timer.stop()
            window.force_close = True
            window.close()

    def test_checked_rows_control_download_selection(self) -> None:
        first = Candidate(video_id="first", title="Первое")
        second = Candidate(video_id="second", title="Второе")
        with patch("ytmax.gui.load_settings", return_value=AppSettings()):
            window = DownloaderApp()
        try:
            with patch.object(window.thumbnail_loader, "load"):
                window._show_candidates([first, second])
            self.assertEqual(window.download_found_button.text(), "Скачать выбранные (2)")

            window.candidate_model.setData(
                window.candidate_model.index(0, CandidateTableModel.PREVIEW_COLUMN),
                Qt.CheckState.Unchecked,
                Qt.ItemDataRole.CheckStateRole,
            )
            self.assertEqual(window.candidate_model.checked_candidates(), [second])
            self.assertEqual(window.download_found_button.text(), "Скачать выбранные (1)")

            with patch.object(window, "_start_candidate_download") as start_download:
                window._download_found()
            start_download.assert_called_once_with([second])

            window._deselect_all()
            self.assertFalse(window.download_found_button.isEnabled())
            window._select_all()
            self.assertEqual(window.candidate_model.checked_candidates(), [first, second])
        finally:
            window.event_timer.stop()
            window.force_close = True
            window.close()

    def test_saved_api_key_profiles_populate_combo_and_selected_key_resolves(self) -> None:
        first_id = "a" * 32
        second_id = "b" * 32
        settings = AppSettings(
            api_key_profiles=[
                ApiKeyProfile(first_id, "Личный", "••••1111"),
                ApiKeyProfile(second_id, "Рабочий", "••••2222"),
            ],
            selected_api_key_id=second_id,
        )
        with patch("ytmax.gui.load_settings", return_value=settings):
            window = DownloaderApp()
        try:
            window.api_key_store = ApiKeyStore(
                FakeBackend({("com.mmm.downloader.openai", second_id): "sk-test-2222"})
            )

            self.assertEqual(window.api_key_combo.count(), 2)
            self.assertEqual(window.api_key_combo.itemText(0), "Личный · ••••1111")
            self.assertEqual(window.api_key_combo.itemText(1), "Рабочий · ••••2222")
            self.assertEqual(window.api_key_combo.currentData(), second_id)
            self.assertEqual(window._selected_api_key(), "sk-test-2222")
            self.assertTrue(window.delete_api_key_button.isEnabled())
        finally:
            window.event_timer.stop()
            window.force_close = True
            window.close()

    def test_busy_state_locks_request_controls(self) -> None:
        with patch("ytmax.gui.load_settings", return_value=AppSettings()):
            window = DownloaderApp()
        try:
            window._set_busy(True)

            self.assertFalse(window.input_text.isEnabled())
            self.assertFalse(window.quality_combo.isEnabled())
            self.assertFalse(window.api_key_combo.isEnabled())
            self.assertFalse(window.add_api_key_button.isEnabled())
            self.assertFalse(window.delete_api_key_button.isEnabled())
            self.assertTrue(window.cancel_button.isEnabled())
            self.assertEqual((window.progress.minimum(), window.progress.maximum()), (0, 0))
        finally:
            window.event_timer.stop()
            window.force_close = True
            window.close()

    def test_preview_can_start_without_output_folder(self) -> None:
        with patch("ytmax.gui.load_settings", return_value=AppSettings()):
            window = DownloaderApp()
        try:
            window.input_text.setPlainText("https://youtu.be/preview")
            with (
                patch("ytmax.gui.threading.Thread") as thread_class,
                patch("ytmax.gui.QMessageBox.warning") as warning,
            ):
                window._start(preview_only=True)

                warning.assert_not_called()
                thread_class.assert_called_once()
                thread_class.return_value.start.assert_called_once()
        finally:
            window.worker = None
            window.event_timer.stop()
            window.force_close = True
            window.close()

    def test_queue_state_follows_download_events_and_persists_result(self) -> None:
        candidate = Candidate(
            video_id="queued-video",
            title="Сохранённое видео",
            thumbnail_url="https://i.ytimg.com/vi/queued-video/mqdefault.jpg",
        )
        with tempfile.TemporaryDirectory() as directory:
            queue_store = QueueStore(Path(directory) / "settings")
            settings = AppSettings(output_dir=str(Path(directory) / "videos"))
            with (
                patch("ytmax.gui.load_settings", return_value=settings),
                patch("ytmax.gui.QueueStore", return_value=queue_store),
            ):
                window = DownloaderApp()
            try:
                with patch.object(window.thumbnail_loader, "load"):
                    window._show_candidates([candidate])
                window.active_batch_candidates = [candidate]
                window._persist_queue([candidate], settings)
                window._handle_event(
                    {"kind": "item_start", "item": 1, "total": 1, "message": "Старт"}
                )
                self.assertEqual(
                    queue_store.get(candidate.video_id).state,  # type: ignore[union-attr]
                    QueueState.DOWNLOADING,
                )
                result_path = str(Path(directory) / "videos" / "result.mp4")
                window._handle_event(
                    {
                        "kind": "item_done",
                        "item": 1,
                        "total": 1,
                        "path": result_path,
                        "message": "Готово",
                    }
                )
                stored = queue_store.get(candidate.video_id)
                self.assertEqual(stored.state, QueueState.DONE)  # type: ignore[union-attr]
                self.assertEqual(stored.output_path, result_path)  # type: ignore[union-attr]
            finally:
                window.event_timer.stop()
                window.force_close = True
                window.close()

    def test_unfinished_queue_restores_candidates_and_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue_store = QueueStore(Path(directory) / "settings")
            output_dir = str(Path(directory) / "restored-videos")
            queue_store.replace(
                [
                    {
                        "video_id": "pending-video",
                        "url": "https://youtu.be/pending-video",
                        "metadata": {"title": "Ожидает"},
                    },
                    {
                        "video_id": "failed-video",
                        "url": "https://youtu.be/failed-video",
                        "metadata": {"title": "С ошибкой"},
                    },
                ],
                settings={
                    "output_dir": output_dir,
                    "quality_mode": "true_max_h264",
                    "cookie_browser": "firefox",
                },
            )
            queue_store.mark_downloading("failed-video")
            queue_store.mark_failed("failed-video", "network")
            with (
                patch("ytmax.gui.load_settings", return_value=AppSettings()),
                patch("ytmax.gui.QueueStore", return_value=queue_store),
            ):
                window = DownloaderApp()
            try:
                with (
                    patch.object(window.thumbnail_loader, "load"),
                    patch(
                        "ytmax.gui.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes,
                    ),
                ):
                    window._offer_resume_queue()

                self.assertEqual(
                    [item.video_id for item in window.preview_candidates],
                    ["pending-video", "failed-video"],
                )
                self.assertEqual(window.output_edit.text(), output_dir)
                self.assertEqual(window.quality_combo.currentData(), "true_max_h264")
                self.assertEqual(window.cookies_combo.currentData(), "firefox")
                self.assertEqual(window.failed_candidate_ids, {"failed-video"})
                self.assertFalse(window.retry_button.isHidden())
            finally:
                window.event_timer.stop()
                window.force_close = True
                window.close()


class ApiKeyDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self, check: ApiKeyCheck | None) -> tuple[ApiKeyDialog, list[dict]]:
        calls: list[dict] = []

        def verifier(secret: str, *, provider: str, model: str) -> ApiKeyCheck:
            calls.append({"secret": secret, "provider": provider, "model": model})
            assert check is not None
            return check

        dialog = ApiKeyDialog("Ключ 1", "gpt-5.6-luna", verifier=verifier)
        dialog.name_edit.setText("Рабочий")
        dialog.secret_edit.setText("sk-test-key-0000-1111")
        return dialog, calls

    def _run_check(self, dialog: ApiKeyDialog) -> None:
        dialog._verify_and_accept()
        dialog.wait_for_check()
        self.app.processEvents()

    def test_key_is_saved_only_after_openai_confirms_it(self) -> None:
        dialog, calls = self._dialog(
            ApiKeyCheck(ok=True, message="Ключ работает.", model_available=True)
        )
        try:
            self._run_check(dialog)

            self.assertEqual(dialog.result(), int(QDialog.DialogCode.Accepted))
            self.assertEqual(
                calls,
                [
                    {
                        "secret": "sk-test-key-0000-1111",
                        "provider": "openai",
                        "model": "gpt-5.6-luna",
                    }
                ],
            )
            self.assertEqual(dialog.provider, "openai")
            self.assertEqual(dialog.check_note()[1], "success")
        finally:
            dialog.deleteLater()

    def test_rejected_key_keeps_the_dialog_open_with_the_reason(self) -> None:
        dialog, _ = self._dialog(
            ApiKeyCheck(ok=False, message="OpenAI отклонил ключ (401).", reason="auth")
        )
        try:
            self._run_check(dialog)

            self.assertNotEqual(dialog.result(), int(QDialog.DialogCode.Accepted))
            self.assertIn("401", dialog.status_label.text())
            self.assertEqual(dialog.status_label.property("tone"), "error")
            self.assertFalse(dialog.save_anyway_button.isVisibleTo(dialog))
            self.assertTrue(dialog.secret_edit.isEnabled())
        finally:
            dialog.deleteLater()

    def test_network_failure_offers_saving_without_a_check(self) -> None:
        dialog, _ = self._dialog(
            ApiKeyCheck(ok=False, message="OpenAI API недоступен", reason="network")
        )
        try:
            self._run_check(dialog)

            self.assertNotEqual(dialog.result(), int(QDialog.DialogCode.Accepted))
            self.assertTrue(dialog.save_anyway_button.isVisibleTo(dialog))

            dialog._accept_without_check()

            self.assertEqual(dialog.result(), int(QDialog.DialogCode.Accepted))
            self.assertEqual(dialog.check_note()[1], "warning")
        finally:
            dialog.deleteLater()

    def test_malformed_key_is_reported_without_asking_openai(self) -> None:
        dialog, calls = self._dialog(None)
        dialog.secret_edit.setText("совсем не ключ")
        try:
            self._run_check(dialog)

            self.assertEqual(calls, [])
            self.assertNotEqual(dialog.result(), int(QDialog.DialogCode.Accepted))
            self.assertEqual(dialog.status_label.property("tone"), "error")
            self.assertTrue(dialog.status_label.text())
        finally:
            dialog.deleteLater()

    def test_openrouter_key_switches_provider_and_its_default_model(self) -> None:
        dialog, calls = self._dialog(
            ApiKeyCheck(
                ok=True,
                message="Ключ OpenRouter принят.",
                provider="openrouter",
            )
        )
        dialog.secret_edit.setText("sk-or-v1-0000111122223333")
        try:
            self._run_check(dialog)

            self.assertEqual(dialog.result(), int(QDialog.DialogCode.Accepted))
            self.assertEqual(dialog.provider, "openrouter")
            # Модель из главного окна принадлежала OpenAI, у чужого ключа она
            # не заработала бы — проверяем модель по умолчанию OpenRouter.
            self.assertEqual(calls[0]["provider"], "openrouter")
            self.assertEqual(calls[0]["model"], "openai/gpt-5.6-luna")
        finally:
            dialog.deleteLater()

    def test_long_reason_never_covers_the_dialog_buttons(self) -> None:
        dialog, _ = self._dialog(
            ApiKeyCheck(
                ok=False,
                message=(
                    "OpenAI отклонил ключ (401). Скопируйте ключ целиком из личного "
                    "кабинета OpenAI и проверьте, что он не отозван."
                ),
                reason="auth",
            )
        )
        try:
            dialog.show()
            self.app.processEvents()
            self._run_check(dialog)

            def bottom(widget: QWidget) -> int:
                return widget.mapTo(dialog, widget.rect().bottomLeft()).y()

            def top(widget: QWidget) -> int:
                return widget.mapTo(dialog, widget.rect().topLeft()).y()

            self.assertLess(bottom(dialog.status_label), top(dialog.save_button))
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_editing_the_key_drops_the_previous_verdict(self) -> None:
        dialog, _ = self._dialog(
            ApiKeyCheck(ok=False, message="OpenAI отклонил ключ (401).", reason="auth")
        )
        try:
            self._run_check(dialog)
            self.assertTrue(dialog.status_label.text())

            dialog.secret_edit.setText("sk-test-key-0000-2222")

            self.assertIsNone(dialog.check)
            self.assertEqual(dialog.status_label.text(), "")
        finally:
            dialog.deleteLater()


class AiStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _window(self) -> DownloaderApp:
        settings = AppSettings(
            use_ai=True,
            api_key_profiles=[ApiKeyProfile("a" * 32, "Рабочий", "••••1111")],
            selected_api_key_id="a" * 32,
        )
        with (
            patch("ytmax.gui.load_settings", return_value=settings),
            patch.object(DownloaderApp, "_offer_resume_queue"),
        ):
            return DownloaderApp()

    def test_failed_ai_parse_is_visible_and_survives_to_the_summary(self) -> None:
        window = self._window()
        settings = window._settings_from_ui()
        try:
            with patch(
                "ytmax.gui.parse_intent_with_ai",
                side_effect=AIIntentError("Модель «gpt-5.4-mini» недоступна", reason="model"),
            ):
                intent = window._parse("скачай метал", settings, "sk-key")
            window._poll_events()

            self.assertEqual(intent.topic, "метал")
            self.assertTrue(window.ai_status_label.isVisibleTo(window))
            self.assertIn("gpt-5.4-mini", window.ai_status_label.text())
            self.assertIn("локально", window.ai_status_label.text())
            self.assertEqual(window.ai_status_label.property("tone"), "warning")

            window._handle_event({"kind": "batch_done", "message": "Готово: 3 видео", "failed": 0})

            self.assertIn("нейро-разбор не применён", window.status_label.text())
        finally:
            window.event_timer.stop()
            window.force_close = True
            window.close()

    def test_successful_ai_parse_names_the_model(self) -> None:
        window = self._window()
        settings = window._settings_from_ui()
        plan = DownloadIntent(topic="метал", count=5)
        try:
            with patch("ytmax.gui.parse_intent_with_ai", return_value=plan):
                intent = window._parse("скачай метал", settings, "sk-key")
            window._poll_events()

            self.assertIs(intent, plan)
            self.assertEqual(window.last_ai_warning, "")
            self.assertIn(settings.ai_model, window.ai_status_label.text())
            self.assertEqual(window.ai_status_label.property("tone"), "success")
        finally:
            window.event_timer.stop()
            window.force_close = True
            window.close()

    def test_link_request_says_the_key_was_not_needed(self) -> None:
        window = self._window()
        settings = window._settings_from_ui()
        try:
            with patch("ytmax.gui.parse_intent_with_ai") as ai_parse:
                window._parse("https://youtu.be/abc", settings, "sk-key")
            window._poll_events()

            ai_parse.assert_not_called()
            self.assertIn("ключ не применялся", window.ai_status_label.text())
            self.assertEqual(window.last_ai_warning, "")
        finally:
            window.event_timer.stop()
            window.force_close = True
            window.close()

    def test_model_list_follows_the_provider_of_the_selected_key(self) -> None:
        openai_id = "a" * 32
        openrouter_id = "b" * 32
        settings = AppSettings(
            use_ai=True,
            ai_model="gpt-5.6-terra",
            api_key_profiles=[
                ApiKeyProfile(openai_id, "OpenAI", "••••1111", provider="openai"),
                ApiKeyProfile(
                    openrouter_id, "OpenRouter", "••••2222", provider="openrouter"
                ),
            ],
            selected_api_key_id=openai_id,
        )
        with (
            patch("ytmax.gui.load_settings", return_value=settings),
            patch.object(DownloaderApp, "_offer_resume_queue"),
        ):
            window = DownloaderApp()

        def models() -> list[str]:
            return [
                window.ai_model_combo.itemText(index)
                for index in range(window.ai_model_combo.count())
            ]

        try:
            self.assertEqual(window._selected_provider(), "openai")
            self.assertEqual(window.ai_model_combo.currentText(), "gpt-5.6-terra")
            self.assertTrue(all("/" not in model for model in models()))

            with patch("ytmax.gui.save_settings"):
                window.api_key_combo.setCurrentIndex(1)

            self.assertEqual(window._selected_provider(), "openrouter")
            self.assertTrue(all("/" in model for model in models()))
            self.assertEqual(window.ai_model_combo.currentText(), "openai/gpt-5.6-luna")
            self.assertIn("OpenRouter", window.ai_status_label.text())

            with patch("ytmax.gui.save_settings"):
                window.api_key_combo.setCurrentIndex(0)

            self.assertEqual(window.ai_model_combo.currentText(), "gpt-5.6-luna")
        finally:
            window.event_timer.stop()
            window.force_close = True
            window.close()

    def test_custom_model_of_the_same_provider_is_kept(self) -> None:
        settings = AppSettings(
            use_ai=True,
            ai_model="anthropic/claude-haiku-4.5",
            api_key_profiles=[
                ApiKeyProfile("c" * 32, "OpenRouter", "••••3333", provider="openrouter")
            ],
            selected_api_key_id="c" * 32,
        )
        with (
            patch("ytmax.gui.load_settings", return_value=settings),
            patch.object(DownloaderApp, "_offer_resume_queue"),
        ):
            window = DownloaderApp()
        try:
            self.assertEqual(
                window.ai_model_combo.currentText(),
                "anthropic/claude-haiku-4.5",
            )
            self.assertEqual(window._current_ai_model(), "anthropic/claude-haiku-4.5")
        finally:
            window.event_timer.stop()
            window.force_close = True
            window.close()

    def test_openrouter_key_is_named_in_the_status(self) -> None:
        settings = AppSettings(
            use_ai=True,
            ai_model="openai/gpt-5.6-luna",
            api_key_profiles=[
                ApiKeyProfile("d" * 32, "Тестовый", "••••4444", provider="openrouter")
            ],
            selected_api_key_id="d" * 32,
        )
        with (
            patch("ytmax.gui.load_settings", return_value=settings),
            patch.object(DownloaderApp, "_offer_resume_queue"),
        ):
            window = DownloaderApp()
        plan = DownloadIntent(topic="метал", count=5)
        try:
            with patch("ytmax.gui.parse_intent_with_ai", return_value=plan) as ai_parse:
                window._parse(
                    "скачай метал",
                    window._settings_from_ui(),
                    "sk-or-v1-key",
                    "openrouter",
                )
            window._poll_events()

            self.assertEqual(ai_parse.call_args.kwargs["provider"], "openrouter")
            self.assertEqual(
                ai_parse.call_args.kwargs["model"],
                "openai/gpt-5.6-luna",
            )
            self.assertIn("OpenRouter", window.ai_status_label.text())
            self.assertEqual(window.ai_status_label.property("tone"), "success")
        finally:
            window.event_timer.stop()
            window.force_close = True
            window.close()

    def test_missing_key_is_stated_next_to_the_request(self) -> None:
        with (
            patch("ytmax.gui.load_settings", return_value=AppSettings(use_ai=True)),
            patch.object(DownloaderApp, "_offer_resume_queue"),
        ):
            window = DownloaderApp()
        try:
            self.assertTrue(window.ai_status_label.isVisibleTo(window))
            self.assertIn("ключ не добавлен", window.ai_status_label.text().lower())
            self.assertEqual(window.ai_status_label.property("tone"), "warning")

            window.ai_checkbox.setChecked(False)

            self.assertEqual(window.ai_status_label.text(), "")
        finally:
            window.event_timer.stop()
            window.force_close = True
            window.close()


if __name__ == "__main__":
    unittest.main()
