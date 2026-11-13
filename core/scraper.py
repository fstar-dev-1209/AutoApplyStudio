"""HTTP job-page scraper with Needs JD fallback."""

from __future__ import annotations

import html as htmllib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

AGGREGATOR_HOSTS = {
    "hiring.cafe",
    "www.hiring.cafe",
    "jobright.ai",
    "www.jobright.ai",
    "builtin.com",
    "www.builtin.com",
    "www.linkedin.com",
    "linkedin.com",
}


@dataclass
class ScrapeResult:
    ok: bool
    url: str
    origin_url: str = ""
    company: str = ""
    role_title: str = ""
    job_description: str = ""
    source: str = ""
    error: str = ""
    salary_text: str = ""  # the posting's pay range when the source states one (e.g. Greenhouse pay transparency)


def html_to_text(raw: str) -> str:
    """Readable text from a posting's HTML. Some APIs and JSON-LD blocks deliver the HTML escaped (``&lt;div&gt;``);
    parsed as it stands that leaves raw tags in the description, so escaped markup is unescaped first."""
    text = str(raw or "")
    if re.search(r"&lt;/?[a-z][^&]{0,200}&gt;", text, re.I) and not re.search(r"<[a-z][^>]{0,200}>", text, re.I):
        text = htmllib.unescape(text)
    soup = BeautifulSoup(text, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(_BLOCK_TAGS):  # a new line at block boundaries; inline tags (<b>, <a>) stay in the sentence
        block.insert_before("\n")
        block.insert_after("\n")
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in soup.get_text("").splitlines()]
    return htmllib.unescape("\n".join(line for line in lines if line))  # one line per paragraph or bullet


_BLOCK_TAGS = ["p", "div", "li", "ul", "ol", "tr", "table", "section", "article", "blockquote",
               "h1", "h2", "h3", "h4", "h5", "h6"]


_FORM_START = re.compile(
    r"^\s*(create a job alert|apply for this job|autofill my application|submit your application)\b", re.I | re.M)
_PAGE_CHROME = {"back to jobs", "apply", "apply now", "new", "share", "save"}


def trim_application_form(text: str) -> str:
    """A job page's text without the page around it: the 'Back to jobs / Apply Now' lines at the top and, from
    'Create a Job Alert' / 'Apply for this job' on, the application form, consent text and survey."""
    m = _FORM_START.search(text)
    if m and m.start() >= 300:
        text = text[: m.start()]
    lines = text.splitlines()
    head = 0
    while head < min(len(lines), 6) and lines[head].strip().lower() in _PAGE_CHROME:
        head += 1
    # "Back to jobs / Title / Location / Apply Now": the lines between the chrome are the title block, not the posting
    body = lines[head:]
    while body and body[0].strip().lower() in _PAGE_CHROME:
        body.pop(0)
    return "\n".join(body).strip()


