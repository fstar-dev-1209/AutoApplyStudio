"""Credential handling and settings migration."""

import json
import sys

import pytest

from core import secrets
from core.settings_store import SettingsStore


def test_secret_roundtrip_and_not_plaintext(tmp_path):
    path = tmp_path / "credentials.dat"
    secrets.store_secret("k", "sk-ant-very-secret-123", path)
    assert secrets.load_secret("k", path) == "sk-ant-very-secret-123"
    assert "very-secret" not in path.read_text(encoding="utf-8")  # ciphertext only
    secrets.store_secret("k", "", path)
    assert secrets.load_secret("k", path) == ""


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_dpapi_ciphertext_is_not_portable_garbage(tmp_path):
    path = tmp_path / "credentials.dat"
    secrets.store_secret("k", "abc", path)
    data = json.loads(path.read_text())
    data["k"] = data["k"][:-8] + "AAAAAAAA"  # corrupt it
    path.write_text(json.dumps(data))
    assert secrets.load_secret("k", path) == ""  # fails closed, no crash


def test_api_key_is_kept_out_of_settings_json(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.update(api_key="sk-ant-xyz")
    on_disk = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert "sk-ant-xyz" not in on_disk
    assert json.loads(on_disk)["api_key_saved"] is True
    reloaded = SettingsStore(tmp_path / "settings.json")
    assert reloaded.settings.api_key == "sk-ant-xyz" and reloaded.has_api_key()


def test_existing_plaintext_key_migrates_and_original_is_backed_up(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"api_key": "sk-old-plain", "theme": "light", "prompt_template": "MY PROMPT",
                                "output_root": "E:\\Remote", "generation_mode": "claude_api"}), encoding="utf-8")
    store = SettingsStore(path)
    assert store.settings.api_key == "sk-old-plain"
    assert "sk-old-plain" not in path.read_text(encoding="utf-8")  # rewritten without the key
    assert (tmp_path / "settings.json.pre-redesign.bak").exists()  # one-time safety copy
    s = json.loads(path.read_text(encoding="utf-8"))
    assert s["prompt_template"] == "MY PROMPT" and s["output_root"] == "E:\\Remote"  # nothing else lost
    assert s["theme"] == "dark" and s["ui_revision"] == 2  # old light default moves to the new dark default once
    assert SettingsStore(path).settings.api_key == "sk-old-plain"


def test_new_option_defaults_do_not_change_existing_behaviour(tmp_path):
    s = SettingsStore(tmp_path / "settings.json").settings
    assert (s.auto_retries, s.export_pdf, s.export_cover_letter, s.ghost_after_days) == (0, True, True, 14)
