"""Small, theme-aware charts drawn with QPainter (bar, line, funnel)."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from ui import theme


def nice_ceiling(value: float) -> int:
    """Smallest 'round' integer >= value so gridlines land on clean numbers."""
    if value <= 4:
        return max(4, int(math.ceil(value)))
    magnitude = 10 ** int(math.floor(math.log10(value)))
    for step in (1, 2, 2.5, 4, 5, 8, 10):
        if value <= step * magnitude:
            return int(math.ceil(step * magnitude))
    return int(math.ceil(value))


class _Base(QWidget):
    PAD_L, PAD_R, PAD_T, PAD_B = 34, 10, 8, 24

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.labels: list[str] = []
        self._empty_text = "No data for this period"

    def _plot(self) -> QRectF:
        return QRectF(self.PAD_L, self.PAD_T, max(10, self.width() - self.PAD_L - self.PAD_R),
                      max(10, self.height() - self.PAD_T - self.PAD_B))

    def _font(self, size: float = 8.5) -> QFont:
        f = QFont(self.font())
        f.setPointSizeF(size)
        return f

    def _grid(self, p: QPainter, plot: QRectF, top: int) -> None:
        c = theme.current()
        p.setFont(self._font())
        for i in range(5):
            y = plot.bottom() - plot.height() * i / 4
            p.setPen(QPen(QColor(c.border_soft if i else c.border), 1))
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(QColor(c.text_secondary))
            p.drawText(QRectF(0, y - 8, self.PAD_L - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       str(round(top * i / 4)))

    def _x_labels(self, p: QPainter, plot: QRectF, n: int, centered: bool) -> None:
        if not n:
            return
        c = theme.current()
        p.setPen(QColor(c.text_secondary))
        p.setFont(self._font(8))
        max_labels = max(2, int(plot.width() // 58))
        stride = max(1, math.ceil(n / max_labels))
        slot = plot.width() / n if centered else plot.width() / max(1, n - 1)
        for i in range(0, n, stride):
            x = plot.left() + (slot * (i + 0.5) if centered else slot * i)
            rect = QRectF(x - 30, plot.bottom() + 4, 60, 16)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.labels[i])

    def _empty(self, p: QPainter) -> None:
        p.setPen(QColor(theme.current().text_secondary))
        p.setFont(self._font(9.5))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._empty_text)


class BarChart(_Base):
    def __init__(self, parent=None, color_index: int = 0) -> None:
        super().__init__(parent)
        self.values: list[int] = []
        self.color_index = color_index
        self.unit = ""

    def set_data(self, labels: list[str], values: list[int], unit: str = "") -> None:
        self.labels, self.values, self.unit = labels, values, unit
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.values or not any(self.values):
            self._empty(p)
            return
        c = theme.current()
        plot = self._plot()
        top = nice_ceiling(max(self.values))
        self._grid(p, plot, top)
        n = len(self.values)
        slot = plot.width() / n
        bar_w = max(2.0, min(34.0, slot * 0.62))
        color = QColor(c.series[self.color_index % len(c.series)])
        for i, v in enumerate(self.values):
            h = plot.height() * v / top
            x = plot.left() + slot * (i + 0.5) - bar_w / 2
            path = QPainterPath()
            path.addRoundedRect(QRectF(x, plot.bottom() - h, bar_w, h), 2, 2)
            p.fillPath(path, color)
            if v and n <= 16:
                p.setPen(QColor(c.text))
                p.setFont(self._font(8))
                p.drawText(QRectF(x - 10, plot.bottom() - h - 15, bar_w + 20, 14), Qt.AlignmentFlag.AlignCenter, str(v))
        self._x_labels(p, plot, n, centered=True)

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if not self.values:
            return
        plot = self._plot()
        idx = int((e.position().x() - plot.left()) / (plot.width() / len(self.values)))
        if 0 <= idx < len(self.values):
            QToolTip.showText(e.globalPosition().toPoint(), f"{self.labels[idx]}: {self.values[idx]}{self.unit}", self)


class LineChart(_Base):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.series: list[tuple[str, list[int]]] = []

    def set_data(self, labels: list[str], series: list[tuple[str, list[int]]]) -> None:
        self.labels, self.series = labels, series
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.series or not any(any(v) for _n, v in self.series):
            self._empty(p)
            return
        c = theme.current()
        plot = self._plot()
        plot.setTop(plot.top() + 18)  # room for the legend
        top = nice_ceiling(max(max(v) for _n, v in self.series if v))
        self._grid(p, plot, top)
        n = len(self.labels)
        step = plot.width() / max(1, n - 1)
        x_of = lambda i: plot.left() + (step * i if n > 1 else plot.width() / 2)  # noqa: E731
        lx = plot.left()
        for si, (name, vals) in enumerate(self.series):
            color = QColor(c.series[si % len(c.series)])
            path = QPainterPath()
            for i, v in enumerate(vals):
                pt = QPointF(x_of(i), plot.bottom() - plot.height() * v / top)
                path.moveTo(pt) if i == 0 else path.lineTo(pt)
            if si == 0 and n > 1:  # soft area under the primary series
                area = QPainterPath(path)
                area.lineTo(QPointF(x_of(n - 1), plot.bottom()))
                area.lineTo(QPointF(x_of(0), plot.bottom()))
                area.closeSubpath()
                grad = QLinearGradient(0, plot.top(), 0, plot.bottom())
                top_c, bot_c = QColor(color), QColor(color)
                top_c.setAlphaF(0.28)
                bot_c.setAlphaF(0.0)
                grad.setColorAt(0, top_c)
                grad.setColorAt(1, bot_c)
                p.fillPath(area, QBrush(grad))
            p.setPen(QPen(color, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawPath(path)
            if n <= 40:
                p.setBrush(color)
                for i, v in enumerate(vals):
                    if v:
                        p.drawEllipse(QPointF(x_of(i), plot.bottom() - plot.height() * v / top), 2.8, 2.8)
                p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(color)
            p.setBrush(color)
            p.drawEllipse(QPointF(lx + 4, 9), 4, 4)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QColor(c.text_secondary))
            p.setFont(self._font(8.5))
            p.drawText(QRectF(lx + 12, 1, 110, 16), Qt.AlignmentFlag.AlignVCenter, name)
            lx += 122
        self._x_labels(p, plot, n, centered=False)

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        n = len(self.labels)
        if not n or not self.series:
            return
        plot = self._plot()
        step = plot.width() / max(1, n - 1)
        idx = round((e.position().x() - plot.left()) / step) if n > 1 else 0
        if 0 <= idx < n:
            tip = self.labels[idx] + "".join(f"\n{name}: {vals[idx]}" for name, vals in self.series)
            QToolTip.showText(e.globalPosition().toPoint(), tip, self)


class FunnelChart(_Base):
    """Horizontal funnel: bar length = share of the first step; labels show counts and conversion."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.steps: list[tuple[str, int, str]] = []
        self.setMinimumHeight(190)

    def set_data(self, steps: list[tuple[str, int, str]]) -> None:
        self.steps = steps
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.steps or not self.steps[0][1]:
            self._empty(p)
            return
        c = theme.current()
        n = len(self.steps)
        label_w, value_w = 82, 132
        row_h = self.height() / n
        bar_max = max(10, self.width() - label_w - value_w - 8)
        top = self.steps[0][1]
        for i, (name, count, note) in enumerate(self.steps):
            y = i * row_h
            p.setPen(QColor(c.text))
            p.setFont(self._font(9))
            p.drawText(QRectF(0, y, label_w, row_h), Qt.AlignmentFlag.AlignVCenter, name)
            w = bar_max * count / top
            path = QPainterPath()
            path.addRoundedRect(QRectF(label_w, y + row_h * 0.2, max(3.0, w), row_h * 0.6), 3, 3)
            shade = QColor(c.accent)
            shade.setAlphaF(1.0 - 0.1 * i if i < 8 else 0.3)
            p.fillPath(path, shade)
            p.setPen(QColor(c.text))
            p.drawText(QRectF(label_w + bar_max + 8, y, value_w, row_h), Qt.AlignmentFlag.AlignVCenter,
                       f"{count}  ·  {note}" if note else str(count))


