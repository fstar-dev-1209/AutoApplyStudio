"""Password dialogs for the app lock: launch login, set / change password, and the eye-button check."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from core.app_lock import MIN_PASSWORD_LENGTH
from core.settings_store import SettingsStore
from ui.dialogs import ask_yes_no
from ui.widgets import ghost_button, muted_label, primary_button


def _password_field(placeholder: str = "") -> QLineEdit:
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    field.setPlaceholderText(placeholder)
    return field


class _Base(QDialog):
    def __init__(self, parent: QWidget | None, title: str, message: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(400)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(24, 22, 24, 18)
        self.root.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("SectionTitle")
        self.root.addWidget(heading)
        self.message = muted_label(message)
        self.message.setWordWrap(True)
        self.root.addWidget(self.message)
        self.error = QLabel("")
        self.error.setObjectName("ErrorText")
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #EF4444;")

    def _buttons(self, ok_text: str, extra: QWidget | None = None) -> None:
        self.root.addWidget(self.error)
        row = QHBoxLayout()
        if extra is not None:
            row.addWidget(extra)
        row.addStretch(1)
        cancel = ghost_button("Cancel")
        cancel.clicked.connect(self.reject)
        self.ok = primary_button(ok_text)
        self.ok.setDefault(True)
        self.ok.clicked.connect(self._submit)
        row.addWidget(cancel)
        row.addWidget(self.ok)
        self.root.addLayout(row)

    def _submit(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class PasswordPrompt(_Base):
    """Ask for the app password; accepted only when ``store`` confirms it."""

    def __init__(self, store: SettingsStore, parent: QWidget | None = None, *, title: str = "Enter password",
                 message: str = "Enter your app password to continue.", unlock: bool = False,
                 allow_reset: bool = False) -> None:
        super().__init__(parent, title, message)
        self.store = store
        self._unlock = unlock
        self.reset_done = False
        self.password = _password_field("Password")
        self.password.returnPressed.connect(self._submit)
        self.root.addWidget(self.password)
        forgot = None
        if allow_reset:
            forgot = ghost_button("Forgot password")
            forgot.clicked.connect(self._forgot)
        self._buttons("Unlock" if unlock else "Show", forgot)
        self.password.setFocus()

    def _submit(self) -> None:
        text = self.password.text()
        ok = self.store.lock.unlock(text) if self._unlock else self.store.lock.verify(text)
        if ok:
            self.accept()
            return
        self.error.setText("That password is not correct.")
        self.password.selectAll()
        self.password.setFocus()

    def _forgot(self) -> None:
        if not ask_yes_no(self, "Reset the app password?",
                          "There is no way to recover the password. Resetting removes the password and clears "
                          "the saved resume prompt and personal information, so you will enter them again. "
                          "Your jobs, applications, history and generated files are not touched. Continue?"):
            return
        self.store.reset_protection()
        self.reset_done = True
        self.accept()


class NewPasswordDialog(_Base):
    """Choose a password (asks for the current one first when changing)."""

    def __init__(self, parent: QWidget | None = None, *, change: bool = False) -> None:
        super().__init__(
            parent,
            "Change app password" if change else "Set an app password",
            "Pick a password for this app. It protects the resume prompt, your personal information, "
            "the API key and the extension token. It cannot be recovered if you forget it.",
        )
        form = QFormLayout()
        form.setSpacing(8)
        self.current = _password_field() if change else None
        if self.current is not None:
            form.addRow("Current password", self.current)
        self.new = _password_field(f"At least {MIN_PASSWORD_LENGTH} characters")
        self.again = _password_field()
        form.addRow("New password", self.new)
        form.addRow("Repeat it", self.again)
        self.root.addLayout(form)
        self._buttons("Save password")
        (self.current or self.new).setFocus()

    def values(self) -> tuple[str, str]:
        return (self.current.text() if self.current is not None else ""), self.new.text()

    def _submit(self) -> None:
        if len(self.new.text()) < MIN_PASSWORD_LENGTH:
            self.error.setText(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
        elif self.new.text() != self.again.text():
            self.error.setText("The two passwords do not match.")
        else:
            self.accept()


def unlock_at_launch(store: SettingsStore) -> bool:
    """Show the login before the main window. False when the user cancelled."""
    if not store.lock.enabled() or store.lock.unlocked():
        return True
    dialog = PasswordPrompt(store, None, title="Application Studio is locked",
                            message="Enter your app password to open Application Studio.",
                            unlock=True, allow_reset=True)
    dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    return dialog.exec() == QDialog.DialogCode.Accepted


def confirm_password(store: SettingsStore, parent: QWidget | None, what: str) -> bool:
    """Gate for the eye buttons: True when no password is set or the right one was typed."""
    if not store.lock.enabled():
        return True
    dialog = PasswordPrompt(store, parent, title="Enter password", message=f"Enter your app password to show {what}.")
    return dialog.exec() == QDialog.DialogCode.Accepted
