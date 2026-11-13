"""Shared UI widgets."""

from __future__ import annotations

import weakref

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui import icons, theme

# widget -> (icon name, colour role); re-tinted when the theme changes
_icon_widgets: "weakref.WeakKeyDictionary[QAbstractButton, tuple[str, str, int]]" = weakref.WeakKeyDictionary()


def _role_color(role: str) -> str:
    c = theme.current()
    return {
        "muted": c.text_secondary,
        "text": c.text,
        "accent": c.accent,
        "white": "#FFFFFF",
        "sidebar": c.sidebar_muted,
        "sidebar_active": c.sidebar_text,
        "danger": c.danger,
        "success": c.success,
    }.get(role, c.text_secondary)


def set_icon(btn: QAbstractButton, name: str, role: str = "muted", size: int = 16) -> None:
    _icon_widgets[btn] = (name, role, size)
    btn.setIcon(icons.icon(name, _role_color(role), size))
    btn.setIconSize(QSize(size, size))


_chips: "weakref.WeakKeyDictionary[QLabel, tuple[str, str]]" = weakref.WeakKeyDictionary()


def _style_chip(chip: QLabel, icon: str, kind: str) -> None:
    fg, bg = theme.badge_colors(kind)
    chip.setStyleSheet(f"QLabel {{ background: {bg}; border-radius: 8px; }}")
    chip.setPixmap(icons.icon(icon, fg, 16).pixmap(16, 16))


def refresh_icons() -> None:
    for chip, (icon, kind) in list(_chips.items()):
        try:
            _style_chip(chip, icon, kind)
        except RuntimeError:
            continue
    for btn, (name, role, size) in list(_icon_widgets.items()):
        try:
            btn.setIcon(icons.icon(name, _role_color(role), size))
        except RuntimeError:  # widget already deleted
            continue


def primary_button(text: str, icon: str = "") -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("PrimaryButton")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if icon:
        set_icon(btn, icon, "white")
    return btn


def success_button(text: str, icon: str = "") -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("SuccessButton")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if icon:
        set_icon(btn, icon, "white")
    return btn


def ghost_button(text: str, icon: str = "") -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("GhostButton")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if icon:
        set_icon(btn, icon, "text")
    return btn


def danger_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("DangerButton")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def chip_button(text: str, compact: bool = False) -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("Chip")
    btn.setProperty("compact", compact)
    btn.setCheckable(True)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def segment_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("Segment")
    btn.setCheckable(True)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def nav_button(text: str, icon: str = "") -> QPushButton:
    btn = QPushButton("  " + text)
    btn.setObjectName("NavButton")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setProperty("active", False)
    btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    if icon:
        set_icon(btn, icon, "sidebar", 18)
    return btn


def search_field(placeholder: str):
    from PySide6.QtWidgets import QLineEdit

    edit = QLineEdit()
    edit.setObjectName("SearchField")
    edit.setPlaceholderText(placeholder)
    edit.setClearButtonEnabled(True)
    edit.setAccessibleName(placeholder)
    edit.addAction(icons.icon("search", theme.current().text_secondary, 16), QLineEdit.ActionPosition.LeadingPosition)
    return edit


def clear_layout(layout) -> None:
    """Remove and dispose every widget in ``layout`` immediately (no stale painted leftovers)."""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


def muted_label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName("Muted")
    label.setWordWrap(True)
    return label


def field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("FieldLabel")
    return label


def page_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("PageTitle")
    return label


def section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("SectionTitle")
    return label


def hline() -> QFrame:
    line = QFrame()
    line.setObjectName("Separator")
    line.setFrameShape(QFrame.Shape.NoFrame)
    return line


def badge(text: str, kind: str = "neutral", dot: bool = False) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Badge")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setStyleSheet(theme.badge_style(kind))
    label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return label


def status_badge(text: str, light: bool = True) -> QLabel:
    label = badge(text, theme.STATUS_KIND.get(text, "neutral"))
    return label


