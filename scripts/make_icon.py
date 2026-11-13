"""Regenerate assets/app.ico (the exe / taskbar icon) from the in-app brand mark.

    python scripts/make_icon.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.icons import brand_pixmap  # noqa: E402


def main() -> None:
    app = QApplication([])  # noqa: F841 - Qt needs an application object to render pixmaps
    out = ROOT / "assets"
    out.mkdir(exist_ok=True)
    png = out / "app-256.png"
    brand_pixmap(256).save(str(png))
    Image.open(png).save(out / "app.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    print("wrote", out / "app.ico")


if __name__ == "__main__":
    main()
