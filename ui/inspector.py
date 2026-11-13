"""Right-side job inspector — card-style Fluent panel."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QSizePolicy,
    QLabel,
    QProgressBar,
    QScrollArea,
    QPlainTextEdit,
    QStyle,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.files import file_refs
from core.models import JobItem, JobStatus
from core.prompt_builder import build_claude_chat_prompt_for_job
from core.settings_store import SettingsStore
from ui import copy as C
from ui.dialogs import show_warning
import re

from ui import icons, theme
from ui.job_steps import STEPS, current_activity, progress_pct, step_states
from ui.widgets import circle_icon, copy_button, flash_copied, ring_icon, row_action, section_title, clear_layout, ghost_button, icon_button, muted_label, status_badge


class JobInspector(QWidget):
    submit_jd = Signal(str, str)
    retry_job = Signal(str, str)
    skip_job = Signal(str)
    open_folder = Signal(str)
    import_json = Signal(str, str)
    pull_claude_json = Signal(str)
    copy_claude_json = Signal()
    sync_files = Signal(str)  # restore this job's missing documents from the database
    allow_duplicate = Signal(str)
    pause_queue = Signal()
    closed = Signal()

    def __init__(
        self,
        settings_store: SettingsStore | None = None,
        events_provider=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("InspectorPane")
        self.setMinimumWidth(280)
        self._job: JobItem | None = None
        self.viewer_mode = False  # True when this window only views the controller's queue
        self._light = True
        self._settings_store = settings_store
        self._events_provider = events_provider

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 4, 14, 10)
        root.setSpacing(0)
        self.card = QFrame()
        self.card.setObjectName("Card")
        cl = QVBoxLayout(self.card)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        root.addWidget(self.card)

        self.empty = QFrame()
        self.empty.setObjectName("EmptyState")
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(20, 36, 20, 36)
        el.setSpacing(8)
        el.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_title = QLabel(C.INSPECTOR_EMPTY_TITLE)
        empty_title.setObjectName("EmptyTitle")
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_body = muted_label(C.INSPECTOR_EMPTY_BODY)
        empty_body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(empty_title)
        el.addWidget(empty_body)
        cl.addWidget(self.empty)

        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(16, 12, 16, 14)
        body_layout.setSpacing(12)

        head = QFrame()
        head.setObjectName("InspectorHead")
        head_row = QHBoxLayout(head)
        head_row.setContentsMargins(18, 12, 12, 12)
        head_title = QLabel("Job Inspector")
        head_title.setObjectName("PageTitle")
        head_title.setStyleSheet("font-size: 18px;")
        head_row.addWidget(head_title, 1)
        close_btn = row_action("x", "Close inspector", "text")
        close_btn.clicked.connect(self.closed.emit)
        head_row.addWidget(close_btn)
        cl.insertWidget(0, head)

        hero = QFrame()
        hero.setObjectName("Hero")
        hero_l = QHBoxLayout(hero)
        hero_l.setContentsMargins(0, 2, 0, 2)
        hero_l.setSpacing(12)
        self.avatar = QLabel()
        self.avatar.setFixedSize(44, 44)
        hero_l.addWidget(self.avatar, alignment=Qt.AlignmentFlag.AlignTop)
        hero_text = QVBoxLayout()
        hero_text.setSpacing(1)
        self.company = QLabel("")
        self.company.setObjectName("PageTitle")
        self.company.setStyleSheet("font-size: 17px;")
        self.title = muted_label("")
        hero_text.addWidget(self.company)
        hero_text.addWidget(self.title)
        hero_l.addLayout(hero_text, 1)
        self.badge_host = QHBoxLayout()
        self.badge_host.setContentsMargins(0, 0, 0, 0)
        hero_l.addLayout(self.badge_host)
        body_layout.addWidget(hero)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("Segmented")
        self.details_tab = QScrollArea()
        self.details_tab.setWidgetResizable(True)
        self.details_tab.setFrameShape(QFrame.Shape.NoFrame)
        self.details_tab.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.details_content = QWidget()
        self.details_tab.setWidget(self.details_content)
        self.jd_tab = QWidget()
        self.files_tab = QWidget()
        self.logs_tab = QWidget()
        for tab, label in ((self.details_tab, "Details"), (self.jd_tab, "Description"),
                           (self.files_tab, "Files"), (self.logs_tab, "History")):
            self.tabs.addTab(tab, label)
        self.tabs.tabBar().setUsesScrollButtons(False)
        self.tabs.tabBar().setExpanding(True)
        body_layout.addWidget(self.tabs, stretch=1)

        self._build_details()
        self._build_jd()
        self._build_files()
        self._build_logs()

        cl.addWidget(self.body, stretch=1)
        self.body.hide()

    def set_theme_light(self, light: bool) -> None:
        self._light = light
        if self._job:
            self.show_job(self._job)

    def clear(self) -> None:
        self._job = None
        self.body.hide()
        self.empty.show()

    def show_job(self, job: JobItem) -> None:
        self._job = job
        self.empty.hide()
        self.body.show()

        role = job.role_title or C.INSPECTOR_UNTITLED
        name = job.company or C.INSPECTOR_UNKNOWN_COMPANY
        self.title.setText(role if len(role) <= 38 else role[:37] + "…")
        self.title.setToolTip(role)
        self.company.setText(name if len(name) <= 24 else name[:23] + "…")
        self.company.setToolTip(name)
        self.avatar.setPixmap(_building_pixmap(44))
        self._fill_steps(job)

        clear_layout(self.badge_host)
        self.badge_host.addWidget(status_badge(job.status.value, light=self._light))

        self.source_val.setText(job.source or "—")
        self.url_val.setText(job.origin_url or job.url or "—")
        self.url_val.setToolTip(job.origin_url or job.url)
        self.stage_val.setText(job.stage or "—")
        self.path_val.setText(job.output_path or "—")
        self.created_val.setText(_short_time(job.created_at))

        show_error = bool(job.error) and job.status in {JobStatus.NEEDS_JD, JobStatus.FAILED}
        show_skip = job.status == JobStatus.SKIPPED and bool(job.skip_reason)
        self.error_banner.setVisible(show_error or show_skip)
        if job.status == JobStatus.FAILED:
            self.error_banner.setStyleSheet(_banner_style("danger", self._light))
            self.error_banner.setText(job.error or C.INSPECTOR_ERROR_GENERIC)
        elif job.status == JobStatus.NEEDS_JD:
            self.error_banner.setStyleSheet(_banner_style("info", self._light))
            self.error_banner.setText(job.error or C.INSPECTOR_JD_BANNER)
        elif show_skip:
            self.error_banner.setStyleSheet(_banner_style("warn", self._light))
            self.error_banner.setText(job.skip_reason)

        self.jd_editor.setPlainText(job.job_description or "")
        needs = job.status == JobStatus.NEEDS_JD
        self.jd_hint.setVisible(needs or not job.job_description)
        save_tip = C.INSPECTOR_SUBMIT_JD if needs else C.INSPECTOR_SAVE_JD
        self.save_jd_btn.setToolTip(save_tip)

        self._rebuild_files(job)

        has_jd = bool(job.job_description.strip())
        self.copy_prompt_btn.setVisible(has_jd)
        show_json_actions = has_jd and job.status in {
            JobStatus.FAILED,
            JobStatus.QUEUED,
            JobStatus.GENERATING,
            JobStatus.NEEDS_JD,
        }
        self.import_json_btn.setVisible(show_json_actions)
        self.pull_claude_btn.setVisible(show_json_actions)
        self.copy_claude_json_btn.setVisible(show_json_actions)
        self.retry_export_btn.setVisible(job.status == JobStatus.FAILED and bool(job.resume_json))
        self.retry_gen_btn.setVisible(job.status == JobStatus.FAILED)
        self.skip_btn.setVisible(job.status in {JobStatus.NEEDS_JD, JobStatus.FAILED, JobStatus.QUEUED})
        self.allow_dup_btn.setVisible(
            job.status == JobStatus.SKIPPED and "company folder" in (job.skip_reason or "").lower()
        )
        if self.viewer_mode:  # generation, retries and Claude hand-offs are done on the controller
            for button in (self.copy_prompt_btn, self.import_json_btn, self.pull_claude_btn,
                           self.copy_claude_json_btn, self.retry_export_btn, self.retry_gen_btn):
                button.setVisible(False)

        self.logs_view.setPlainText("\n".join(job.logs) if job.logs else C.INSPECTOR_NO_LOGS)

        if job.status == JobStatus.NEEDS_JD:
            self.tabs.setCurrentWidget(self.jd_tab)
        elif job.status == JobStatus.DONE:
            self.tabs.setCurrentWidget(self.files_tab)
        elif job.status == JobStatus.FAILED:
            self.tabs.setCurrentWidget(self.details_tab)

    def _build_details(self) -> None:
        layout = QVBoxLayout(self.details_content)
        layout.setContentsMargins(4, 14, 4, 8)
        layout.setSpacing(10)

        self.error_banner = QLabel("")
        self.error_banner.setWordWrap(True)
        self.error_banner.hide()
        layout.addWidget(self.error_banner)

        layout.addWidget(section_title("Processing Stage"))
        self.activity_row = QHBoxLayout()
        self.activity_row.setSpacing(8)
        self.activity_icon = QLabel()
        self.activity_icon.setFixedSize(20, 20)
        self.activity_label = QLabel("")
        self.activity_label.setWordWrap(True)
        self.activity_row.addWidget(self.activity_icon)
        self.activity_row.addWidget(self.activity_label, 1)
        layout.addLayout(self.activity_row)
        self.steps_bar = QProgressBar()
        self.steps_bar.setObjectName("StepBar")
        self.steps_bar.setTextVisible(False)
        self.steps_bar.setMaximum(100)
        layout.addWidget(self.steps_bar)
        self.steps_box = QVBoxLayout()
        self.steps_box.setSpacing(10)
        layout.addLayout(self.steps_box)
        layout.addSpacing(4)
        layout.addWidget(section_title("Recent Activity"))
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(7)
        layout.addLayout(self.recent_box)

        self.source_val = self._kv(layout, "Source")
        self.url_val = self._kv(layout, "Origin URL", copy=True)
        self.stage_val = self._kv(layout, "Stage")
        self.path_val = self._kv(layout, "Output path", copy=True)
        self.created_val = self._kv(layout, "Added")

        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.copy_prompt_btn = icon_button(
            C.INSPECTOR_COPY_PROMPT,
            icon=QStyle.StandardPixmap.SP_DialogSaveButton,
            theme_name="edit-copy",
        )
        self.copy_prompt_btn.clicked.connect(self._copy_prompt)
        self.import_json_btn = icon_button(
            C.INSPECTOR_IMPORT_JSON,
            icon=QStyle.StandardPixmap.SP_ArrowDown,
            theme_name="document-import",
        )
        self.import_json_btn.clicked.connect(self._import_json)
        self.pull_claude_btn = icon_button(
            C.INSPECTOR_PULL_CLAUDE,
            icon=QStyle.StandardPixmap.SP_BrowserReload,
            theme_name="view-refresh",
            variant="primary",
        )
        self.pull_claude_btn.clicked.connect(self._pull_claude)
        self.copy_claude_json_btn = icon_button(
            C.INSPECTOR_COPY_CLAUDE_JSON,
            icon=QStyle.StandardPixmap.SP_DialogSaveButton,
            theme_name="edit-copy",
        )
        self.copy_claude_json_btn.clicked.connect(lambda: self.copy_claude_json.emit())
        self.retry_export_btn = icon_button(
            C.INSPECTOR_RETRY_EXPORT,
            icon=QStyle.StandardPixmap.SP_DialogApplyButton,
            theme_name="document-save",
            variant="primary",
        )
        self.retry_export_btn.clicked.connect(lambda: self._retry("export"))
        self.retry_gen_btn = icon_button(
            C.INSPECTOR_RETRY_GEN,
            icon=QStyle.StandardPixmap.SP_BrowserReload,
            theme_name="view-refresh",
        )
        self.retry_gen_btn.clicked.connect(lambda: self._retry("generation"))
        self.skip_btn = icon_button(
            C.INSPECTOR_SKIP,
            icon=QStyle.StandardPixmap.SP_DialogCancelButton,
            theme_name="process-stop",
        )
        self.skip_btn.clicked.connect(self._skip)
        self.allow_dup_btn = ghost_button("Allow this company again && process")
        self.allow_dup_btn.setToolTip("Overrides the one-folder-per-company rule for this job only.")
        self.allow_dup_btn.clicked.connect(lambda: self._job and self.allow_duplicate.emit(self._job.id))
        actions.addWidget(self.copy_prompt_btn)
        actions.addWidget(self.pull_claude_btn)
        actions.addWidget(self.copy_claude_json_btn)
        actions.addWidget(self.import_json_btn)
        actions.addWidget(self.retry_export_btn)
        actions.addWidget(self.retry_gen_btn)
        actions.addWidget(self.skip_btn)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addWidget(self.allow_dup_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        self.pause_job_btn = ghost_button("Pause queue", "pause")
        self.pause_job_btn.setObjectName("PauseJob")
        self.pause_job_btn.clicked.connect(self.pause_queue.emit)
        self.pause_job_btn.hide()
        layout.addWidget(self.pause_job_btn)
        layout.addStretch(1)

    def _fill_steps(self, job: JobItem) -> None:
        c = theme.current()
        states = step_states(job)
        clear_layout(self.steps_box)
        for label, state in zip(STEPS, states):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(10)
            ic = QLabel()
            ic.setFixedSize(22, 22)
            if state == "done":
                ic.setPixmap(circle_icon("check", "#2FD072", 22, "#06280F"))
            elif state == "active":
                ic.setPixmap(ring_icon("#3B82F6", 22))
            elif state == "failed":
                ic.setPixmap(circle_icon("x", "#EF5350", 22))
            else:
                ic.setPixmap(ring_icon(c.border, 22))
            text = QLabel(label)
            if state == "pending":
                text.setObjectName("Muted")
            elif state == "active":
                text.setStyleSheet("font-weight: 650;")
            row.addWidget(ic)
            row.addWidget(text, 1)
            holder = QWidget()
            holder.setLayout(row)
            self.steps_box.addWidget(holder)
        self.steps_bar.setValue(progress_pct(job))
        working = job.status in {JobStatus.EXTRACTING, JobStatus.GENERATING, JobStatus.EXPORTING}
        self.activity_icon.setPixmap(ring_icon("#3B82F6", 20) if working else (
            circle_icon("check", "#2FD072", 20, "#06280F") if job.status == JobStatus.DONE else
            circle_icon("alert", "#EF5350", 20) if job.status == JobStatus.FAILED else ring_icon(c.border, 20)))
        self.activity_label.setText(current_activity(job))
        self.pause_job_btn.setVisible(working and not self.viewer_mode)
        clear_layout(self.recent_box)
        lines = [ln for ln in job.logs[-4:]][::-1]
        if not lines:
            self.recent_box.addWidget(muted_label("No activity recorded yet."))
        for ln in lines:
            m = re.match(r"\[(\d\d:\d\d)(?::\d\d)?\]\s*(.*)", ln)
            when, msg = (m.group(1), m.group(2)) if m else ("", ln)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(10)
            dot = QLabel("●")
            dot.setStyleSheet("color: #3B82F6; font-size: 9px;")
            tm = QLabel(when)
            tm.setObjectName("Muted")
            tm.setMinimumWidth(48)
            txt = QLabel(msg)
            txt.setWordWrap(True)
            row.addWidget(dot, alignment=Qt.AlignmentFlag.AlignTop)
            row.addWidget(tm, alignment=Qt.AlignmentFlag.AlignTop)
            row.addWidget(txt, 1)
            holder = QWidget()
            holder.setLayout(row)
            self.recent_box.addWidget(holder)

    def _build_jd(self) -> None:
        layout = QVBoxLayout(self.jd_tab)
        layout.setContentsMargins(4, 14, 4, 8)
        layout.setSpacing(10)
        self.jd_hint = muted_label(C.INSPECTOR_JD_HINT)
        layout.addWidget(self.jd_hint)
        self.jd_editor = QPlainTextEdit()
        self.jd_editor.setPlaceholderText(C.INSPECTOR_JD_PLACEHOLDER)
        self.jd_editor.setMinimumHeight(180)
        layout.addWidget(self.jd_editor, stretch=1)
        jd_actions = QHBoxLayout()
        jd_actions.addStretch(1)
        self.save_jd_btn = icon_button(
            C.INSPECTOR_SUBMIT_JD,
            icon=QStyle.StandardPixmap.SP_DialogOkButton,
            theme_name="document-save",
            variant="primary",
        )
        self.save_jd_btn.clicked.connect(self._save_jd)
        jd_actions.addWidget(self.save_jd_btn)
        layout.addLayout(jd_actions)

    def _build_files(self) -> None:
        layout = QVBoxLayout(self.files_tab)
        layout.setContentsMargins(4, 14, 4, 8)
        layout.setSpacing(10)
        self.files_host = QVBoxLayout()
        self.files_host.setSpacing(8)
        layout.addLayout(self.files_host)
        self.restore_btn = ghost_button("Restore missing files", "refresh")
        self.restore_btn.setToolTip("Rebuild the deleted documents from the resume JSON stored in the database")
        self.restore_btn.clicked.connect(lambda: self._job and self.sync_files.emit(self._job.id))
        self.restore_btn.hide()
        layout.addWidget(self.restore_btn)
        self.open_folder_btn = icon_button(
            C.INSPECTOR_OPEN_FOLDER,
            icon=QStyle.StandardPixmap.SP_DirOpenIcon,
            theme_name="folder-open",
        )
        self.open_folder_btn.clicked.connect(self._open_folder)
        layout.addWidget(self.open_folder_btn)
        layout.addStretch(1)

    def _build_logs(self) -> None:
        layout = QVBoxLayout(self.logs_tab)
        layout.setContentsMargins(4, 14, 4, 8)
        self.logs_view = QPlainTextEdit()
        self.logs_view.setReadOnly(True)
        layout.addWidget(self.logs_view)

    def _kv(self, layout: QVBoxLayout, label: str, copy: bool = False) -> QLabel:
        layout.addWidget(muted_label(label))
        value = QLabel("—")
        value.setWordWrap(True)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        value.setStyleSheet("font-weight: 550; padding-bottom: 4px;")
        if not copy:
            layout.addWidget(value)
            return value
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(value, 1)
        row.addWidget(copy_button(f"Copy {label.lower()}", lambda: "" if value.text() in ("", "—") else value.text()),
                      alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(row)
        return value

    def _rebuild_files(self, job: JobItem) -> None:
        clear_layout(self.files_host)
        self.restore_btn.setVisible(bool(job.status == JobStatus.DONE and job.resume_json
                                         and any(r.path and not r.exists for r in file_refs(job))))
        if not job.generated_files:
            self.files_host.addWidget(muted_label(C.INSPECTOR_NO_FILES))
            self.open_folder_btn.setEnabled(bool(job.output_path))
            return
        for path_str in job.generated_files:
            path = Path(path_str)
            row = QFrame()
            row.setObjectName("SurfaceCard")
            hl = QHBoxLayout(row)
            hl.setContentsMargins(12, 10, 12, 10)
            exists = path.exists()
            size = f"{path.stat().st_size / 1024:.1f} KB" if exists else "Missing on disk"
            label = QLabel(f"{path.name}\n{size}")
            label.setStyleSheet("font-weight: 550;" if exists else
                                f"font-weight: 550; color: {theme.current().danger};")
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            hl.addWidget(label, stretch=1)
            open_btn = icon_button(
                C.INSPECTOR_OPEN,
                icon=QStyle.StandardPixmap.SP_DialogOpenButton,
                theme_name="document-open",
            )
            open_btn.setEnabled(exists)
            open_btn.clicked.connect(lambda _=False, p=path_str: self._open_file(p))
            copy_btn = icon_button(
                C.INSPECTOR_COPY,
                icon=QStyle.StandardPixmap.SP_DialogSaveButton,
                theme_name="edit-copy",
            )
            copy_btn.clicked.connect(lambda _=False, p=path_str, b=copy_btn: (self._copy_path(p), flash_copied(b)))
            hl.addWidget(open_btn)
            hl.addWidget(copy_btn)
            self.files_host.addWidget(row)
        self.open_folder_btn.setEnabled(bool(job.output_path or job.generated_files))

    def _save_jd(self) -> None:
        if not self._job:
            return
        text = self.jd_editor.toPlainText().strip()
        if not text:
            show_warning(self, "Job description", C.INSPECTOR_JD_REQUIRED)
            return
        self.submit_jd.emit(self._job.id, text)

    def _retry(self, mode: str) -> None:
        if self._job:
            self.retry_job.emit(self._job.id, mode)

    def _skip(self) -> None:
        if self._job:
            self.skip_job.emit(self._job.id)

    def _open_folder(self) -> None:
        if not self._job:
            return
        path = self._job.output_path
        if not path and self._job.generated_files:
            path = str(Path(self._job.generated_files[0]).parent)
        if path:
            self.open_folder.emit(path)
            _reveal_path(path)

    def _open_file(self, path: str) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def flash_copy_claude_json(self) -> None:
        flash_copied(self.copy_claude_json_btn)

    def _copy_path(self, path: str) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(path)

    def _copy_prompt(self) -> None:
        if not self._job or not self._settings_store:
            return
        if not self._job.job_description.strip():
            show_warning(self, C.NAV_CLAUDE, C.INSPECTOR_JD_REQUIRED)
            return
        prompt = build_claude_chat_prompt_for_job(self._settings_store.settings, self._job)
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(prompt)
        flash_copied(self.copy_prompt_btn)
        host = self.window()
        if hasattr(host, "toast"):
            host.toast.show_message(C.INSPECTOR_PROMPT_COPIED, kind="success")

    def _import_json(self) -> None:
        if not self._job:
            return
        from PySide6.QtWidgets import QApplication

        text = QApplication.clipboard().text().strip()
        if not text:
            show_warning(self, C.INSPECTOR_IMPORT_JSON, "Clipboard is empty.")
            return
        self.import_json.emit(self._job.id, text)

    def _pull_claude(self) -> None:
        if self._job:
            self.pull_claude_json.emit(self._job.id)


def _reveal_path(path: str) -> None:
    folder = Path(path)
    if folder.is_file():
        folder = folder.parent
    if sys.platform == "win32":
        os.startfile(folder)  # type: ignore[attr-defined]
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


def _short_time(iso: str) -> str:
    if not iso:
        return "—"
    try:
        return iso.replace("T", " ")[:19]
    except Exception:  # noqa: BLE001
        return iso


def _banner_style(kind: str, light: bool) -> str:
    fg, bg = theme.badge_colors({"danger": "danger", "warn": "warning"}.get(kind, "info"))
    return f"QLabel {{ background: {bg}; color: {fg}; border-radius: 8px; padding: 10px 12px; font-size: 12.5px; }}"


def _building_pixmap(size: int):
    """Rounded accent tile with a building glyph (company mark)."""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor, QPainter, QPixmap

    c = theme.current()
    scale = 2
    px = QPixmap(size * scale, size * scale)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(c.accent_soft))
    p.drawRoundedRect(0, 0, size * scale, size * scale, 12 * scale, 12 * scale)
    glyph = icons.icon("building", c.accent, 24).pixmap(QSize(24, 24))
    off = (size * scale - glyph.width()) // 2
    p.drawPixmap(QRect(off, off, 24 * scale, 24 * scale), glyph)
    p.end()
    px.setDevicePixelRatio(scale)
    return px
