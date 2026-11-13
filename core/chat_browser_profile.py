"""Persistent Qt WebEngine profiles for the web chats (one per site, so the sign-ins never mix)."""

from __future__ import annotations

import shutil

from PySide6.QtWebEngineCore import QWebEngineProfile

from core.paths import config_dir

_profiles: dict[str, QWebEngineProfile] = {}


def chat_browser_root(site_key: str):
    return config_dir() / "browser" / site_key


def chat_web_profile(site_key: str) -> QWebEngineProfile:
    existing = _profiles.get(site_key)
    if existing is not None:
        return existing
    root = chat_browser_root(site_key)
    storage, cache = root / "storage", root / "cache"
    storage.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    profile = QWebEngineProfile(f"{site_key}-chat", None)
    profile.setPersistentStoragePath(str(storage))
    profile.setCachePath(str(cache))
    profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)
    _profiles[site_key] = profile
    return profile


def clear_chat_browser_data(site_key: str) -> None:
    """Remove a saved sign-in (the user must sign in again)."""
    profile = _profiles.pop(site_key, None)
    if profile is not None:
        profile.deleteLater()
    root = chat_browser_root(site_key)
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)
