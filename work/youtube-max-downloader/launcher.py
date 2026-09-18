from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile

from ytmax.runtime import configure_frozen_runtime


def _self_test() -> None:
    from ytmax.api_keys import ApiKeyStore
    from ytmax.media import ffmpeg_executable
    from ytmax.queue_store import QueueState, QueueStore

    keyring_backend = ApiKeyStore().backend
    ffmpeg = ffmpeg_executable()
    encoders = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout
    missing = [codec for codec in ("libx264", "aac") if codec not in encoders]
    if missing:
        raise RuntimeError(f"FFmpeg is missing required encoders: {', '.join(missing)}")

    deno = shutil.which("deno")
    if not deno:
        raise RuntimeError("Bundled Deno was not found")
    deno_version = subprocess.run(
        [deno, "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.splitlines()[0]
    backend_name = f"{type(keyring_backend).__module__}.{type(keyring_backend).__name__}"
    with tempfile.TemporaryDirectory(prefix="mmm-self-test-") as directory:
        queue_store = QueueStore(directory)
        queue_store.replace(
            [{"video_id": "self-test", "url": "https://youtu.be/self-test"}]
        )
        queue_store.mark_downloading("self-test")
        recovered = QueueStore(directory).get("self-test")
        if recovered is None or recovered.state is not QueueState.PENDING:
            raise RuntimeError("Persistent queue recovery check failed")
    print(
        f"MMM Downloader self-test: OK; {deno_version}; FFmpeg: {ffmpeg}; "
        f"Key store: {backend_name}; Queue: OK"
    )


def main() -> None:
    configure_frozen_runtime()
    if "--self-test" in sys.argv:
        _self_test()
        return

    from ytmax.gui import run_gui

    run_gui()


if __name__ == "__main__":
    main()
