"""Compatibility names for the Claude web chat driver; the implementation is ``ui.chat_service.ChatService``."""

from __future__ import annotations

from ui.chat_service import CLAUDE_REPLY_WAIT_SEC, ChatService

ClaudeChatService = ChatService

__all__ = ["CLAUDE_REPLY_WAIT_SEC", "ClaudeChatService", "ChatService"]
