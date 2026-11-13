"""JSON editor: line-number gutter and syntax colouring that follows the theme."""

from __future__ import annotations

import re

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import QPlainTextEdit, QWidget

from ui import theme

_TOKEN = re.compile(
    r'(?P<key>"(?:[^"\\]|\\.)*"(?=\s*:))|(?P<str>"(?:[^"\\]|\\.)*")|(?P<num>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)'
    r"|(?P<lit>\b(?:true|false|null)\b)|(?P<punct>[{}\[\],:])")


def _colors() -> dict[str, QColor]:
    if theme.current().is_light:
        return {"key": QColor("#1D4ED8"), "str": QColor("#15803D"), "num": QColor("#C2410C"),
                "lit": QColor("#7C3AED"), "punct": QColor("#64748B")}
    return {"key": QColor("#7FB0FF"), "str": QColor("#8BE0A4"), "num": QColor("#FFB27A"),
            "lit": QColor("#C4A6FF"), "punct": QColor("#7C8AA5")}


class JsonHighlighter(QSyntaxHighlighter):
    def highlightBlock(self, text: str) -> None:  # noqa: N802
        colors = _colors()
        for m in _TOKEN.finditer(text):
            fmt = QTextCharFormat()
            fmt.setForeground(colors[m.lastgroup])
            self.setFormat(m.start(), m.end() - m.start(), fmt)


class _Gutter(QWidget):
    def __init__(self, editor: "JsonEditor") -> None:
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, e) -> None:  # noqa: N802
        self.editor.paint_gutter(e)


class JsonEditor(QPlainTextEdit):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(10)
        self.setFont(font)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(28)
        self.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.highlighter = JsonHighlighter(self.document())
        self._gutter = _Gutter(self)
        self.blockCountChanged.connect(self._update_width)
        self.updateRequest.connect(self._scroll_gutter)
        self.cursorPositionChanged.connect(self._gutter.update)
        self._update_width()

    def gutter_width(self) -> int:
        digits = max(2, len(str(self.blockCount())))
        return 22 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_width(self) -> None:
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _scroll_gutter(self, rect, dy) -> None:
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_width()

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        self.highlighter.rehighlight()  # pick up a theme change made while hidden

    def paint_gutter(self, event) -> None:
        c = theme.current()
        p = QPainter(self._gutter)
        p.fillRect(event.rect(), QColor(c.panel_alt))
        p.setPen(QColor(c.border))
        p.drawLine(self._gutter.width() - 1, event.rect().top(), self._gutter.width() - 1, event.rect().bottom())
        current = self.textCursor().blockNumber()
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        font = QFont(self.font())
        p.setFont(font)
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                p.setPen(QColor(c.text if number == current else c.text_secondary))
                p.drawText(0, top, self._gutter.width() - 10, self.fontMetrics().height(),
                           Qt.AlignmentFlag.AlignRight, str(number + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1
