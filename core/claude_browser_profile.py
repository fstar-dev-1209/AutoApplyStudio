"""Persistent Qt WebEngine profile for claude.ai sessions (kept for callers that predate multiple chat sites)."""

from __future__ import annotations

from PySide6.QtWebEngineCore import QWebEngineProfile

from core.chat_browser_profile import chat_browser_root, chat_web_profile, clear_chat_browser_data

CLAUDE_CHAT_HOME = "https://claude.ai/"
CLAUDE_NEW_CHAT = "https://claude.ai/new"


def claude_browser_root() -> str:
    root = chat_browser_root("claude")
    (root / "storage").mkdir(parents=True, exist_ok=True)
    (root / "cache").mkdir(parents=True, exist_ok=True)
    return str(root)


def claude_web_profile() -> QWebEngineProfile:
    return chat_web_profile("claude")


def clear_claude_browser_data() -> None:
    """Remove saved Claude session (user must sign in again)."""
    clear_chat_browser_data("claude")
