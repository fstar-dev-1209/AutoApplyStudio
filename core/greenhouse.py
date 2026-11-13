"""Applying on a Greenhouse board: open the form, decide every answer, fill it, send it, confirm it was received.

The flow is deliberately cautious. Nothing is sent unless every required field has an answer we are entitled to give,
the filled form re-reads as complete, and no verification is waiting for a person. A job that fails any check is
reported with the reason and a screenshot, never half-sent.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from core.chat_questions import Question, clean_answer, match_option
from core.form_answers import CHAT, FILL, NEEDS, SKIP, Context, Decision, Field, decide
from core.form_filler import FillError, Files, fill_field, probe_combobox
from core.form_scan import ScanError, scan

SUBMITTED, DRY_RUN, NEEDS_YOU, HUMAN_CHECK, EXPIRED, FAILED = (
    "submitted", "dry_run", "needs_you", "human_check", "expired", "failed")

_EXPIRED = re.compile(
    r"no longer (available|accepting|open)|(job|position|posting|role) (is |has been )?(closed|filled|no longer|expired)|"
    r"not (currently )?accepting applications|page you (are|were) looking for|this job is not available", re.I)
_CONFIRMED = re.compile(
    r"thank you for (applying|your (application|interest))|application (has been )?(submitted|received)|"
    r"we('ve| have) received your application|your application (was|has been) (sent|submitted|received)", re.I)
_CAPTCHA_ERROR = re.compile(r"recaptcha|hcaptcha|captcha", re.I)
_FILE_REQUIRED = re.compile(r"(resume|\bcv\b|cover).{0,40}required|required.{0,40}(resume|\bcv\b|cover)", re.I)

_ROLE_NOISE = {"senior", "junior", "lead", "staff", "principal", "remote", "hybrid", "the", "and", "for", "with",
               "our", "west", "east", "north", "south", "united", "states", "usa", "all", "new", "team", "ii", "iii"}


def posting_matches(company: str, role: str, page_text: str, url: str = "") -> bool:
    """Is the posting on the page the one this job is for? True when at least half of the role's distinctive words
    appear in the page's title, or the company's name appears in the title or the board's address. With nothing
    to compare, it is not a reason to doubt. This catches a link or a resume that belongs to a different job."""
    words = [w for w in re.findall(r"[a-z0-9+#.]{3,}", (role or "").lower()) if w not in _ROLE_NOISE]
    text = re.sub(r"[^a-z0-9]+", " ", (page_text or "").lower())
    if words and sum(1 for w in words if re.sub(r"[^a-z0-9]+", " ", w).strip() in text) / len(words) >= 0.5:
        return True
    key = re.sub(r"[^a-z0-9]+", "", (company or "").lower())
    haystack = re.sub(r"[^a-z0-9]+", "", f"{page_text} {url}".lower())
    if len(key) >= 3 and key in haystack:
        return True
    return not words and len(key) < 3


AnswerFn = Callable[[Question], str]
CodeFn = Callable[[str], str]  # (company) -> code, '' when none arrived
Pause = Callable[[float, float], None]


def _sleep(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


@dataclass
class ApplyOutcome:
    state: str
    reason: str = ""
    plan: list[dict] = field(default_factory=list)  # one entry per field: what was decided and why
    needs: list[str] = field(default_factory=list)  # what stopped it
    confirmation: str = ""
    screenshot: str = ""
    url: str = ""


GREENHOUSE_BOARD_API = "https://boards-api.greenhouse.io/v1/boards"
_GENERIC_LABELS = {"www", "careers", "career", "jobs", "job", "apply", "join", "work", "boards", "com", "io", "co",
                   "net", "org", "ai", "us", "app"}


def board_guesses(company: str, page_url: str) -> list[str]:
    """Likely Greenhouse board names for a company that hosts the form on its own site (Toast -> 'toast').

    Only guesses: each one is confirmed against Greenhouse's public job API before it is used."""
    out: list[str] = []

    def add(name: str) -> None:
        name = (name or "").strip("-").lower()
        if len(name) >= 2 and name not in out and name not in _GENERIC_LABELS:
            out.append(name)

    words = [w for w in re.split(r"[^A-Za-z0-9]+", company or "") if w]
    if words:
        add("".join(words))
        add("-".join(words))
        add(words[0])
    host = (urlsplit(page_url).hostname or "").lower()
    for label in host.split("."):
        add(label)
    return out[:6]


