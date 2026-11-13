"""The web chats the app can drive (Claude, ChatGPT): addresses, page scripts and how to recognise a conversation."""

from __future__ import annotations

import re
from dataclasses import dataclass

from core import chatgpt_chat_scripts as gpt
from core import claude_chat_scripts as claude

CLAUDE = "claude"
CHATGPT = "chatgpt"
PROVIDERS = (CLAUDE, CHATGPT)


@dataclass(frozen=True)
class ChatSite:
    key: str
    label: str
    home: str
    new_chat: str
    chat_url: re.Pattern[str]  # a saved conversation (as opposed to the new-chat page)
    page_state_js: str
    extract_json_js: str
    insert_prompt_js: str
    click_send_js: str
    click_copy_js: str
    click_continue_js: str
    last_reply_js: str
    poll_clipboard: bool = False  # click the page's Copy button while waiting (only where that works unfocused)
    require_idle: bool = False  # take the reply only once the chat has stopped generating (its thinking text quotes the prompt)

    def is_chat_url(self, url: str) -> bool:
        return bool(self.chat_url.match((url or "").split("#")[0]))

    def clean_chat_url(self, url: str) -> str:
        """The stable address of a conversation, or '' when ``url`` is not one (query and fragment dropped)."""
        base = (url or "").split("#")[0].split("?")[0].rstrip("/")
        return base if self.chat_url.match(base) else ""


# A page in one of these states needs a person. Generation stops and is not retried on its own.
PAGE_BLOCKS = ("login", "challenge", "limit")


def block_message(label: str, blocked: str) -> str:
    """Why the chat page cannot be driven, or '' when ``blocked`` is not one of those states."""
    if blocked == "challenge":
        return (
            f"{label} is showing a human-verification check. Open the {label} tab, complete it once, "
            "then retry."
        )
    if blocked == "limit":
        return (
            f"{label} is showing a usage limit. This job was not sent on. "
            "Wait until the limit clears, then retry."
        )
    if blocked == "login":
        return f"Not signed in to {label}. Open the {label} tab, sign in once, then retry."
    return ""


_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

SITES: dict[str, ChatSite] = {
    CLAUDE: ChatSite(
        key=CLAUDE,
        label="Claude",
        home="https://claude.ai/",
        new_chat="https://claude.ai/new",
        chat_url=re.compile(rf"^https://claude\.ai/chat/{_UUID}/?(\?.*)?$"),
        page_state_js=claude.CLAUDE_PAGE_STATE_JS,
        extract_json_js=claude.CLAUDE_EXTRACT_JSON_JS,
        insert_prompt_js=claude.CLAUDE_INSERT_PROMPT_JS,
        click_send_js=claude.CLAUDE_CLICK_SEND_JS,
        click_copy_js=claude.CLAUDE_CLICK_COPY_LAST_JS,
        click_continue_js=claude.CLAUDE_CLICK_CONTINUE_JS,
        last_reply_js=claude.CLAUDE_LAST_REPLY_JS,
        poll_clipboard=True,
        require_idle=True,
    ),
    CHATGPT: ChatSite(
        key=CHATGPT,
        label="ChatGPT",
        home="https://chatgpt.com/",
        new_chat="https://chatgpt.com/",
        chat_url=re.compile(rf"^https://chatgpt\.com/(g/[^/]+/)?c/{_UUID}/?(\?.*)?$"),
        page_state_js=gpt.CHATGPT_PAGE_STATE_JS,
        extract_json_js=gpt.CHATGPT_EXTRACT_JSON_JS,
        insert_prompt_js=gpt.CHATGPT_INSERT_PROMPT_JS,
        click_send_js=gpt.CHATGPT_CLICK_SEND_JS,
        click_copy_js=gpt.CHATGPT_CLICK_COPY_LAST_JS,
        click_continue_js=gpt.CHATGPT_CLICK_CONTINUE_JS,
        last_reply_js=gpt.CHATGPT_LAST_REPLY_JS,
        require_idle=True,
    ),
}


def site_for(provider: str) -> ChatSite:
    return SITES.get(provider, SITES[CLAUDE])
