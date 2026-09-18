from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ytmax.settings import (
    AI_PROVIDERS,
    DEFAULT_AI_MODEL,
    DEFAULT_PROVIDER,
    ApiKeyProfile,
    AppSettings,
    load_settings,
    prepare_output_directory,
    safe_filename,
    save_settings,
    settings_dir,
    unique_path,
)


class SettingsTests(unittest.TestCase):
    def test_tests_never_reach_the_real_user_profile(self) -> None:
        # Без изоляции DownloaderApp находил живую очередь пользователя и
        # показывал модальный вопрос о её восстановлении, из-за чего прогон
        # тестов (и сборка, которая их запускает) вставал навсегда.
        isolated_root = os.environ.get("MMM_TEST_APP_DATA", "")

        self.assertTrue(isolated_root, "фикстура изоляции не сработала")
        self.assertTrue(
            settings_dir().is_relative_to(Path(isolated_root)),
            f"настройки ведут наружу: {settings_dir()}",
        )

    def test_native_h264_is_the_default(self) -> None:
        self.assertEqual(AppSettings().quality_mode, "native_h264")

    def test_every_provider_offers_its_own_default_model(self) -> None:
        self.assertEqual(AppSettings().ai_model, DEFAULT_AI_MODEL)
        for key, provider in AI_PROVIDERS.items():
            with self.subTest(provider=key):
                self.assertEqual(provider.key, key)
                self.assertIn(provider.default_model, provider.models)
                self.assertTrue(provider.base_url.startswith("https://"))
        # У OpenRouter идентификаторы вида «автор/модель», у OpenAI — без косой
        # черты; на этом различии держится выбор модели при смене ключа.
        self.assertTrue(
            all("/" in model for model in AI_PROVIDERS["openrouter"].models)
        )
        self.assertFalse(
            any("/" in model for model in AI_PROVIDERS["openai"].models)
        )

    def test_key_profile_provider_is_validated_on_load(self) -> None:
        key_id = "a" * 32
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch("ytmax.settings.settings_path", return_value=path):
                for stored, expected in (
                    ("openrouter", "openrouter"),
                    ("openai", "openai"),
                    ("nonsense", DEFAULT_PROVIDER),
                    (None, DEFAULT_PROVIDER),
                ):
                    profile = {"key_id": key_id, "name": "Рабочий", "hint": "••••1111"}
                    if stored is not None:
                        profile["provider"] = stored
                    path.write_text(
                        json.dumps({"api_key_profiles": [profile]}),
                        encoding="utf-8",
                    )
                    with self.subTest(stored=stored):
                        loaded = load_settings().api_key_profiles[0]
                        self.assertEqual(loaded.provider, expected)

    def test_retired_default_model_is_migrated_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps({"use_ai": True, "ai_model": "gpt-5.4-mini"}),
                encoding="utf-8",
            )
            with patch("ytmax.settings.settings_path", return_value=path):
                settings = load_settings()

        self.assertEqual(settings.ai_model, DEFAULT_AI_MODEL)

    def test_custom_and_missing_ai_models_are_left_to_the_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch("ytmax.settings.settings_path", return_value=path):
                path.write_text(json.dumps({"ai_model": "my-own-model"}), encoding="utf-8")
                custom = load_settings()
                path.write_text(json.dumps({"ai_model": "  "}), encoding="utf-8")
                blank = load_settings()

        self.assertEqual(custom.ai_model, "my-own-model")
        self.assertEqual(blank.ai_model, DEFAULT_AI_MODEL)

    def test_filename_is_safe_for_windows(self) -> None:
        filename = safe_filename('../../CON: bad? <name> | "x".', "abc123")

        self.assertEqual(filename, "_.._CON_ bad_ _name_ _ _x_ [abc123].mp4")
        self.assertNotIn("/", filename)
        self.assertNotIn("\\", filename)

    def test_reserved_windows_filename_gets_prefix(self) -> None:
        self.assertEqual(safe_filename("CON", "id"), "_CON [id].mp4")
        self.assertEqual(safe_filename("CON.txt", "id"), "_CON.txt [id].mp4")

    def test_long_filename_is_bounded(self) -> None:
        self.assertLessEqual(len(safe_filename("а" * 500, "video-id")), 120)

    def test_unique_path_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Video [id].mp4").touch()
            path = unique_path(root, "Video [id].mp4")

            self.assertEqual(path.name, "Video [id] (2).mp4")

    def test_output_directory_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "new" / "videos"

            self.assertEqual(prepare_output_directory(str(target)), target.resolve())
            self.assertTrue(target.is_dir())

    def test_settings_with_wrong_json_shape_fall_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("[]", encoding="utf-8")
            with patch("ytmax.settings.settings_path", return_value=path):
                self.assertEqual(load_settings(), AppSettings())

    def test_api_key_profiles_are_filtered_and_invalid_selection_falls_back(self) -> None:
        first_id = "a" * 32
        second_id = "b" * 32
        payload = {
            "api_key_profiles": [
                {"key_id": first_id, "name": "Первый", "hint": "••••1111"},
                {"key_id": first_id, "name": "Дубликат", "hint": "••••9999"},
                {"key_id": "NOT-A-KEY-ID", "name": "Неверный", "hint": "••••0000"},
                {"key_id": second_id, "name": "Второй", "hint": "••••2222"},
                {"key_id": "c" * 32, "name": "", "hint": "••••3333"},
                "wrong shape",
            ],
            "selected_api_key_id": "d" * 32,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with patch("ytmax.settings.settings_path", return_value=path):
                settings = load_settings()

        self.assertEqual(
            settings.api_key_profiles,
            [
                ApiKeyProfile(first_id, "Первый", "••••1111"),
                ApiKeyProfile(second_id, "Второй", "••••2222"),
            ],
        )
        self.assertEqual(settings.selected_api_key_id, first_id)

    def test_valid_selected_api_key_is_preserved(self) -> None:
        first_id = "a" * 32
        second_id = "b" * 32
        payload = {
            "api_key_profiles": [
                {"key_id": first_id, "name": "Первый", "hint": "••••1111"},
                {"key_id": second_id, "name": "Второй", "hint": "••••2222"},
            ],
            "selected_api_key_id": second_id,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with patch("ytmax.settings.settings_path", return_value=path):
                settings = load_settings()

        self.assertEqual(settings.selected_api_key_id, second_id)

    def test_saved_settings_contain_metadata_but_never_an_api_secret(self) -> None:
        key_id = "a" * 32
        secret = "sk-secret-that-must-not-be-written"
        settings = AppSettings(
            api_key_profiles=[
                ApiKeyProfile(key_id, "Рабочий", "••••4321", provider="openrouter")
            ],
            selected_api_key_id=key_id,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("ytmax.settings.settings_dir", return_value=root):
                save_settings(settings)
            raw_text = (root / "settings.json").read_text(encoding="utf-8")
            payload = json.loads(raw_text)

        self.assertNotIn(secret, raw_text)
        self.assertEqual(
            payload["api_key_profiles"],
            [
                {
                    "key_id": key_id,
                    "name": "Рабочий",
                    "hint": "••••4321",
                    "provider": "openrouter",
                }
            ],
        )
        self.assertEqual(payload["selected_api_key_id"], key_id)
        self.assertNotIn("api_key", payload)


if __name__ == "__main__":
    unittest.main()
