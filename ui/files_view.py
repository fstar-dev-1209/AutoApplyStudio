"""Generated Files library: searchable, grouped by company, with on-disk existence indicators."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.application_db import ApplicationDatabase
from core.files import FileRef, file_refs, output_folder
from core.models import JobStatus
from core.timeutil import format_local
from ui import system
from ui.files_gallery import FileEntry, FileGallery, PreviewPane
from ui.widgets import copy_button, ghost_button, muted_label, page_title, primary_button, row_action, search_field, segment_button

FILE_ICONS = {"resume": "doc", "pdf": "pdf", "cover": "note"}
COL_NAME, COL_GENERATED, COL_RESUME, COL_PDF, COL_COVER, COL_ACTIONS = range(6)
SLICE_FROM = 40  # a list longer than this is drawn in slices of SLICE rows
SLICE = 15


class GeneratedFilesView(QWidget):
    sync_requested = Signal(list)  # job ids whose missing documents should be rebuilt from the database

    def __init__(self, output_root: Path | None = None, db: ApplicationDatabase | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ContentPane")
        self._root = output_root or Path.home() / "Remote"
        self._tree_build = 0
        self.db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 0, 16, 10)
        layout.setSpacing(10)
        box = QVBoxLayout()
        box.setSpacing(2)
        box.addWidget(page_title("Generated Files"))
        box.addWidget(muted_label("Resumes and cover letters stay on disk; this library tracks their locations."))
        layout.addLayout(box)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.search = search_field("Search by company, role or file name")
        self.search.textChanged.connect(self.refresh)
        bar.addWidget(self.search, 1)
        self.kind_combo = QComboBox()
        for value, label in (("", "All files"), ("resume", "Resumes (DOCX)"), ("pdf", "Resumes (PDF)"),
                             ("cover", "Cover letters")):
            self.kind_combo.addItem(label, value)
        self.kind_combo.currentIndexChanged.connect(self.refresh)
        bar.addWidget(self.kind_combo)
        self.group_box = QCheckBox("Group by company")
        self.group_box.setChecked(True)
        self.group_box.toggled.connect(self.refresh)
        bar.addWidget(self.group_box)
        self.group_box.hide()  # only the list view groups
        self.gallery_btn = segment_button("Gallery")
        self.tracked_btn = segment_button("List")
        self.disk_btn = segment_button("Folder view")
        self.gallery_btn.setChecked(True)
        self.gallery_btn.clicked.connect(lambda: self._mode(0))
        self.tracked_btn.clicked.connect(lambda: self._mode(1))
        self.disk_btn.clicked.connect(lambda: self._mode(2))
        for b in (self.gallery_btn, self.tracked_btn, self.disk_btn):
            bar.addWidget(b)
        self.sync_btn = ghost_button("Sync to disk", "refresh")
        self.sync_btn.clicked.connect(self._sync_clicked)
        refresh = ghost_button("Refresh", "refresh")
        refresh.clicked.connect(self.refresh)
        root_btn = ghost_button("Output folder", "folder-open")
        root_btn.clicked.connect(self._open_root)
        bar.addWidget(self.sync_btn)
        bar.addWidget(refresh)
        bar.addWidget(root_btn)
        layout.addLayout(bar)

        self.path_label = muted_label(str(self._root))
        layout.addWidget(self.path_label)

        self.stack = QStackedWidget()
        self.gallery = FileGallery()
        self.preview = PreviewPane()
        gallery_card = QFrame()
        gallery_card.setObjectName("TableCard")
        gl = QVBoxLayout(gallery_card)
        gl.setContentsMargins(6, 6, 6, 6)
        gl.addWidget(self.gallery)
        self.gallery_empty = QFrame()
        self.gallery_empty.setObjectName("EmptyState")
        ge = QVBoxLayout(self.gallery_empty)
        ge.setContentsMargins(30, 40, 30, 40)
        self.gallery_empty_title = QLabel("No documents yet")
        self.gallery_empty_title.setObjectName("EmptyTitle")
        self.gallery_empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        gb = muted_label("Completed jobs appear here, organised by company and role.")
        gb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ge.addWidget(self.gallery_empty_title)
        ge.addWidget(gb)
        gl.addWidget(self.gallery_empty)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(10)
        split.addWidget(gallery_card)
        split.addWidget(self.preview)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([640, 460])
        self.stack.addWidget(split)
        self.gallery.entry_selected.connect(self.preview.show_entry)
        self.gallery.entry_activated.connect(lambda e: e and system.open_path(e.ref.path))
        self.gallery.itemSelectionChanged.connect(self._update_sync_button)
        self.preview.restore_requested.connect(lambda e: self.sync_requested.emit([e.job.id]))

        card = QFrame()
        card.setObjectName("TableCard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(6, 4, 6, 4)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Company / Role", "Generated", "Resume", "PDF", "Cover letter", ""])
        self.tree.setIndentation(16)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self._update_sync_button)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setColumnWidth(COL_NAME, 340)
        for c, w in ((COL_GENERATED, 130), (COL_RESUME, 70), (COL_PDF, 70), (COL_COVER, 90), (COL_ACTIONS, 120)):
            self.tree.setColumnWidth(c, w)
        self.tree.itemDoubleClicked.connect(self._double_click)
        cl.addWidget(self.tree)
        self.empty = QFrame()
        self.empty.setObjectName("EmptyState")
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(30, 40, 30, 40)
        self.empty_title = QLabel("No documents yet")
        self.empty_title.setObjectName("EmptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        eb = muted_label("Completed jobs appear here, organised by company and role.")
        eb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(self.empty_title)
        el.addWidget(eb)
        cl.addWidget(self.empty)
        self.stack.addWidget(card)
        self.disk = DiskBrowser(self._root)
        self.stack.addWidget(self.disk)
        layout.addWidget(self.stack, 1)
        self.count_label = muted_label("")
        layout.addWidget(self.count_label)

    # --------------------------------------------------------------------- api

    def set_database(self, db: ApplicationDatabase) -> None:
        self.db = db

    def set_output_root(self, root: Path, *, refresh: bool = True) -> None:
        self._root = root
        self.path_label.setText(str(root))
        self.disk.set_root(root)
        if refresh:  # a page that is not on screen redraws when it is opened
            self.refresh()

    def focus_search(self) -> None:
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def _mode(self, index: int) -> None:
        self.gallery_btn.setChecked(index == 0)
        self.tracked_btn.setChecked(index == 1)
        self.disk_btn.setChecked(index == 2)
        self.stack.setCurrentIndex(index)
        self.group_box.setVisible(index == 1)
        self.kind_combo.setVisible(index != 2)
        self.sync_btn.setVisible(index != 2)
        self.refresh()

    def _open_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        system.open_path(self._root)

    # ----------------------------------------------------------------- library

    def refresh(self, *_a) -> None:
        if self.stack.currentIndex() == 2:
            self.disk.set_query(self.search.text())
            self.disk.refresh()
            self.count_label.setText("")
            return
        self.tree.clear()
        jobs = [j for j in (self.db.all_jobs() if self.db else []) if j.generated_files]
        q = self.search.text().strip().lower()
        if q:
            jobs = [j for j in jobs if q in f"{j.company} {j.role_title} {' '.join(Path(p).name for p in j.generated_files)}".lower()]
        jobs.sort(key=lambda j: j.completed_at or j.updated_at, reverse=True)
        total_files = missing = 0
        kind = self.kind_combo.currentData()
        entries: list[FileEntry] = []
        for job in jobs:
            refs = file_refs(job)
            entries += [FileEntry(job, r) for r in refs if r.path and (not kind or r.kind == kind)]
            for ref in refs[:3]:
                total_files += bool(ref.path)
                missing += bool(ref.path) and not ref.exists
        # The tracked-files tree has several buttons on every row, which is slow to draw; it is built only while it is
        # the view on screen, and in slices, so the window keeps answering (a frozen window gets closed by Windows).
        self._tree_build += 1
        if self.stack.currentIndex() == 1:
            first = len(jobs) if len(jobs) <= SLICE_FROM else SLICE
            groups: dict[str, QTreeWidgetItem] = {}
            self._add_tree_rows(jobs[:first], groups)
            if first < len(jobs):
                build = self._tree_build
                QTimer.singleShot(0, lambda: self._tree_slice(build, jobs, first, groups))
        has = bool(jobs)
        self.tree.setVisible(has)
        self.empty.setVisible(not has)
        self.empty_title.setText("No matching documents" if q else "No documents yet")
        self._view_jobs = jobs
        self.gallery.set_entries(entries)
        self.gallery.setVisible(bool(entries))
        self.gallery_empty.setVisible(not entries)
        self.gallery_empty_title.setText("No matching documents" if q or kind else "No documents yet")
        note = ""
        if missing:
            can = sum(1 for j in jobs if self._restorable(j))
            note = f" · {missing} file(s) missing on disk" + (
                f". Use Sync to disk to restore {can} job(s) from the database" if can else
                " (no stored resume data to rebuild them from)")
        self.count_label.setText(f"{len(jobs)} job(s), {total_files} file(s){note}")
        self._update_sync_button()

    def _add_tree_rows(self, jobs: list, groups: dict) -> None:
        for job in jobs:
            parent = self.tree.invisibleRootItem()
            if self.group_box.isChecked():
                key = (job.company or "Unknown company").casefold()
                if key not in groups:
                    groups[key] = QTreeWidgetItem([job.company or "Unknown company", "", "", "", "", ""])
                    f = groups[key].font(COL_NAME)
                    f.setBold(True)
                    groups[key].setFont(COL_NAME, f)
                    self.tree.addTopLevelItem(groups[key])
                parent = groups[key]
                label = job.role_title or "Untitled role"
            else:
                label = f"{job.company or 'Unknown company'} — {job.role_title or 'Untitled role'}"
            item = QTreeWidgetItem([label, format_local(job.completed_at or job.updated_at, "%Y-%m-%d %H:%M"),
                                    "", "", "", ""])
            item.setToolTip(COL_NAME, str(output_folder(job) or ""))
            item.setData(COL_NAME, Qt.ItemDataRole.UserRole, job.id)
            parent.addChild(item) if parent is not self.tree.invisibleRootItem() else self.tree.addTopLevelItem(item)
            for ref, col in zip(file_refs(job), (COL_RESUME, COL_PDF, COL_COVER)):
                self.tree.setItemWidget(item, col, self._file_button(ref))
            self.tree.setItemWidget(item, COL_ACTIONS, self._actions(job))
        self.tree.expandAll()

    def release_rows(self) -> None:
        """Drop the tree's row widgets (before an app-wide restyle, which would otherwise visit each of them)."""
        self._tree_build += 1
        self.tree.clear()

    def _tree_slice(self, build: int, jobs: list, start: int, groups: dict) -> None:
        if build != self._tree_build or self.stack.currentIndex() != 1:
            return
        end = min(start + SLICE, len(jobs))
        self._add_tree_rows(jobs[start:end], groups)
        if end < len(jobs):
            QTimer.singleShot(0, lambda: self._tree_slice(build, jobs, end, groups))

    # -------------------------------------------------------------------- sync

    @staticmethod
    def _restorable(job) -> bool:
        return job.status == JobStatus.DONE and bool(job.resume_json) and any(
            r.path and not r.exists for r in file_refs(job))

    def _selected_job_ids(self) -> list[str]:
        if self.stack.currentIndex() == 0:
            ids = [e.job.id for e in self.gallery.selected_entries()]
        else:
            ids = [it.data(COL_NAME, Qt.ItemDataRole.UserRole) for it in self.tree.selectedItems()]
        return list(dict.fromkeys(i for i in ids if i))

    def _update_sync_button(self) -> None:
        by_id = {j.id: j for j in getattr(self, "_view_jobs", [])}
        selected = [by_id[i] for i in self._selected_job_ids() if i in by_id]
        scope = selected if len(selected) > 1 else list(by_id.values())
        n = sum(1 for j in scope if self._restorable(j))
        self.sync_btn.setEnabled(n > 0)
        what = "the selected documents" if len(selected) > 1 else "every document in this view"
        self.sync_btn.setToolTip(
            f"Rebuild files deleted from disk, from the data stored in the app ({n} job(s) in {what}). "
            "Files that still exist are never overwritten." if n else
            "Nothing to sync: every file is on disk (or has no stored data to rebuild it from).")

    def _sync_clicked(self) -> None:
        by_id = {j.id: j for j in getattr(self, "_view_jobs", [])}
        selected = [i for i in self._selected_job_ids() if i in by_id]
        scope = selected if len(selected) > 1 else list(by_id)
        ids = [i for i in scope if self._restorable(by_id[i])]
        if ids:
            self.sync_requested.emit(ids)

    @staticmethod
    def _file_button(ref: FileRef) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(4, 0, 0, 0)
        if not ref.path:
            lab = QLabel("—")
            lab.setObjectName("Muted")
            h.addWidget(lab)
            return w
        btn = row_action(FILE_ICONS[ref.kind], f"Open {ref.label}" if ref.exists else f"{ref.label}: missing on disk",
                         "text" if ref.exists else "danger")
        btn.clicked.connect(lambda _=False, p=ref.path, b=btn: system.open_path(p) or system.toast(
            b, f"File not found on disk: {p}", "warn"))
        h.addWidget(btn)
        h.addStretch(1)
        return w

    def _actions(self, job) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        folder = output_folder(job)
        op = row_action("folder-open", "Open containing folder", "text")
        op.setEnabled(bool(folder and folder.exists()))
        op.clicked.connect(lambda _=False, f=folder: f and system.open_folder(f))
        cp = copy_button("Copy output path", lambda f=folder: str(f) if f else "")
        cp.setEnabled(bool(folder))
        link = row_action("external", "Open job posting", "text")
        link.clicked.connect(lambda _=False, u=job.origin_url or job.url: system.open_url(u))
        restore = row_action("refresh", "Restore missing files from the database", "text")
        restore.setVisible(self._restorable(job))
        restore.clicked.connect(lambda _=False, j=job.id: self.sync_requested.emit([j]))
        for b in (restore, op, cp, link):
            h.addWidget(b)
        h.addStretch(1)
        return w

    def _double_click(self, item: QTreeWidgetItem, _col: int) -> None:
        tip = item.toolTip(COL_NAME)
        if tip:
            system.open_folder(tip)


