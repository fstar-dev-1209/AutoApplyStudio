"""Finding the emailed security code Greenhouse asks for, in the Outlook inbox open in the bot browser.

The reading is done in the signed-in browser (no mailbox password is ever stored). The parsing is plain text work and
is tested on its own; the page selectors for Outlook's message list are in one place at the top of the reader.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

# Words that look like a code to a pattern but are ordinary text in these emails.
_STOP = {"security", "code", "password", "complete", "continue", "greenhouse", "required", "applying"}
_CODE = re.compile(r"\b[A-Za-z0-9]{8}\b")
_PHRASE = re.compile(r"security code|verification code|verify your (email|application)|copy and paste this code", re.I)


def looks_like_code_mail(text: str) -> bool:
    return bool(_PHRASE.search(text or "")) and bool(re.search(r"greenhouse|application", text or "", re.I))


def parse_security_code(text: str) -> str:
    """The 8-character code in a Greenhouse 'security code' email (or its preview line), else ''."""
    flat = re.sub(r"\s+", " ", text or "")
    start = _PHRASE.search(flat)
    scope = flat[start.start():] if start else flat
    best, best_rank = "", 0
    for token in _CODE.findall(scope):  # in reading order; a stronger-looking token beats an earlier weak one
        rank = _code_rank(token)
        if rank > best_rank:
            best, best_rank = token, rank
            if rank == 3:
                break
    return best


def _code_rank(token: str) -> int:
    """3 = has a digit, 2 = mixed case with a capital inside, 1 = all capitals; 0 = looks like an ordinary word."""
    if token.lower() in _STOP:
        return 0
    if any(c.isdigit() for c in token):
        return 3
    if token[1:] != token[1:].lower() and token[1:] != token[1:].upper():
        return 2
    return 1 if token.isupper() else 0


# --- Outlook on the web (outlook.live.com / outlook.office.com). Selectors live here only. ---------------------
ROW_SELECTOR = '[role="option"][aria-label], div[role="listbox"] [role="option"], [data-convid]'
BODY_SELECTOR = '[aria-label="Message body"], [role="document"], div[id^="UniqueMessageBody"], [role="main"]'
OUTLOOK_HOME = "https://outlook.live.com/mail/0/inbox"


_ROW_JS = """() => {
  const nodes = Array.from(document.querySelectorAll(
    '[role="option"][aria-label], div[role="listbox"] [role="option"], [data-convid]')).slice(0, 40);
  return nodes.map((n) => (n.getAttribute('aria-label') || n.innerText || '').replace(/\\s+/g, ' ').trim())
    .filter(Boolean);
}"""


def _rows(page) -> list[tuple[str, object]]:
    """The visible inbox rows. One page read, so a heavy Outlook page cannot stall the app row by row."""
    try:
        texts = page.evaluate(_ROW_JS) or []
    except Exception:  # noqa: BLE001 - a closed or navigating page is treated as an empty inbox this pass
        return []
    rows = page.locator(ROW_SELECTOR)
    return [(text, rows.nth(i)) for i, text in enumerate(texts) if text]


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def company_matches(company: str, mail_text: str) -> bool:
    """True when the mail's subject/preview names this company (punctuation and 'Inc.'-style endings tolerated).

    A name of three letters, such as ESO, matches only as its own word. A shorter name matches nothing, so a fragment
    cannot land on the wrong mail."""
    mail, whole = _key(mail_text), _key(company)
    if len(whole) >= 4 and whole not in _SUFFIXES and whole in mail:
        return True
    words = [w for w in re.split(r"[^A-Za-z0-9]+", company or "") if len(w) >= 3 and w.lower() not in _SUFFIXES]
    if not words:
        return False
    if len(words[0]) < 4:
        return words[0].lower() in {w.lower() for w in re.findall(r"[A-Za-z0-9]+", mail_text or "")}
    return _key(words[0]) in mail


_SUFFIXES = {"inc", "corp", "corporation", "company", "group", "limited", "holdings", "technologies", "labs"}


def open_inbox(context, home: str = OUTLOOK_HOME):
    """The Outlook inbox tab, opened or reloaded until its message list shows.

    Raises MailUnavailable (saying what the tab showed instead) when the list never appears: signed out, a tab left
    on some other Outlook screen, or a page that did not finish loading behind a slow proxy."""
    page = next((p for p in context.pages if not p.is_closed() and "outlook." in (p.url or "")), None)
    fresh = page is None
    if fresh:
        page = context.new_page()
    last = ""
    for attempt in range(3):
        try:
            if fresh or attempt:  # first use the tab as it is; if the list is not there, load the inbox again
                page.goto(home, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector(ROW_SELECTOR, timeout=20000 if attempt == 0 and not fresh else 45000)
            return page
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}"
    try:
        where = f"{page.url[:80]} | {page.title()[:60]}"
    except Exception:  # noqa: BLE001
        where = "the tab did not answer"
    raise MailUnavailable(f"The Outlook inbox did not show its message list ({last}; tab: {where}).")


class MailUnavailable(RuntimeError):
    """The mailbox could not be read in the bot browser."""



def snapshot(context, home: str = OUTLOOK_HOME) -> set[str]:
    """The messages in the inbox right now, so a code mail that arrives later is told apart from older ones."""
    page = open_inbox(context, home)
    return {text for text, _ in _rows(page)}


def fetch_code(
    context,
    *,
    before: set[str],
    company: str = "",
    timeout_s: float = 600,  # these mails have been seen to take five minutes to reach Outlook
    poll_s: float = 4,
    home: str = OUTLOOK_HOME,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] | None = None,
    shot_path: str = "",
) -> str:
    """Wait for a new security-code mail and return its code ('' if none arrived in time)."""
    page = open_inbox(context, home)
    started = time.monotonic()
    deadline = started + timeout_s
    last_reload = started
    while time.monotonic() < deadline:
        fresh = [(t, r) for t, r in _rows(page) if t not in before and looks_like_code_mail(t)]
        # The subject names the company. Only that company's mail is trusted; if nothing matches by half-time and
        # exactly one new code mail exists, it is almost certainly ours (the site may spell the company differently).
        mine = [tr for tr in fresh if company_matches(company, tr[0])] if company else fresh
        if not mine and len(fresh) == 1 and time.monotonic() - started > timeout_s / 2:
            mine = fresh
        for text, row in mine:
            code = parse_security_code(text)
            if not code:
                try:
                    row.click(timeout=4000)
                    page.wait_for_timeout(1200)
                    code = parse_security_code(page.locator(BODY_SELECTOR).first.inner_text(timeout=4000))
                except Exception:  # noqa: BLE001
                    code = ""
            if code:
                return code
        if time.monotonic() - last_reload > 25:  # the list normally updates itself; a reload is the safety net
            try:
                page.reload(wait_until="domcontentloaded", timeout=20000)
                page.wait_for_selector(ROW_SELECTOR, timeout=15000)
            except Exception:  # noqa: BLE001 - a reload that fails leaves the list we already have
                pass
            last_reload = time.monotonic()
        sleep(poll_s)
    if log is not None:  # what the inbox showed, so a reader that missed the mail can be fixed
        try:
            rows = [t for t, _ in _rows(page)]
            new = [t for t in rows if t not in before]
            log(f"[mail] no code for {company!r} after {timeout_s:g}s | tab {page.url[:70]} | {len(rows)} row(s), "
                f"{len(new)} new, {sum(looks_like_code_mail(t) for t in new)} look like a code mail\n    "
                + "\n    ".join(t[:110] for t in (new or rows)[:8]))
            if shot_path:
                page.screenshot(path=shot_path, timeout=10000)
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            log(f"[mail] no code for {company!r}; the inbox could not be described ({type(exc).__name__})")
    return ""


class OutlookMail:
    """What the Greenhouse applier needs from a mailbox: look before sending, then wait for the code mail."""

    def __init__(self, home: str = OUTLOOK_HOME, log: Callable[[str], None] | None = None,
                 shot_path: str = "") -> None:
        self.home, self.log, self.shot_path = home, log, shot_path

    def snapshot(self, context) -> set[str]:
        return snapshot(context, self.home)

    def fetch(self, context, before: set[str], company: str) -> str:
        return fetch_code(context, before=before, company=company, home=self.home, log=self.log,
                          shot_path=self.shot_path)
