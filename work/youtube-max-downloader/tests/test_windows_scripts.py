from __future__ import annotations

import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WindowsScriptTests(unittest.TestCase):
    def test_bootstrap_downloads_are_pinned_and_verified(self) -> None:
        script = (PROJECT_ROOT / "scripts" / "bootstrap_windows.ps1").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("/latest/download/", script)
        self.assertIn("Get-FileHash", script)
        self.assertIn("-ExpectedSha256 $UvSha256", script)
        self.assertIn("-ExpectedSha256 $DenoSha256", script)
        self.assertIn("sync --locked", script)

    def test_windows_build_packages_qt_theme_and_only_needed_binding(self) -> None:
        script = (PROJECT_ROOT / "scripts" / "build_windows.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn('$env:QT_API = "pyside6"', script)
        self.assertIn('"--hidden-import", "PySide6.support.deprecated"', script)
        self.assertIn('"--exclude-module", "tkinter"', script)
        self.assertIn('"--add-data", "${ThemeQss};assets"', script)
        self.assertIn('"--add-data", "${ChevronSvg};assets"', script)
        self.assertIn('"--add-data", "${CheckSvg};assets"', script)
        self.assertNotIn('"--collect-all", "PySide6"', script)


if __name__ == "__main__":
    unittest.main()
