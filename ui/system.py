"""OS integration helpers (open files / folders / links, clipboard)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication


def open_path(path: str | Path) -> bool:
    p = Path(path)
    if not p.exists():
        return False
    if sys.platform == "win32":
        os.startfile(str(p))  # type: ignore[attr-defined]
        return True
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))


def open_folder(path: str | Path) -> bool:
    p = Path(path)
    if p.is_file():
        p = p.parent
    return open_path(p)


def open_url(url: str) -> bool:
    url = (url or "").strip()
    if not url:
        return False
    return QDesktopServices.openUrl(QUrl.fromUserInput(url))


def copy_text(text: str) -> None:
    QApplication.clipboard().setText(text)


def toast(widget, message: str, kind: str = "info") -> None:
    host = widget.window()
    if hasattr(host, "toast"):
        host.toast.show_message(message, kind=kind)