class GreenhouseApplier:
    def __init__(
        self,
        *,
        answer: AnswerFn | None = None,
        snapshot_mail: Callable[[object], set[str]] | None = None,  # (browser context) -> the inbox as it is now
        fetch_code: Callable[[object, set[str], str], str] | None = None,  # (context, inbox before, company) -> code
        shots_dir: Path | None = None,
        human_wait_s: float = 600,
        notify: Callable[[str, str], None] = lambda title, body: None,
        should_stop: Callable[[], bool] = lambda: False,
        pause: Pause = _sleep,
        debug_log: Callable[[str], None] = lambda text: None,
        board_api: str = GREENHOUSE_BOARD_API,  # '' = never look a board up (tests without a network)
    ) -> None:
        self.answer, self.snapshot_mail, self.fetch_code = answer, snapshot_mail, fetch_code
        self.board_api = board_api
        self._company = ""
        self.shots_dir, self.human_wait_s, self.notify = shots_dir, human_wait_s, notify
        self.should_stop, self.pause, self.debug_log = should_stop, pause, debug_log
        self.remember: Callable[[str, list[str]], None] | None = None  # a required choice with no saved answer
        self._context = None

    # ------------------------------------------------------------------ the whole application

    def apply(self, context, url: str, *, form_ctx: Context, files: Files, submit: bool, tag: str = "job",
              company: str = "", role: str = "") -> ApplyOutcome:
        page = context.new_page()
        self._context = context
        outcome = None
        try:
            outcome = self._apply(page, url, form_ctx, files, submit, tag, company, role)
        except ScanError as exc:
            outcome = self._done(page, tag, "scan", FAILED, f"Could not read the form ({exc}).")
        except Exception as exc:  # noqa: BLE001 - one job's failure is recorded, never raised into the queue
            outcome = self._done(page, tag, "error", FAILED, f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}")
        finally:
            # Leave a filled form open so you can see it. A sent application must not close the last
            # browser page: that shuts Chrome down and takes this app with it.
            if outcome is None or outcome.state not in {NEEDS_YOU, DRY_RUN, HUMAN_CHECK}:
                self._release_page(context, page)
        return outcome

    def _apply(self, page, url, form_ctx, files, submit, tag, company, role="") -> ApplyOutcome:
        self._company = company
        status = self._open_form(page, url)
        if status:
            return self._done(page, tag, "open", EXPIRED if status == EXPIRED else FAILED,
                              "The posting is closed." if status == EXPIRED else status)
        if self._challenge_visible(page):
            if not self._wait_for_person(page, "A verification is showing on the application page"):
                return self._done(page, tag, "challenge", HUMAN_CHECK, "A human verification was not completed in time.")

        slow_page = False
        if not self._wait_until_interactive(page, self.INTERACTIVE_WAIT_MS):
            # The form is on screen but its scripts have not attached (a script that did not arrive over the proxy):
            # no dropdown opens and no file attaches on such a page. Load it once more before touching anything.
            self.debug_log(f"[{tag}] the form did not become usable in time; loading it again")
            try:
                page.reload(wait_until="domcontentloaded", timeout=90000)
            except Exception:  # noqa: BLE001
                pass
            slow_page = not self._wait_until_interactive(page, self.RELOAD_WAIT_MS)
        fields, meta = scan(page)
        page_title = ""
        try:
            page_title = page.title()
        except Exception:  # noqa: BLE001
            pass
        if not posting_matches(company, role, f"{meta.get('title', '')} {page_title}", page.url):
            return self._done(page, tag, "mismatch", NEEDS_YOU,
                              f"The posting on the page ('{str(meta.get('title') or page_title)[:80]}') does not look like "
                              f"{company or 'the company'} - {role or 'the role'}. Not filled.")
        notes: list[str] = []
        for f in fields:
            if f.kind == "combobox" and not f.value:
                f.options = probe_combobox(page, f, notes)  # a dropdown only lists its choices once opened
        dropdowns = [f for f in fields if f.kind == "combobox" and not f.value]
        if slow_page and len(dropdowns) >= 3 and not any(f.options for f in dropdowns):
            # Not one dropdown opened on a page whose scripts never attached: the page is dead, not the questions
            # unanswerable. That is a failed load to try again later, not something for you to answer.
            self.debug_log(f"[{tag}] {company} - {role} | {page.url}\n    " + "\n    ".join(notes[:3]))
            return self._done(page, tag, "open", FAILED,
                              "The application form loaded but never became usable (its scripts did not finish "
                              "loading). It will be tried again later.")
        if notes:  # how each dropdown had to be opened, or what it looked like when it would not open
            self.debug_log(f"[{tag}] {company} - {role} | {page.url}\n    " + "\n    ".join(notes))
        plan, needs, decisions = self._plan(fields, form_ctx)
        # Type every answer we have. A missing required answer stops the send, and the fields we could answer stay filled.
        for f, d in decisions:
            if self.should_stop():
                return self._done(page, tag, "stopped", FAILED, "Stopped before sending.", plan)
            try:
                fill_field(page, f, d, files, pause=self.pause)
            except FillError as exc:
                if f.required:
                    needs.append(str(exc))
            self.pause(0.2, 0.7)
        if needs:
            return self._done(page, tag, "needs", NEEDS_YOU, "; ".join(needs[:6]), plan, needs)

        # Greenhouse draws some required questions, such as Race, only after the other answers are in.
        late = self._fill_what_appeared_later(page, form_ctx, files, plan, notes)
        if late:
            return self._done(page, tag, "needs", NEEDS_YOU, "; ".join(late[:6]), plan, late)

        problems = self._missing_required(page, form_ctx)
        if problems:
            return self._done(page, tag, "verify", NEEDS_YOU, "Still empty after filling: " + "; ".join(problems[:6]), plan, problems)
        if not meta["has_submit"]:
            return self._done(page, tag, "nosubmit", FAILED, "The form has no submit button.", plan)
        if not submit:
            return self._done(page, tag, "dryrun", DRY_RUN, "Filled and checked; not sent (dry run).", plan)
        if self.should_stop():
            return self._done(page, tag, "stopped", FAILED, "Stopped before sending.", plan)
        return self._submit(page, tag, company, plan)

    # ------------------------------------------------------------------ planning

    def _plan(self, fields: list[Field], form_ctx: Context):
        plan, needs, todo = [], [], []
        for f in fields:
            d = decide(f, form_ctx)
            if d.action == CHAT:
                d = self._ask(f, form_ctx)
            entry = {"label": f.label, "kind": f.kind, "action": d.action, "source": d.source, "required": f.required,
                     "value": (d.value[:400] if d.action == FILL and f.kind != "file" else d.value[:40]),
                     "reason": d.reason}
            if d.action == NEEDS and f.options:
                entry["options"] = [o.label for o in f.options][:24]
            plan.append(entry)
            if d.action == NEEDS:
                needs.append(d.reason or f"Needs an answer: {f.label}")
                # A question we have no list for, or whose saved choice matched none of the options.
                # Questions that already have a dropdown on Your answers are not copied here.
                reason = d.reason or ""
                if (self.remember and f.options and len(f.options) <= 24
                        and (reason.startswith("Not covered") or reason.startswith("No option matches"))):
                    self.remember(f.label, [o.label for o in f.options])
            elif d.action == FILL:
                todo.append((f, d))
        return plan, needs, todo

    def _ask(self, f: Field, form_ctx: Context) -> Decision:
        if self.answer is None:
            return Decision(NEEDS if f.required else SKIP, reason=f"No answer for: {f.label}")
        choices = [o.label for o in f.options] if f.kind in {"select", "combobox", "radio", "checkgroup"} else []
        question = Question(f.label, options=choices, max_chars=f.max_chars,
                            kind="choice" if choices else "number" if f.kind == "number" else "text")
        try:
            raw = self.answer(question)
        except Exception as exc:  # noqa: BLE001
            return Decision(NEEDS if f.required else SKIP, reason=f"The chat could not answer '{f.label}': {exc}")
        text = clean_answer(raw, f.max_chars)
        if not text:
            return Decision(NEEDS if f.required else SKIP, reason=f"The chat gave no usable answer to: {f.label}")
        if choices:  # the answer must be one of the form's own options, word for word
            picked = match_option(text, choices)
            if not picked:
                return Decision(NEEDS if f.required else SKIP,
                                reason=f"The chat's answer '{text[:40]}' is not one of the options for: {f.label}")
            text = picked
        return Decision(FILL, text, "chat")

    # ------------------------------------------------------------------ opening

    def _open_form(self, page, url: str) -> str:
        """'' when the form is on screen; EXPIRED when the posting is gone; else a short reason."""
        resp = self._goto(page, url)
        if resp is not None and resp.status in {404, 410}:
            return EXPIRED
        for _ in range(3):
            if self._has_form(page):
                return ""
            src = page.evaluate(
                "() => { const f = Array.from(document.querySelectorAll('iframe')).find(i => "
                "/\\/embed\\/job_app/.test(i.src)); return f ? f.src : ''; }")
            if src:
                self._goto(page, src)
                continue
            embed = self._embed_from_company_page(page) or self._embed_from_board_lookup(page)
            if embed:
                self._goto(page, embed)
                continue
            try:
                body = page.inner_text("body", timeout=4000)
            except Exception:  # noqa: BLE001
                body = ""
            if _EXPIRED.search(body[:3000]):
                return EXPIRED
            self._dismiss_cookie_banner(page, body)
            button = page.locator("a, button").filter(has_text=re.compile(r"^\s*apply( now| for this job)?\s*$", re.I)).first
            if button.count() and button.is_visible():
                try:
                    button.click(timeout=5000)
                except Exception:  # noqa: BLE001 - something still covers it (a banner, a chat bubble): click it directly
                    button.evaluate("el => el.click()")
                page.wait_for_timeout(2500)
                continue
            break
        return "" if self._has_form(page) else "No application form was found on the page."

    _BOARD_JS = r"""() => {
      const urls = [...Array.from(document.scripts).map(x => x.src || ''),
                    ...Array.from(document.querySelectorAll('iframe, a[href]')).map(x => x.src || x.href || ''),
                    ...Array.from(document.scripts).map(x => (x.textContent || '').slice(0, 20000))];
      for (const u of urls) {
        const m = /greenhouse\.io\/(?:embed\/job_board(?:\/js)?|embed\/job_app)\?[^"'\s]*?\bfor=([A-Za-z0-9_-]+)/.exec(u);
        if (m) return m[1];
      }
      return '';
    }"""

    def _embed_from_company_page(self, page) -> str:
        """A company careers page with ``?gh_jid=`` hosts Greenhouse's form; when the page names its Greenhouse board,
        the form is opened straight from Greenhouse instead of through the company's own page and its pop-ups."""
        match = re.search(r"[?&]gh_jid=(\d+)", page.url or "")
        if not match:
            return ""
        try:
            board = page.evaluate(self._BOARD_JS)
        except Exception:  # noqa: BLE001
            return ""
        return f"https://job-boards.greenhouse.io/embed/job_app?for={board}&token={match.group(1)}" if board else ""

    def _embed_from_board_lookup(self, page) -> str:
        """For a company page with ``?gh_jid=`` that does not name its board (it runs a form of its own on top of
        Greenhouse): find the board by asking Greenhouse's public API about this job under a few likely names."""
        match = re.search(r"[?&]gh_jid=(\d+)", page.url or "")
        if not match or not self.board_api or "greenhouse.io" in (urlsplit(page.url).hostname or ""):
            return ""
        token = match.group(1)
        for board in board_guesses(self._company, page.url):
            try:
                resp = page.request.get(f"{self.board_api}/{board}/jobs/{token}", timeout=20000)
                if resp.ok and str(resp.json().get("id")) == token:
                    self.debug_log(f"[board] {self._company}: job {token} is on the Greenhouse board '{board}'")
                    return f"https://job-boards.greenhouse.io/embed/job_app?for={board}&token={token}"
            except Exception:  # noqa: BLE001 - a guess that cannot be checked is not used
                continue
        return ""

    _COOKIE_NO = re.compile(r"^\s*(i\s+)?(do\s+not\s+accept|don.?t\s+accept|decline( all)?|reject( all)?( cookies)?|"
                            r"deny|necessary\s+only|only\s+necessary|use\s+necessary\s+cookies\s+only)\s*$", re.I)
    _COOKIE_YES = re.compile(r"^\s*(i\s+)?(accept( all)?( cookies)?|agree|allow( all)?|got\s+it|ok(ay)?)\s*$", re.I)

    def _dismiss_cookie_banner(self, page, body: str) -> None:
        """Close a cookie-consent pop-up that covers the page, choosing the 'necessary only' answer when offered."""
        if "cookie" not in (body or "").lower():
            return
        for pattern in (self._COOKIE_NO, self._COOKIE_YES):
            try:
                button = page.locator("button, a[role=button], [role=button]").filter(has_text=pattern).first
                if button.count() and button.is_visible():
                    button.click(timeout=3000)
                    page.wait_for_timeout(800)
                    return
            except Exception:  # noqa: BLE001 - no banner to close, or it closed itself
                continue

    INTERACTIVE_WAIT_MS = 60000  # for the form's scripts to attach, before the page is loaded once more
    RELOAD_WAIT_MS = 90000

    _INTERACTIVE_JS = ("() => { const w = document.querySelector('input.select__input, [class*=\"select__control\"] input'); "
                       "if (!w) return true; return Object.keys(w).some(k => k.startsWith('__reactProps') || k.startsWith('__reactFiber')); }")

    @classmethod
    def _wait_until_interactive(cls, page, timeout_ms: int = 60000) -> bool:
        """The form's HTML shows up several seconds before its scripts attach (about 5-8 s versus 12-16 s on a real
        Greenhouse form); until then clicks do nothing. Wait for the dropdowns to be live, then a moment more."""
        try:
            page.wait_for_function(cls._INTERACTIVE_JS, timeout=timeout_ms)
        except Exception:  # noqa: BLE001 - not interactive in time
            return False
        page.wait_for_timeout(600)
        return True

    @staticmethod
    def _goto(page, url: str):
        """Open a page, waiting longer than a browser's default (a proxy can be slow) and trying once more on a timeout."""
        try:
            return page.goto(url, wait_until="domcontentloaded", timeout=90000)
        except Exception as exc:  # noqa: BLE001
            if "Timeout" not in type(exc).__name__ and "Timeout" not in str(exc):
                raise
            return page.goto(url, wait_until="commit", timeout=60000)  # the second try only needs the page to have started

    @staticmethod
    def _has_form(page) -> bool:
        try:
            page.wait_for_selector("#first_name, input[name*='first_name']", timeout=9000)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ checks

    def _fill_what_appeared_later(self, page, form_ctx: Context, files: Files, plan: list, notes: list[str]) -> list[str]:
        """Questions Greenhouse draws after an earlier answer, such as Race after Hispanic/Latino.

        A race list is filled from the saved answer even when the form does not mark it required.
        Returns reasons that still block sending."""
        quiet = 0
        for _ in range(4):
            fields, _meta = scan(page)
            pending = [f for f in fields if not (f.value or "").strip() and (
                f.required or f.kind in {"combobox", "select", "radio", "checkgroup"})]
            if not pending:
                quiet += 1
                if quiet >= 2:  # the race list is inserted a moment after Hispanic/Latino is set
                    return []
                page.wait_for_timeout(400)
                continue
            quiet = 0
            for f in pending:
                if f.kind == "combobox" and not f.options:
                    f.options = probe_combobox(page, f, notes)
            extra, found, decisions = self._plan(pending, form_ctx)
            plan.extend(extra)
            if found:
                return found
            if not decisions:
                return [f.label or f.ref for f in pending if f.required]
            for f, d in decisions:
                if self.should_stop():
                    return ["Stopped before sending."]
                try:
                    fill_field(page, f, d, files, pause=self.pause)
                except FillError as exc:
                    return [str(exc)]
                self.pause(0.2, 0.5)
        return []

    def _missing_required(self, page, form_ctx: Context) -> list[str]:
        """Required fields that read back empty (radio / checkbox fields read back as 'checked' when chosen)."""
        fields, _meta = scan(page)
        return [f.label or f.ref for f in fields if f.required and not f.value.strip()]

    @staticmethod
    def _challenge_visible(page) -> bool:
        try:
            return bool(page.evaluate(
                "() => Array.from(document.querySelectorAll('iframe')).some(i => /(recaptcha|hcaptcha|challenge)/i.test(i.src) && "
                "/(bframe|challenge)/i.test(i.src) && i.getBoundingClientRect().width > 150 && i.getBoundingClientRect().height > 150 "
                "&& getComputedStyle(i).visibility !== 'hidden')"))
        except Exception:  # noqa: BLE001
            return False

    def _wait_for_captcha_answer(self, page) -> bool:
        """The error text is up and the checkbox is still unchecked. Wait, then the caller presses Submit again."""
        self.notify("Verification needs you",
                    "Complete the reCAPTCHA in the bot browser. The application is sent again once you finish.")
        deadline = time.monotonic() + self.human_wait_s
        page.wait_for_timeout(1200)  # the message is still on the page; the first look is not "already solved"
        while time.monotonic() < deadline:
            if self._captcha_cleared(page) and not self._challenge_visible(page):
                return True
            page.wait_for_timeout(1500)
        return False

    @staticmethod
    def _captcha_cleared(page) -> bool:
        """Solved when the response token is filled, or when the captcha error itself has gone."""
        try:
            return bool(page.evaluate(
                """() => {
                    const token = document.querySelector(
                      'textarea[name="g-recaptcha-response"], textarea[name="h-captcha-response"], textarea[id^="g-recaptcha-response"]');
                    if (token && (token.value || '').trim().length > 10) return true;
                    const still = [...document.querySelectorAll('[role="alert"], .helper-text--error')]
                      .some(el => /recaptcha|hcaptcha|captcha/i.test(el.innerText || ''));
                    return !still;
                }"""))
        except Exception:  # noqa: BLE001
            return False

    def _wait_for_person(self, page, why: str) -> bool:
        """A captcha is for a person: alert, then wait for them to finish it in this very tab."""
        self.notify("Verification needs you", f"{why}. Complete it in the bot browser; the application continues by itself.")
        deadline = time.monotonic() + self.human_wait_s
        while time.monotonic() < deadline:
            if not self._challenge_visible(page):
                return True
            page.wait_for_timeout(1500)
        return False

    # ------------------------------------------------------------------ sending

    def _submit(self, page, tag, company, plan) -> ApplyOutcome:
        # Most forms never ask for an emailed code, so an inbox that will not open must not stop the application.
        # It only matters if the code step appears, and then it is reported as what it is.
        before, mail_problem = set(), ""
        if self.snapshot_mail:
            for attempt in (1, 2):
                try:
                    before, mail_problem = self.snapshot_mail(self._context), ""
                    break
                except Exception as exc:  # noqa: BLE001
                    mail_problem = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
            if mail_problem:
                self.debug_log(f"[{tag}] the mailbox could not be opened before sending ({mail_problem}); sending anyway")
        page.locator('[data-aas="submit"]').first.click(timeout=8000)
        deadline = time.monotonic() + 60
        code_tried = False
        captcha_retries = 0
        file_retries = 0
        while time.monotonic() < deadline:
            try:
                page.wait_for_timeout(900)
            except Exception:  # noqa: BLE001 - the page closed because the application was accepted
                if code_tried:
                    return ApplyOutcome(SUBMITTED, "Application received.", plan or [], [], "", "")
                raise
            if self._confirmed(page):
                return self._done(page, tag, "sent", SUBMITTED, "Application received.", plan,
                                  confirmation=self._confirmation_text(page))
            if self._code_step(page):
                if self.fetch_code is None:
                    return self._done(page, tag, "code", NEEDS_YOU,
                                      "Greenhouse asked for an emailed security code that could not be read.", plan)
                if code_tried:
                    # A correct code clears the boxes and leaves this page a few seconds later. Seeing the
                    # boxes once more is not a failure to read the mail; keep waiting for the page to move on.
                    continue
                code_tried = True
                if mail_problem:
                    # Without knowing which mails were already there, an older code could be mistaken for this one.
                    return self._done(page, tag, "code", NEEDS_YOU,
                                      "Greenhouse emailed a security code, but the Outlook inbox would not open in the "
                                      "bot browser. Open Outlook there, sign in if asked, and enter the code.", plan)
                try:
                    code = self.fetch_code(self._context, before, company)
                except Exception as exc:  # noqa: BLE001 - the inbox stopped answering while waiting for the mail
                    self.debug_log(f"[{tag}] reading the code mail failed: {type(exc).__name__}: {exc}")
                    code = ""
                if not code:
                    return self._done(page, tag, "code", NEEDS_YOU, "The security code email did not arrive in time.", plan)
                self.debug_log(f"[{tag}] security code read from mail; submitting it")
                try:
                    self._enter_code(page, code)
                except Exception as exc:  # noqa: BLE001 - the click can close the page once the code is accepted
                    self.debug_log(f"[{tag}] code submit: {type(exc).__name__}: {exc}")
                deadline = max(deadline, time.monotonic() + 20)
                continue
            if code_tried:
                return self._done(page, tag, "sent", SUBMITTED, "Application received.", plan,
                                  confirmation=self._confirmation_text(page))
            if self._challenge_visible(page):
                if not self._wait_for_person(page, "A verification appeared while sending the application"):
                    return self._done(page, tag, "challenge", HUMAN_CHECK, "A human verification was not completed in time.", plan)
                deadline = max(deadline, time.monotonic() + 45)
                continue
            try:
                _, meta = scan(page) if self._on_form(page) else ([], {"errors": []})
            except Exception:  # noqa: BLE001 - navigation after the code replaced the page
                if code_tried:
                    return self._done(page, tag, "sent", SUBMITTED, "Application received.", plan)
                raise
            if meta["errors"]:
                # "Please complete the reCAPTCHA and resubmit" is the checkbox, which is too small to count as
                # the challenge popup. Wait for the person, then press Submit again. Do not solve it.
                if captcha_retries < 1 and any(_CAPTCHA_ERROR.search(e) for e in meta["errors"]):
                    captcha_retries += 1
                    if not self._wait_for_captcha_answer(page):
                        return self._done(page, tag, "challenge", HUMAN_CHECK,
                                          "A human verification was not completed in time.", plan)
                    page.locator('[data-aas="submit"]').first.click(timeout=8000)
                    deadline = max(deadline, time.monotonic() + 45)
                    continue
                # The resume upload can finish in the moment after Submit. Once the file name is up, send once more.
                if (file_retries < 1 and page.locator(".file-upload__filename").count()
                        and any(_FILE_REQUIRED.search(e) for e in meta["errors"])):
                    file_retries += 1
                    page.locator('[data-aas="submit"]').first.click(timeout=8000)
                    deadline = max(deadline, time.monotonic() + 20)
                    continue
                return self._done(page, tag, "errors", NEEDS_YOU, "The site rejected the form: " + "; ".join(meta["errors"][:4]), plan)
        if code_tried and self._code_step(page):
            self.debug_log(f"[{tag}] security code form still showing after submit")
            return self._done(page, tag, "code", NEEDS_YOU, "The security code was not accepted.", plan)
        return self._done(page, tag, "unknown", NEEDS_YOU,
                          "No confirmation appeared after sending; the application may or may not have gone through.", plan)

    @staticmethod
    def _on_form(page) -> bool:
        try:
            return bool(page.evaluate("() => !!document.querySelector('form [data-aas]')"))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _confirmed(page) -> bool:
        """Received only when the address says so, or the thank-you text appears AND the form is gone.

        The text alone is not enough: job posts often begin with "Thank you for your interest in ..." while the form
        is still on the page, and marking a job applied on that would be wrong."""
        try:
            if re.search(r"/confirmation\b", page.url):
                return True
            form_still_there = page.evaluate(
                "() => !!document.querySelector('#first_name, input[name*=\"first_name\"]')")
            return not form_still_there and bool(_CONFIRMED.search(page.inner_text("body", timeout=3000)[:4000]))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _confirmation_text(page) -> str:
        try:
            m = _CONFIRMED.search(page.inner_text("body", timeout=3000))
            return m.group(0) if m else page.url
        except Exception:  # noqa: BLE001
            return page.url

    @staticmethod
    def _code_step(page) -> bool:
        try:
            return bool(page.evaluate(
                "() => Array.from(document.querySelectorAll('input[id^=\"security-input\"], "
                "input[name*=\"security_code\" i], input[aria-label*=\"security code\" i]')).some(i => "
                "{ const r = i.getBoundingClientRect(); return r.width > 0 && r.height > 0 && "
                "getComputedStyle(i).visibility !== 'hidden'; })"))
        except Exception:  # noqa: BLE001
            return False

    def _enter_code(self, page, code: str) -> None:
        first = page.locator('input[id^="security-input"], input[name*="security_code" i], input[aria-label*="security code" i]').first
        first.click(timeout=5000)
        page.keyboard.type(code, delay=random.randint(60, 120))  # the boxes advance focus on their own
        self.pause(0.5, 1.2)
        button = page.locator("button").filter(has_text=re.compile(r"submit|verify|continue", re.I)).last
        if button.count() and button.is_visible() and button.is_enabled():
            button.click(timeout=5000)

    # ------------------------------------------------------------------ finishing

    def _done(self, page, tag, stage, state, reason, plan=None, needs=None, *, confirmation="") -> ApplyOutcome:
        shot = ""
        if self.shots_dir is not None:
            try:
                self.shots_dir.mkdir(parents=True, exist_ok=True)
                path = self.shots_dir / f"{re.sub(r'[^A-Za-z0-9_-]+', '_', tag)}-{stage}.png"
                page.screenshot(path=str(path), full_page=False, timeout=8000)
                shot = str(path)
            except Exception:  # noqa: BLE001 - a missing screenshot must not hide the outcome
                shot = ""
        return ApplyOutcome(state, reason, plan or [], needs or ([reason] if state == NEEDS_YOU else []),
                            confirmation, shot, getattr(page, "url", ""))

    @staticmethod
    def _release_page(context, page) -> None:
        """Close the application tab. The last tab stays, blank, so the browser process stays up."""
        try:
            others = [p for p in context.pages if p is not page]
        except Exception:  # noqa: BLE001
            others = []
        try:
            if others:
                page.close()
            else:
                page.goto("about:blank", wait_until="domcontentloaded", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