def row_action(name: str, tooltip: str, role: str = "muted") -> QToolButton:
    btn = QToolButton()
    btn.setObjectName("RowAction")
    btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedSize(26, 26)
    set_icon(btn, name, role, 16)
    return btn


class RowTable(QTableWidget):
    """Table whose hover highlight spans the whole row (QSS ``item:hover`` only lights one cell)."""

    def __init__(self, rows: int = 0, cols: int = 0, parent=None) -> None:
        super().__init__(rows, cols, parent)
        self._hover = -1
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setItemDelegate(RowDelegate(self))

    def hover_row(self) -> int:
        return self._hover

    def _set_hover(self, row: int) -> None:
        if row != self._hover:
            self._hover = row
            self.viewport().update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._set_hover(self.rowAt(int(event.position().y())))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._set_hover(-1)
        super().leaveEvent(event)

    def setCellWidget(self, row: int, column: int, widget) -> None:  # noqa: N802
        widget.installEventFilter(self)  # buttons inside a row must keep that row highlighted
        super().setCellWidget(row, column, widget)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        from PySide6.QtCore import QEvent

        if event.type() == QEvent.Type.Enter and isinstance(obj, QWidget):
            self._set_hover(self.indexAt(obj.pos()).row())
        return super().eventFilter(obj, event)


def paint_row_hover(painter, option, index) -> None:
    from PySide6.QtGui import QColor

    table = option.widget
    if isinstance(table, RowTable) and table.hover_row() == index.row() and not (
            option.state & QStyle.StateFlag.State_Selected):
        painter.fillRect(option.rect, QColor(theme.current().hover))


class RowDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index) -> None:  # noqa: N802
        paint_row_hover(painter, option, index)
        super().paint(painter, option, index)


def flash_copied(btn: QAbstractButton, ms: int = 1600) -> None:
    """Swap a copy button's icon for a green check for a moment, as copy buttons do on the web."""
    from PySide6.QtCore import QTimer

    entry = _icon_widgets.get(btn)
    if entry is None:
        return
    name, role, size = entry
    if getattr(btn, "_copied_timer", None) is None:
        timer = QTimer(btn)
        timer.setSingleShot(True)

        def restore() -> None:
            btn.setToolTip(getattr(btn, "_copy_tip", btn.toolTip()))
            set_icon(btn, name, role, size)

        timer.timeout.connect(restore)
        btn._copied_timer = timer
        btn._copy_tip = btn.toolTip()
    btn._copy_tip = btn.toolTip() if btn.toolTip() != "Copied" else btn._copy_tip
    btn.setIcon(icons.icon("check", theme.current().success, size))
    btn.setToolTip("Copied")
    btn._copied_timer.start(ms)


def copy_button(tooltip: str, text_getter) -> QToolButton:
    """Row-action copy button: copies ``text_getter()`` and confirms with a check icon."""
    from ui import system

    btn = row_action("copy", tooltip, "text")

    def clicked() -> None:
        text = text_getter()
        if text:
            system.copy_text(str(text))
            flash_copied(btn)

    btn.clicked.connect(clicked)
    return btn


