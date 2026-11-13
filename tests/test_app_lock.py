"""App password: key derivation, encrypted settings, change / remove / forgot-password."""

from __future__ import annotations

import json

import pytest

from core.app_lock import AppLock, LockedError
from core.settings_store import DEFAULT_PROMPT, SettingsStore

PW = "correct horse"


def _store(tmp_path) -> SettingsStore:
    store = SettingsStore(tmp_path / "settings.json")
    store.settings.prompt_template = "MY SECRET PROMPT"
    store.settings.profile.name = "Jane Q Public"
    store.settings.profile.phone = "+1 (555) 010-0199"
    store.save()
    return store


def test_password_is_checked_and_never_stored(tmp_path):
    lock = AppLock(tmp_path / "lock.json")
    assert not lock.enabled()
    lock.create(PW)
    assert lock.enabled() and lock.unlocked()
    assert PW not in (tmp_path / "lock.json").read_text(encoding="utf-8")
    lock.forget_key()
    assert not lock.unlocked()
    assert not lock.unlock("wrong password") and not lock.unlocked()
    assert lock.verify(PW) and not lock.unlocked()  # verifying alone does not unlock
    assert lock.unlock(PW) and lock.unlocked()
    with pytest.raises(ValueError):
        AppLock(tmp_path / "other.json").create("short")


def test_prompt_and_profile_are_encrypted_on_disk_and_come_back(tmp_path):
    store = _store(tmp_path)
    store.enable_protection(PW)
    text = (tmp_path / "settings.json").read_text(encoding="utf-8")
    for secret in ("MY SECRET PROMPT", "Jane Q Public", "555"):
        assert secret not in text
    assert "protected" in json.loads(text)

    store.lock.forget_key()  # a new launch: nothing unlocked yet
    locked = SettingsStore(tmp_path / "settings.json")
    assert locked.data_locked and locked.settings.prompt_template == DEFAULT_PROMPT
    locked.settings.theme = "light"
    locked.save()  # saving while locked must not destroy the protected data
    assert locked.lock.unlock(PW)
    reopened = SettingsStore(tmp_path / "settings.json")
    assert not reopened.data_locked
    assert reopened.settings.prompt_template == "MY SECRET PROMPT"
    assert reopened.settings.profile.name == "Jane Q Public" and reopened.settings.theme == "light"


def test_change_password_and_remove(tmp_path):
    store = _store(tmp_path)
    store.enable_protection(PW)
    assert not store.change_password("nope nope", "new password 1")
    assert store.change_password(PW, "new password 1")
    store.lock.forget_key()
    assert not store.lock.unlock(PW) and store.lock.unlock("new password 1")
    assert SettingsStore(tmp_path / "settings.json").settings.prompt_template == "MY SECRET PROMPT"

    assert not store.disable_protection("nope nope")
    assert store.disable_protection("new password 1")
    assert not store.lock.enabled()
    plain = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert plain["prompt_template"] == "MY SECRET PROMPT" and plain["profile"]["name"] == "Jane Q Public"
    assert "protected" not in plain


def test_forgotten_password_wipes_secrets_but_keeps_other_settings(tmp_path):
    store = _store(tmp_path)
    store.settings.output_root = "D:\\Out"
    store.enable_protection(PW)
    store.lock.forget_key()
    forgotten = SettingsStore(tmp_path / "settings.json")
    forgotten.reset_protection()
    assert not forgotten.lock.enabled()
    after = SettingsStore(tmp_path / "settings.json").settings
    assert after.prompt_template == DEFAULT_PROMPT and after.profile.name != "Jane Q Public"
    assert after.output_root == "D:\\Out"
    with pytest.raises(LockedError):
        AppLock(tmp_path / "lock.json").seal("x")