class DonutChart(_Base):
    """Ring chart with the total in the centre and a legend on the right."""

    COLORS = ["#34D07B", "#F5B73B", "#A78BFA", "#4C86F5", "#F26D6D", "#7C8BA1"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.parts: list[tuple[str, int]] = []
        self.setMinimumHeight(190)

    def set_data(self, parts: list[tuple[str, int]]) -> None:
        self.parts = [(n, v) for n, v in parts if v]
        self.update()

    def _color(self, i: int) -> QColor:
        return QColor(self.COLORS[i % len(self.COLORS)])

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        total = sum(v for _n, v in self.parts)
        if not total:
            self._empty(p)
            return
        c = theme.current()
        side = min(self.height() - 12, self.width() * 0.4)
        ring = QRectF(8, (self.height() - side) / 2, side, side)
        pen_w = side * 0.17
        inner = ring.adjusted(pen_w / 2, pen_w / 2, -pen_w / 2, -pen_w / 2)
        start = 90 * 16
        for i, (_n, v) in enumerate(self.parts):
            span = -int(360 * 16 * v / total)
            p.setPen(QPen(self._color(i), pen_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawArc(inner, start, span)
            start += span
        p.setPen(QColor(c.text))
        p.setFont(self._font(18))
        f = p.font()
        f.setBold(True)
        p.setFont(f)
        p.drawText(ring.adjusted(0, -8, 0, 0), Qt.AlignmentFlag.AlignCenter, str(total))
        p.setPen(QColor(c.text_secondary))
        p.setFont(self._font(8.5))
        p.drawText(ring.adjusted(0, 26, 0, 0), Qt.AlignmentFlag.AlignCenter, "applications")
        x = ring.right() + 22
        row_h = min(26.0, (self.height() - 10) / max(1, len(self.parts)))
        y = (self.height() - row_h * len(self.parts)) / 2
        for i, (name, v) in enumerate(self.parts):
            p.setBrush(self._color(i))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(x + 4, y + row_h / 2), 4, 4)
            p.setPen(QColor(c.text))
            p.setFont(self._font(9))
            p.drawText(QRectF(x + 16, y, self.width() - x - 70, row_h), Qt.AlignmentFlag.AlignVCenter, name)
            p.setPen(QColor(c.text_secondary))
            p.drawText(QRectF(self.width() - 64, y, 56, row_h),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       f"{v}  ({round(100 * v / total)}%)")
            y += row_h


class RankedBars(_Base):
    """Horizontal ranked list (e.g. top companies) with proportional bars."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.items: list[tuple[str, int]] = []
        self.setMinimumHeight(190)

    def set_data(self, items: list[tuple[str, int]]) -> None:
        self.items = items
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.items:
            self._empty(p)
            return
        c = theme.current()
        top = max(v for _n, v in self.items)
        row_h = min(34.0, self.height() / len(self.items))
        label_w, val_w = 120, 34
        bar_x, bar_w = label_w + 8, max(10, self.width() - label_w - val_w - 16)
        for i, (name, v) in enumerate(self.items):
            y = i * row_h
            p.setPen(QColor(c.text))
            p.setFont(self._font(9))
            p.drawText(QRectF(0, y, label_w, row_h), Qt.AlignmentFlag.AlignVCenter,
                       p.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, label_w))
            track = QPainterPath()
            track.addRoundedRect(QRectF(bar_x, y + row_h / 2 - 4, bar_w, 8), 4, 4)
            p.fillPath(track, QColor(c.border_soft))
            fill = QPainterPath()
            fill.addRoundedRect(QRectF(bar_x, y + row_h / 2 - 4, max(8.0, bar_w * v / top), 8), 4, 4)
            p.fillPath(fill, QColor(c.series[i % len(c.series)]))
            p.setPen(QColor(c.text_secondary))
            p.drawText(QRectF(self.width() - val_w, y, val_w, row_h),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, str(v))


class GroupedBarChart(_Base):
    """Per time bucket, one bar per measure (bids, replies, screening, ...), with a legend and a detailed tooltip."""

    COLORS = ["#4C86F5", "#A78BFA", "#F5B73B", "#2DD4BF", "#F58A3B", "#34D07B"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.series: list[tuple[str, list[int]]] = []
        self.setMinimumHeight(250)

    def set_data(self, labels: list[str], series: list[tuple[str, list[int]]]) -> None:
        self.labels, self.series = labels, series
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.series or not self.series[0][1] or not any(self.series[0][1]):
            self._empty(p)
            return
        c = theme.current()
        plot = self._plot()
        plot.setTop(plot.top() + 22)  # legend
        top = nice_ceiling(max(max(v) for _n, v in self.series if v))
        self._grid(p, plot, top)
        n = len(self.labels)
        k = len(self.series)
        slot = plot.width() / n
        bar_w = max(3.0, min(22.0, slot * 0.82 / k))
        group_w = bar_w * k
        for gi in range(n):
            x0 = plot.left() + slot * (gi + 0.5) - group_w / 2
            for si, (_name, vals) in enumerate(self.series):
                v = vals[gi]
                h = plot.height() * v / top
                path = QPainterPath()
                path.addRoundedRect(QRectF(x0 + si * bar_w + 0.5, plot.bottom() - h, max(2.0, bar_w - 1.5), h), 2, 2)
                p.fillPath(path, QColor(self.COLORS[si % len(self.COLORS)]))
                if v and bar_w >= 11:
                    p.setPen(QColor(c.text))
                    p.setFont(self._font(7.5))
                    p.drawText(QRectF(x0 + si * bar_w - 6, plot.bottom() - h - 14, bar_w + 12, 13),
                               Qt.AlignmentFlag.AlignCenter, str(v))
        lx = plot.left()
        for si, (name, vals) in enumerate(self.series):
            color = QColor(self.COLORS[si % len(self.COLORS)])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(lx, 5, 10, 10), 3, 3)
            p.setPen(QColor(c.text_secondary))
            p.setFont(self._font(8.5))
            label = f"{name}  {sum(vals)}"
            p.drawText(QRectF(lx + 15, 1, 120, 18), Qt.AlignmentFlag.AlignVCenter, label)
            lx += 28 + p.fontMetrics().horizontalAdvance(label)
        p.setBrush(Qt.BrushStyle.NoBrush)
        self._x_labels(p, plot, n, centered=True)

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        n = len(self.labels)
        if not n or not self.series:
            return
        plot = self._plot()
        idx = int((e.position().x() - plot.left()) / (plot.width() / n))
        if not 0 <= idx < n:
            return
        bids = self.series[0][1][idx]
        lines = [self.labels[idx]]
        for i, (name, vals) in enumerate(self.series):
            pct = "" if i == 0 or not bids else f"  ({round(100 * vals[idx] / bids)}% of bids)"
            lines.append(f"{name}: {vals[idx]}{pct}")
        QToolTip.showText(e.globalPosition().toPoint(), "\n".join(lines), self)
