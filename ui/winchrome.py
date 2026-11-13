"""Windows 11 native title-bar tinting (dark caption in the app's own colours)."""

from __future__ import annotations

import sys


def _colorref(hex_color: str) -> int:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b << 16) | (g << 8) | r  # COLORREF is 0x00BBGGRR


def tint_titlebar(window, bg: str, fg: str, dark: bool) -> bool:
    """Best effort: returns False where unsupported (older Windows, other platforms)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = int(window.winId())
        dwm = ctypes.windll.dwmapi

        def set_attr(attr: int, value: int) -> int:
            v = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), attr, ctypes.byref(v), ctypes.sizeof(v))

        set_attr(20, 1 if dark else 0)  # DWMWA_USE_IMMERSIVE_DARK_MODE
        ok = set_attr(35, _colorref(bg)) == 0  # DWMWA_CAPTION_COLOR (Windows 11)
        set_attr(36, _colorref(fg))  # DWMWA_TEXT_COLOR
        set_attr(34, _colorref(bg))  # DWMWA_BORDER_COLOR
        return ok
    except Exception:  # noqa: BLE001
        return False
