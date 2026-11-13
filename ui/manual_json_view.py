"""Manual JSON export view."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QButtonGroup,
    QCheckBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QRadioButton,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.profile_apply import apply_settings_profile
from core.settings_store import SettingsStore
from engine import (
    PdfExportError,
    ResumeError,
    application_output_dir,
    collect_warnings,
    cover_letter_stem,
    export_resume,
    load_resume,
    resume_stem,
)
from ui import copy as C
from ui import system, theme
from ui.dialogs import ask_yes_no, show_error, show_info, show_success, show_warning
from ui.json_editor import JsonEditor
from ui.widgets import card_header, copy_button, ghost_button, muted_label, page_title, primary_button, clear_layout, row_action


class ManualJsonView(QWidget):
    def __init__(
        self,
        output_root: Path | None = None,
        settings_store: SettingsStore | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ContentPane")
        self._output_root = output_root
        self._settings_store = settings_store
        self._folder_locked = False
        self._last_output: Path | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 0, 16, 10)
        root.setSpacing(10)

        head = QHBoxLayout()
        header = QVBoxLayout()
        header.setSpacing(2)
        header.addWidget(page_title(C.JSON_TITLE))
        header.addWidget(muted_label(C.JSON_SUBTITLE))
        head.addLayout(header, 1)
        self.syntax = QLabel("")
        self.syntax.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.syntax.hide()
        head.addWidget(self.syntax, alignment=Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)

        split = QHBoxLayout()
        split.setSpacing(14)

        left_card = QFrame()
        left_card.setObjectName("SurfaceCard")
        left = QVBoxLayout(left_card)
        left.setContentsMargins(14, 8, 14, 10)
        left.setSpacing(6)
        self.tabs = QTabWidget()
        left.addWidget(self.tabs, stretch=1)
        self.editor = JsonEditor()
        self.editor.setPlaceholderText(
            '{\n  "name": "...",\n  "company_name": "...",\n  "role_title": "...",\n'
            '  "experience": [...],\n  "cover_letter": "..."\n}'
        )
        editor_box = QFrame()
        editor_box.setObjectName("EditorBox")
        eb = QVBoxLayout(editor_box)
        eb.setContentsMargins(1, 1, 1, 1)
        eb.addWidget(self.editor)
        self.tabs.addTab(editor_box, "JSON Input")
        self.validation = QPlainTextEdit()
        self.validation.setReadOnly(True)
        self.validation.setFont(self.editor.font())
        self.validation.setPlainText("Paste or open JSON, then press Validate.")
        self.tabs.addTab(self.validation, "Validation")

        tools = QWidget()
        tl = QHBoxLayout(tools)
        tl.setContentsMargins(0, 0, 0, 4)
        tl.setSpacing(6)
        open_btn = ghost_button(C.JSON_OPEN, "doc")
        open_btn.clicked.connect(self._open_file)
        self.format_btn = ghost_button("Format", "edit")
        self.format_btn.setToolTip("Pretty-print the JSON with 2-space indentation")
        self.format_btn.clicked.connect(self._format)
        check_btn = ghost_button(C.JSON_CHECK, "check")
        check_btn.clicked.connect(self._check)
        for b_ in (open_btn, self.format_btn, check_btn):
            tl.addWidget(b_)
        self.tabs.setCornerWidget(tools, Qt.Corner.TopRightCorner)

        self.cursor_info = muted_label("")
        self.cursor_info.setWordWrap(False)
        left.addWidget(self.cursor_info)
        self.editor.cursorPositionChanged.connect(self._cursor_info)
        self.editor.textChanged.connect(self._cursor_info)
        self._lint = QTimer(self)
        self._lint.setSingleShot(True)
        self._lint.setInterval(400)
        self._lint.timeout.connect(self._live_syntax)
        self.editor.textChanged.connect(self._lint.start)
        split.addWidget(left_card, stretch=3)

        right_card = QFrame()
        right_card.setObjectName("SurfaceCard")
        right_card.setMinimumWidth(340)
        right = QVBoxLayout(right_card)
        right.setContentsMargins(18, 16, 18, 16)
        right.setSpacing(8)
        right.addLayout(card_header(C.JSON_EXPORT_TITLE, "Choose what to produce, then generate.", "settings"))
        right.addSpacing(4)

        right.addWidget(self._field_label("Output format"))
        self.fmt_both = QRadioButton("DOCX + PDF (resume)")
        self.fmt_docx = QRadioButton("DOCX only")
        self.fmt_both.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.fmt_both)
        group.addButton(self.fmt_docx)
        right.addWidget(self.fmt_both)
        right.addWidget(self.fmt_docx)

        right.addSpacing(4)
        right.addWidget(self._field_label("Cover letter"))
        self.cover_check = QCheckBox("Generate cover letter")
        self.cover_check.setChecked(True)
        right.addWidget(self.cover_check)

        right.addSpacing(4)
        right.addWidget(self._field_label("Output folder"))
        folder_row = QHBoxLayout()
        folder_row.setSpacing(8)
        self.folder = QLineEdit()
        if output_root:
            self.folder.setText(str(output_root))
        folder_row.addWidget(self.folder, stretch=1)
        browse = ghost_button(C.SETTINGS_BROWSE, "folder-open")
        browse.clicked.connect(self._browse)
        folder_row.addWidget(browse)
        right.addLayout(folder_row)

        right.addWidget(self._field_label("File name"))
        self.name = QLineEdit()
        self.name.setPlaceholderText(C.JSON_STEM_PLACEHOLDER)
        right.addWidget(self.name)

        self.status = muted_label(C.JSON_STATUS_IDLE)
        right.addWidget(self.status)

        right.addSpacing(2)
        right.addWidget(self._field_label("Output"))
        self.results = QVBoxLayout()
        self.results.setSpacing(2)
        self.results.addWidget(muted_label("Exported files appear here."))
        right.addLayout(self.results)
        right.addStretch(1)

        self.open_folder_btn = ghost_button(C.JSON_OPEN_FOLDER, "folder-open")
        self.open_folder_btn.setEnabled(False)
        self.open_folder_btn.clicked.connect(self._open_folder)
        right.addWidget(self.open_folder_btn)
        self.generate_btn = primary_button("Generate Documents", "play")
        self.generate_btn.setMinimumHeight(38)
        self.generate_btn.clicked.connect(self._generate)
        right.addWidget(self.generate_btn)
        split.addWidget(right_card, stretch=2)

        root.addLayout(split, stretch=1)
        self._cursor_info()

    def refresh_theme(self) -> None:
        self.editor.highlighter.rehighlight()
        self._live_syntax()

    def _generate(self) -> None:
        self._export(self.fmt_both.isChecked())

    def _cursor_info(self) -> None:
        cur = self.editor.textCursor()
        size = len(self.editor.toPlainText().encode("utf-8"))
        if not size:
            self.cursor_info.setText("Empty")
            return
        self.cursor_info.setText(f"Ln {cur.blockNumber() + 1}, Col {cur.columnNumber() + 1}  ·  "
                                 f"{self.editor.blockCount()} lines  ·  {size / 1024:.1f} KB")

    def _format(self) -> None:
        text = self.editor.toPlainText().strip()
        if not text:
            return
        try:
            data = json.loads(text.lstrip("\ufeff"))
        except json.JSONDecodeError as exc:
            self._set_syntax(f"Line {exc.lineno}, column {exc.colno}: {exc.msg}", "danger")
            return
        self.editor.setPlainText(json.dumps(data, indent=2, ensure_ascii=False))
        self._live_syntax()

    def _set_syntax(self, text: str, kind: str) -> None:
        self.syntax.setText(text)
        self.syntax.setVisible(bool(text))
        if text:
            self.syntax.setStyleSheet(theme.badge_style(kind))

    def _live_syntax(self) -> None:
        text = self.editor.toPlainText().strip()
        if not text:
            self._set_syntax("", "neutral")
            return
        try:
            json.loads(text.lstrip("\ufeff"))
        except json.JSONDecodeError as exc:
            self._set_syntax(f"Line {exc.lineno}, column {exc.colno}: {exc.msg}", "danger")
        else:
            self._set_syntax("Valid JSON syntax", "success")

    def _show_results(self, paths: list[Path]) -> None:
        clear_layout(self.results)
        for p in paths:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(QLabel(p.name), 1)
            op = row_action("external", f"Open {p.name}", "text")
            op.clicked.connect(lambda _=False, f=p: system.open_path(f))
            cp = copy_button("Copy path", lambda f=p: str(f))
            h.addWidget(op)
            h.addWidget(cp)
            self.results.addWidget(row)

    def _field_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("FieldLabel")
        return label

    def set_output_root(self, root: Path) -> None:
        self._output_root = root
        if not self._folder_locked:
            self.folder.setText(str(root))

    def _apply_profile_defaults(self) -> None:
        store = self._settings_store or SettingsStore()
        apply_settings_profile(store.settings)

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Output folder", self.folder.text())
        if chosen:
            self.folder.setText(chosen)
            self._folder_locked = True

    def _open_file(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Open JSON", "", "JSON files (*.json);;All files (*)"
        )
        if not chosen:
            return
        try:
            text = Path(chosen).read_text(encoding="utf-8-sig")
        except OSError as exc:
            show_error(self, "Could not open file", str(exc))
            return
        self.editor.setPlainText(text)
        try:
            resume = load_resume(text)
            self._apply_paths(resume)
        except ResumeError:
            pass

    def _apply_paths(self, resume: dict) -> None:
        if not self._folder_locked:
            from engine import safe_stem

            if self._output_root:
                name = safe_stem(str(resume.get("name") or "Candidate"))
                company = safe_stem(str(resume.get("company_name") or "Company"))
                role = safe_stem(
                    str(resume.get("role_title") or resume.get("subtitle") or "Role")
                )
                self.folder.setText(str(self._output_root / name / company / role))
            else:
                self.folder.setText(str(application_output_dir(resume)))
        if not self.name.text().strip():
            self.name.setText(resume_stem(resume))

    def _check(self) -> None:
        self._apply_profile_defaults()
        try:
            resume = load_resume(self.editor.toPlainText())
        except ResumeError as exc:
            self.status.setText(str(exc))
            self.validation.setPlainText(f"✗ Invalid\n\n{exc}")
            self.tabs.setCurrentWidget(self.validation)
            show_error(self, "Check JSON", str(exc))
            return
        self._apply_paths(resume)
        warnings = collect_warnings(resume)
        jobs = resume.get("experience")
        job_count = len(jobs) if isinstance(jobs, list) else 0
        self.status.setText(f"{resume.get('name')}  ·  {job_count} roles  ·  ready to export")
        self.validation.setPlainText("✓ Valid resume JSON\n\n" + (
            "\n".join(f"• {w}" for w in warnings) if warnings else "No warnings."))
        detail = f"Output folder:\n{self.folder.text()}\n\nReady to export."
        if warnings:
            detail += "\n\n" + "\n".join(f"• {w}" for w in warnings)
        show_info(self, C.JSON_CHECK, C.JSON_VALID, detail=detail)

    def _export(self, make_pdf: bool) -> None:
        self._apply_profile_defaults()
        try:
            resume = load_resume(self.editor.toPlainText())
        except ResumeError as exc:
            self.status.setText(str(exc))
            show_error(self, "Export", str(exc))
            return
        self._apply_paths(resume)
        folder = Path(self.folder.text().strip() or application_output_dir(resume))
        stem = resume_stem(resume)
        self.name.setText(stem)
        cover_stem = cover_letter_stem(resume)
        docx_path = folder / f"{stem}.docx"
        pdf_path = folder / f"{stem}.pdf" if make_pdf else None
        cover_path = folder / f"{cover_stem}.docx" if self.cover_check.isChecked() else None
        existing = [p for p in (docx_path, pdf_path, cover_path) if p is not None and p.exists()]
        if existing:
            names = "\n".join(p.name for p in existing)
            if not ask_yes_no(
                self,
                C.JSON_REPLACE_TITLE,
                C.JSON_REPLACE_BODY.format(names=names),
            ):
                return
        try:
            result = export_resume(resume, docx_path, pdf_path, cover_path)
        except PdfExportError as exc:
            self._last_output = docx_path.parent
            self.open_folder_btn.setEnabled(True)
            self.status.setText(C.JSON_PDF_FAIL)
            show_warning(self, "PDF export failed", str(exc), detail=str(docx_path))
            return
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
            show_error(self, "Export failed", str(exc))
            return

        self._last_output = result.docx_path.parent
        self.open_folder_btn.setEnabled(True)
        lines = [str(result.docx_path)]
        if result.pdf_path:
            lines.append(str(result.pdf_path))
        if result.cover_letter_path:
            lines.append(str(result.cover_letter_path))
        self.status.setText(C.JSON_EXPORTED)
        self._show_results([Path(x) for x in lines])
        if result.warnings:
            show_warning(
                self,
                "Export completed with warnings",
                "Documents were saved with additional notes.",
                detail="\n".join(lines + [""] + result.warnings),
            )
        else:
            show_success(self, "Export complete", C.JSON_EXPORTED, detail="\n".join(lines))

    def _open_folder(self) -> None:
        system.open_folder(self._last_output or Path(self.folder.text()))
