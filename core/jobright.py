"""Reading new postings from Jobright in the signed-in browser.

The recommend list only discovers jobs (id, rough title, when it was posted). The posting itself is read by opening
that job's own Jobright page, the same way the browser extension does: embedded ``jobResult`` / ``companyResult``
when it still matches the heading on screen, otherwise the visible sections. The employer's apply link comes off
that page too. The employer site is where the bot applies later, not where the job text comes from.

``scripts/capture_jobright.py`` saves what the live list actually sends, so a renamed list field is a small fix in
the tables below.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from core import ats
from core.discovery import Listing

JOBRIGHT_HOME = "https://jobright.ai"
DEFAULT_URL = "https://jobright.ai/jobs/recommend"


class LoginRequired(RuntimeError):
    """The browser is not signed in to Jobright."""


class HumanCheck(RuntimeError):
    """The site is showing a captcha / verification page that only a person should complete."""


# ---------------------------------------------------------------------------- records in JSON

_ID = ("jobId", "job_id", "id", "jobid")
_TITLE = ("jobTitle", "job_title", "title", "positionTitle")
_COMPANY = ("companyName", "company_name", "company", "employer")
_LOCATION = ("jobLocation", "job_location", "location")
_APPLY = ("applyLink", "apply_link", "applyUrl", "apply_url", "originalUrl", "original_url", "jobUrl", "externalUrl")
_POSTED = ("publishTime", "publish_time", "postedAt", "posted_at", "postDate", "datePosted", "publishedAt", "createdAt")
_SALARY = ("salaryDesc", "salary_desc", "salary", "salaryText", "compensation")
_DESC = ("jobSummary", "jobDescription", "job_description", "description", "summary")


def _first(d: dict, keys: Iterable[str]) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _text(v: Any) -> str:
    if isinstance(v, dict):
        v = _first(v, ("name", "companyName", "displayName", "value"))
    return str(v).strip() if isinstance(v, (str, int, float)) else ""


def _looks_like_job(d: dict, *, needs_company: bool = True) -> bool:
    if not (_text(_first(d, _TITLE)) and _first(d, _ID) is not None):
        return False
    return not needs_company or _first(d, _COMPANY) is not None or _first(d, _APPLY) is not None


def to_datetime(value: Any, now: datetime | None = None) -> datetime | None:
    """Epoch (s or ms), ISO text, or Jobright-style "3 hours ago" / "just now" / "today" -> aware datetime."""
    now = now or datetime.now(timezone.utc)
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d{9,14}", value.strip())):
        n = float(value)
        try:
            return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().lower()
    if text in {"just now", "now", "today", "moments ago"}:
        return now
    m = re.fullmatch(r"(?:about\s+|over\s+)?(\d+|an?)\s*(second|minute|min|hour|hr|day|week)s?\s*ago", text)
    if m:
        n = 1 if m.group(1) in {"a", "an"} else int(m.group(1))
        unit = {"second": "seconds", "minute": "minutes", "min": "minutes", "hour": "hours", "hr": "hours",
                "day": "days", "week": "weeks"}[m.group(2)]
        return now - timedelta(**{unit: n})
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _listing_from(job: dict, company_block: Any = None) -> Listing:
    ident = str(_first(job, _ID))
    company = _text(_first(job, _COMPANY)) or _text(_first(company_block, _COMPANY) if isinstance(company_block, dict) else "")
    apply = _text(_first(job, _APPLY))
    source_url = f"{JOBRIGHT_HOME}/jobs/info/{ident}" if ident and not ident.startswith("http") else ""
    if apply and urlsplit(apply).hostname and urlsplit(apply).hostname.endswith("jobright.ai"):
        source_url, apply = source_url or apply, ""  # a link back into Jobright is not the employer's page
    posted = to_datetime(_first(job, _POSTED))
    return Listing(
        external_id=ident, title=_text(_first(job, _TITLE)), company=company,
        location=_text(_first(job, _LOCATION)), source_url=source_url, apply_url=apply,
        posted_at=posted.isoformat() if posted else str(_first(job, _POSTED) or ""),
        description=_text(_first(job, _DESC)) if len(_text(_first(job, _DESC))) > 2000 else "",  # a short blurb is not the posting
        salary_text=_text(_first(job, _SALARY)),
    )


def extract_listings(payload: Any) -> list[Listing]:
    """Every job-shaped record anywhere in ``payload`` (nested ``jobResult`` + ``companyResult`` pairs included)."""
    found: dict[str, Listing] = {}

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            inner = node.get("jobResult")
            if isinstance(inner, dict) and _looks_like_job(inner, needs_company=False):
                listing = _listing_from(inner, node.get("companyResult"))
                found.setdefault(listing.external_id, listing)
            elif _looks_like_job(node):
                listing = _listing_from(node)
                found.setdefault(listing.external_id, listing)
            for value in node.values():
                if isinstance(value, (list, dict)):
                    visit(value)

    visit(payload)
    return list(found.values())


# The extension drops Jobright's own name when the page offers it as the employer.
_NOT_A_COMPANY = re.compile(r"^(jobright|hiring\s?cafe)\b", re.I)
_TRACKING = {"jr_id", "jr_src", "utm_source", "utm_medium", "utm_campaign"}
_SECTION = re.compile(r"(?m)^(Summary|Responsibilities|Qualifications|Benefits|Company|Required|Preferred)\s*$")
_MONEY_SYMBOL = {"USD": "$", "CAD": "CA$", "GBP": "£", "EUR": "€", "AUD": "A$", "INR": "₹"}


def is_full_posting(text: str) -> bool:
    """True when ``text`` is the sectioned posting the extension builds, not a one-line list blurb.

    A short role can still be the real posting (a summary and a few bullets). The queue must not throw that away
    and replace it with whatever the employer page happens to show."""
    return bool(_SECTION.search(text or ""))


def without_tracking(href: str) -> str:
    """The address without Jobright's and the usual campaign parameters. The rest of the query stays."""
    raw = (href or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return raw
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in _TRACKING]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def _is_jobright_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "jobright.ai" or host.endswith(".jobright.ai")


