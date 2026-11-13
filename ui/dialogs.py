"""App-styled dialogs — frameless cards, never the tiny OS message box."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ui import theme
from ui.widgets import ghost_button, primary_button


class AppMessageDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None,
        *,
        title: str,
        message: str,
        kind: str = "info",
        detail: str = "",
        buttons: str = "ok",
    ) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setMinimumWidth(460)
        self.setMaximumWidth(560)
        # Frameless — avoids the broken OS chrome that truncates "Settings saved"
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setObjectName("AppMessageDialog")

        c = theme.current()
        accent = theme.badge_colors({"info": "info", "success": "success", "warn": "warning", "error": "danger"}.get(kind, "info"))

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)

        card = QWidget(self)
        card.setObjectName("DialogCard")
        card.setStyleSheet(
            "QWidget#DialogCard {"
            f"  background: {c.panel};"
            f"  border: 1px solid {c.border};"
            "  border-radius: 16px;"
            "}"
        )
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 10)
        shadow.setColor(Qt.GlobalColor.black)
        # softer via stylesheet-ish opacity not available; keep mild
        card.setGraphicsEffect(shadow)
        shell.addWidget(card)

        root = QVBoxLayout(card)
        root.setContentsMargins(28, 26, 28, 22)
        root.setSpacing(18)

        head = QHBoxLayout()
        head.setSpacing(16)
        icon = QLabel(_icon_glyph(kind))
        icon.setFixedSize(44, 44)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(
            f"QLabel {{ background: {accent[1]}; color: {accent[0]}; border-radius: 22px; "
            f"font-size: 18px; font-weight: 700; }}"
        )
        head.addWidget(icon, alignment=Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(8)
        title_label = QLabel(title)
        title_label.setWordWrap(True)
        title_label.setStyleSheet(
            f"font-size: 17px; font-weight: 700; color: {c.text};"
        )
        text_col.addWidget(title_label)
        msg = QLabel(message)
        msg.setWordWrap(True)
        msg.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        msg.setMinimumWidth(320)
        msg.setStyleSheet(f"font-size: 13px; color: {c.text_secondary};")
        text_col.addWidget(msg)
        head.addLayout(text_col, stretch=1)
        root.addLayout(head)

        if detail.strip():
            browser = QTextBrowser()
            browser.setPlainText(detail.strip())
            browser.setMinimumHeight(120)
            browser.setMaximumHeight(220)
            browser.setStyleSheet(
                f"QTextBrowser {{ background: {c.panel_alt}; border: 1px solid {c.border}; "
                f"border-radius: 8px; padding: 10px; font-size: 13px; color: {c.text}; }}"
            )
            root.addWidget(browser)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch(1)
        self._result = False
        if buttons == "yesno":
            cancel = ghost_button("Cancel")
            cancel.clicked.connect(self.reject)
            ok = primary_button("Yes")
            ok.clicked.connect(self._accept_yes)
            actions.addWidget(cancel)
            actions.addWidget(ok)
        else:
            ok = primary_button("OK")
            ok.setMinimumWidth(104)
            ok.clicked.connect(self.accept)
            actions.addWidget(ok)
            ok.setDefault(True)
            ok.setFocus()
        root.addLayout(actions)

        self.adjustSize()
        self.resize(max(self.width(), 480), max(self.sizeHint().height(), 180))

    def _accept_yes(self) -> None:
        self._result = True
        self.accept()

    @property
    def confirmed(self) -> bool:
        return self._result


def _icon_glyph(kind: str) -> str:
    return {"info": "i", "success": "✓", "warn": "!", "error": "!"}.get(kind, "i")


def show_info(parent: QWidget | None, title: str, message: str, detail: str = "") -> None:
    AppMessageDialog(parent, title=title, message=message, kind="info", detail=detail).exec()


def show_success(parent: QWidget | None, title: str, message: str, detail: str = "") -> None:
    AppMessageDialog(parent, title=title, message=message, kind="success", detail=detail).exec()


def show_warning(parent: QWidget | None, title: str, message: str, detail: str = "") -> None:
    AppMessageDialog(parent, title=title, message=message, kind="warn", detail=detail).exec()


def show_error(parent: QWidget | None, title: str, message: str, detail: str = "") -> None:
    AppMessageDialog(parent, title=title, message=message, kind="error", detail=detail).exec()


def ask_yes_no(parent: QWidget | None, title: str, message: str) -> bool:
    dialog = AppMessageDialog(parent, title=title, message=message, kind="warn", buttons="yesno")
    dialog.exec()
    return dialog.confirmed
