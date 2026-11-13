"""Settings for the three ways to run the app: what is shown, what is saved, and what stays secret."""

from __future__ import annotations

import json

import pytest

from core.settings_store import MODE_CONTROLLER, MODE_STANDALONE, MODE_VIEWER, AppSettings, SettingsStore
from ui.settings_view import SettingsView


@pytest.fixture
def make_view(qtbot, tmp_path):
    def build(mode: str):
        store = SettingsStore(tmp_path / "settings.json")
        store.settings.app_mode = mode
        view = SettingsView(store)
        qtbot.addWidget(view)
        view.show()
        return view, store

    return build


def test_each_mode_shows_only_the_settings_that_apply(make_view):
    view, _ = make_view(MODE_VIEWER)
    assert view._cards["connection"].isVisible() and not view._cards["controller"].isVisible()
    assert not view._cards["claude"].isVisible() and not view._cards["profile"].isVisible()  # a viewer never uses Claude
    assert not view._cards["extension"].isVisible()
    assert view.validate_btn.text() == "Test connection"

    view, _ = make_view(MODE_CONTROLLER)
    assert view._cards["controller"].isVisible() and view._cards["claude"].isVisible()
    assert not view._cards["connection"].isVisible()

    view, _ = make_view(MODE_STANDALONE)
    assert view._cards["claude"].isVisible()
    assert not view._cards["connection"].isVisible() and not view._cards["controller"].isVisible()


def test_connection_settings_are_saved_and_the_token_stays_out_of_the_settings_file(make_view, tmp_path):
    view, store = make_view(MODE_STANDALONE)
    view.app_mode.setCurrentIndex(view.app_mode.findData(MODE_VIEWER))
    view.controller_url.setText("100.64.0.5:47821/ignored/path")
    view.controller_token.setText("s3cret-token")
    view.auto_sync.setChecked(False)
    view.save()

    assert store.settings.app_mode == MODE_VIEWER
    assert store.settings.controller_url == "http://100.64.0.5:47821"
    assert store.settings.auto_sync_files is False
    assert store.controller_token() == "s3cret-token"
    assert "s3cret-token" not in (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert view.controller_token.text() == ""  # the field is cleared, not left holding the secret

    reloaded = SettingsStore(tmp_path / "settings.json")
    assert reloaded.settings.app_mode == MODE_VIEWER and reloaded.is_viewer
    assert reloaded.controller_token() == "s3cret-token"


def test_a_blank_token_field_keeps_the_saved_token(make_view):
    view, store = make_view(MODE_VIEWER)
    store.set_controller_token("kept")
    view.load_from_store()
    view.save()
    assert store.controller_token() == "kept"


def test_controller_listening_settings_round_trip(make_view):
    view, store = make_view(MODE_CONTROLLER)
    view.intake_bind.setText(" 0.0.0.0 ")
    view.allowed_hosts.setText("100.64.0.5:47821")
    view.save()
    assert (store.settings.intake_bind, store.settings.intake_allowed_hosts) == ("0.0.0.0", "100.64.0.5:47821")
    view.intake_bind.setText("")
    view.save()
    assert store.settings.intake_bind == "127.0.0.1"  # blank means this computer only, never "everywhere"


def test_bad_or_old_settings_files_fall_back_safely():
    assert AppSettings.from_dict({}).app_mode == MODE_STANDALONE  # an existing install is unchanged
    assert AppSettings.from_dict({"app_mode": "root"}).app_mode == MODE_STANDALONE
    assert AppSettings.from_dict({"controller_url": "ftp://x"}).controller_url == ""
    assert AppSettings.from_dict({"intake_bind": "  "}).intake_bind == "127.0.0.1"
    assert json.loads(json.dumps(AppSettings().to_dict()))["app_mode"] == MODE_STANDALONE
