from __future__ import annotations

import re
import sys
import unittest

from ytmax.api_keys import (
    SERVICE_NAME,
    ApiKeyStore,
    ApiKeyStoreError,
    normalize_secret,
)


class FakeBackend:
    def __init__(self) -> None:
        self.passwords: dict[tuple[str, str], str] = {}
        self.failure: Exception | None = None

    def get_password(self, service: str, username: str) -> str | None:
        if self.failure is not None:
            raise self.failure
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.failure is not None:
            raise self.failure
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.failure is not None:
            raise self.failure
        self.passwords.pop((service, username), None)


class ApiKeyStoreTests(unittest.TestCase):
    def test_create_get_and_delete_use_only_the_native_backend_for_secret(self) -> None:
        backend = FakeBackend()
        store = ApiKeyStore(backend)

        profile = store.create("  Основной  ", "  sk-test-secret-0000-1234  ")

        self.assertRegex(profile.key_id, re.compile(r"^[a-f0-9]{32}$"))
        self.assertEqual(profile.name, "Основной")
        self.assertEqual(profile.hint, "••••1234")
        self.assertEqual(
            backend.passwords[(SERVICE_NAME, profile.key_id)],
            "sk-test-secret-0000-1234",
        )
        self.assertEqual(store.get(profile.key_id), "sk-test-secret-0000-1234")

        store.delete(profile.key_id)

        self.assertNotIn((SERVICE_NAME, profile.key_id), backend.passwords)

    def test_delete_of_missing_key_is_a_noop(self) -> None:
        backend = FakeBackend()

        ApiKeyStore(backend).delete("c" * 32)

        self.assertEqual(backend.passwords, {})

    def test_empty_name_and_secret_are_rejected_before_backend_access(self) -> None:
        backend = FakeBackend()
        store = ApiKeyStore(backend)

        with self.assertRaisesRegex(ValueError, "название"):
            store.create("  ", "sk-test")
        with self.assertRaisesRegex(ValueError, "API-ключ"):
            store.create("Рабочий", "  ")

        self.assertEqual(backend.passwords, {})

    def test_backend_errors_are_wrapped_without_exposing_the_secret(self) -> None:
        secret = "sk-super-secret-value"
        backend = FakeBackend()
        backend.failure = RuntimeError("credential backend failed")
        store = ApiKeyStore(backend)

        with self.assertRaises(ApiKeyStoreError) as captured:
            store.create("Рабочий", secret)

        self.assertNotIn(secret, str(captured.exception))
        self.assertIsInstance(captured.exception.__cause__, RuntimeError)

    def test_missing_secret_has_actionable_error(self) -> None:
        store = ApiKeyStore(FakeBackend())

        with self.assertRaisesRegex(ApiKeyStoreError, "не найден"):
            store.get("c" * 32)

    def test_malformed_keys_are_rejected_with_the_actual_reason(self) -> None:
        cases = (
            ("sk-abcdefghij klmnopqrst", "пробел"),
            ("sk-abcdefghij\nklmnopqrst", "пробел"),
            ("proj-abcdefghijklmnopqrst", "sk-"),
            ("sk-short", "короткий"),
            ("sk-abcdefghij$klmnopqrst", "недопустимые"),
        )
        for secret, expected in cases:
            with self.subTest(secret=secret):
                with self.assertRaisesRegex(ValueError, expected):
                    normalize_secret(secret)

    def test_valid_key_is_accepted_and_trimmed(self) -> None:
        self.assertEqual(
            normalize_secret("  sk-proj-abcdefghijklmnop_QRST-1234  "),
            "sk-proj-abcdefghijklmnop_QRST-1234",
        )

    def test_malformed_key_never_reaches_the_backend(self) -> None:
        backend = FakeBackend()

        with self.assertRaises(ValueError):
            ApiKeyStore(backend).create("Рабочий", "not-a-key")

        self.assertEqual(backend.passwords, {})

    @unittest.skipUnless(
        sys.platform in {"darwin", "win32"},
        "нативное хранилище есть только в macOS и Windows",
    )
    def test_native_backend_is_installed_in_this_environment(self) -> None:
        # Остальные тесты подставляют свой backend, поэтому отсутствие keyring
        # в окружении они не замечают, а приложение в таком окружении не может
        # сохранить ни одного ключа.
        backend = ApiKeyStore().backend

        self.assertTrue(hasattr(backend, "get_password"))
        self.assertTrue(hasattr(backend, "set_password"))


if __name__ == "__main__":
    unittest.main()
