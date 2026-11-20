"""Persistent Qt WebEngine profiles for the web chats (one per site and account, so the sign-ins never mix).

``account`` names a second sign-in for the same site: the application questions use their own ChatGPT account
("questions"), kept apart from the one that writes resumes (""), so one account's usage limit, human check or sign-out
never stops the other's work."""

from __future__ import annotations

import shutil

from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings

from core.paths import config_dir

_profiles: dict[str, QWebEngineProfile] = {}


def _key(site_key: str, account: str = "") -> str:
    return f"{site_key}-{account}" if account else site_key


def chat_browser_root(site_key: str, account: str = ""):
    return config_dir() / "browser" / _key(site_key, account)


def _wipe_marker(site_key: str, account: str = ""):
    return config_dir() / "browser" / f"{_key(site_key, account)}.wipe"


def request_wipe(site_key: str, account: str = "") -> None:
    """Remove this account's saved files at the next start, when no page is using them (see sign_out)."""
    marker = _wipe_marker(site_key, account)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("signed out\n", encoding="utf-8")


def cancel_wipe(site_key: str, account: str = "") -> None:
    """Signed in again after a sign-out: keep this account's files (the new sign-in is in them)."""
    _wipe_marker(site_key, account).unlink(missing_ok=True)


def sign_out(profile: QWebEngineProfile) -> None:
    """Sign a profile out while its pages stay open: its cookies (the sign-in) and its cache are cleared now.

    Deleting the profile or its folder while a page still used it crashed the app and left the folder half-removed;
    the files themselves are removed at the next start instead (request_wipe)."""
    profile.cookieStore().deleteAllCookies()
    profile.clearHttpCache()
    profile.clearAllVisitedLinks()


def chat_web_profile(site_key: str, account: str = "") -> QWebEngineProfile:
    key = _key(site_key, account)
    existing = _profiles.get(key)
    if existing is not None:
        return existing
    root = chat_browser_root(site_key, account)
    marker = _wipe_marker(site_key, account)
    if marker.exists():  # signed out last time: start this account from empty files, before anything opens them
        shutil.rmtree(root, ignore_errors=True)
        marker.unlink(missing_ok=True)
    storage, cache = root / "storage", root / "cache"
    storage.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    profile = QWebEngineProfile(f"{key}-chat", None)
    profile.setPersistentStoragePath(str(storage))
    profile.setCachePath(str(cache))
    profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)
    # The page's Copy button writes the reply to the clipboard when the app clicks it (that is how a reply is read now)
    profile.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True)
    _profiles[key] = profile
    return profile


def clear_chat_browser_data(site_key: str, account: str = "") -> None:
    """Remove a saved sign-in (the user must sign in again)."""
    profile = _profiles.pop(_key(site_key, account), None)
    if profile is not None:
        profile.deleteLater()
    root = chat_browser_root(site_key, account)
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)