def icon_tool(tooltip: str, name: str, variant: str = "ghost", size: int = 32) -> QPushButton:
    """Square icon-only button; full label in the tooltip."""
    btn = QPushButton()
    btn.setToolTip(tooltip)
    btn.setAccessibleName(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedSize(size, size)
    btn.setObjectName({"ghost": "IconButton", "primary": "IconButtonPrimary", "success": "IconButtonSuccess"}[variant])
    set_icon(btn, name, "text" if variant == "ghost" else "white", 16)
    return btn


def kpi_card(label: str, tooltip: str = "", icon: str = "", kind: str = "info") -> tuple[QFrame, QLabel, QLabel]:
    """Metric tile: label + big value + sub-line, with an optional tinted icon chip."""
    card = QFrame()
    card.setObjectName("Kpi")
    outer = QHBoxLayout(card)
    outer.setContentsMargins(14, 11, 12, 11)
    outer.setSpacing(8)
    lay = QVBoxLayout()
    lay.setSpacing(1)
    name = QLabel(label)
    name.setObjectName("KpiLabel")
    value = QLabel("0")
    value.setObjectName("KpiValue")
    sub = QLabel("")
    sub.setObjectName("KpiSub")
    lay.addWidget(name)
    lay.addWidget(value)
    lay.addWidget(sub)
    outer.addLayout(lay, 1)
    if icon:
        chip = QLabel()
        chip.setFixedSize(30, 30)
        chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _chips[chip] = (icon, kind)
        _style_chip(chip, icon, kind)
        outer.addWidget(chip, alignment=Qt.AlignmentFlag.AlignTop)
    if tooltip:
        card.setToolTip(tooltip)
    return card, value, sub


def icon_chip(icon: str, kind: str = "info", size: int = 30) -> QLabel:
    """Small tinted rounded square holding an icon; re-tints on theme change."""
    chip = QLabel()
    chip.setFixedSize(size, size)
    chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
    _chips[chip] = (icon, kind)
    _style_chip(chip, icon, kind)
    return chip


def card_header(title: str, subtitle: str = "", icon: str = "", kind: str = "info") -> QHBoxLayout:
    """Card heading: optional icon chip, a title and a one-line muted description."""
    row = QHBoxLayout()
    row.setSpacing(10)
    if icon:
        row.addWidget(icon_chip(icon, kind, 30), alignment=Qt.AlignmentFlag.AlignTop)
    col = QVBoxLayout()
    col.setSpacing(1)
    t = QLabel(title)
    t.setObjectName("SectionTitle")
    col.addWidget(t)
    if subtitle:
        sub = QLabel(subtitle)
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        col.addWidget(sub)
    row.addLayout(col, 1)
    return row


def standard_icon(fallback: QStyle.StandardPixmap, theme_name: str = "") -> QIcon:
    if theme_name:
        themed = QIcon.fromTheme(theme_name)
        if not themed.isNull():
            return themed
    app = QApplication.instance()
    if app and app.style():
        return app.style().standardIcon(fallback)
    return QIcon()


_STD_TO_NAME = {
    "SP_FileDialogNewFolder": "plus", "SP_MediaPlay": "play", "SP_MediaPause": "pause", "SP_MediaStop": "stop",
    "SP_ArrowForward": "chevron", "SP_DialogSaveButton": "copy", "SP_ArrowDown": "inbox",
    "SP_BrowserReload": "refresh", "SP_DialogApplyButton": "check", "SP_DialogCancelButton": "skip",
    "SP_DialogOkButton": "check", "SP_DirOpenIcon": "folder-open", "SP_DialogOpenButton": "external",
}


def icon_button(
    tooltip: str,
    *,
    icon: QStyle.StandardPixmap | None = None,
    theme_name: str = "",
    variant: str = "ghost",
    size: int = 32,
    name: str = "",
) -> QPushButton:
    """Backward-compatible icon button (Qt standard pixmap names map to the line-icon set)."""
    icon_name = name or _STD_TO_NAME.get(getattr(icon, "name", ""), "more")
    return icon_tool(tooltip, icon_name, variant, size)


# ---- avatars ----------------------------------------------------------------------------

_AVATAR_HUES = ["#3B82F6", "#8B5CF6", "#14B8A6", "#F59E0B", "#EC4899", "#22C55E", "#F97316", "#06B6D4"]


def avatar_color(name: str) -> str:
    return _AVATAR_HUES[sum(ord(ch) for ch in (name or "?")) % len(_AVATAR_HUES)]


def avatar_pixmap(name: str, size: int = 26, radius: int = 7):
    from PySide6.QtGui import QColor, QFont, QPainter, QPixmap

    scale = 2
    px = QPixmap(size * scale, size * scale)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    base = QColor(avatar_color(name))
    bg = QColor(base)
    bg.setAlphaF(0.20)
    p.setBrush(bg)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, 0, size * scale, size * scale, radius * scale, radius * scale)
    font = QFont()
    font.setBold(True)
    font.setPixelSize(int(size * scale * 0.46))
    p.setFont(font)
    p.setPen(base.lighter(125) if theme.current().is_light is False else base.darker(115))
    p.drawText(px.rect(), Qt.AlignmentFlag.AlignCenter, (name or "?").strip()[:1].upper() or "?")
    p.end()
    px.setDevicePixelRatio(scale)
    return px