def _one(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clean_company(name: str) -> str:
    text = _one(name)
    return "" if _NOT_A_COMPANY.match(text) else text


def _bullets(items: Any) -> str:
    """One bullet per item. A run-on item joined by '; ' becomes one bullet each, as the extension does."""
    lines: list[str] = []
    for item in items or []:
        for part in re.split(r";\s+(?=[a-z])", str(item or "")):
            text = _one(part)
            if text:
                lines.append("• " + text)
    return "\n".join(lines)


def _block(title: str, body: str) -> str:
    body = (body or "").strip()
    return f"{title}\n{body}" if body else ""


def _clean_text(text: str) -> str:
    text = (text or "").replace("\u00a0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _money(amount: Any, currency: str = "USD") -> str:
    try:
        number = float(amount)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    symbol = _MONEY_SYMBOL.get(currency or "USD", "$")
    return symbol + f"{int(round(number)):,}"


def _salary_from_numbers(low: Any, high: Any, currency: str = "USD") -> str:
    """'$150,000 - $180,000/yr' from Jobright's numeric range. The extension treats that range as yearly."""
    left, right = _money(low, currency), _money(high, currency)
    if left and right and left != right:
        return f"{left} - {right}/yr"
    return f"{left or right}/yr" if (left or right) else ""


def _skills(items: Any) -> str:
    names: list[str] = []
    for item in items or []:
        if isinstance(item, dict):
            name = item.get("skill") or item.get("name") or ""
        else:
            name = item
        text = _one(name)
        if text:
            names.append(text)
    return ", ".join(names)


def compose_from_data(ds: dict) -> dict[str, str]:
    """Title, company, salary, apply link and the full description, from one Jobright ``jobResult``.

    This is the extension's ``jobrightFromData``: summary, responsibilities, required and preferred
    qualifications, skills, benefits and the company notes, each under its own heading.
    """
    job = ds.get("jobResult") if isinstance(ds.get("jobResult"), dict) else {}
    company_block = ds.get("companyResult") if isinstance(ds.get("companyResult"), dict) else {}
    quals = job.get("qualifications") if isinstance(job.get("qualifications"), dict) else {}
    title = _one(job.get("jobTitle"))
    company = _clean_company(_one(company_block.get("companyName")) or _one(job.get("companyName")))
    facts = " · ".join(part for part in (
        _one(job.get("workModel")) or ("Remote" if job.get("isRemote") else ""),
        _one(job.get("employmentType")),
        _one(job.get("jobSeniority")),
        _one(job.get("jobLocation")),
        f"{_one(job.get('minYearsOfExperience'))}+ years experience" if job.get("minYearsOfExperience") else "",
    ) if part)
    salary = _one(job.get("salaryDesc")) or _salary_from_numbers(job.get("minSalary"), job.get("maxSalary"))
    skills = _skills(job.get("jdCoreSkills"))
    categories = company_block.get("companyCategories")
    company_lines = "\n".join(part for part in (
        _one(company_block.get("companyDesc")),
        _one(company_block.get("gptShortDescription"))
        if _one(company_block.get("gptShortDescription")) not in {"", _one(company_block.get("companyDesc"))} else "",
        f"Size: {_one(company_block.get('companySize'))}" if company_block.get("companySize") else "",
        f"Founded: {_one(company_block.get('companyFoundYear'))}" if company_block.get("companyFoundYear") else "",
        f"Headquarters: {_one(company_block.get('companyLocation'))}" if company_block.get("companyLocation") else "",
        f"Website: {_one(company_block.get('companyURL'))}" if company_block.get("companyURL") else "",
        f"Industries: {', '.join(str(categories).split(','))}" if categories else "",
        f"Stage: {_one(company_block.get('fundraisingCurrentStage'))}" if company_block.get("fundraisingCurrentStage") else "",
    ) if part)
    required = "\n".join(part for part in (
        _bullets(quals.get("mustHave")), _bullets(job.get("educationSummaries"))) if part)
    qualifications = "\n\n".join(part for part in (
        _block("Required", required),
        _block("Preferred", _bullets(quals.get("preferredHave"))),
        f"Key skills: {skills}" if skills else "",
    ) if part)
    description = _clean_text("\n\n".join([
        f"{title}{' — ' + company if company else ''}",
        facts,
        f"Salary: {salary}" if salary else "",
        "",
        _block("Summary", _one(job.get("jobSummary"))),
        _block("Responsibilities", _bullets(job.get("coreResponsibilities"))),
        _block("Qualifications", qualifications),
        _block("Benefits", _bullets(job.get("benefitsSummaries"))),
        _block("Company", company_lines),
    ]))
    return {
        "title": title,
        "company": company,
        "salary": salary,
        "apply": without_tracking(_one(job.get("applyLink") or job.get("originalUrl") or "")),
        "description": description,
        "location": _one(job.get("jobLocation")),
        "posted": _one(job.get("publishTime") or job.get("postedAt") or ""),
    }


def compose_from_page(raw: dict) -> dict[str, str]:
    """What the extension reads off the screen when the embedded data is missing or belongs to another job."""
    title = _one(raw.get("title"))
    company = _clean_company(raw.get("company") or "")
    sections = [_block(str(s.get("title") or ""), str(s.get("body") or "")) for s in (raw.get("sections") or [])]
    sections = [s for s in sections if s]
    company_text = str(raw.get("company_text") or "").strip()
    if company_text:
        sections.append(_block("Company", company_text))
    description = _clean_text("\n\n".join([f"{title}{' — ' + company if company else ''}", *sections]))
    return {
        "title": title, "company": company, "salary": _one(raw.get("salary")),
        "apply": without_tracking(_one(raw.get("apply"))), "description": description,
        "location": _one(raw.get("location")), "posted": "",
    }


def usable_description(text: str) -> str:
    """The posting text worth saving. A title line on its own is not a description."""
    text = (text or "").strip()
    if is_full_posting(text) or len(text) >= 200:
        return text
    return ""


def listing_from_detail(base: Listing, raw: dict | None) -> Listing:
    """``base`` (from the list) with the detail page's title, company, salary, description and apply link on top.

    The list keeps the id and the posted time. A field the page did not have stays as the list gave it. A list
    blurb is not the description: only the page's posting, or a list field long enough to already be the posting.
    """
    raw = raw or {}
    kind = raw.get("kind")
    built: dict[str, str] = {}
    if kind == "data" and isinstance(raw.get("ds"), dict):
        built = compose_from_data(raw["ds"])
    elif kind == "page":
        built = compose_from_page(raw)
    description = usable_description(built.get("description", "")) or (
        base.description if len(base.description) > 2000 else "")
    apply = built.get("apply") or ""
    if _is_jobright_url(apply):
        apply = ""  # the list's employer link, if it has one, is applied by the caller after the page is searched
    return Listing(
        external_id=base.external_id,
        title=(built.get("title") or "").strip() or base.title,
        company=(built.get("company") or "").strip() or _clean_company(base.company),
        location=(built.get("location") or "").strip() or base.location,
        source_url=base.source_url,
        apply_url=apply,
        posted_at=base.posted_at or built.get("posted") or "",
        description=description,
        salary_text=(built.get("salary") or "").strip() or base.salary_text,
    )


def is_recent(listing: Listing, hours: float, now: datetime | None = None) -> bool:
    """False only when the posting is known to be older than ``hours``; an unreadable date keeps it."""
    posted = to_datetime(listing.posted_at, now)
    if posted is None:
        return True
    return (now or datetime.now(timezone.utc)) - posted <= timedelta(hours=hours)


# ---------------------------------------------------------------------------- the page

@dataclass
class ReadResult:
    listings: list[Listing] = field(default_factory=list)
    pages_scrolled: int = 0
    from_json: int = 0
    from_dom: int = 0
    trace: list[str] = field(default_factory=list)  # what the page showed and sent: the answer to "why nothing?"


_BLOCK_WORDS = ("verify you are human", "are you a robot", "unusual traffic", "captcha")
_LOGIN_PATH = re.compile(r"/(login|signin|sign-in|sign_in|signup|sign-up|register)(/|$|\?)", re.I)


def page_state(page) -> str:
    """'' when the page is a normal Jobright page, 'login' when signed out, 'check' for a human-verification page."""
    url = page.url or ""
    if _LOGIN_PATH.search(urlsplit(url).path) or "accounts.google.com" in url:
        return "login"
    try:
        text = (page.inner_text("body", timeout=3000) or "")[:4000].lower()
    except Exception:  # noqa: BLE001
        return ""
    if any(w in text for w in _BLOCK_WORDS) or page.query_selector("iframe[src*='captcha'], iframe[src*='challenge']"):
        return "check"
    if page.query_selector("input[type=password]") and "sign in" in text:
        return "login"
    return ""


def _open_list_page(context, url: str, host_hint: str, on_response, attempts: int = 3):
    """The Jobright tab, opened (again) until the list page loads.

    A slow proxy, a page that was mid-navigation ("frame was detached"), or a tab closed out from under us all fail
    the first try now and then; each retry starts on a fresh tab. The response listener is attached before
    navigation, so the job list that arrives with the page is kept."""
    page = _existing_page(context, host_hint) or context.new_page()
    last: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            try:
                page.remove_listener("response", on_response)
                page.close()
            except Exception:  # noqa: BLE001 - an already-closed tab is fine
                pass
            page = context.new_page()
        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return page
        except Exception as exc:  # noqa: BLE001
            last = exc
            if _usable_after_error(page, host_hint):
                return page  # the page itself arrived; only the load event was slow
            time.sleep(3 + 4 * attempt)
    assert last is not None
    raise last


def _usable_after_error(page, host_hint: str) -> bool:
    """True when a navigation 'failed' but the Jobright page is in fact showing (a timed-out load event)."""
    try:
        if page.is_closed() or host_hint not in (page.url or ""):
            return False
        return bool(page.evaluate("() => document.readyState !== 'loading' && !!document.body "
                                  "&& document.body.innerText.length > 200"))
    except Exception:  # noqa: BLE001
        return False


LIST_API = "/swan/recommend/list/jobs"


def newest_first(url: str) -> str:
    """The job-list request with Jobright's 'most recent' order (sortCondition=1) instead of 'recommended' (0).

    Reading stops once a screen is entirely older than the lookback, which is only right on a newest-first list."""
    parts = urlsplit(url)
    if LIST_API not in parts.path:
        return url
    query = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k == "sortCondition" for k, _ in query):
        return url
    query = [(k, "1" if k == "sortCondition" else v) for k, v in query]
    return urlunsplit(parts._replace(query=urlencode(query)))


def newest_first_order(listings: list[Listing], now: datetime) -> bool:
    """True when the dated postings on a screen run newest to oldest (a few minutes out of order is tolerated)."""
    dates = [d for d in (to_datetime(l.posted_at, now) for l in listings) if d is not None]
    return all(b <= a + timedelta(minutes=30) for a, b in zip(dates, dates[1:]))


def _existing_page(context, host_hint: str):
    for page in list(context.pages):
        try:
            if page.is_closed():
                continue
            if host_hint in (page.url or ""):
                return page
        except Exception:  # noqa: BLE001
            continue
    return None


def read_new_listings(
    context,
    url: str = DEFAULT_URL,
    *,
    max_scrolls: int = 15,
    is_known: Callable[[str], bool] = lambda _id: False,
    pause: Callable[[float, float], None] | None = None,
    host_hint: str = "jobright",
    debug_dir: Path | None = None,
    max_age_hours: float | None = None,
    now: Callable[[], datetime] | None = None,
) -> ReadResult:
    """Open the Jobright list (your filters are saved in the account) and read it 20 jobs at a time by scrolling to
    the bottom. Stops when a screen is all already seen, when a screen is entirely older than ``max_age_hours``
    (the list is newest first), when no more load, or after ``max_scrolls`` screens.
    Returns the listings with whatever application links the list already carried.
    ``host_hint`` is the text that marks Jobright's own addresses (tests point it at a local server)."""
    pause = pause or (lambda lo, hi: None)
    captured: list[Any] = []
    others: list[str] = []  # data responses that held no jobs (account, settings, ...)
    result = ReadResult()

    def on_response(resp) -> None:
        """Every data request the page makes to Jobright: anything that parses as JSON is kept, whatever it is labelled."""
        try:
            if host_hint not in resp.url:
                return
            kind = resp.request.resource_type
            ctype = (resp.headers.get("content-type") or "").lower()
            if kind not in {"xhr", "fetch"} and "json" not in ctype:
                return
            payload = json.loads(resp.text())
            captured.append(payload)
            found = len(extract_listings(payload))
            path = urlsplit(resp.url).path
            if found:  # the responses that carry jobs get a line with their shape; the rest are just counted
                result.trace.append(f"jobs: {resp.status} {path} {urlsplit(resp.url).query[:60]} -> {found} job(s) | "
                                    f"{json.dumps(shape(payload))[:300]}")
            else:
                others.append(path)
        except Exception as exc:  # noqa: BLE001 - a body that is not JSON is simply not a job list
            if urlsplit(resp.url).path.startswith("/_next/") or "collect" in resp.url:
                return  # the site's own scripts, styles and analytics: not worth a line
            others.append(f"{urlsplit(resp.url).path}({type(exc).__name__})")

    def sort_newest(route) -> None:
        try:
            target = newest_first(route.request.url)
            if target != route.request.url:
                sorted_requests.append(target)
                route.continue_(url=target)
            else:
                route.continue_()
        except Exception:  # noqa: BLE001 - a request we could not adjust goes as the page sent it
            try:
                route.continue_()
            except Exception:  # noqa: BLE001
                pass

    sorted_requests: list[str] = []
    try:
        context.route(f"**{LIST_API}**", sort_newest)
    except Exception:  # noqa: BLE001 - without it the order check below keeps the reading safe
        pass
    page = _open_list_page(context, url, host_hint, on_response)
    try:
        pause(2.5, 4.5)
        state = page_state(page)
        result.trace.insert(0, f"page {page.url} | title {page.title()!r} | state {state or 'ok'}")
        if state in {"login", "check"}:
            problem = (LoginRequired("Jobright is not signed in. Open the bot browser and sign in once.")
                       if state == "login" else
                       HumanCheck("Jobright is asking for a human check. Complete it in the bot browser."))
            problem.trace = list(result.trace)  # type: ignore[attr-defined]
            _save_debug(page, debug_dir, result)
            raise problem

        seen_ids: set[str] = set()
        clock = now or (lambda: datetime.now(timezone.utc))
        _wait_for_jobs(page, captured, 12000)
        for scroll in range(max_scrolls + 1):
            batch = [l for payload in captured for l in extract_listings(payload)]
            captured.clear()
            fresh = [l for l in batch if l.external_id not in seen_ids]
            for l in fresh:
                seen_ids.add(l.external_id)
            result.listings.extend(fresh)
            result.from_json += len(fresh)
            if fresh and all(is_known(l.external_id) for l in fresh):
                result.trace.append(f"stopped: all {len(fresh)} jobs on this screen were already seen")
                break  # a screen of nothing new: everything newer is already in hand
            if scroll and not fresh:
                result.trace.append("stopped: scrolling to the bottom loaded no more jobs")
                break
            ordered = newest_first_order(fresh, clock())
            if fresh and not ordered and max_age_hours is not None:
                result.trace.append("this screen is not newest-first, so the age limit does not stop the reading")
            if (max_age_hours is not None and fresh and ordered
                    and not any(is_recent(l, max_age_hours, clock()) for l in fresh)):
                result.trace.append(f"stopped: this screen is all older than {max_age_hours:g}h")
                break
            if scroll == max_scrolls:
                result.trace.append(f"stopped: reached the limit of {max_scrolls} screens")
                break
            for attempt in (1, 2):  # Jobright loads the next 20 only when the bottom of the list is reached
                how = scroll_list(page, nudge=attempt == 2)
                result.trace.append(f"scroll {scroll + 1}{'b' if attempt == 2 else ''}: {how} | "
                                    f"{len(fresh) if attempt == 1 else 0} new job(s) on the last screen")
                pause(1.8, 3.2)
                _wait_for_jobs(page, captured, 7000 if attempt == 1 else 4000)
                if any(extract_listings(p) for p in captured):
                    break
            result.pages_scrolled += 1

        if not result.listings:
            for l in dom_listings(page):
                if l.external_id not in seen_ids:
                    seen_ids.add(l.external_id)
                    result.listings.append(l)
                    result.from_dom += 1
            result.trace.append(f"no jobs in any response; page links found: {result.from_dom}")
            _save_debug(page, debug_dir, result)
        return result
    finally:
        if sorted_requests:
            result.trace.append(f"asked Jobright for the newest jobs first ({len(sorted_requests)} list request(s))")
        try:
            context.unroute(f"**{LIST_API}**", sort_newest)
        except Exception:  # noqa: BLE001
            pass
        if others:
            result.trace.append("other data responses (no jobs): " + ", ".join(sorted(set(others)))[:600])
        try:
            page.remove_listener("response", on_response)
        except Exception:  # noqa: BLE001
            pass


_SCROLL_JS = r"""
() => {
  // The job list is usually a panel of its own, not the whole page: scroll the tallest scrollable element.
  let best = null, bestH = 0;
  for (const el of document.querySelectorAll('*')) {
    if (!/(auto|scroll)/.test(getComputedStyle(el).overflowY)) continue;
    if (el.scrollHeight - el.clientHeight < 200 || el.clientHeight < 250) continue;
    if (el.clientHeight > bestH) { best = el; bestH = el.clientHeight; }
  }
  const target = best || document.scrollingElement || document.documentElement;
  const before = target.scrollTop;
  if (nudge) target.scrollTo(0, Math.max(0, target.scrollHeight - target.clientHeight - 600));  // back up a little first
  target.scrollTo(0, target.scrollHeight);  // the next 20 load when the bottom of the list is reached
  return { where: best ? 'list panel' : 'page', moved: Math.round(target.scrollTop - before),
           x: Math.round(innerWidth * 0.6), y: Math.round(innerHeight * 0.55) };
}
"""


def scroll_list(page, nudge: bool = False) -> str:
    """Scroll the job list to its bottom (what a person does to see more), and say how.
    ``nudge`` first steps back up a little, which re-triggers a list that did not notice the first arrival."""
    try:
        info = page.evaluate(_SCROLL_JS.replace("() => {", "(nudge) => {", 1), nudge)
        page.mouse.move(info["x"], info["y"])  # the wheel acts on what is under the pointer, not on the page corner
        page.mouse.wheel(0, 300)
        return f"{info['where']} moved {info['moved']}px"
    except Exception as exc:  # noqa: BLE001
        return f"scroll failed ({type(exc).__name__})"


def shape(obj: Any, depth: int = 0) -> Any:
    """The structure of a JSON value without its contents (key names, list lengths, value types): safe to log."""
    if isinstance(obj, dict):
        return "{...}" if depth >= 4 else {k: shape(v, depth + 1) for k, v in list(obj.items())[:10]}
    if isinstance(obj, list):
        return [f"len={len(obj)}"] + ([shape(obj[0], depth + 1)] if obj else [])
    return type(obj).__name__


def _save_debug(page, debug_dir: Path | None, result: ReadResult) -> None:
    """When a read finds nothing, keep a screenshot of what the bot saw."""
    if debug_dir is None:
        return
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / "jobright_last.png"
        page.screenshot(path=str(path), full_page=False, timeout=10000)
        result.trace.append(f"screenshot saved: {path}")
    except Exception as exc:  # noqa: BLE001
        result.trace.append(f"screenshot failed: {type(exc).__name__}")


def _wait_for_jobs(page, captured: list, timeout_ms: int) -> None:
    """Give the page's own requests time to come back: return once a response that holds jobs has arrived.
    (Other data requests, such as the account or filter settings, do not count: they arrive first.)"""
    waited = 0
    while waited < timeout_ms and not any(extract_listings(p) for p in captured):
        page.wait_for_timeout(200)
        waited += 200


_DOM_JS = r"""
() => Array.from(document.querySelectorAll('a[href*="/jobs/info/"]')).map(a => ({
  href: a.href, text: (a.innerText || '').trim().split('\n').filter(Boolean).slice(0, 4)
}))
"""


def dom_listings(page) -> list[Listing]:
    """Last resort: the job cards' links. Gives an id, a title guess and the Jobright detail address."""
    out: dict[str, Listing] = {}
    try:
        rows = page.evaluate(_DOM_JS)
    except Exception:  # noqa: BLE001
        return []
    for row in rows or []:
        m = re.search(r"/jobs/info/([A-Za-z0-9_-]+)", row.get("href", ""))
        if not m:
            continue
        lines = row.get("text") or []
        out.setdefault(m.group(1), Listing(
            external_id=m.group(1), title=lines[0] if lines else "", company=lines[1] if len(lines) > 1 else "",
            source_url=row["href"]))
    return list(out.values())


# ---------------------------------------------------------------------------- the job's own page

# The extension's Jobright reader, in the page: embedded data when it still matches the heading, otherwise the
# sections on screen. Composition of the embedded record happens in Python (``compose_from_data``) so the two stay one.
_DETAIL_JS = r"""
() => {
  const one = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const text = (node) => {
    const raw = node ? (node.innerText || node.textContent || "") : "";
    return raw.replace(/\u00a0/g, " ").replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  };
  const absolute = (href) => { try { return new URL(href, location.href).href; } catch (e) { return ""; } };
  const selectors = ["script#__NEXT_DATA__", "script#jobright-helper-job-detail-info"];
  for (const sel of selectors) {
    const node = document.querySelector(sel);
    if (!node) continue;
    try {
      const j = JSON.parse(node.textContent);
      const candidate = j.jobResult ? j : (j.props && j.props.pageProps && j.props.pageProps.dataSource);
      if (!candidate || !candidate.jobResult) continue;
      const shown = one((document.querySelector("h1") || {}).innerText).toLowerCase();
      const title = one(candidate.jobResult.jobTitle).toLowerCase();
      if (shown && title && shown.indexOf(title.slice(0, 40)) < 0 && title.indexOf(shown.slice(0, 40)) < 0) continue;
      return { kind: "data", ds: candidate };
    } catch (e) { /* the other source, or the visible page */ }
  }
  const sections = [];
  document.querySelectorAll("h2[title]").forEach((h) => {
    const name = h.getAttribute("title") || "";
    if (!/^(Responsibilities|Qualification|Benefits)$/i.test(name)) return;
    const box = h.closest("section") || (h.parentElement && h.parentElement.parentElement);
    const body = text(box).replace(new RegExp("^" + name + "\\s*", "i"), "");
    if (body) sections.push({ title: name === "Qualification" ? "Qualifications" : name, body: body });
  });
  const intro = document.querySelector("[class*='companyIntroduction']");
  const link = document.querySelector("a[class*='index_origin']");
  const heading = document.querySelector("h1");
  return {
    kind: "page",
    title: one(heading && heading.innerText),
    company: one(text(document.querySelector("[class*='company-name']"))),
    salary: one(text(document.querySelector('[data-time-filter-field="salaryDesc"]'))),
    apply: link ? absolute(link.getAttribute("href")) : "",
    location: "",
    sections: sections,
    company_text: intro ? text(intro) : ""
  };
}
"""

_DETAIL_READY_JS = """() => {
  // A bare heading is the shell. Wait until the embedded record, or the sections the extension reads, is actually there.
  if (document.querySelector('script#__NEXT_DATA__, script#jobright-helper-job-detail-info')) return true;
  const h1 = document.querySelector('h1');
  if (!h1 || !(h1.innerText || '').trim()) return false;
  return !!document.querySelector(
    'h2[title], [class*="company-name"], [data-time-filter-field="salaryDesc"], a[class*="index_origin"]');
}"""


def detail_url(listing: Listing) -> str:
    """The Jobright page for this posting. The list's own link wins; otherwise the id builds it."""
    if listing.source_url and "/jobs/info/" in listing.source_url:
        return listing.source_url
    if listing.external_id and not listing.external_id.startswith("http"):
        return f"{JOBRIGHT_HOME}/jobs/info/{listing.external_id}"
    return listing.source_url


def _wait_for_detail(page, timeout_ms: int = 12000) -> None:
    """The posting is drawn after the shell. Wait until the embedded record or the heading is actually there."""
    try:
        page.wait_for_function(_DETAIL_READY_JS, timeout=timeout_ms)
    except Exception:  # noqa: BLE001 - the reader then reports whatever is on the page
        pass


def read_detail(context, listing: Listing, *, pause: Callable[[float, float], None] | None = None,
                host_hint: str = "jobright.ai") -> Listing:
    """Open this job's Jobright page and return it filled the way the extension would.

    Title, company, salary and the sectioned description come from that page. The apply link does too; a link the
    page did not have falls back to one the list already carried. A signed-out or verification page is raised, the
    same as a list read, because a half-read posting must not be queued."""
    pause = pause or (lambda lo, hi: None)
    url = detail_url(listing)
    if not url:
        return listing_from_detail(listing, {})
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        pause(1.2, 2.4)
        state = page_state(page)
        if state == "login":
            raise LoginRequired("Jobright is not signed in. Open the bot browser and sign in once.")
        if state == "check":
            raise HumanCheck("Jobright is asking for a human check. Complete it in the bot browser.")
        _wait_for_detail(page)
        try:
            raw = page.evaluate(_DETAIL_JS) or {}
        except Exception:  # noqa: BLE001
            raw = {}
        found = listing_from_detail(listing, raw if isinstance(raw, dict) else {})
        if not found.source_url:
            found.source_url = url
        if not (found.apply_url and ats.classify(found.apply_url).ats):
            picked = _employer_link(context, page, url, host_hint, existing=found.apply_url)
            if picked:
                found.apply_url = without_tracking(picked)
        if not found.apply_url:
            fallback = without_tracking(listing.apply_url)
            if fallback and not _is_jobright_url(fallback):
                found.apply_url = fallback
        return found
    finally:
        page.close()


# ---------------------------------------------------------------------------- the employer's link

_APPLY_LINKS_JS = r"""
() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
  href: a.href, text: (a.innerText || a.getAttribute('aria-label') || '').trim().toLowerCase()
})).filter(l => /^https?:/.test(l.href))
"""


def resolve_apply_url(context, listing: Listing, *, pause: Callable[[float, float], None] | None = None,
                      host_hint: str = "jobright.ai") -> str:
    """The employer's application page for a posting: from the list data, else from the posting's Jobright page
    (an outbound link labelled apply / original), following redirects to the page it lands on."""
    if listing.apply_url and ats.classify(listing.apply_url).ats:
        return listing.apply_url
    if not listing.source_url:
        return listing.apply_url
    pause = pause or (lambda lo, hi: None)
    page = context.new_page()
    try:
        page.goto(listing.source_url, wait_until="domcontentloaded")
        pause(1.5, 3.0)
        if page_state(page) in {"login", "check"}:
            return ""
        return _employer_link(context, page, listing.source_url, host_hint)
    finally:
        page.close()


def _employer_link(context, page, source_url: str, host_hint: str, existing: str = "") -> str:
    """An employer address from the open Jobright page.

    A link that is already a known application site is used as it stands. Otherwise a Greenhouse (or other known)
    link printed on the page wins, then ``existing`` followed through its redirects, then a link labelled Apply.
    """
    if existing and ats.classify(existing).ats:
        return existing
    try:
        links = page.evaluate(_APPLY_LINKS_JS) or []
    except Exception:  # noqa: BLE001
        links = []
    own = urlsplit(source_url).netloc
    outbound = [l for l in links if host_hint not in l["href"] and urlsplit(l["href"]).netloc != own
                and not _is_jobright_url(l["href"])]
    direct = [l for l in outbound if ats.classify(l["href"]).ats]
    if direct:
        return direct[0]["href"]
    if existing and not _is_jobright_url(existing):
        landed = _follow(context, existing)
        if landed and not _is_jobright_url(landed):
            return landed
    labelled = [l for l in outbound if re.search(r"\bapply|original", l["text"])]
    for candidate in labelled[:2]:
        landed = _follow(context, urljoin(source_url, candidate["href"]))
        if landed and not _is_jobright_url(landed):
            return landed
    return ""


def _follow(context, href: str) -> str:
    """Where an outbound link ends up after its redirects (tracking hops included)."""
    page = context.new_page()
    try:
        page.goto(href, wait_until="domcontentloaded", timeout=30000)
        return page.url
    except Exception:  # noqa: BLE001
        return ""
    finally:
        page.close()
