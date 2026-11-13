"""Consistent line icons (inline SVG, tinted to the current theme)."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtSvg import QSvgRenderer

_PATHS: dict[str, str] = {
    "jobs": '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2M3 13h18"/>',
    "applications": '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4h6v3H9zM9 12h6M9 16h4"/>',
    "analytics": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "files": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "json": '<path d="M8 4c-2 0-3 1-3 3v2c0 1-1 3-2 3 1 0 2 2 2 3v2c0 2 1 3 3 3M16 4c2 0 3 1 3 3v2c0 1 1 3 2 3-1 0-2 2-2 3v2c0 2-1 3-3 3"/>',
    "settings": '<path d="M4 6h10M18 6h2M4 12h2M10 12h10M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    "claude": '<path d="M12 3l2.2 6.3L21 12l-6.8 2.7L12 21l-2.2-6.3L3 12l6.8-2.7z"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "play": '<path d="M7 4l13 8-13 8z" fill="currentColor"/>',
    "pause": '<path d="M8 5v14M16 5v14" stroke-width="3"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
    "external": '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    "doc": '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 13h6M9 17h6"/>',
    "pdf": '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><path d="M9 15h1.5a1.5 1.5 0 0 0 0-3H9v5"/>',
    "copy": '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h8"/>',
    "check": '<path d="M5 12l5 5 9-10"/>',
    "x": '<path d="M6 6l12 12M18 6L6 18"/>',
    "more": '<circle cx="5" cy="12" r="1.6" fill="currentColor"/><circle cx="12" cy="12" r="1.6" fill="currentColor"/><circle cx="19" cy="12" r="1.6" fill="currentColor"/>',
    "archive": '<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11h14V8M10 12h4"/>',
    "calendar": '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/>',
    "message": '<path d="M4 5h16v11H9l-5 4z"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "filter": '<path d="M4 5h16l-6 8v6l-4-2v-4z"/>',
    "refresh": '<path d="M20 11a8 8 0 1 0-2 6M20 5v6h-6"/>',
    "trash": '<path d="M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13"/>',
    "edit": '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
    "chevron-up": '<path d="M6 15l6-6 6 6"/>',
    "chevron-right": '<path d="M9 6l6 6-6 6"/>',
    "dot": '<circle cx="12" cy="12" r="4" fill="currentColor"/>',
    "trend": '<path d="M3 17l6-6 4 4 8-8M15 7h6v6"/>',
    "users": '<circle cx="9" cy="8" r="3.5"/><path d="M2 20c0-3.5 3-5.5 7-5.5s7 2 7 5.5M17 5a3.5 3.5 0 0 1 0 7M22 20c0-2.5-1.5-4-4-5"/>',
    "award": '<circle cx="12" cy="9" r="6"/><path d="M8.5 14L7 22l5-3 5 3-1.5-8"/>',
    "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
    "layers": '<path d="M12 3l9 5-9 5-9-5zM3 13l9 5 9-5"/>',
    "chevron": '<path d="M6 9l6 6 6-6"/>',
    "folder-open": '<path d="M3 8a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v1H7l-4 8z"/><path d="M3 18l4-8h16l-4 8z"/>',
    "alert": '<path d="M12 4l10 17H2z"/><path d="M12 10v5M12 18h0"/>',
    "retry": '<path d="M4 12a8 8 0 1 1 3 6.2M4 19v-5h5"/>',
    "skip": '<path d="M5 5l9 7-9 7zM18 5v14"/>',
    "send": '<path d="M4 12L21 4l-6 16-3-7z"/>',
    "briefcase-check": '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2M9 14l2 2 4-4"/>',
    "inbox": '<path d="M4 13l3-8h10l3 8v6H4zM4 13h5l1 2h4l1-2h5"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/>',
    "note": '<path d="M5 4h14v12l-5 5H5z"/><path d="M14 21v-5h5"/>',
    "minimize": '<path d="M6 12h12"/>',
    "maximize": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "restore": '<rect x="6" y="9" width="9" height="9" rx="1.5"/><path d="M9 9V7a1.5 1.5 0 0 1 1.5-1.5H17A1.5 1.5 0 0 1 18.5 7v6.5A1.5 1.5 0 0 1 17 15h-2"/>',
    "building": '<rect x="5" y="3" width="14" height="18" rx="1.5"/><path d="M9 7h2M13 7h2M9 11h2M13 11h2M9 15h2M13 15h2M10 21v-3h4v3"/>',
    "chevron-left": '<path d="M15 6l-6 6 6 6"/>',
    "pin": '<path d="M12 3l3 6 5 1-4 4 1 6-5-3-5 3 1-6-4-4 5-1z"/>',
    "eye": '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "eye-off": '<path d="M3 3l18 18M10.6 6.1A9.8 9.8 0 0 1 12 6c6 0 10 6 10 6a17 17 0 0 1-3.2 3.7M6.6 7.6A17 17 0 0 0 2 12s4 7 10 7a9.6 9.6 0 0 0 4-.9M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
}

_cache: dict[tuple[str, str, int], QIcon] = {}


def icon(name: str, color: str = "#94A3B8", size: int = 18) -> QIcon:
    key = (name, color, size)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    body = _PATHS.get(name, _PATHS["more"])
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
        f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" color="{color}">{body}</svg>'
    )
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    out = QIcon()
    for scale in (1, 2):  # crisp on high-DPI displays
        px = QPixmap(size * scale, size * scale)
        px.fill(Qt.GlobalColor.transparent)
        painter = QPainter(px)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter, QRectF(0, 0, size * scale, size * scale))
        painter.end()
        px.setDevicePixelRatio(scale)
        out.addPixmap(px)
        dim = QPixmap(px.size())  # disabled: same glyph at ~35% opacity so it reads as "off"
        dim.fill(Qt.GlobalColor.transparent)
        dp = QPainter(dim)
        dp.setOpacity(0.35)
        dp.drawPixmap(0, 0, px)
        dp.end()
        dim.setDevicePixelRatio(scale)
        out.addPixmap(dim, QIcon.Mode.Disabled)
    _cache[key] = out
    return out


def brand_pixmap(size: int) -> QPixmap:
    """The app mark (blue rounded badge + white briefcase), same as the sidebar logo."""
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0, QColor("#3B8BFF"))
    grad.setColorAt(1, QColor("#1F5BDB"))
    badge = QPainterPath()
    badge.addRoundedRect(QRectF(0, 0, size, size), size * 0.24, size * 0.24)
    p.fillPath(badge, grad)
    glyph = int(size * 0.58)
    mark = icon("briefcase-check", "#FFFFFF", 256).pixmap(256, 256)
    p.drawPixmap(QRectF((size - glyph) / 2, (size - glyph) / 2, glyph, glyph), mark, QRectF(mark.rect()))
    p.end()
    return px


def brand_icon() -> QIcon:
    out = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        out.addPixmap(brand_pixmap(s))
    return out


def clear_cache() -> None:
    _cache.clear()