class DiskBrowser(QWidget):
    """The original folder tree (<root>/<Name>/<Company>/<Role>/files) for anything not tracked in the library."""

    EXT = {".docx", ".pdf", ".json", ".txt"}

    def __init__(self, root: Path, parent=None) -> None:
        super().__init__(parent)
        self._root = root
        self._query = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("TableCard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(6, 4, 6, 4)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Type", "Size"])
        self.tree.setColumnWidth(0, 420)
        self.tree.itemDoubleClicked.connect(lambda it, _c: self._open(it))
        cl.addWidget(self.tree)
        lay.addWidget(card, 1)
        row = QHBoxLayout()
        op = primary_button("Open")
        op.clicked.connect(lambda: self.tree.selectedItems() and self._open(self.tree.selectedItems()[0]))
        fo = ghost_button("Open folder")
        fo.clicked.connect(self._open_folder)
        row.addWidget(op)
        row.addWidget(fo)
        row.addStretch(1)
        lay.addLayout(row)

    def set_root(self, root: Path) -> None:
        self._root = root

    def set_query(self, q: str) -> None:
        self._query = q.strip().lower()

    def _open(self, item: QTreeWidgetItem) -> None:
        raw = item.data(0, Qt.ItemDataRole.UserRole)
        if raw:
            system.open_path(raw)

    def _open_folder(self) -> None:
        items = self.tree.selectedItems()
        if items and items[0].data(0, Qt.ItemDataRole.UserRole):
            system.open_folder(items[0].data(0, Qt.ItemDataRole.UserRole))

    def refresh(self) -> None:
        self.tree.clear()
        root, q = self._root, self._query
        if not root.exists():
            return
        for person in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name.lower()):
            p_item = QTreeWidgetItem([person.name, "Person", ""])
            p_item.setData(0, Qt.ItemDataRole.UserRole, str(person))
            for company in sorted((p for p in person.iterdir() if p.is_dir()), key=lambda p: p.name.lower()):
                c_item = QTreeWidgetItem([company.name, "Company", ""])
                c_item.setData(0, Qt.ItemDataRole.UserRole, str(company))
                for role in sorted((p for p in company.iterdir() if p.is_dir()), key=lambda p: p.name.lower()):
                    if q and q not in f"{company.name} {role.name}".lower():
                        continue
                    r_item = QTreeWidgetItem([role.name, "Role", ""])
                    r_item.setData(0, Qt.ItemDataRole.UserRole, str(role))
                    for f in sorted(role.glob("*")):
                        if f.is_file() and f.suffix.lower() in self.EXT:
                            it = QTreeWidgetItem([f.name, f.suffix.upper().lstrip("."), f"{f.stat().st_size / 1024:.1f} KB"])
                            it.setData(0, Qt.ItemDataRole.UserRole, str(f))
                            r_item.addChild(it)
                    if r_item.childCount():
                        c_item.addChild(r_item)
                if c_item.childCount():
                    p_item.addChild(c_item)
            if p_item.childCount():
                self.tree.addTopLevelItem(p_item)
        self.tree.expandAll() if q else None
