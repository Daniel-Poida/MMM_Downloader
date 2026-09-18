from __future__ import annotations

import re
import sys
import uuid
from typing import Protocol

from .settings import DEFAULT_PROVIDER, ApiKeyProfile

SERVICE_NAME = "com.mmm.downloader.openai"
_KEY_ID_PATTERN = r"[a-f0-9]{32}"
_SECRET_PREFIX = "sk-"
_SECRET_MIN_LENGTH = 20
_SECRET_PATTERN = r"[A-Za-z0-9_\-]+"
# Ключи OpenRouter выдаются с префиксом sk-or-v1-, поэтому провайдера видно по
# самому ключу и не нужно спрашивать отдельно.
_OPENROUTER_PREFIX = "sk-or-"


class ApiKeyStoreError(RuntimeError):
    pass


class _KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


def _native_backend() -> _KeyringBackend:
    try:
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring

            backend = Keyring()
        elif sys.platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring

            backend = WinVaultKeyring()
            backend.persist = "local machine"
        else:
            raise ApiKeyStoreError(
                "Безопасное хранение API-ключей поддерживается только в macOS и Windows."
            )
        _ = type(backend).priority
        return backend
    except ApiKeyStoreError:
        raise
    except Exception as exc:
        raise ApiKeyStoreError(
            "Системное хранилище API-ключей недоступно."
        ) from exc


def _key_hint(secret: str) -> str:
    value = secret.strip()
    return f"••••{value[-4:]}" if len(value) >= 4 else "••••"


def _validated_key_id(key_id: str) -> str:
    value = key_id.strip()
    if not re.fullmatch(_KEY_ID_PATTERN, value):
        raise ApiKeyStoreError("Некорректный идентификатор API-ключа.")
    return value


def normalize_secret(secret: str) -> str:
    """Проверить форму ключа до обращения к сети и хранилищу.

    Быстрая локальная проверка ловит самые частые ошибки — вставлен не тот
    текст, ключ скопирован не полностью или вместе с переносом строки — и
    отвечает понятной причиной, а не общим «ключ не работает» после запроса.
    """
    value = (secret or "").strip()
    if not value:
        raise ValueError("Вставьте API-ключ.")
    if any(character.isspace() for character in value):
        raise ValueError(
            "В ключе есть пробел или перенос строки. Скопируйте его целиком, "
            "без лишних символов."
        )
    if not value.startswith(_SECRET_PREFIX):
        raise ValueError(
            f"Ключ OpenAI начинается с «{_SECRET_PREFIX}». Похоже, скопирована "
            "не та строка."
        )
    if len(value) < _SECRET_MIN_LENGTH:
        raise ValueError("Ключ слишком короткий — скорее всего, скопирован не полностью.")
    if not re.fullmatch(_SECRET_PATTERN, value):
        raise ValueError("В ключе есть недопустимые символы. Скопируйте его заново.")
    return value


def provider_for_secret(secret: str) -> str:
    """Определить провайдера по виду ключа."""
    return (
        "openrouter"
        if (secret or "").strip().startswith(_OPENROUTER_PREFIX)
        else DEFAULT_PROVIDER
    )


class ApiKeyStore:
    def __init__(self, backend: _KeyringBackend | None = None) -> None:
        self._backend = backend

    @property
    def backend(self) -> _KeyringBackend:
        if self._backend is None:
            self._backend = _native_backend()
        return self._backend

    def create(self, name: str, secret: str) -> ApiKeyProfile:
        clean_name = name.strip()[:60]
        if not clean_name:
            raise ValueError("Укажите название ключа.")
        clean_secret = normalize_secret(secret)
        key_id = uuid.uuid4().hex
        try:
            self.backend.set_password(SERVICE_NAME, key_id, clean_secret)
        except Exception as exc:
            raise ApiKeyStoreError(
                "Не удалось сохранить ключ в системном хранилище."
            ) from exc
        return ApiKeyProfile(
            key_id=key_id,
            name=clean_name,
            hint=_key_hint(clean_secret),
            provider=provider_for_secret(clean_secret),
        )

    def get(self, key_id: str) -> str:
        key_id = _validated_key_id(key_id)
        try:
            secret = self.backend.get_password(SERVICE_NAME, key_id)
        except Exception as exc:
            raise ApiKeyStoreError(
                "Не удалось прочитать ключ из системного хранилища."
            ) from exc
        if not secret:
            raise ApiKeyStoreError(
                "Выбранный API-ключ не найден. Удалите его из списка и добавьте снова."
            )
        return secret

    def delete(self, key_id: str) -> None:
        key_id = _validated_key_id(key_id)
        try:
            if self.backend.get_password(SERVICE_NAME, key_id) is None:
                return
            self.backend.delete_password(SERVICE_NAME, key_id)
        except Exception as exc:
            raise ApiKeyStoreError(
                "Не удалось удалить ключ из системного хранилища."
            ) from exc
