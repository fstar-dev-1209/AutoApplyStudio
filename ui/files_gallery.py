"""Thumbnail gallery + preview pane for Generated Files (resume / PDF / cover letter pages, like a file explorer)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QModelIndex, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from core.files import FileRef
from core.models import JobItem, JobStatus
from core.timeutil import format_local
from ui import doc_preview, icons, system, theme
from ui.widgets import badge, copy_button, ghost_button, muted_label, primary_button

TILE = QSize(176, 244)
THUMB_W = 132
THUMB_H = 171  # US Letter ratio
THUMB_PX = THUMB_W * 2  # rendered at 2x so thumbnails stay sharp on HiDPI

R_PATH, R_ENTRY, R_THUMB = (Qt.ItemDataRole.UserRole + i for i in range(3))

KIND_LABEL = {"resume": "Resume · DOCX", "pdf": "Resume · PDF", "cover": "Cover letter · DOCX"}
KIND_SHORT = {"resume": "DOCX", "pdf": "PDF", "cover": "DOCX"}


@dataclass
class FileEntry:
    job: JobItem
    ref: FileRef

    @property
    def path(self) -> Path:
        return Path(self.ref.path)

    @property
    def title(self) -> str:
        return self.job.company or "Unknown company"

    @property
    def subtitle(self) -> str:
        return self.job.role_title or "Untitled role"


class _TileDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return TILE

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:  # noqa: N802
        c = theme.current()
        entry: FileEntry = index.data(R_ENTRY)
        thumb: QPixmap | None = index.data(R_THUMB)
        painter.save()
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
                               | QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(option.rect).adjusted(5, 5, -5, -5)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            path = QPainterPath()
            path.addRoundedRect(r, 9, 9)
            painter.fillPath(path, QColor(c.accent_soft if selected else c.hover))
            if selected:
                painter.setPen(QPen(QColor(c.accent), 1.4))
                painter.drawPath(path)

        tx = r.center().x() - THUMB_W / 2
        ty = r.top() + 12
        box = QRectF(tx, ty, THUMB_W, THUMB_H)
        # soft shadow
        for i, a in enumerate((14, 9, 5)):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, a))
            painter.drawRoundedRect(box.adjusted(-i, -i + 2, i, i + 2), 3, 3)
        painter.setBrush(QColor("white"))
        painter.setPen(QPen(QColor(c.border), 1))
        painter.drawRoundedRect(box, 3, 3)
        missing = not entry.ref.exists
        if missing:
            self._placeholder(painter, box, c, "Missing on disk", c.danger)
        elif thumb is None or thumb.isNull():
            self._placeholder(painter, box, c, "", c.text_secondary)
        else:
            ratio = thumb.devicePixelRatio() or 1.0
            tw, th = thumb.width() / ratio, thumb.height() / ratio
            scale = min(box.width() / tw, box.height() / th)
            w, h = tw * scale, th * scale
            painter.drawPixmap(QRectF(box.left() + (box.width() - w) / 2, box.top(), w, h), thumb,
                               QRectF(thumb.rect()))
        # kind chip on the thumbnail corner
        chip = KIND_SHORT[entry.ref.kind]
        font = QFont(option.font)
        font.setPixelSize(9)
        font.setBold(True)
        painter.setFont(font)
        cw = QFontMetrics(font).horizontalAdvance(chip) + 10
        chip_rect = QRectF(box.right() - cw - 5, box.bottom() - 19, cw, 14)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#E5484D" if entry.ref.kind == "pdf" else "#2773C4"))
        painter.drawRoundedRect(chip_rect, 4, 4)
        painter.setPen(QColor("white"))
        painter.drawText(chip_rect, Qt.AlignmentFlag.AlignCenter, chip)

        # caption: company, then role
        f1 = QFont(option.font)
        f1.setPixelSize(13)
        f1.setWeight(QFont.Weight.DemiBold)
        f2 = QFont(option.font)
        f2.setPixelSize(11.5)
        y = box.bottom() + 12
        text_w = r.width() - 16
        painter.setFont(f1)
        painter.setPen(QColor(c.text))
        painter.drawText(QRectF(r.left() + 8, y, text_w, 18), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                         QFontMetrics(f1).elidedText(entry.title, Qt.TextElideMode.ElideRight, int(text_w)))
        painter.setFont(f2)
        painter.setPen(QColor(c.text_secondary))
        sub = f"{entry.subtitle}"
        painter.drawText(QRectF(r.left() + 8, y + 18, text_w, 16), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                         QFontMetrics(f2).elidedText(sub, Qt.TextElideMode.ElideRight, int(text_w)))
        painter.restore()

    @staticmethod
    def _placeholder(painter: QPainter, box: QRectF, c, text: str, color: str) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(c.panel_alt))
        painter.drawRoundedRect(box.adjusted(1, 1, -1, -1), 3, 3)
        pm = icons.icon("doc", color, 30).pixmap(30, 30)
        painter.drawPixmap(int(box.center().x() - 15), int(box.center().y() - 24), pm)
        if text:
            f = QFont(painter.font())
            f.setPixelSize(11)
            painter.setFont(f)
            painter.setPen(QColor(color))
            painter.drawText(QRectF(box.left(), box.center().y() + 14, box.width(), 18), Qt.AlignmentFlag.AlignCenter, text)


class FileGallery(QListWidget):
    """Icon-mode list; thumbnails are rendered lazily, a few per event-loop tick, so the page opens instantly."""

    entry_selected = Signal(object)  # FileEntry | None
    entry_activated = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Gallery")
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setWrapping(True)
        self.setGridSize(TILE)
        self.setSpacing(0)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setItemDelegate(_TileDelegate(self))
        self._pending: list[QListWidgetItem] = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._render_some)
        self.currentItemChanged.connect(lambda cur, _prev: self.entry_selected.emit(
            cur.data(R_ENTRY) if cur else None))
        self.itemActivated.connect(lambda it: self.entry_activated.emit(it.data(R_ENTRY)))
        self.itemDoubleClicked.connect(lambda it: self.entry_activated.emit(it.data(R_ENTRY)))

    def set_entries(self, entries: list[FileEntry]) -> None:
        keep = self.current_path()
        self.blockSignals(True)
        self.clear()
        self._pending.clear()
        for e in entries:
            it = QListWidgetItem()
            it.setData(R_PATH, e.ref.path)
            it.setData(R_ENTRY, e)
            it.setSizeHint(TILE)
            it.setToolTip(f"{e.title} — {e.subtitle}\n{KIND_LABEL[e.ref.kind]}\n{e.ref.path}")
            self.addItem(it)
            if e.ref.exists:
                self._pending.append(it)
        self.blockSignals(False)
        target = next((self.item(i) for i in range(self.count()) if self.item(i).data(R_PATH) == keep), None)
        if target is None and self.count():  # default to the first file that is actually on disk
            target = next((self.item(i) for i in range(self.count()) if self.item(i).data(R_ENTRY).ref.exists),
                          self.item(0))
        if target is not None:
            self.setCurrentItem(target)
        else:
            self.entry_selected.emit(None)
        self._timer.start(0)

    def selected_entries(self) -> list[FileEntry]:
        return [it.data(R_ENTRY) for it in self.selectedItems()]

    def current_path(self) -> str:
        it = self.currentItem()
        return it.data(R_PATH) if it else ""

    def _render_some(self) -> None:
        for _ in range(2):
            if not self._pending:
                return
            it = self._pending.pop(0)
            try:
                path = it.data(R_PATH)
            except RuntimeError:  # the item was cleared in the meantime
                continue
            pm = doc_preview.first_page(path, THUMB_PX)
            if pm is not None:
                pm.setDevicePixelRatio(2.0)
                it.setData(R_THUMB, pm)
        if self._pending:
            self._timer.start(0)


def _fmt_size(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


class PreviewPane(QFrame):
    """Large page preview of the selected document plus its details and actions."""

    restore_requested = Signal(object)  # FileEntry

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("TableCard")
        self.setMinimumWidth(340)
        self._entry: FileEntry | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        self.title = QLabel("")
        self.title.setObjectName("SectionTitle")
        self.sub = muted_label("")
        self.kind = badge("", "info")
        self.state = badge("", "danger")
        head = QHBoxLayout()
        head.setSpacing(8)
        col = QVBoxLayout()
        col.setSpacing(1)
        col.addWidget(self.title)
        col.addWidget(self.sub)
        head.addLayout(col, 1)
        head.addWidget(self.state, alignment=Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.kind, alignment=Qt.AlignmentFlag.AlignTop)
        lay.addLayout(head)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pages_host = QWidget()
        self.pages_host.setObjectName("PreviewPages")
        self.pages_lay = QVBoxLayout(self.pages_host)
        self.pages_lay.setContentsMargins(0, 0, 0, 0)
        self.pages_lay.setSpacing(12)
        self.pages_lay.addStretch(1)
        self.scroll.setWidget(self.pages_host)
        lay.addWidget(self.scroll, 1)

        self.empty = QLabel("Select a document to preview it here")
        self.empty.setObjectName("Muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        lay.addWidget(self.empty, 1)

        self.meta = QVBoxLayout()
        self.meta.setSpacing(2)
        lay.addLayout(self.meta)
        self.meta_labels: dict[str, QLabel] = {}
        for key in ("File", "Folder", "Size", "Modified", "Generated"):
            row = QHBoxLayout()
            row.setSpacing(8)
            k = muted_label(key)
            k.setFixedWidth(64)
            v = QLabel("")
            v.setObjectName("KvValue")
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            v.setMinimumWidth(10)
            self.meta_labels[key] = v
            row.addWidget(k)
            row.addWidget(v, 1)
            if key == "Folder":
                self.copy_folder = copy_button("Copy folder path", lambda: str(self._entry.path.parent) if self._entry else "")
                row.addWidget(self.copy_folder)
            self.meta.addLayout(row)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.restore_btn = primary_button("Restore", "refresh")
        self.restore_btn.setToolTip("Rebuild this document from the resume JSON stored in the database")
        self.restore_btn.clicked.connect(lambda: self._entry and self.restore_requested.emit(self._entry))
        self.open_btn = primary_button("Open", "external")
        self.open_btn.clicked.connect(self._open)
        self.folder_btn = ghost_button("Show in folder", "folder-open")
        self.folder_btn.clicked.connect(self._show_folder)
        self.posting_btn = ghost_button("Job posting", "external")
        self.posting_btn.clicked.connect(self._open_posting)
        for b in (self.restore_btn, self.open_btn, self.folder_btn, self.posting_btn):
            actions.addWidget(b)
        actions.addStretch(1)
        lay.addLayout(actions)

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._render)
        self.show_entry(None)

    # ------------------------------------------------------------------ state

    def show_entry(self, entry: FileEntry | None) -> None:
        self._entry = entry
        has = entry is not None
        for w in (self.title, self.sub, self.kind, self.scroll, self.open_btn, self.folder_btn, self.posting_btn):
            w.setVisible(has)
        self.state.setVisible(False)
        self.restore_btn.setVisible(False)
        self.empty.setVisible(not has)
        for lab in self.meta_labels.values():
            lab.setText("")
        if not has:
            self.copy_folder.setVisible(False)
            self._clear_pages()
            return
        e = entry
        self.title.setText(e.title)
        self.sub.setText(e.subtitle)
        self.kind.setText(KIND_SHORT[e.ref.kind])
        exists = e.ref.exists
        self.state.setText("Missing on disk")
        self.state.setVisible(not exists)
        can_restore = not exists and bool(e.job.resume_json) and e.job.status == JobStatus.DONE
        self.restore_btn.setVisible(can_restore)
        self.open_btn.setVisible(exists)
        self.open_btn.setEnabled(exists)
        self.folder_btn.setEnabled(e.path.parent.exists())
        self.posting_btn.setEnabled(bool(e.job.origin_url or e.job.url))
        self.copy_folder.setVisible(True)
        m = self.meta_labels
        m["File"].setText(e.path.name)
        m["File"].setToolTip(str(e.path))
        m["Folder"].setText(self._elide(str(e.path.parent), m["Folder"]))
        m["Folder"].setToolTip(str(e.path.parent))
        try:
            st = e.path.stat()
            m["Size"].setText(_fmt_size(st.st_size))
            m["Modified"].setText(datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"))
        except OSError:
            m["Size"].setText("—")
            m["Modified"].setText("—")
        m["Generated"].setText(format_local(e.job.completed_at or e.job.updated_at, "%Y-%m-%d %H:%M"))
        self._render()

    @staticmethod
    def _elide(text: str, label: QLabel) -> str:
        return QFontMetrics(label.font()).elidedText(text, Qt.TextElideMode.ElideMiddle, 250)

    def _clear_pages(self) -> None:
        while self.pages_lay.count() > 1:
            item = self.pages_lay.takeAt(0)
            w = item.widget()
            if w:
                w.hide()  # a widget taken out of the layout keeps painting until Qt deletes it
                w.setParent(None)
                w.deleteLater()

    def _render(self) -> None:
        self._clear_pages()
        e = self._entry
        if e is None or not e.ref.exists:
            if e is not None:
                self._message("This file is no longer at the saved location."
                              + ("" if e.job.resume_json and e.job.status == JobStatus.DONE else
                                 "\nThere is no stored resume data to rebuild it from."))
            return
        dpr = self.devicePixelRatioF()
        width = max(220, self.scroll.viewport().width() - 4)
        pages = doc_preview.render_pages(e.path, round(width * dpr), 3)
        if not pages:
            self._message("No preview available for this file.")
            return
        c = theme.current()
        for pm in pages:
            pm = QPixmap(pm)
            pm.setDevicePixelRatio(dpr)
            lab = QLabel()
            lab.setPixmap(pm)
            lab.setFixedSize(pm.deviceIndependentSize().toSize())
            lab.setStyleSheet(f"background: white; border: 1px solid {c.border};")
            self.pages_lay.insertWidget(self.pages_lay.count() - 1, lab, alignment=Qt.AlignmentFlag.AlignHCenter)

    def _message(self, text: str) -> None:
        lab = muted_label(text)
        lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pages_lay.insertWidget(0, lab)

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        if self._entry is not None:
            self._resize_timer.start(140)

    def retheme(self) -> None:
        if self._entry is not None:
            self._render()

    # ---------------------------------------------------------------- actions

    def _open(self) -> None:
        if self._entry and not system.open_path(self._entry.ref.path):
            system.toast(self, f"File not found on disk: {self._entry.ref.path}", "warn")

    def _show_folder(self) -> None:
        if self._entry:
            system.open_folder(self._entry.path.parent)

    def _open_posting(self) -> None:
        if self._entry:
            system.open_url(self._entry.job.origin_url or self._entry.job.url)
