"""Stand-in chat pages for the web-chat driver tests.

They imitate the structure the drivers look for (composer, send/stop buttons, assistant messages, conversation
address) and stream a reply the way the real sites do. They are NOT copies of claude.ai or chatgpt.com: tests built
on them prove the driver logic, not that the live sites still match the selectors (``scripts/diagnose_chat.py``
checks that on a real, signed-in page).
"""

from __future__ import annotations

from PySide6.QtCore import QEventLoop, QTimer, QUrl
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile

CHAT_ID = "11111111-2222-3333-4444-555555555555"

_SCRIPT = r"""
const thread = document.getElementById('thread');
const composer = document.querySelector(COMPOSER_SELECTOR);
window.__sent = [];
window.__reply = null;
function add(role, text) {
  const wrap = document.createElement(WRAP_TAG);
  wrap.setAttribute('data-testid', 'conversation-turn-' + thread.children.length);
  const el = document.createElement('div');
  MARK_ROLE(el, role, wrap);
  el.textContent = text;
  wrap.appendChild(el); thread.appendChild(wrap);
  return el;
}
window.__add = add;
document.getElementById('send').addEventListener('click', () => {
  const text = composer.innerText.trim();
  if (!text) return;
  window.__sent.push(text);
  if (window.__holdSend) return;
  composer.textContent = '';
  add('user', text);
  if (!location.pathname.includes(CHAT_PATH)) history.pushState({}, '', CHAT_PATH_PREFIX + '%CHAT_ID%');
  const stop = document.createElement('button');
  STOP_ATTRS(stop);
  document.body.appendChild(stop);
  const full = window.__reply ? window.__reply(text) : 'ok';
  const el = add('assistant', '');
  let i = 0;
  const step = Math.max(1, Math.ceil(full.length / 3));
  const timer = setInterval(() => {
    i += step;
    el.textContent = full.slice(0, i);
    if (i >= full.length) { clearInterval(timer); stop.remove(); }
  }, 150);
});
"""

_FLAVORS = {
    "chatgpt": {
        "title": "ChatGPT",
        "head": "",
        "composer": '<div id="prompt-textarea" class="ProseMirror" contenteditable="true"></div>',
        "send": '<button id="send" type="button" data-testid="send-button" aria-label="Send prompt">Send</button>',
        "js": {
            "COMPOSER_SELECTOR": "'#prompt-textarea'",
            "CHAT_PATH": "'/c/'", "CHAT_PATH_PREFIX": "'/c/'", "WRAP_TAG": "'div'",
            "MARK_ROLE": "(el, role) => el.setAttribute('data-message-author-role', role)",
            "STOP_ATTRS": "(b) => { b.setAttribute('data-testid', 'stop-button'); b.setAttribute('aria-label', 'Stop streaming'); }",
        },
        "home": "https://chatgpt.com/",
        "chat": "https://chatgpt.com/c/" + CHAT_ID,
    },
    "claude": {
        "title": "Claude",
        "head": "",
        "composer": '<div contenteditable="true" data-testid="chat-input" class="ProseMirror"></div>',
        "send": '<button id="send" type="button" aria-label="Send message">Send</button>',
        "js": {
            "COMPOSER_SELECTOR": "'div[contenteditable=\"true\"]'",
            "CHAT_PATH": "'/chat/'", "CHAT_PATH_PREFIX": "'/chat/'", "WRAP_TAG": "'div'",
            "MARK_ROLE": ("(el, role) => { if (role === 'user') el.setAttribute('data-testid', 'user-message'); "
                          "else { el.setAttribute('data-testid', 'assistant-message'); el.className = 'font-claude-message'; } }"),
            "STOP_ATTRS": "(b) => { b.setAttribute('aria-label', 'Stop response'); }",
        },
        "home": "https://claude.ai/new",
        "chat": "https://claude.ai/chat/" + CHAT_ID,
    },
}


def _variant(base: str, **changes) -> dict:
    out = {**_FLAVORS[base], "js": {**_FLAVORS[base]["js"]}, "site": base}
    out["js"].update(changes.pop("js", {}))
    out.update(changes)
    return out


