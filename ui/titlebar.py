"""Frameless-window chrome: a slim drag strip with minimise / maximise / close controls."""

from __future__ import annotations

import sys

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QHBoxLayout, QToolButton, QWidget

from ui.widgets import set_icon

RESIZE_MARGIN = 6
STRIP_HEIGHT = 28


class _CtrlButton(QToolButton):
    def __init__(self, name: str, tooltip: str, danger: bool = False) -> None:
        super().__init__()
        self.setObjectName("WinClose" if danger else "WinButton")
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFixedSize(38, 26)
        set_icon(self, name, "muted", 16)


class TitleStrip(QWidget):
    """Empty draggable band above the page; owns the window controls at its right edge."""

    def __init__(self, window) -> None:
        super().__init__()
        self._window = window
        self.setObjectName("TitleStrip")
        self.setFixedHeight(STRIP_HEIGHT)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 8, 0)
        lay.setSpacing(2)
        lay.addStretch(1)
        self.min_btn = _CtrlButton("minimize", "Minimize")
        self.max_btn = _CtrlButton("maximize", "Maximize")
        self.close_btn = _CtrlButton("x", "Close", danger=True)
        self.min_btn.clicked.connect(window.showMinimized)
        self.max_btn.clicked.connect(self.toggle_maximize)
        self.close_btn.clicked.connect(window.close)
        for b in (self.min_btn, self.max_btn, self.close_btn):
            lay.addWidget(b)

    def toggle_maximize(self) -> None:
        w = self._window
        w.showNormal() if w.isMaximized() else w.showMaximized()
        self.sync()

    def sync(self) -> None:
        set_icon(self.max_btn, "restore" if self._window.isMaximized() else "maximize", "muted", 16)
        self.max_btn.setToolTip("Restore" if self._window.isMaximized() else "Maximize")

    # Non-Windows fallback; on Windows WM_NCHITTEST (below) gives native drag/snap.
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and sys.platform != "win32":
            handle = self._window.windowHandle()
            if handle is not None:
                handle.startSystemMove()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximize()


def hit_test(window, strip: TitleStrip, msg_ptr: int) -> int | None:
    """Windows-only: map WM_NCHITTEST to resize edges / caption so a frameless window behaves natively."""
    import ctypes
    from ctypes import wintypes

    WM_NCHITTEST = 0x0084
    HT = dict(CLIENT=1, CAPTION=2, LEFT=10, RIGHT=11, TOP=12, TOPLEFT=13, TOPRIGHT=14,
              BOTTOM=15, BOTTOMLEFT=16, BOTTOMRIGHT=17)
    msg = wintypes.MSG.from_address(msg_ptr)
    if msg.message != WM_NCHITTEST:
        return None
    x = ctypes.c_short(msg.lParam & 0xFFFF).value
    y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
    dpr = window.devicePixelRatioF() or 1.0
    p = window.mapFromGlobal(QPoint(int(x / dpr), int(y / dpr)))
    w, h, m = window.width(), window.height(), RESIZE_MARGIN
    if not window.isMaximized():
        left, right, top, bottom = p.x() < m, p.x() >= w - m, p.y() < m, p.y() >= h - m
        if top and left:
            return HT["TOPLEFT"]
        if top and right:
            return HT["TOPRIGHT"]
        if bottom and left:
            return HT["BOTTOMLEFT"]
        if bottom and right:
            return HT["BOTTOMRIGHT"]
        if left:
            return HT["LEFT"]
        if right:
            return HT["RIGHT"]
        if top:
            return HT["TOP"]
        if bottom:
            return HT["BOTTOM"]
    sp = strip.mapFrom(window, p)
    if strip.rect().contains(sp):
        child = strip.childAt(sp)
        if child is None:
            return HT["CAPTION"]
    return None
