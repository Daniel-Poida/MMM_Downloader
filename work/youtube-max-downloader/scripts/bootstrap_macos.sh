#!/bin/zsh

set -euo pipefail

PROJECT_ROOT="${0:A:h:h}"
RUNTIME_ROOT="$PROJECT_ROOT/.runtime-macos"
UV_ROOT="$RUNTIME_ROOT/uv"
DENO_ROOT="$RUNTIME_ROOT/deno"
UV_EXE="$UV_ROOT/uv"
DENO_EXE="$DENO_ROOT/deno"
UV_VERSION="0.12.12"
DENO_VERSION="2.9.5"

case "$(uname -m)" in
  arm64)
    UV_ASSET="uv-aarch64-apple-darwin.tar.gz"
    UV_SHA256="46740540b63fdee9a6cb2e19baf3f1f475b850c440a33e63455087a6871263f1"
    DENO_ASSET="deno-aarch64-apple-darwin.zip"
    DENO_SHA256="b796aadd131f6930560c1ee040cf0d6f53933fbb987464e9ff46bd7ea4830615"
    ;;
  x86_64)
    UV_ASSET="uv-x86_64-apple-darwin.tar.gz"
    UV_SHA256="0dc8cd6c961582b0d140b5398f96b23502885277fb3464241456a2435e460dfa"
    DENO_ASSET="deno-x86_64-apple-darwin.zip"
    DENO_SHA256="c1b8b89a81e91b2a8b3f96def3195d08cfe3a105651da7908d53061f7140510d"
    ;;
  *)
    print -u2 "MMM Downloader поддерживает Mac с Apple Silicon и Intel."
    exit 1
    ;;
esac

mkdir -p "$RUNTIME_ROOT"

tool_is_valid() {
  local executable="$1"
  local expected_sha="$2"
  local marker="${executable:h}/.archive-sha256"
  [[ -x "$executable" ]] || return 1
  [[ -f "$marker" ]] || return 1
  [[ "$(<"$marker")" == "$expected_sha" ]] || return 1
  "$executable" --version >/dev/null 2>&1
}

install_tool() {
  local name="$1"
  local url="$2"
  local expected_sha="$3"
  local archive_kind="$4"
  local executable_name="$5"
  local destination="$6"
  local archive="$RUNTIME_ROOT/${name}-download-$$"
  local staging="$RUNTIME_ROOT/${name}-staging-$$"
  local prepared="$RUNTIME_ROOT/${name}-prepared-$$"
  local previous="$RUNTIME_ROOT/${name}-previous-$$"

  case "$staging $prepared $previous" in
    "$RUNTIME_ROOT"/*) ;;
    *) print -u2 "Небезопасный временный путь"; exit 1 ;;
  esac

  rm -rf -- "$staging" "$prepared" "$previous"
  mkdir -p "$staging" "$prepared"
  print "Загружаю $name…"
  if ! /usr/bin/curl --fail --location --retry 3 --output "$archive" "$url"; then
    rm -rf -- "$archive" "$staging" "$prepared"
    return 1
  fi

  local actual_sha
  actual_sha="$(/usr/bin/shasum -a 256 "$archive" | /usr/bin/awk '{print $1}')"
  if [[ "$actual_sha" != "$expected_sha" ]]; then
    rm -rf -- "$archive" "$staging" "$prepared"
    print -u2 "Проверка SHA-256 не пройдена для $name."
    return 1
  fi

  if [[ "$archive_kind" == "tar" ]]; then
    /usr/bin/tar -xzf "$archive" -C "$staging"
  else
    /usr/bin/ditto -x -k "$archive" "$staging"
  fi

  local unpacked
  unpacked="$(/usr/bin/find "$staging" -type f -name "$executable_name" -print -quit)"
  if [[ -z "$unpacked" ]]; then
    rm -rf -- "$archive" "$staging" "$prepared"
    print -u2 "В архиве $name не найден исполняемый файл."
    return 1
  fi
  /usr/bin/install -m 755 "$unpacked" "$prepared/$executable_name"
  print -n "$expected_sha" > "$prepared/.archive-sha256"
  "$prepared/$executable_name" --version >/dev/null

  if [[ -e "$destination" ]]; then
    /bin/mv "$destination" "$previous"
  fi
  /bin/mv "$prepared" "$destination"
  rm -rf -- "$archive" "$staging" "$previous"
}

if ! tool_is_valid "$UV_EXE" "$UV_SHA256"; then
  install_tool \
    "uv" \
    "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$UV_ASSET" \
    "$UV_SHA256" \
    "tar" \
    "uv" \
    "$UV_ROOT"
fi

if ! tool_is_valid "$DENO_EXE" "$DENO_SHA256"; then
  install_tool \
    "Deno" \
    "https://github.com/denoland/deno/releases/download/v$DENO_VERSION/$DENO_ASSET" \
    "$DENO_SHA256" \
    "zip" \
    "deno" \
    "$DENO_ROOT"
fi

export UV_CACHE_DIR="$RUNTIME_ROOT/uv-cache"
export UV_PYTHON_INSTALL_DIR="$RUNTIME_ROOT/python"
export UV_PYTHON_INSTALL_REGISTRY=0
export UV_PROJECT_ENVIRONMENT="$RUNTIME_ROOT/venv"
export DENO_DIR="$RUNTIME_ROOT/deno-cache"
export PATH="$DENO_ROOT:$PATH"

cd "$PROJECT_ROOT"
if [[ "${1:-}" == "--update-only" ]]; then
  "$UV_EXE" lock --upgrade-package yt-dlp --upgrade-package yt-dlp-ejs
fi
"$UV_EXE" sync --locked
"$UV_EXE" run --no-sync python "$PROJECT_ROOT/scripts/verify_runtime.py"
"$UV_EXE" cache clean >/dev/null

if [[ "${1:-}" != "--skip-launch" && "${1:-}" != "--update-only" ]]; then
  exec "$UV_EXE" run --no-sync mmm-downloader-gui
fi