def detect_source(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if "greenhouse.io" in host:
        return "Greenhouse"
    if "lever.co" in host:
        return "Lever"
    if "ashbyhq.com" in host:
        return "Ashby"
    if "workday" in host or "myworkdayjobs" in host:
        return "Workday"
    if "builtin.com" in host:
        return "Builtin"
    if "hiring.cafe" in host:
        return "HiringCafe"
    if "jobright" in host:
        return "Jobright"
    if "linkedin" in host:
        return "LinkedIn"
    if host:
        return host.split(".")[0].title()
    return "Unknown"


def normalize_url(raw: str) -> str | None:
    text = raw.strip().strip('"').strip("'")
    if not text:
        return None
    if not re.match(r"^https?://", text, re.I):
        if text.startswith("www.") or "." in text.split("/")[0]:
            text = "https://" + text
        else:
            return None
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return text


def parse_url_list(text: str) -> tuple[list[str], list[str], list[str]]:
    """Return (valid_unique, duplicates, invalid)."""
    seen: set[str] = set()
    valid: list[str] = []
    duplicates: list[str] = []
    invalid: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        url = normalize_url(line)
        if url is None:
            invalid.append(line)
            continue
        key = url.rstrip("/")
        if key in seen:
            duplicates.append(url)
            continue
        seen.add(key)
        valid.append(url)
    return valid, duplicates, invalid


# Renders a page in a real browser and returns its HTML (None when it cannot); supplied by the UI.
PageRenderer = Callable[[str], "str | None"]


def scrape_job_url(url: str, timeout: float = 25.0, render: PageRenderer | None = None) -> ScrapeResult:
    """Fetch a job posting. `render` is the fallback for script-built or bot-protected pages."""
    result = _scrape_http(url, timeout)
    if result.ok or render is None:
        return result
    try:
        html = render(result.origin_url or url)
    except Exception:  # noqa: BLE001
        html = None
    if html:
        rendered = _from_html(html, result.origin_url or url, result.source or detect_source(url),
                              company_hint=result.company)
        if rendered.ok:
            rendered.url = url
            rendered.source = result.source or rendered.source
            rendered.role_title = rendered.role_title or result.role_title
            return rendered
    return result


def _scrape_http(url: str, timeout: float) -> ScrapeResult:
    source = detect_source(url)
    origin = url
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as client:
            origin = _resolve_origin(client, url)
            source = detect_source(origin)
            result = _scrape_origin(client, origin, source)
            result.url = url
            result.origin_url = origin
            result.source = source or result.source
            return result
    except httpx.HTTPError as exc:
        return ScrapeResult(
            ok=False,
            url=url,
            origin_url=origin,
            source=source,
            error=f"Network error while fetching the job page: {exc}",
        )
    except Exception as exc:  # noqa: BLE001
        return ScrapeResult(
            ok=False,
            url=url,
            origin_url=origin,
            source=source,
            error=f"Could not extract job description: {exc}",
        )


def _resolve_origin(client: httpx.Client, url: str) -> str:
    host = urlparse(url).netloc.lower()
    if not any(host == h or host.endswith("." + h) for h in AGGREGATOR_HOSTS) and "hiring.cafe" not in host:
        # Still follow redirects for non-aggregators
        if any(x in host for x in ("hiring.cafe", "jobright", "builtin.com")):
            pass
        else:
            return url

    response = client.get(url)
    final = str(response.url)
    html = response.text
    soup = BeautifulSoup(html, "html.parser")

    # Common "apply on company site" patterns
    for selector in (
        "a[href*='greenhouse.io']",
        "a[href*='lever.co']",
        "a[href*='ashbyhq.com']",
        "a[href*='myworkdayjobs.com']",
        "a[data-testid*='apply']",
        "a.apply-button",
        "a[href*='/jobs/']",
    ):
        for anchor in soup.select(selector):
            href = anchor.get("href") or ""
            absolute = urljoin(final, href)
            if _looks_like_origin_job(absolute) and not _is_aggregator(absolute):
                return absolute

    # Meta refresh / canonical
    canonical = soup.find("link", rel=lambda v: v and "canonical" in str(v).lower())
    if canonical and canonical.get("href"):
        candidate = urljoin(final, canonical["href"])
        if _looks_like_origin_job(candidate) and not _is_aggregator(candidate):
            return candidate

    # Query param passthrough (?url= / ?jobUrl=)
    qs = parse_qs(urlparse(url).query)
    for key in ("url", "jobUrl", "job_url", "redirect", "destination"):
        values = qs.get(key) or []
        if values:
            candidate = normalize_url(values[0])
            if candidate and not _is_aggregator(candidate):
                return candidate

    return final


def _is_aggregator(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return any(host == h or host.endswith("." + h.removeprefix("www.")) for h in AGGREGATOR_HOSTS) or any(
        x in host for x in ("hiring.cafe", "jobright", "builtin.com", "linkedin.com")
    )


def _looks_like_origin_job(url: str) -> bool:
    lower = url.lower()
    return any(
        token in lower
        for token in (
            "greenhouse.io",
            "lever.co",
            "ashbyhq.com",
            "myworkdayjobs.com",
            "/jobs/",
            "/job/",
            "/careers/",
            "/position/",
        )
    )


def _scrape_origin(client: httpx.Client, url: str, source: str) -> ScrapeResult:
    host = urlparse(url).netloc.lower()

    if "greenhouse.io" in host:
        return _scrape_greenhouse(client, url)
    if "lever.co" in host:
        return _scrape_lever(client, url)
    if "ashbyhq.com" in host:
        return _scrape_ashby(client, url)
    if host.endswith("myworkdayjobs.com"):
        result = _scrape_workday(client, url)
        if result.ok:
            return result
    if "/hcmUI/CandidateExperience" in url or host.endswith("oraclecloud.com"):
        result = _scrape_oracle(client, url)
        if result.ok:
            return result

    response = client.get(url)
    if response.status_code >= 400:
        return ScrapeResult(
            ok=False,
            url=url,
            origin_url=url,
            source=source,
            error=f"Job page returned HTTP {response.status_code}. Paste the job description manually.",
        )
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        return _from_generic_json(response.json(), url, source)

    return _from_html(response.text, url, source)


def _scrape_greenhouse(client: httpx.Client, url: str) -> ScrapeResult:
    # boards.greenhouse.io/{board}/jobs/{id} or job-boards API
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    board = parts[0] if parts else ""
    job_id = ""
    if "jobs" in parts:
        idx = parts.index("jobs")
        if idx + 1 < len(parts):
            job_id = parts[idx + 1].split("#")[0]
    if parts[:2] == ["embed", "job_app"]:
        # .../embed/job_app?for=<board>&token=<job id>: the board and the job are in the query, not the path
        query = parse_qs(parsed.query)
        board = (query.get("for") or [""])[0]
        job_id = (query.get("token") or [""])[0]
    if board and job_id and job_id.isdigit():
        api = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?pay_transparency=true"
        response = client.get(api)
        if response.status_code == 200:
            data = response.json()
            # the API names the company properly ("Empower Pharmacy"); the board slug is only a fallback
            company = str(data.get("company_name") or "").strip() or board.replace("-", " ").title()
            title = str(data.get("title") or "")
            text = html_to_text(str(data.get("content") or ""))
            where = str((data.get("location") or {}).get("name") or "").strip()
            if len(text) > 80:
                return ScrapeResult(
                    ok=True,
                    url=url,
                    origin_url=url,
                    company=company,
                    role_title=title,
                    job_description=(f"Location: {where}\n\n" if where else "") + text,
                    source="Greenhouse",
                    salary_text=_greenhouse_pay(data.get("pay_input_ranges")),
                )
    response = client.get(url)
    return _from_html(response.text, url, "Greenhouse", company_hint=board.replace("-", " ").title())


def _greenhouse_pay(ranges) -> str:
    """'$150,000 - $180,000 USD' from Greenhouse's pay_input_ranges (amounts are in cents), or ''."""
    if not isinstance(ranges, list):
        return ""
    for item in ranges:
        if not isinstance(item, dict):
            continue
        low, high = item.get("min_cents"), item.get("max_cents")
        if not isinstance(low, (int, float)) or not isinstance(high, (int, float)) or high <= 0:
            continue
        cur = str(item.get("currency_type") or "USD")
        sign = "$" if cur == "USD" else ""
        a, b = int(low) // 100, int(high) // 100
        return f"{sign}{a:,} - {sign}{b:,} {cur} per year".strip()
    return ""


def _scrape_lever(client: httpx.Client, url: str) -> ScrapeResult:
    # jobs.lever.co/{company}/{id}
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    company_slug = parts[0] if parts else ""
    job_id = parts[1] if len(parts) > 1 else ""
    if company_slug and job_id:
        api = f"https://api.lever.co/v0/postings/{company_slug}/{job_id}"
        response = client.get(api)
        if response.status_code == 200:
            data = response.json()
            title = str(data.get("text") or "")
            company = company_slug.replace("-", " ").title()
            lists = data.get("lists") or []
            chunks = []
            desc = data.get("descriptionPlain") or data.get("description") or ""
            if desc:
                chunks.append(BeautifulSoup(str(desc), "html.parser").get_text("\n", strip=True))
            for block in lists:
                if isinstance(block, dict):
                    chunks.append(str(block.get("text") or ""))
                    content = block.get("content") or ""
                    chunks.append(BeautifulSoup(str(content), "html.parser").get_text("\n", strip=True))
            text = "\n\n".join(c for c in chunks if c).strip()
            if len(text) > 80:
                return ScrapeResult(
                    ok=True,
                    url=url,
                    origin_url=url,
                    company=company,
                    role_title=title,
                    job_description=text,
                    source="Lever",
                )
    response = client.get(url)
    return _from_html(
        response.text,
        url,
        "Lever",
        company_hint=company_slug.replace("-", " ").title() if company_slug else "",
    )


def _scrape_ashby(client: httpx.Client, url: str) -> ScrapeResult:
    response = client.get(url)
    return _from_html(response.text, url, "Ashby")


def _html_text(content: object) -> str:
    return BeautifulSoup(str(content or ""), "html.parser").get_text("\n", strip=True)


def _scrape_workday(client: httpx.Client, url: str) -> ScrapeResult:
    """Workday's public CXS JSON endpoint: /wday/cxs/{tenant}/{site}/job/{slug}."""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if "job" not in parts or parts.index("job") == 0:
        return ScrapeResult(ok=False, url=url, source="Workday")
    idx = parts.index("job")
    site, slug = parts[idx - 1], "/".join(parts[idx + 1:])
    tenant = parsed.netloc.split(".")[0]
    response = client.get(f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}/job/{slug}",
                          headers={"Accept": "application/json"})
    if response.status_code != 200:
        return ScrapeResult(ok=False, url=url, source="Workday")
    info = (response.json() or {}).get("jobPostingInfo") or {}
    text = _html_text(info.get("jobDescription"))
    if len(text) < 80:
        return ScrapeResult(ok=False, url=url, source="Workday")
    extra = [f"{label}: {info[key]}" for label, key in (("Location", "location"), ("Time type", "timeType"))
             if info.get(key)]
    return ScrapeResult(
        ok=True, url=url, origin_url=url, company=tenant.replace("-", " ").title(),
        role_title=str(info.get("title") or ""),
        job_description="\n".join([*extra, "", text]).strip(), source="Workday",
    )


def _scrape_oracle(client: httpx.Client, url: str) -> ScrapeResult:
    """Oracle Recruiting (HCM) candidate experience: public REST requisition details."""
    parsed = urlparse(url)
    match = re.search(r"/sites/([^/]+)/job/(\d+)", parsed.path)
    if not match:
        return ScrapeResult(ok=False, url=url, source="Oracle")
    site, job_id = match.groups()
    api = (f"{parsed.scheme}://{parsed.netloc}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
           f'?expand=all&onlyData=true&finder=ById;Id="{job_id}",siteNumber={site}')
    response = client.get(api, headers={"Accept": "application/json"})
    if response.status_code != 200:
        return ScrapeResult(ok=False, url=url, source="Oracle")
    items = (response.json() or {}).get("items") or []
    if not items:
        return ScrapeResult(ok=False, url=url, source="Oracle")
    item = items[0]
    chunks = [_html_text(item.get(k)) for k in (
        "ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")]
    text = "\n\n".join(c for c in chunks if c)
    if len(text) < 80:
        return ScrapeResult(ok=False, url=url, source="Oracle")
    extra = [f"{label}: {item[key]}" for label, key in (
        ("Location", "PrimaryLocation"), ("Workplace", "WorkplaceType"), ("Schedule", "JobSchedule")) if item.get(key)]
    return ScrapeResult(
        ok=True, url=url, origin_url=url, role_title=str(item.get("Title") or ""),
        job_description="\n".join([*extra, "", text]).strip(), source="Oracle",
    )


def _from_generic_json(data: object, url: str, source: str) -> ScrapeResult:
    if not isinstance(data, dict):
        return ScrapeResult(ok=False, url=url, source=source, error="Unexpected JSON job payload.")
    title = str(data.get("title") or data.get("text") or data.get("name") or "")
    company = str(data.get("company") or data.get("company_name") or "")
    content = (
        data.get("content")
        or data.get("description")
        or data.get("descriptionHtml")
        or data.get("jobDescription")
        or ""
    )
    text = BeautifulSoup(str(content), "html.parser").get_text("\n", strip=True)
    if len(text) < 80:
        return ScrapeResult(
            ok=False,
            url=url,
            origin_url=url,
            company=company,
            role_title=title,
            source=source,
            error="Job JSON did not include a usable description.",
        )
    return ScrapeResult(
        ok=True,
        url=url,
        origin_url=url,
        company=company,
        role_title=title,
        job_description=text,
        source=source,
    )


def _from_html(
    html: str,
    url: str,
    source: str,
    company_hint: str = "",
) -> ScrapeResult:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        # Keep JSON-LD scripts
        if tag.name == "script" and tag.get("type") == "application/ld+json":
            continue
        if tag.name in {"script", "style", "noscript", "svg"}:
            tag.decompose()

    # JSON-LD JobPosting
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            types = item.get("@type")
            type_list = types if isinstance(types, list) else [types]
            if "JobPosting" not in type_list:
                continue
            title = str(item.get("title") or "")
            company = ""
            org = item.get("hiringOrganization")
            if isinstance(org, dict):
                company = str(org.get("name") or "")
            desc = html_to_text(str(item.get("description") or ""))
            if len(desc) > 80:
                return ScrapeResult(
                    ok=True,
                    url=url,
                    origin_url=url,
                    company=company or company_hint,
                    role_title=title,
                    job_description=desc,
                    source=source,
                )

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True) or title

    company = company_hint
    for selector in (
        "meta[property='og:site_name']",
        "[data-company]",
        "[class*='company']",
    ):
        node = soup.select_one(selector)
        if not node:
            continue
        found = str(node.get("content") or "") if node.name == "meta" else node.get_text(" ", strip=True)
        if 0 < len(found) <= 60:  # a long "company" node is page chrome, not a name
            company = company or found
        if company:
            break

    description = ""
    for selector in (
        "[class*='job-description']",
        "[class*='jobDescription']",
        "[id*='job-description']",
        "[data-testid*='description']",
        "article",
        "main",
        "[role='main']",
    ):
        node = soup.select_one(selector)
        if not node:
            continue
        text = node.get_text("\n", strip=True)
        if len(text) > len(description):
            description = text

    if len(description) < 120:
        body = soup.body.get_text("\n", strip=True) if soup.body else soup.get_text("\n", strip=True)
        description = body

    description = trim_application_form(_clean_text(description))
    if len(description) < 120:
        return ScrapeResult(
            ok=False,
            url=url,
            origin_url=url,
            company=company,
            role_title=_clean_title(title),
            source=source,
            error=(
                "Job description could not be extracted from this page "
                "(blocked, login-walled, or unsupported layout). Paste it manually."
            ),
        )

    return ScrapeResult(
        ok=True,
        url=url,
        origin_url=url,
        company=company,
        role_title=_clean_title(title),
        job_description=description,
        source=source,
    )


def _clean_title(title: str) -> str:
    text = re.sub(r"\s+", " ", title).strip()
    # Strip common "Role - Company | Board" patterns lightly
    for sep in (" | ", " · ", " — ", " - "):
        if sep in text:
            text = text.split(sep)[0].strip()
            break
    return text[:200]


def _clean_text(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def description_looks_broken(text: str) -> bool:
    """True for a saved description that is really page debris: raw HTML tags, or the application form and survey that
    follow the posting. Such a description is thrown away and the page read again."""
    sample = text or ""
    if len(re.findall(r"<(?:div|p|li|ul|ol|span|h[1-6]|br)\b[^>]*>", sample, re.I)) >= 3:
        return True
    return bool(re.search(r"Apply for this job", sample) and re.search(r"First Name", sample)
                or re.search(r"Voluntary Self-Identification", sample))
