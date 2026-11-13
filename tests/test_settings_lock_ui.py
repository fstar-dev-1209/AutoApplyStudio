"""Settings screen with an app password: hidden by default, eye button asks for the password."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QLineEdit

import ui.settings_view as sv
from core.settings_store import SettingsStore
from ui.lock_dialogs import NewPasswordDialog, PasswordPrompt

PW = "correct horse"


@pytest.fixture
def locked_view(qtbot, tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.settings.prompt_template = "MY SECRET PROMPT"
    store.settings.profile.name = "Jane Q Public"
    store.save()
    store.enable_protection(PW)
    view = sv.SettingsView(store)
    qtbot.addWidget(view)
    return view


def test_prompt_and_personal_info_start_hidden_and_read_only(locked_view):
    v = locked_view
    assert v.name.text() == sv.MASK and v.name.isReadOnly()
    assert v.prompt.toPlainText() == sv.MASK and v.prompt.isReadOnly()
    assert "Jane" not in v.name.text() and "SECRET" not in v.prompt.toPlainText()


def test_eye_asks_for_the_password_then_shows_and_locks_again(locked_view, monkeypatch):
    v = locked_view
    asked: list[str] = []
    answers = iter([False, True])
    monkeypatch.setattr(sv, "confirm_password", lambda store, parent, what: (asked.append(what), next(answers))[1])

    v._eyes["profile"].click()  # wrong password: stays hidden
    assert v.name.text() == sv.MASK and asked == ["personal information"]
    v._eyes["profile"].click()  # right password: shown and editable
    assert v.name.text() == "Jane Q Public" and not v.name.isReadOnly()
    assert v.prompt.toPlainText() == sv.MASK  # the prompt has its own eye
    v._eyes["profile"].click()  # eye again: locked, no password needed
    assert v.name.text() == sv.MASK and v.name.isReadOnly() and len(asked) == 2


def test_saving_while_hidden_keeps_the_real_values(locked_view, monkeypatch):
    v = locked_view
    v.save()
    assert v.store.settings.profile.name == "Jane Q Public"
    assert v.store.settings.prompt_template == "MY SECRET PROMPT"
    assert v.name.text() == sv.MASK  # hidden again after saving

    monkeypatch.setattr(sv, "confirm_password", lambda *a: True)
    v._eyes["prompt"].click()
    v.prompt.setPlainText("NEW PROMPT")
    v.save()
    assert SettingsStore(v.store.path).settings.prompt_template == "NEW PROMPT"


def test_api_key_and_token_show_buttons_need_the_password(locked_view, monkeypatch):
    v = locked_view
    monkeypatch.setattr(sv, "confirm_password", lambda *a: False)
    v.show_key.setChecked(True)
    assert not v.show_key.isChecked() and v.api_key.echoMode() == QLineEdit.EchoMode.Password
    v.intake_show.setChecked(True)
    assert not v.intake_show.isChecked() and v.intake_token.echoMode() == QLineEdit.EchoMode.Password


def test_without_a_password_nothing_is_hidden(qtbot, tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.settings.profile.name = "Jane Q Public"
    store.save()
    view = sv.SettingsView(store)
    qtbot.addWidget(view)
    assert view.name.text() == "Jane Q Public" and not view.name.isReadOnly()
    assert view.lock_set.isVisibleTo(view) and not view.lock_change.isVisibleTo(view)


def test_password_dialogs_validate(qtbot, tmp_path):
    new = NewPasswordDialog()
    qtbot.addWidget(new)
    new.new.setText("short")
    new.again.setText("short")
    new._submit()
    assert new.result() == 0 and "at least" in new.error.text()
    new.new.setText(PW)
    new.again.setText("different")
    new._submit()
    assert "match" in new.error.text()
    new.again.setText(PW)
    new._submit()
    assert new.result() == 1 and new.values() == ("", PW)

    store = SettingsStore(tmp_path / "settings.json")
    store.enable_protection(PW)
    store.lock.forget_key()
    prompt = PasswordPrompt(store, unlock=True)
    qtbot.addWidget(prompt)
    prompt.password.setText("wrong")
    prompt._submit()
    assert prompt.result() == 0 and not store.lock.unlocked() and "not correct" in prompt.error.text()
    prompt.password.setText(PW)
    prompt._submit()
    assert prompt.result() == 1 and store.lock.unlocked()
