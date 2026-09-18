# MMM Downloader

Локальное приложение для скачивания **разрешённых вам** видео с YouTube в MP4/H.264 — с графическим интерфейсом на Qt, поиском по обычному запросу и необязательным нейро-разбором через OpenAI или OpenRouter.

Полное описание возможностей, режимов качества и работы с ключами — в [README приложения](work/youtube-max-downloader/README.md).

## Что где лежит

| Папка | Что это |
|---|---|
| [`work/youtube-max-downloader`](work/youtube-max-downloader) | само приложение: движок, Qt-интерфейс, CLI, тесты и сценарии сборки |
| [`work/mmm-downloader-bootstrap`](work/mmm-downloader-bootstrap) | Go-лаунчер для Windows: встраивает исходники, распаковывает их на машине пользователя и поднимает среду |

## Сборка

**macOS (Apple Silicon).** Из папки приложения:

```
./scripts/build_macos.sh
```

Скрипт сам поставит изолированную среду в `.runtime-macos`, прогонит тесты и ruff, соберёт `.app`, подпишет его ad-hoc, прогонит самопроверку собранного бандла и упакует в `.zip` и `.dmg`.

**Windows.** Отгружаемый `.exe` — это Go-лаунчер, он кросс-компилируется с macOS или Linux:

```
cd work/mmm-downloader-bootstrap
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -H windowsgui" -o "MMM Downloader.exe" .
```

Перед сборкой в `payload.zip` кладётся папка `MMM-Downloader-Windows` с исходниками приложения. Полностью автономная Windows-сборка с PyInstaller и подписью Authenticode делается отдельно, уже на Windows, через `scripts/build_windows.ps1`.

## Запуск из исходников

```
uv run mmm-downloader-gui
```

Требуется Python 3.11+. Зависимости и движки загружаются при первом запуске в папку проекта; системный Python не используется.

## Лицензии зависимостей

См. [THIRD_PARTY_NOTICES.md](work/youtube-max-downloader/THIRD_PARTY_NOTICES.md).
