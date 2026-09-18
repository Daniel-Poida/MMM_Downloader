#!/bin/zsh

set -euo pipefail

PROJECT_ROOT="${0:A:h:h}"
RUNTIME_ROOT="$PROJECT_ROOT/.runtime-macos"
UV_EXE="$RUNTIME_ROOT/uv/uv"
DENO_ROOT="$RUNTIME_ROOT/deno"
DENO_EXE="$DENO_ROOT/deno"
BUILD_ROOT="$PROJECT_ROOT/build/macos"
DIST_ROOT="$PROJECT_ROOT/dist/macos"
APP_PATH="$DIST_ROOT/MMM Downloader.app"
ZIP_PATH="$DIST_ROOT/MMM-Downloader-macOS-Apple-Silicon.zip"
DMG_PATH="$DIST_ROOT/MMM-Downloader-macOS-Apple-Silicon.dmg"

if [[ "$(uname -m)" != "arm64" ]]; then
  print -u2 "Этот сценарий собирает версию для Apple Silicon и требует arm64 Mac."
  exit 1
fi

"$PROJECT_ROOT/scripts/bootstrap_macos.sh" --skip-launch

export UV_CACHE_DIR="$RUNTIME_ROOT/build-cache"
export UV_PYTHON_INSTALL_DIR="$RUNTIME_ROOT/python"
export UV_PYTHON_INSTALL_REGISTRY=0
export UV_PROJECT_ENVIRONMENT="$RUNTIME_ROOT/venv"
export DENO_DIR="$RUNTIME_ROOT/deno-cache"
export PATH="$DENO_ROOT:$PATH"
export QT_API="pyside6"
export PYINSTALLER_CONFIG_DIR="$RUNTIME_ROOT/pyinstaller-config"

cd "$PROJECT_ROOT"
"$UV_EXE" sync --locked --extra build --extra dev
"$UV_EXE" run --no-sync pytest -q
"$UV_EXE" run --no-sync ruff check src tests launcher.py

FFMPEG_EXE="$("$UV_EXE" run --no-sync python -c \
  'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')"
if [[ ! -x "$FFMPEG_EXE" || ! -x "$DENO_EXE" ]]; then
  print -u2 "FFmpeg или Deno не готовы к упаковке."
  exit 1
fi

case "$BUILD_ROOT $DIST_ROOT" in
  "$PROJECT_ROOT/build/macos $PROJECT_ROOT/dist/macos") ;;
  *) print -u2 "Небезопасный путь сборки"; exit 1 ;;
esac
rm -rf -- "$BUILD_ROOT" "$DIST_ROOT"
mkdir -p "$BUILD_ROOT" "$DIST_ROOT"

"$UV_EXE" run --no-sync pyinstaller \
  --noconfirm \
  --clean \
  --onedir \
  --windowed \
  --name "MMM Downloader" \
  --icon "$PROJECT_ROOT/assets/mmm_downloader.png" \
  --osx-bundle-identifier "com.mmm.downloader" \
  --target-architecture arm64 \
  --distpath "$DIST_ROOT" \
  --workpath "$BUILD_ROOT/pyinstaller" \
  --specpath "$BUILD_ROOT" \
  --paths "$PROJECT_ROOT/src" \
  --collect-all yt_dlp \
  --collect-all yt_dlp_ejs \
  --hidden-import imageio_ffmpeg \
  --hidden-import imageio_ffmpeg.binaries \
  --hidden-import keyring.backends.macOS \
  --hidden-import keyring.backends.macOS.api \
  --hidden-import PySide6.support.deprecated \
  --exclude-module tkinter \
  --exclude-module PyQt5 \
  --exclude-module PyQt6 \
  --exclude-module PySide2 \
  --add-data "$PROJECT_ROOT/assets/mmm_downloader.png:assets" \
  --add-data "$PROJECT_ROOT/assets/mmm_downloader.qss:assets" \
  --add-data "$PROJECT_ROOT/assets/chevron_down.svg:assets" \
  --add-data "$PROJECT_ROOT/assets/check.svg:assets" \
  --add-data "$PROJECT_ROOT/assets/plus.svg:assets" \
  --add-data "$PROJECT_ROOT/assets/minus.svg:assets" \
  --add-binary "$FFMPEG_EXE:imageio_ffmpeg/binaries" \
  --add-binary "$DENO_EXE:." \
  "$PROJECT_ROOT/launcher.py"

PLIST="$APP_PATH/Contents/Info.plist"
set_plist_string() {
  local key="$1"
  local value="$2"
  if /usr/libexec/PlistBuddy -c "Print :$key" "$PLIST" >/dev/null 2>&1; then
    /usr/libexec/PlistBuddy -c "Set :$key $value" "$PLIST"
  else
    /usr/libexec/PlistBuddy -c "Add :$key string $value" "$PLIST"
  fi
}
set_plist_string "LSMinimumSystemVersion" "15.0"
set_plist_string "CFBundleShortVersionString" "0.2.1"
set_plist_string "CFBundleVersion" "0.2.1"
/bin/cp "$PROJECT_ROOT/README.md" "$APP_PATH/Contents/Resources/README.md"
/bin/cp "$PROJECT_ROOT/THIRD_PARTY_NOTICES.md" \
  "$APP_PATH/Contents/Resources/THIRD_PARTY_NOTICES.md"

/usr/bin/codesign --force --deep --sign - --timestamp=none "$APP_PATH"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$APP_PATH"
"$APP_PATH/Contents/MacOS/MMM Downloader" --self-test

/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP_PATH" "$ZIP_PATH"

DMG_STAGE="$BUILD_ROOT/dmg"
mkdir -p "$DMG_STAGE"
/usr/bin/ditto "$APP_PATH" "$DMG_STAGE/MMM Downloader.app"
/bin/ln -s /Applications "$DMG_STAGE/Applications"
/usr/bin/hdiutil create \
  -volname "MMM Downloader" \
  -srcfolder "$DMG_STAGE" \
  -ov \
  -format UDZO \
  "$DMG_PATH"
/usr/bin/hdiutil verify "$DMG_PATH"

print ""
print "Готово: $APP_PATH"
print "Готово: $ZIP_PATH"
print "Готово: $DMG_PATH"