# ChatGPT layouts without the author attribute the first driver relied on: a turn headed by a hidden "ChatGPT said:"
# heading, and a bare layout with no markers and no <main> element at all.
_FLAVORS["chatgpt_headed"] = _variant("chatgpt", js={
    "WRAP_TAG": "'section'",
    "MARK_ROLE": ("(el, role, wrap) => { const h = document.createElement(role === 'user' ? 'h5' : 'h6'); "
                  "h.className = 'sr-only'; h.textContent = role === 'user' ? 'You said:' : 'ChatGPT said:'; "
                  "wrap.appendChild(h); if (role !== 'user') el.className = 'markdown prose'; }")})
_FLAVORS["chatgpt_bare"] = _variant("chatgpt", js={"MARK_ROLE": "(el, role, wrap) => {}"}, root="div")


def urls(flavor: str) -> dict[str, str]:
    return {"home": _FLAVORS[flavor]["home"], "chat": _FLAVORS[flavor]["chat"]}


def page_html(flavor: str, *, state: str = "ready") -> str:
    """``state``: ready | logged_out | challenge | limit."""
    f = _FLAVORS[flavor]
    consts = "".join(f"const {key} = {value};\n" for key, value in f["js"].items())
    script = consts + _SCRIPT.replace("%CHAT_ID%", CHAT_ID)
    banner = ""
    title = f["title"]
    if state == "logged_out":
        banner = ('<a href="/auth/login" data-testid="login-button">Log in</a>'
                  if f.get("site", flavor) == "chatgpt" else '<input type="email" name="email">')
    elif state == "challenge":
        title = "Just a moment..."
        banner = '<div id="challenge-form">Checking your browser</div>'
    elif state == "limit":
        banner = ('<div role="alert" data-testid="usage-limit">'
                  'You have reached your usage limit. Try again later.</div>')
    return (f"<!doctype html><html><head><title>{title}</title></head><body>{banner}"
            f'<{f.get("root", "main")}><div id="thread"></div></{f.get("root", "main")}>'
            f'<form>{f["composer"]}{f["send"]}</form>'
            f"<script>{script}</script></body></html>")


class FakeBrowser:
    def __init__(self, qapp) -> None:
        self.profile = QWebEngineProfile(qapp)  # off the record: nothing touches the disk
        self.page = QWebEnginePage(self.profile, qapp)
        self.seeds = []
        self.hold_send_flag = False
        self.disable_send_flag = False

    reply_js: str | None = None
    seeds: list[tuple[str, str]]
    hold_send_flag: bool
    disable_send_flag: bool

    def load(self, flavor: str, url: str, *, state: str = "ready") -> bool:
        """Every load is a fresh page (as a real navigation is); the reply and seeded messages are re-applied."""
        loop, ok = QEventLoop(), []
        self.page.loadFinished.connect(lambda good: (ok.append(good), loop.quit()))
        QTimer.singleShot(20000, loop.quit)
        self.page.setHtml(page_html(flavor, state=state), QUrl(url))
        loop.exec()
        loaded = bool(ok and ok[0])
        if loaded:
            if self.hold_send_flag:
                self.run("window.__holdSend = true; true")
            if self.disable_send_flag:
                self.run("document.getElementById('send').disabled = true; true")
            if self.reply_js:
                self.run(f"window.__reply = {self.reply_js}; true")
            for role, text in self.seeds:
                self._add(role, text)
        return loaded

    def hold_send(self, hold: bool = True) -> None:
        """The Send button keeps the prompt and starts no reply."""
        self.hold_send_flag = hold

    def disable_send(self, disabled: bool = True) -> None:
        """Send stays disabled, the way ChatGPT does after scripted typing."""
        self.disable_send_flag = disabled

    def run(self, script: str):
        loop, box = QEventLoop(), []
        self.page.runJavaScript(script, 0, lambda value: (box.append(value), loop.quit()))
        QTimer.singleShot(15000, loop.quit)
        loop.exec()
        assert box, "the page did not answer"
        return box[0]

    def set_reply(self, js_function: str) -> None:
        """``js_function`` is the source of ``(userText) => replyText``."""
        self.reply_js = js_function
        self.run(f"window.__reply = {js_function}; true")

    def seed(self, role: str, text: str) -> None:
        self.seeds.append((role, text))
        self._add(role, text)

    def _add(self, role: str, text: str) -> None:
        import json

        self.run(f"window.__add({json.dumps(role)}, {json.dumps(text)}); true")

    def sent(self) -> list[str]:
        import json

        return json.loads(self.run("JSON.stringify(window.__sent)"))

    def close(self) -> None:
        self.page.deleteLater()
        self.profile.deleteLater()
