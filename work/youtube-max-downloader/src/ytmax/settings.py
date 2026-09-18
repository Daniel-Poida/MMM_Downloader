from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "MMM Downloader"


@dataclass(frozen=True, slots=True)
class AiProvider:
    """Где спрашивать план и какие модели там существуют.

    Идентификатор модели должен существовать у провайдера: при неизвестной
    модели запрос падает на HTTP 400/404, приложение уходит на локальный
    разбор, и со стороны это выглядит как «ключ не применяется».
    """

    key: str
    title: str
    base_url: str
    default_model: str
    models: tuple[str, ...]


AI_PROVIDERS: dict[str, AiProvider] = {
    "openai": AiProvider(
        key="openai",
        title="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-5.6-luna",
        models=("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"),
    ),
    "openrouter": AiProvider(
        key="openrouter",
        title="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        default_model="openai/gpt-5.6-luna",
        models=(
            "openai/gpt-5.6-luna",
            "google/gemini-2.5-flash-lite",
            "openai/gpt-oss-20b",
        ),
    ),
}

DEFAULT_PROVIDER = "openai"
DEFAULT_AI_MODEL = AI_PROVIDERS[DEFAULT_PROVIDER].default_model

# Значения, которые прошлые сборки записывали в settings.json по умолчанию и
# которых нет в каталоге OpenAI. Заменяются при загрузке настроек.
_MIGRATED_AI_MODELS = {"gpt-5.4-mini": DEFAULT_AI_MODEL}


def ai_provider(key: str) -> AiProvider:
    return AI_PROVIDERS.get(key or "", AI_PROVIDERS[DEFAULT_PROVIDER])


@dataclass(slots=True)
class ApiKeyProfile:
    key_id: str
    name: str
    hint: str
    # Провайдер определяется по самому ключу при добавлении и хранится здесь,
    # чтобы выбирать адрес и список моделей, не читая секрет из хранилища.
    provider: str = DEFAULT_PROVIDER


@dataclass(slots=True)
class AppSettings:
    output_dir: str = ""
    quality_mode: str = "native_h264"
    cookie_browser: str = ""
    use_ai: bool = False
    ai_model: str = DEFAULT_AI_MODEL
    api_key_profiles: list[ApiKeyProfile] = field(default_factory=list)
    selected_api_key_id: str = ""


def settings_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".mmm-downloader"


def settings_path() -> Path:
    return settings_dir() / "settings.json"


def load_settings() -> AppSettings:
    path = settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return AppSettings()
    if not isinstance(raw, dict):
        return AppSettings()

    defaults = AppSettings()
    profiles: list[ApiKeyProfile] = []
    seen_ids: set[str] = set()
    raw_profiles = raw.get("api_key_profiles")
    if isinstance(raw_profiles, list):
        for item in raw_profiles[:50]:
            if not isinstance(item, dict):
                continue
            key_id = str(item.get("key_id") or "").strip()
            name = str(item.get("name") or "").strip()
            hint = str(item.get("hint") or "").strip()
            if (
                not re.fullmatch(r"[a-f0-9]{32}", key_id)
                or not name
                or key_id in seen_ids
            ):
                continue
            provider = str(item.get("provider") or "").strip()
            seen_ids.add(key_id)
            profiles.append(
                ApiKeyProfile(
                    key_id=key_id,
                    name=name[:60],
                    hint=hint[:12],
                    provider=provider if provider in AI_PROVIDERS else DEFAULT_PROVIDER,
                )
            )
    ai_model = str(raw.get("ai_model") or "").strip()
    ai_model = _MIGRATED_AI_MODELS.get(ai_model, ai_model) or defaults.ai_model

    selected_api_key_id = str(raw.get("selected_api_key_id") or "").strip()
    if selected_api_key_id not in seen_ids:
        selected_api_key_id = profiles[0].key_id if profiles else ""

    return AppSettings(
        output_dir=str(raw.get("output_dir", defaults.output_dir)),
        quality_mode=(
            raw.get("quality_mode")
            if raw.get("quality_mode") in {"native_h264", "true_max_h264"}
            else defaults.quality_mode
        ),
        cookie_browser=(
            raw.get("cookie_browser")
            if raw.get("cookie_browser") in {"", "chrome", "edge", "firefox"}
            else ""
        ),
        use_ai=bool(raw.get("use_ai", defaults.use_ai)),
        ai_model=ai_model,
        api_key_profiles=profiles,
        selected_api_key_id=selected_api_key_id,
    )


def save_settings(settings: AppSettings) -> None:
    directory = settings_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "settings.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)


_WINDOWS_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(title: str, video_id: str, *, limit: int = 120) -> str:
    """Return a Windows-safe, collision-resistant MP4 filename."""
    clean = _WINDOWS_FORBIDDEN.sub("_", title)
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    if not clean:
        clean = "video"
    if clean.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        clean = f"_{clean}"
    suffix = f" [{video_id}].mp4"
    clean = clean[: max(1, limit - len(suffix))].rstrip(" .")
    return f"{clean}{suffix}"


def unique_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(2, 10_000):
        alternative = directory / f"{stem} ({index}){suffix}"
        if not alternative.exists():
            return alternative
    raise OSError("Не удалось подобрать свободное имя файла")


def prepare_output_directory(value: str) -> Path:
    if not value or "\x00" in value:
        raise ValueError("Выберите папку для сохранения")
    path = Path(value).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"Это не папка: {path}")
    return path.resolve()