def avatar_label(name: str, size: int = 38) -> QLabel:
    lab = QLabel()
    lab.setFixedSize(size, size)
    lab.setPixmap(avatar_pixmap(name, size, max(8, size // 4)))
    return lab


def company_delegate(parent=None):
    """Item delegate: coloured initial tile + bold company name (the item's text stays the data)."""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QStyle, QStyledItemDelegate

    class _Delegate(QStyledItemDelegate):
        def paint(self, painter, option, index):  # noqa: N802
            from PySide6.QtWidgets import QStyleOptionViewItem

            paint_row_hover(painter, option, index)

            opt = QStyleOptionViewItem(option)
            self.initStyleOption(opt, index)
            name = opt.text
            opt.text = ""
            style = opt.widget.style() if opt.widget else QApplication.style()
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
            role = index.data(Qt.ItemDataRole.UserRole + 1) or ""
            r = option.rect
            c = theme.current()
            size = 32 if role else 24
            y = r.top() + (r.height() - size) // 2
            painter.save()
            painter.drawPixmap(r.left() + 10, y, avatar_pixmap(name, size, 9 if role else 7))
            f = painter.font()
            f.setBold(True)
            painter.setFont(f)
            painter.setPen(QColor(c.text))
            x = r.left() + 10 + size + 10
            w = r.right() - x - 6
            fm = painter.fontMetrics()
            if role:
                top = QRect(x, r.top() + 7, w, r.height() // 2 - 5)
                painter.drawText(top, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft,
                                 fm.elidedText(name, Qt.TextElideMode.ElideRight, w))
                f.setBold(False)
                f.setPixelSize(max(11, f.pixelSize() - 1) if f.pixelSize() > 0 else 12)
                painter.setFont(f)
                painter.setPen(QColor(c.text_secondary))
                fm2 = painter.fontMetrics()
                bottom = QRect(x, r.top() + r.height() // 2 + 1, w, r.height() // 2 - 5)
                painter.drawText(bottom, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                                 fm2.elidedText(role, Qt.TextElideMode.ElideRight, w))
            else:
                painter.drawText(QRect(x, r.top(), w, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                                 fm.elidedText(name, Qt.TextElideMode.ElideRight, w))
            painter.restore()

    return _Delegate(parent)


# ---- glyph tiles ------------------------------------------------------------------------------


def circle_icon(name: str, color: str, size: int = 28, glyph: str = "#FFFFFF"):
    """Solid coloured circle with a white line glyph (stats row / checklist)."""
    from PySide6.QtGui import QColor, QPainter, QPixmap

    scale = 2
    px = QPixmap(size * scale, size * scale)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(0, 0, size * scale, size * scale)
    inner = int(size * 0.58) * scale
    g = icons.icon(name, glyph, int(size * 0.58)).pixmap(int(size * 0.58), int(size * 0.58))
    p.drawPixmap((size * scale - inner) // 2, (size * scale - inner) // 2, inner, inner, g)
    p.end()
    px.setDevicePixelRatio(scale)
    return px


def ring_icon(color: str, size: int = 20):
    """Blue ring with a dot: the 'in progress' marker."""
    from PySide6.QtGui import QColor, QPainter, QPen, QPixmap

    scale = 2
    px = QPixmap(size * scale, size * scale)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), 2.4 * scale)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    m = 2 * scale
    p.drawEllipse(m, m, size * scale - 2 * m, size * scale - 2 * m)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    c = size * scale / 2
    p.drawEllipse(int(c - 3.5 * scale), int(c - 3.5 * scale), int(7 * scale), int(7 * scale))
    p.end()
    px.setDevicePixelRatio(scale)
    return px
