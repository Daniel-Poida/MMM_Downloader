from __future__ import annotations

import pytest

ISOLATION_MARKER = "MMM_TEST_APP_DATA"


@pytest.fixture(autouse=True)
def isolated_app_data(tmp_path, monkeypatch):
    """Увести настройки и очередь каждого теста в свою временную папку.

    ``QueueStore()`` и ``settings_dir()`` без аргументов берут папку профиля
    пользователя. Из-за этого тесты читали живую очередь разработчика, а
    ``DownloaderApp`` при незавершённых позициях показывал модальный вопрос
    «вернуть очередь?» — и прогон вставал навсегда, ожидая щелчка мышью.
    Заодно тесты перестают писать в настоящий settings.json.
    """
    root = tmp_path / "app-data"
    root.mkdir()
    for variable in ("HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA"):
        monkeypatch.setenv(variable, str(root))
    # Метка для теста, который следит, что изоляция вообще действует.
    monkeypatch.setenv(ISOLATION_MARKER, str(root))
    return root
