from __future__ import annotations

import os
import sys
from pathlib import Path


def configure_frozen_runtime() -> None:
    """Expose binaries bundled beside a PyInstaller executable."""
    if not getattr(sys, "frozen", False):
        return
    bundle_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    executable_dir = Path(sys.executable).parent
    candidates = [
        bundle_dir,
        bundle_dir / "runtime",
        executable_dir,
        executable_dir / "runtime",
    ]
    existing = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join([*(str(item) for item in candidates), existing])

    if sys.platform == "darwin":
        deno_cache = Path.home() / "Library" / "Caches" / "MMM Downloader" / "deno"
        os.environ.setdefault("DENO_DIR", str(deno_cache))
