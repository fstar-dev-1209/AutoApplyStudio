"""In-app toast — soft success/error feedback without OS dialogs."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QTimer, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget


class Toast(QFrame):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("Toast")
        self.hide()
        self._anim: QPropertyAnimation | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)
        self.icon = QLabel("✓")
        self.icon.setFixedWidth(22)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message = QLabel("")
        self.message.setWordWrap(False)
        layout.addWidget(self.icon)
        layout.addWidget(self.message, stretch=1)

        self.setMinimumWidth(280)
        self.setMaximumWidth(480)
        self.raise_()

    def show_message(self, text: str, *, kind: str = "success", ms: int = 2600) -> None:
        from ui import theme

        fg, bg = theme.badge_colors({"success": "success", "info": "info", "warn": "warning", "error": "danger"}.get(kind, "info"))
        accent = fg
        glyph = {"success": "✓", "info": "i", "warn": "!", "error": "!"}.get(kind, "i")
        self.icon.setText(glyph)
        self.icon.setStyleSheet(
            f"color: {accent}; font-size: 16px; font-weight: 700; background: transparent;"
        )
        self.message.setText(text)
        self.message.setStyleSheet(
            f"color: {fg}; font-size: 14px; font-weight: 600; background: transparent;"
        )
        self.setStyleSheet(
            f"QFrame#Toast {{ background: {bg}; border: 1px solid {accent}; "
            f"border-radius: 14px; }}"
        )
        self.adjustSize()
        parent = self.parentWidget()
        if parent is not None:
            x = parent.width() - self.sizeHint().width() - 28
            y = parent.height() - self.sizeHint().height() - 28
            self.move(max(16, x), max(16, y))
        self.show()
        self.raise_()
        self.setWindowOpacity(0.0)
        self._anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._anim.setDuration(180)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.start()
        QTimer.singleShot(ms, self._fade_out)

    def _fade_out(self) -> None:
        self._anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._anim.setDuration(220)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self.hide)
        self._anim.start()
