"""Settings: Profile, Claude, Output, Processing, Appearance."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core import secrets
from core.browser_intake import get_token, regenerate_token
from core.claude_client import ClaudeClient
from core.settings_store import (
    MODE_CONTROLLER,
    MODE_STANDALONE,
    MODE_VIEWER,
    SettingsStore,
    normalize_controller_url,
    normalize_proxy_url,
    yes_no,
)
from core.viewer_client import ControllerClient
from ui import copy as C
from ui.dialogs import show_error, show_success
from ui.lock_dialogs import NewPasswordDialog, PasswordPrompt, confirm_password
from ui.widgets import (
    card_header,
    copy_button,
    field_label,
    ghost_button,
    icon_tool,
    muted_label,
    page_title,
    primary_button,
    segment_button,
    set_icon,
)

MASK = "••••••••••"


class SettingsView(QWidget):
    saved = Signal()

    def __init__(self, store: SettingsStore, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ContentPane")
        self.store = store
        self._key_dirty = False
        self._real: dict[QWidget, str] = {}  # true values of the fields that are masked on screen
        self._shown = {"profile": True, "prompt": True}  # False while hidden behind the app password
        self._eyes: dict[str, object] = {}
        self._groups: dict[str, list[QWidget]] = {}

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        shell.addWidget(scroll, 1)
        page = QWidget()
        page.setObjectName("Workspace")
        scroll.setWidget(page)

        root = QVBoxLayout(page)
        root.setContentsMargins(24, 0, 6, 18)  # + scrollbar = same 16px right gutter as the other pages
        root.setSpacing(14)
        header = QVBoxLayout()
        header.setSpacing(2)
        header.addWidget(page_title(C.SETTINGS_TITLE))
        header.addWidget(muted_label("Profile, Claude, output, processing and appearance."))
        root.addLayout(header)

        self._cards: dict[str, QFrame] = {}
        root.addWidget(self._build_mode_card())
        root.addWidget(self._build_connection_card())
        root.addWidget(self._build_controller_card())
        top = QHBoxLayout()
        top.setSpacing(14)
        self._cards["profile"] = self._build_profile_card()
        self._cards["claude"] = self._build_claude_card()
        top.addWidget(self._cards["profile"], 1)
        top.addWidget(self._cards["claude"], 1)
        root.addLayout(top)
        bottom = QHBoxLayout()
        bottom.setSpacing(14)
        bottom.addWidget(self._build_output_card(), 1)
        bottom.addWidget(self._build_processing_card(), 1)
        bottom.addWidget(self._build_appearance_card(), 1)
        root.addLayout(bottom)
        self._cards["autoapply"] = self._build_autoapply_card()
        root.addWidget(self._cards["autoapply"])
        self._cards["extension"] = self._build_extension_card()
        root.addWidget(self._cards["extension"])
        self._cards["security"] = self._build_security_card()
        root.addWidget(self._cards["security"])
        root.addStretch(1)

        footer = QFrame()
        footer.setObjectName("SettingsFooter")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(24, 10, 16, 10)
        self.footer_hint = muted_label(C.SETTINGS_SAVE_HINT)
        fl.addWidget(self.footer_hint, 1)
        self.validate_btn = ghost_button(C.SETTINGS_VALIDATE)
        self.validate_btn.clicked.connect(self._validate)
        fl.addWidget(self.validate_btn)
        save = primary_button(C.SETTINGS_SAVE)
        save.setMinimumWidth(110)
        save.clicked.connect(self.save)
        fl.addWidget(save)
        shell.addWidget(footer)
        self.load_from_store()

    # ------------------------------------------------------------------ builders

    def _build_mode_card(self) -> QFrame:
        card = self._card("How this app runs", "Standalone, controller (on the VPS) or viewer (on your computer).",
                          "settings", "info")
        lay = card.layout()
        self.app_mode = QComboBox()
        self.app_mode.addItem("Standalone", MODE_STANDALONE)
        self.app_mode.addItem("Controller", MODE_CONTROLLER)
        self.app_mode.addItem("Viewer", MODE_VIEWER)
        lay.addWidget(self._tiles(self.app_mode))
        lay.addWidget(muted_label(
            "Standalone: everything on this computer. Controller: generates resumes and keeps the one real "
            "database; run it on the VPS. Viewer: shows the controller's data and lets you manage it, but never "
            "uses Claude. Changing this takes effect after you restart the app."))
        return card

    def _build_connection_card(self) -> QFrame:
        card = self._card("Controller connection", "Where the controller runs and how to reach it.", "info", "info")
        self._cards["connection"] = card
        lay = card.layout()
        self.controller_url = QLineEdit()
        self.controller_url.setPlaceholderText("e.g. http://100.64.0.5:47821 (the VPS address on your VPN)")
        lay.addLayout(self._row("Address", self.controller_url))
        self.controller_token = QLineEdit()
        self.controller_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.controller_token.setPlaceholderText("Token from the controller's Settings -> Browser extension")
        lay.addLayout(self._row("Token", self.controller_token))
        self.auto_sync = QCheckBox("Save each newly finished resume to this computer automatically")
        lay.addWidget(self.auto_sync)
        self.link_status = muted_label("")
        lay.addLayout(self._row("Status", self.link_status))
        return card

    def _build_controller_card(self) -> QFrame:
        card = self._card("Serve a viewer", "Lets the viewer on your computer read this app's data.", "info", "warning")
        self._cards["controller"] = card
        lay = card.layout()
        self.intake_bind = QLineEdit()
        self.intake_bind.setPlaceholderText("127.0.0.1 = this computer only; 0.0.0.0 = every network interface")
        lay.addLayout(self._row("Listen on", self.intake_bind))
        self.allowed_hosts = QLineEdit()
        self.allowed_hosts.setPlaceholderText("host:port the viewer uses, e.g. 100.64.0.5:47821 (comma separated)")
        lay.addLayout(self._row("Allowed names", self.allowed_hosts))
        lay.addWidget(muted_label(
            "The link itself is not encrypted. Reach the controller only over a VPN such as Tailscale or "
            "WireGuard, or behind an HTTPS proxy, and keep the firewall closed to everything else. Anyone with "
            "the token and network access can read and change your data. Restart the app after changing these."))
        return card

    def _card(self, title: str, subtitle: str, icon: str = "", kind: str = "info") -> QFrame:
        card = QFrame()
        card.setObjectName("SurfaceCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        layout.addLayout(card_header(title, subtitle, icon, kind))
        sep = QFrame()
        sep.setObjectName("Separator")
        layout.addWidget(sep)
        return card

    @staticmethod
    def _stack(label: str, widget: QWidget) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(4)
        box.addWidget(field_label(label))
        box.addWidget(widget)
        return box

    @staticmethod
    def _row(label: str, widget: QWidget | QLayout) -> QHBoxLayout:
        """Label on the left, control on the right: compact form row."""
        row = QHBoxLayout()
        row.setSpacing(12)
        lab = field_label(label)
        lab.setFixedWidth(108)
        row.addWidget(lab)
        if isinstance(widget, QLayout):
            row.addLayout(widget, 1)
        else:
            row.addWidget(widget, 1)
        return row

    def _tiles(self, combo: QComboBox) -> QWidget:
        """Segmented tiles that mirror a (hidden) combo box, which stays the source of truth."""
        combo.setParent(self)
        combo.hide()
        host = QWidget()
        lay = QHBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        group = QButtonGroup(host)
        group.setExclusive(True)
        buttons = []
        for i in range(combo.count()):
            b = segment_button(combo.itemText(i))
            b.setMinimumHeight(34)
            group.addButton(b, i)
            lay.addWidget(b, 1)
            buttons.append(b)
        group.idClicked.connect(combo.setCurrentIndex)

        def sync(index: int) -> None:
            if 0 <= index < len(buttons):
                buttons[index].setChecked(True)

        combo.currentIndexChanged.connect(sync)
        sync(combo.currentIndex())
        return host

    def _build_profile_card(self) -> QFrame:
        card = self._card(C.SETTINGS_PROFILE_TITLE, C.SETTINGS_PROFILE_SUB, "user", "info")
        self.name, self.email, self.phone, self.location = (QLineEdit() for _ in range(4))
        self.edu_school, self.edu_location, self.edu_degree, self.edu_dates = (QLineEdit() for _ in range(4))
        self.edu_dates.setPlaceholderText(C.SETTINGS_EDU_DATES_PH)
        lay = card.layout()
        for label, w in ((C.SETTINGS_FULL_NAME, self.name), (C.SETTINGS_EMAIL, self.email),
                         (C.SETTINGS_PHONE, self.phone), (C.SETTINGS_LOCATION, self.location)):
            lay.addLayout(self._row(label, w))
        edu = QLabel("Education")
        edu.setObjectName("SectionTitle")
        lay.addSpacing(6)
        lay.addWidget(edu)
        for label, w in ((C.SETTINGS_EDU_SCHOOL, self.edu_school), (C.SETTINGS_EDU_LOCATION, self.edu_location),
                         (C.SETTINGS_EDU_DEGREE, self.edu_degree), (C.SETTINGS_EDU_DATES, self.edu_dates)):
            lay.addLayout(self._row(label, w))
        history_title = QLabel("Work history")
        history_title.setObjectName("SectionTitle")
        lay.addSpacing(6)
        lay.addWidget(history_title)
        self.work_history = QPlainTextEdit()
        self.work_history.setMinimumHeight(150)
        self.work_history.setPlaceholderText(
            "Your real jobs, newest first, one block each. Resumes are written from exactly this, and any resume that "
            "names another employer is rejected. Auto-apply stays off until this is filled in.\n\n"
            "Senior Engineer | Acme Corp | Austin, TX | Jan 2021 - Present\n- what you did there")
        lay.addWidget(self.work_history)
        self.linkedin, self.github, self.website, self.work_auth, self.sponsorship = (QLineEdit() for _ in range(5))
        self.work_auth.setPlaceholderText(C.SETTINGS_WORK_AUTH_PH)
        self.sponsorship.setPlaceholderText(C.SETTINGS_SPONSORSHIP_PH)
        apply_title = QLabel(C.SETTINGS_APPLY_HEADING)
        apply_title.setObjectName("SectionTitle")
        lay.addSpacing(6)
        lay.addWidget(apply_title)
        for label, w in ((C.SETTINGS_LINKEDIN, self.linkedin), (C.SETTINGS_GITHUB, self.github),
                         (C.SETTINGS_WEBSITE, self.website), (C.SETTINGS_WORK_AUTH, self.work_auth),
                         (C.SETTINGS_SPONSORSHIP, self.sponsorship)):
            lay.addLayout(self._row(label, w))
        lay.addStretch(1)
        self._groups["profile"] = [self.name, self.email, self.phone, self.location,
                                   self.edu_school, self.edu_location, self.edu_degree, self.edu_dates,
                                   self.work_history, self.linkedin, self.github, self.website, self.work_auth, self.sponsorship]
        lay.itemAt(0).layout().addWidget(self._eye("profile", "personal information"))
        return card

    def _eye(self, group: str, what: str):
        btn = icon_tool(f"Show or hide {what}", "eye", size=30)
        btn.clicked.connect(lambda _=False, g=group, w=what: self._toggle_group(g, w))
        self._eyes[group] = btn
        return btn

    def _build_claude_card(self) -> QFrame:
        card = self._card(C.SETTINGS_CLAUDE_TITLE, C.SETTINGS_CLAUDE_SUB, "claude", "violet")
        lay = card.layout()
        self.generation_mode = QComboBox()
        self.generation_mode.addItem(C.SETTINGS_MODE_CHAT, "claude_chat")
        self.generation_mode.addItem(C.SETTINGS_MODE_API, "claude_api")
        lay.addLayout(self._row("Provider mode", self.generation_mode))
        self.chat_provider = QComboBox()
        self.chat_provider.addItem("Claude (claude.ai)", "claude")
        self.chat_provider.addItem("ChatGPT (chatgpt.com)", "chatgpt")
        self.chat_provider.setToolTip("Which website writes the resumes. Restart the app after changing it.")
        lay.addLayout(self._row("Web chat site", self.chat_provider))
        self.web_proxy = QLineEdit()
        self.web_proxy.setPlaceholderText("Optional, e.g. http://1.2.3.4:8080 or socks5://1.2.3.4:1080")
        lay.addLayout(self._row("Proxy", self.web_proxy))
        self.proxy_user = QLineEdit()
        self.proxy_user.setPlaceholderText("Proxy user (leave empty if none)")
        self.proxy_password = QLineEdit()
        self.proxy_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.proxy_password.setPlaceholderText("Proxy password")
        creds = QHBoxLayout()
        creds.setSpacing(8)
        creds.addWidget(self.proxy_user, 1)
        creds.addWidget(self.proxy_password, 1)
        lay.addLayout(self._row("Proxy login", creds))
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText(C.SETTINGS_API_KEY_PH)
        self.api_key.textChanged.connect(lambda _t: setattr(self, "_key_dirty", True))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.api_key, 1)
        self.show_key = ghost_button(C.SETTINGS_SHOW)
        self.show_key.setCheckable(True)
        self.show_key.toggled.connect(self._toggle_key)
        row.addWidget(self.show_key)
        lay.addLayout(self._row("API key", row))
        self.key_hint = muted_label("")
        hint_row = QHBoxLayout()
        hint_row.addSpacing(120)
        hint_row.addWidget(self.key_hint, 1)
        lay.addLayout(hint_row)
        self.model = QLineEdit()
        self.model.setPlaceholderText("claude-sonnet-4-20250514")
        lay.addLayout(self._row(C.SETTINGS_MODEL, self.model))
        self.prompt = QPlainTextEdit()
        self.prompt.setMinimumHeight(140)
        self.prompt.setPlaceholderText(C.SETTINGS_PROMPT_PH)
        self._groups["prompt"] = [self.prompt]
        box = QVBoxLayout()
        box.setSpacing(4)
        label_row = QHBoxLayout()
        label_row.addWidget(field_label(C.SETTINGS_PROMPT), 1)
        label_row.addWidget(self._eye("prompt", "the resume prompt"))
        box.addLayout(label_row)
        box.addWidget(self.prompt)
        lay.addLayout(box, 1)
        return card

    def _build_output_card(self) -> QFrame:
        card = self._card(C.SETTINGS_DOCS_TITLE, "Where documents are written and which files are produced.",
                          "folder-open", "success")
        lay = card.layout()
        self.output_root = QLineEdit()
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.output_root, 1)
        browse = ghost_button(C.SETTINGS_BROWSE, "folder-open")
        browse.clicked.connect(self._browse_output)
        row.addWidget(browse)
        lay.addLayout(self._stack_layout(C.SETTINGS_OUTPUT_ROOT, row))
        self.export_pdf = QCheckBox("Export resume PDF")
        self.export_cover = QCheckBox("Export cover letter (DOCX)")
        lay.addWidget(self.export_pdf)
        lay.addWidget(self.export_cover)
        lay.addWidget(field_label("File naming (fixed, so existing files stay valid)"))
        self.naming_hint = QLabel("")
        self.naming_hint.setObjectName("CodeNote")
        self.naming_hint.setWordWrap(True)
        self.naming_hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.naming_hint)
        lay.addStretch(1)
        return card

    def _build_autoapply_card(self) -> QFrame:
        card = self._card("Auto apply",
                          "Finds new Greenhouse jobs on Jobright and applies to them. Switch the applying mode on the "
                          "Auto Apply page.", "send", "success")
        lay = card.layout()
        self.disc_enabled = QCheckBox("Look for new jobs on Jobright")
        lay.addWidget(self.disc_enabled)
        self.disc_interval = QSpinBox()
        self.disc_interval.setRange(2, 240)
        self.disc_interval.setSuffix(" min")
        lay.addLayout(self._row("Check every", self.disc_interval))
        self.disc_cap = QSpinBox()
        self.disc_cap.setRange(1, 200)
        self.disc_cap.setSuffix(" jobs")
        self.disc_cap.setToolTip(
            "New jobs queued per hour, freshest first. Jobs that did not fit, and jobs still waiting for a resume when "
            "the next batch arrives, are dropped so the newest postings are always applied to first.")
        lay.addLayout(self._row("Queue at most, per hour", self.disc_cap))
        self.skip_below_floor = QCheckBox("Skip jobs whose posted pay tops out below my minimum salary")
        lay.addWidget(self.skip_below_floor)
        self.disc_max_age = QSpinBox()
        self.disc_max_age.setRange(1, 168)
        self.disc_max_age.setSuffix(" hours")
        lay.addLayout(self._row("Only postings from the last", self.disc_max_age))
        self.disc_url = QLineEdit()
        self.disc_url.setPlaceholderText("Blank = Jobright's recommended list (your filters are saved in your account)")
        lay.addLayout(self._row("Jobright list address", self.disc_url))
        self.disc_blocked = QLineEdit()
        self.disc_blocked.setPlaceholderText("Companies never to apply to, separated by commas")
        lay.addLayout(self._row("Blocked companies", self.disc_blocked))
        self.disc_exclude = QLineEdit()
        self.disc_exclude.setPlaceholderText("Title words that reject a job, e.g. intern, manager")
        lay.addLayout(self._row("Reject titles with", self.disc_exclude))
        self.bot_tz = QLineEdit()
        self.bot_tz.setPlaceholderText("America/New_York")
        lay.addLayout(self._row("Time zone", self.bot_tz))
        hours = QHBoxLayout()
        hours.setSpacing(8)
        self.hour_from, self.hour_to = QSpinBox(), QSpinBox()
        self.hour_from.setRange(0, 23)
        self.hour_to.setRange(1, 24)
        for w in (self.hour_from, self.hour_to):
            w.setSuffix(":00")
        hours.addWidget(self.hour_from)
        hours.addWidget(QLabel("to"))
        hours.addWidget(self.hour_to)
        hours.addStretch(1)
        lay.addLayout(self._row("Active hours", hours))
        self.apply_cap = QSpinBox()
        self.apply_cap.setRange(1, 500)
        self.apply_cap.setSuffix(" a day")
        lay.addLayout(self._row("Send at most", self.apply_cap))
        self.salary_floor = QSpinBox()
        self.salary_floor.setRange(0, 5_000_000)
        self.salary_floor.setSingleStep(5000)
        self.salary_floor.setPrefix("$ ")
        self.salary_floor.setSuffix(" a year")
        self.salary_floor.setToolTip("Salary answers never go below this. A job whose posted pay tops out below it is not queued.")
        lay.addLayout(self._row("Minimum salary I accept", self.salary_floor))
        self.salary_band_low = QSpinBox()
        self.salary_band_low.setRange(0, 100)
        self.salary_band_low.setSuffix(" %")
        self.salary_band_high = QSpinBox()
        self.salary_band_high.setRange(0, 100)
        self.salary_band_high.setSuffix(" %")
        band = QHBoxLayout()
        band.setSpacing(8)
        band.addWidget(self.salary_band_low)
        band.addWidget(QLabel("to"))
        band.addWidget(self.salary_band_high)
        band.addWidget(QLabel("of the posted pay range"))
        band.addStretch(1)
        lay.addLayout(self._row("Salary to ask for", band))
        lay.addWidget(muted_label("The chat picks one figure per job, in the lower-middle of the range the posting shows, "
                                  "never above its maximum and never below your minimum."))
        self.skip_optional = QCheckBox("Leave questions blank when the form does not require them")
        self.skip_optional.setToolTip("Required questions are always answered. Resume and cover letter are still attached.")
        lay.addWidget(self.skip_optional)
        self.bot_browser = QComboBox()
        self.bot_browser.addItem("Google Chrome", "chrome")
        self.bot_browser.addItem("Microsoft Edge", "msedge")
        lay.addLayout(self._row("Bot browser", self.bot_browser))
        lay.addWidget(muted_label("The bot uses this browser with its own saved sign-ins and the proxy above. "
                                  "Sign in to Jobright and Outlook in it once (Auto Apply > Open bot browser)."))
        self.discord_hook = QLineEdit()
        self.discord_hook.setEchoMode(QLineEdit.EchoMode.Password)
        self.discord_hook.setPlaceholderText("Discord webhook address (optional)")
        lay.addLayout(self._row("Alerts: Discord", self.discord_hook))
        tg = QHBoxLayout()
        tg.setSpacing(8)
        self.tg_token = QLineEdit()
        self.tg_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.tg_token.setPlaceholderText("Telegram bot token (optional)")
        self.tg_chat = QLineEdit()
        self.tg_chat.setPlaceholderText("Chat id")
        tg.addWidget(self.tg_token, 2)
        tg.addWidget(self.tg_chat, 1)
        lay.addLayout(self._row("Alerts: Telegram", tg))
        self.notify_reports = QCheckBox("Also send status reports: job search, each resume, each application and its answers")
        lay.addWidget(self.notify_reports)
        return card

    def _build_extension_card(self) -> QFrame:
        card = self._card("Browser extension",
                          "Send jobs from Chrome or Edge, with the description read straight from the page.",
                          "send", "info")
        lay = card.layout()
        self.intake_enabled = QCheckBox("Accept jobs from the browser extension")
        self.intake_auto = QCheckBox("Start the queue automatically when a job arrives (unless paused)")
        lay.addWidget(self.intake_enabled)
        lay.addWidget(self.intake_auto)
        self.intake_status = muted_label("")
        lay.addLayout(self._row("Status", self.intake_status))
        self.intake_token = QLineEdit()
        self.intake_token.setReadOnly(True)
        self.intake_token.setEchoMode(QLineEdit.EchoMode.Password)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.intake_token, 1)
        self.intake_show = ghost_button(C.SETTINGS_SHOW)
        self.intake_show.setCheckable(True)
        self.intake_show.toggled.connect(self._toggle_token)
        row.addWidget(self.intake_show)
        row.addWidget(copy_button("Copy token", self._token_to_copy))
        regen = ghost_button("New token", "refresh")
        regen.setToolTip("Replace the token. The extension must be given the new one.")
        regen.clicked.connect(self._regenerate_token)
        row.addWidget(regen)
        lay.addLayout(self._row("Token", row))
        lay.addWidget(muted_label("Paste this token into the extension once. Only programs on this computer "
                                  "that know it can add jobs."))
        return card

    def _build_security_card(self) -> QFrame:
        card = self._card("App password", "Protects the resume prompt, personal information, API key and token.",
                          "lock", "warning")
        lay = card.layout()
        self.lock_status = muted_label("")
        lay.addWidget(self.lock_status)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.lock_set = primary_button("Set password")
        self.lock_change = ghost_button("Change password")
        self.lock_remove = ghost_button("Remove password")
        self.lock_set.clicked.connect(self._set_password)
        self.lock_change.clicked.connect(self._change_password)
        self.lock_remove.clicked.connect(self._remove_password)
        for b in (self.lock_set, self.lock_change, self.lock_remove):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(muted_label("There is no way to recover a forgotten password. The prompt and personal "
                                  "information are stored encrypted, so a lost password means entering them again."))
        return card

    def _set_password(self) -> None:
        dialog = NewPasswordDialog(self)
        if dialog.exec():
            self.store.enable_protection(dialog.values()[1])
            self.load_from_store()
            show_success(self, "App password", "The password is set. The prompt and personal information are now "
                                               "encrypted and hidden until you press the eye button.")

    def _change_password(self) -> None:
        dialog = NewPasswordDialog(self, change=True)
        if not dialog.exec():
            return
        old, new = dialog.values()
        if self.store.change_password(old, new):
            show_success(self, "App password", "The password was changed.")
        else:
            show_error(self, "App password", "The current password was not correct. Nothing was changed.")

    def _remove_password(self) -> None:
        prompt = PasswordPrompt(self.store, self, title="Remove app password",
                                message="Enter your password to remove the lock. The prompt and personal "
                                        "information will be stored unencrypted again.")
        if prompt.exec() and self.store.disable_protection(prompt.password.text()):
            self.load_from_store()

    def _refresh_lock_card(self) -> None:
        on = self.store.lock.enabled()
        self.lock_status.setText("A password is set. The app asks for it when it opens." if on
                                 else "No password is set. Anyone who opens the app can read everything.")
        self.lock_set.setVisible(not on)
        self.lock_change.setVisible(on)
        self.lock_remove.setVisible(on)

    # ----------------------------------------------------------- hide / show behind the password

    def _capture(self, group: str) -> None:
        """Remember what is typed in a group that is currently visible."""
        if not self._shown[group]:
            return
        for w in self._groups[group]:
            self._real[w] = w.toPlainText() if isinstance(w, QPlainTextEdit) else w.text()

    def _value(self, w: QWidget) -> str:
        if w in self._groups_by_widget() and not self._shown[self._groups_by_widget()[w]]:
            return self._real.get(w, "")
        return w.toPlainText() if isinstance(w, QPlainTextEdit) else w.text()

    def _groups_by_widget(self) -> dict[QWidget, str]:
        return {w: g for g, ws in self._groups.items() for w in ws}

    def _display(self, group: str, show: bool) -> None:
        self._shown[group] = show
        for w in self._groups[group]:
            real = self._real.get(w, "")
            if isinstance(w, QPlainTextEdit):
                w.setPlainText(real if show else MASK)
            else:
                w.setText(real if show else MASK)
            w.setReadOnly(not show)
        eye = self._eyes.get(group)
        if eye is not None:
            set_icon(eye, "eye-off" if show else "eye", "text", 16)
            eye.setVisible(self.store.lock.enabled())

    def _toggle_group(self, group: str, what: str) -> None:
        if self._shown[group]:
            self._capture(group)
            self._display(group, False)
        elif confirm_password(self.store, self, what):
            self._display(group, True)

    def _gate(self, button, show: bool, what: str) -> bool:
        """Show buttons of the API key and the token: ask for the password first when one is set."""
        if show and not confirm_password(self.store, self, what):
            button.blockSignals(True)
            button.setChecked(False)
            button.blockSignals(False)
            return False
        return True

    def set_intake_status(self, running: bool, port: int, error: str = "") -> None:
        if running:
            self.intake_status.setText(f"Listening on 127.0.0.1:{port}")
        else:
            self.intake_status.setText(error or "Off")

    def _token_to_copy(self) -> str:
        if self.intake_show.isChecked() or confirm_password(self.store, self, "the extension token"):
            return self.intake_token.text()
        return ""

    def _toggle_token(self, show: bool) -> None:
        if not self._gate(self.intake_show, show, "the extension token"):
            return
        self.intake_token.setEchoMode(QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password)
        self.intake_show.setText(C.SETTINGS_HIDE if show else C.SETTINGS_SHOW)

    def _regenerate_token(self) -> None:
        self.intake_token.setText(regenerate_token(self.store.secret_path))

    @staticmethod
    def _stack_layout(label: str, inner: QLayout) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(4)
        box.addWidget(field_label(label))
        box.addLayout(inner)
        return box

    def _build_processing_card(self) -> QFrame:
        card = self._card("Processing", "Queue behaviour and when to call an application ghosted.", "clock", "warning")
        lay = card.layout()
        self.delay = QDoubleSpinBox()
        self.delay.setRange(0, 60)
        self.delay.setSingleStep(0.5)
        self.delay.setSuffix(" s")
        self.auto_retries = QSpinBox()
        self.auto_retries.setRange(0, 5)
        self.auto_retries.setToolTip("Extra automatic attempts after a failed generation. 0 = retry manually.")
        self.ghost_days = QSpinBox()
        self.ghost_days.setRange(1, 120)
        self.ghost_days.setSuffix(" days")
        lay.addLayout(self._stack("Pause between jobs", self.delay))
        lay.addLayout(self._stack("Automatic retries after a failed generation", self.auto_retries))
        lay.addLayout(self._stack("Show “No response” after", self.ghost_days))
        lay.addWidget(muted_label("A job that needs a description never blocks the queue; "
                                  "the other jobs keep processing."))
        lay.addStretch(1)
        return card

    def _build_appearance_card(self) -> QFrame:
        card = self._card(C.SETTINGS_APPEARANCE_TITLE, "Theme and table density.", "settings", "violet")
        lay = card.layout()
        self.theme = QComboBox()
        self.theme.addItem("Dark", "dark")
        self.theme.addItem("Light", "light")
        self.theme.addItem("System", "system")
        self.density = QComboBox()
        self.density.addItem("Comfortable", "comfortable")
        self.density.addItem("Compact", "compact")
        lay.addWidget(field_label(C.SETTINGS_THEME))
        lay.addWidget(self._tiles(self.theme))
        lay.addSpacing(4)
        lay.addWidget(field_label("Table density"))
        lay.addWidget(self._tiles(self.density))
        lay.addStretch(1)
        return card

    # ------------------------------------------------------------------- state

    def load_from_store(self) -> None:
        s = self.store.settings
        p = s.profile
        for w, v in ((self.name, p.name), (self.email, p.email), (self.phone, p.phone), (self.location, p.location),
                     (self.edu_school, p.education_school), (self.edu_location, p.education_location),
                     (self.edu_degree, p.education_degree), (self.edu_dates, p.education_dates),
                     (self.work_history, p.work_history), (self.linkedin, p.linkedin_url), (self.github, p.github_url), (self.website, p.website_url),
                     (self.work_auth, p.work_authorization), (self.sponsorship, p.needs_sponsorship),
                     (self.prompt, s.prompt_template)):
            self._real[w] = v
        self.api_key.blockSignals(True)
        self.api_key.setText(s.api_key)
        self.api_key.blockSignals(False)
        self._key_dirty = False
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        where = secrets.backend_name()
        self.key_hint.setText((C.SETTINGS_API_SAVED.format(masked=s.masked_api_key()) if s.api_key
                               else C.SETTINGS_API_NONE) + f" · stored with {where}")
        self.generation_mode.setCurrentIndex(max(0, self.generation_mode.findData(s.generation_mode)))
        self.chat_provider.setCurrentIndex(max(0, self.chat_provider.findData(s.chat_provider)))
        self.web_proxy.setText(s.web_proxy)
        proxy_user, _ = self.store.proxy_credentials()
        self.proxy_user.setText(proxy_user)
        self.proxy_password.clear()
        self.proxy_password.setPlaceholderText("Saved. Type a new password to replace it." if proxy_user
                                               else "Proxy password")
        self.model.setText(s.model)
        protected = self.store.lock.enabled()  # with a password set, these stay hidden until the eye is pressed
        for group in self._groups:
            self._display(group, not protected)
        self._refresh_lock_card()
        self.output_root.setText(s.output_root)
        self.export_pdf.setChecked(s.export_pdf)
        self.export_cover.setChecked(s.export_cover_letter)
        self.naming_hint.setText(f"{s.output_root or 'E:\\Remote'}\\{{Name}}\\{{Company}}\\{{Role}}\\\n"
                                 "{Name}_Resume.docx · {Name}_Resume.pdf · {Name}_Cover_Letter.docx")
        self.delay.setValue(s.request_delay_seconds)
        self.auto_retries.setValue(s.auto_retries)
        self.ghost_days.setValue(s.ghost_after_days)
        self.intake_enabled.setChecked(s.intake_enabled)
        self.intake_auto.setChecked(s.intake_auto_start)
        self.intake_token.setText(get_token(self.store.secret_path))
        self.theme.setCurrentIndex(max(0, self.theme.findData(s.theme)))
        self.density.setCurrentIndex(max(0, self.density.findData(s.density)))
        self.app_mode.setCurrentIndex(max(0, self.app_mode.findData(s.app_mode)))
        self.controller_url.setText(s.controller_url)
        self.controller_token.clear()
        self.controller_token.setPlaceholderText(
            "Saved. Type a new token to replace it." if self.store.controller_token()
            else "Token from the controller's Settings -> Browser extension")
        self.auto_sync.setChecked(s.auto_sync_files)
        self.disc_enabled.setChecked(s.discovery_enabled)
        self.disc_interval.setValue(s.discovery_interval_minutes)
        self.disc_cap.setValue(s.discovery_hourly_cap)
        self.disc_max_age.setValue(s.discovery_max_age_hours)
        self.disc_url.setText(s.discovery_url)
        self.disc_blocked.setText(s.discovery_blocked_companies)
        self.disc_exclude.setText(s.discovery_exclude_words)
        self.bot_tz.setText(s.bot_timezone)
        self.hour_from.setValue(s.active_start_hour)
        self.hour_to.setValue(s.active_end_hour)
        self.apply_cap.setValue(s.apply_daily_cap)
        self.skip_optional.setChecked(s.apply_skip_optional)
        self.salary_floor.setValue(s.salary_floor)
        self.salary_band_low.setValue(s.salary_band_low)
        self.salary_band_high.setValue(s.salary_band_high)
        self.skip_below_floor.setChecked(s.discovery_skip_below_floor)
        self.bot_browser.setCurrentIndex(max(0, self.bot_browser.findData(s.bot_browser)))
        for edit, name, empty in ((self.discord_hook, "discord_webhook", "Discord webhook address (optional)"),
                                  (self.tg_token, "telegram_token", "Telegram bot token (optional)")):
            edit.clear()  # a saved secret is never shown again, only replaced
            edit.setPlaceholderText("Saved. Type a new value to replace it." if self.store.notify_secret(name) else empty)
        self.tg_chat.setText(s.notify_telegram_chat)
        self.notify_reports.setChecked(s.notify_reports)
        self.intake_bind.setText(s.intake_bind)
        self.allowed_hosts.setText(s.intake_allowed_hosts)
        self._apply_mode_visibility()

    def _apply_mode_visibility(self) -> None:
        """Show only the settings that matter for the saved mode (the viewer never uses Claude)."""
        mode = self.store.settings.app_mode
        shown = {
            "connection": mode == MODE_VIEWER,
            "controller": mode == MODE_CONTROLLER,
            "profile": mode != MODE_VIEWER,
            "claude": mode != MODE_VIEWER,
            "autoapply": mode != MODE_VIEWER,
            "extension": mode != MODE_VIEWER,
        }
        for key, visible in shown.items():
            self._cards[key].setVisible(visible)
        self.validate_btn.setText("Test connection" if mode == MODE_VIEWER else C.SETTINGS_VALIDATE)

    def _test_controller(self) -> None:
        url = normalize_controller_url(self.controller_url.text())
        token = self.controller_token.text().strip() or self.store.controller_token()
        if not url:
            show_error(self, "Controller", "Enter the controller's address first, e.g. http://100.64.0.5:47821")
            return
        try:
            status = ControllerClient(lambda: url, lambda: token, timeout=8).status()
        except Exception as exc:  # noqa: BLE001 - every failure is explained to the person
            show_error(self, "Controller", str(exc))
            return
        note = " It is locked, so its profile cannot be shared." if status.get("locked") else ""
        show_success(self, "Controller", f"Connected. The controller's queue is {status.get('worker', 'idle')}.{note}")

    def set_link_status(self, connected: bool, error: str, last_ok: str = "") -> None:
        self.link_status.setText(("Connected" + (f" · last update {last_ok}" if last_ok else "")) if connected
                                 else (error or "Not connected"))

    def _toggle_key(self, show: bool) -> None:
        if not self._gate(self.show_key, show, "the API key"):
            return
        self.api_key.setEchoMode(QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password)
        self.show_key.setText(C.SETTINGS_HIDE if show else C.SETTINGS_SHOW)

    def _browse_output(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Output root", self.output_root.text())
        if chosen:
            self.output_root.setText(chosen)

    def _validate(self) -> None:
        if self.store.settings.app_mode == MODE_VIEWER:
            self._test_controller()
            return
        snap = self.store.snapshot()
        key = self.api_key.text().strip()
        if key:
            snap.api_key = key
        ok, message = ClaudeClient(snap).validate_connection()
        (show_success if ok else show_error)(self, C.DLG_CLAUDE_TITLE, message)

    def save(self):
        s = self.store.settings
        s.profile.name = self._value(self.name).strip()
        s.profile.email = self._value(self.email).strip()
        s.profile.phone = self._value(self.phone).strip()
        s.profile.location = self._value(self.location).strip()
        s.profile.education_school = self._value(self.edu_school).strip()
        s.profile.education_location = self._value(self.edu_location).strip()
        s.profile.education_degree = self._value(self.edu_degree).strip()
        s.profile.education_dates = self._value(self.edu_dates).strip()
        s.profile.work_history = self._value(self.work_history).strip()
        s.profile.linkedin_url = self._value(self.linkedin).strip()
        s.profile.github_url = self._value(self.github).strip()
        s.profile.website_url = self._value(self.website).strip()
        s.profile.work_authorization = yes_no(self._value(self.work_auth))
        s.profile.needs_sponsorship = yes_no(self._value(self.sponsorship))
        if self._key_dirty:
            s.api_key = self.api_key.text().strip()
        s.generation_mode = self.generation_mode.currentData() or "claude_chat"
        s.chat_provider = self.chat_provider.currentData() or "claude"
        s.web_proxy = normalize_proxy_url(self.web_proxy.text())
        old_user, old_password = self.store.proxy_credentials()
        user = self.proxy_user.text().strip()
        password = self.proxy_password.text() or (old_password if user == old_user else "")
        self.store.set_proxy_credentials(user, password)
        self.proxy_password.clear()
        s.model = self.model.text().strip() or s.model
        s.prompt_template = self._value(self.prompt)
        s.output_root = self.output_root.text().strip()
        s.export_pdf = self.export_pdf.isChecked()
        s.export_cover_letter = self.export_cover.isChecked()
        s.request_delay_seconds = float(self.delay.value())
        s.auto_retries = self.auto_retries.value()
        s.ghost_after_days = self.ghost_days.value()
        s.intake_enabled = self.intake_enabled.isChecked()
        s.intake_auto_start = self.intake_auto.isChecked()
        s.theme = self.theme.currentData()
        s.density = self.density.currentData()
        s.app_mode = self.app_mode.currentData() or MODE_STANDALONE
        s.controller_url = normalize_controller_url(self.controller_url.text())
        typed = self.controller_token.text().strip()
        if typed:
            self.store.set_controller_token(typed)
            self.controller_token.clear()
        s.auto_sync_files = self.auto_sync.isChecked()
        s.discovery_enabled = self.disc_enabled.isChecked()
        s.discovery_interval_minutes = self.disc_interval.value()
        s.discovery_hourly_cap = self.disc_cap.value()
        s.discovery_max_age_hours = self.disc_max_age.value()
        s.discovery_url = self.disc_url.text().strip()
        s.discovery_blocked_companies = self.disc_blocked.text().strip()
        s.discovery_exclude_words = self.disc_exclude.text().strip()
        s.bot_timezone = self.bot_tz.text().strip() or "America/New_York"
        s.active_start_hour = self.hour_from.value()
        s.active_end_hour = self.hour_to.value()
        s.apply_daily_cap = self.apply_cap.value()
        s.apply_skip_optional = self.skip_optional.isChecked()
        s.salary_floor = self.salary_floor.value()
        s.salary_band_low = self.salary_band_low.value()
        s.salary_band_high = max(self.salary_band_high.value(), self.salary_band_low.value())
        s.discovery_skip_below_floor = self.skip_below_floor.isChecked()
        s.bot_browser = self.bot_browser.currentData() or "chrome"
        s.notify_telegram_chat = self.tg_chat.text().strip()
        s.notify_reports = self.notify_reports.isChecked()
        for edit, name in ((self.discord_hook, "discord_webhook"), (self.tg_token, "telegram_token")):
            if edit.text().strip():
                self.store.set_notify_secret(name, edit.text())
                edit.clear()
        s.intake_bind = self.intake_bind.text().strip() or "127.0.0.1"
        s.intake_allowed_hosts = self.allowed_hosts.text().strip()
        self.store.save()
        self._key_dirty = False
        self.show_key.setChecked(False)
        self.load_from_store()
        self.footer_hint.setText(C.SETTINGS_SAVED)
        self.saved.emit()
        return s
