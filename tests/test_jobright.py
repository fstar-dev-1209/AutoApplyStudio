"""Reading Jobright: record extraction from JSON, relative dates, and the page reader against a stand-in site.

The stand-in imitates how a single-page app loads a job list (JSON fetched by the page, more on scroll) and how a
posting links out to an employer through a redirect. It is not Jobright: ``scripts/capture_jobright.py`` records what
the real site sends.
"""

from __future__ import annotations

import json
import socket
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import jobright
from core.discovery import Listing

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ pure parts

def test_dates_are_read_in_every_form_the_site_uses():
    assert jobright.to_datetime("2 hours ago", NOW) == NOW - timedelta(hours=2)
    assert jobright.to_datetime("a day ago", NOW) == NOW - timedelta(days=1)
    assert jobright.to_datetime("Just now", NOW) == NOW
    assert jobright.to_datetime(1_793_890_800_000, NOW) == datetime.fromtimestamp(1_793_890_800, tz=timezone.utc)
    assert jobright.to_datetime(1_793_890_800, NOW) == datetime.fromtimestamp(1_793_890_800, tz=timezone.utc)
    assert jobright.to_datetime("2026-11-05T09:30:00Z", NOW) == datetime(2026, 11, 5, 9, 30, tzinfo=timezone.utc)
    assert jobright.to_datetime("2026-11-05 09:30:00", NOW) == datetime(2026, 11, 5, 9, 30, tzinfo=timezone.utc)
    assert jobright.to_datetime("whenever", NOW) is None and jobright.to_datetime("", NOW) is None


def test_recency_drops_only_postings_known_to_be_old():
    fresh = Listing("1", posted_at="3 hours ago")
    old = Listing("2", posted_at=(NOW - timedelta(days=2)).isoformat())
    unknown = Listing("3", posted_at="")
    assert jobright.is_recent(fresh, 24, NOW) and not jobright.is_recent(old, 24, NOW)
    assert jobright.is_recent(unknown, 24, NOW)  # an unreadable date is not a reason to lose a job


def test_records_are_found_in_nested_pairs_and_flat_lists():
    payload = {"success": True, "result": {"jobList": [
        {"jobResult": {"jobId": "a1", "jobTitle": "Backend Engineer", "jobLocation": "Remote",
                       "publishTime": "2 hours ago", "applyLink": "https://boards.greenhouse.io/acme/jobs/1",
                       "salaryDesc": "$150K/yr"},
         "companyResult": {"companyName": "Acme"}},
        {"jobResult": {"jobId": "a2", "jobTitle": "SRE"}, "companyResult": {"companyName": "Beta"}},
    ], "other": [{"id": 7, "title": "Not a job", "note": "no company or link"}]},
        "flat": [{"id": "z9", "title": "Data Engineer", "company": {"name": "Gamma"},
                  "original_url": "https://jobs.lever.co/gamma/abc"}]}
    by_id = {l.external_id: l for l in jobright.extract_listings(payload)}
    assert set(by_id) == {"a1", "a2", "z9"}
    a1 = by_id["a1"]
    assert (a1.title, a1.company, a1.location, a1.salary_text) == ("Backend Engineer", "Acme", "Remote", "$150K/yr")
    assert a1.apply_url == "https://boards.greenhouse.io/acme/jobs/1"
    assert a1.source_url == "https://jobright.ai/jobs/info/a1"
    assert by_id["a2"].company == "Beta" and by_id["a2"].apply_url == ""
    assert by_id["z9"].company == "Gamma" and by_id["z9"].apply_url.endswith("/gamma/abc")


def test_a_link_back_into_jobright_is_not_the_employers_page():
    payload = [{"jobId": "q1", "jobTitle": "Dev", "companyName": "Acme", "applyLink": "https://jobright.ai/jobs/info/q1"}]
    (listing,) = jobright.extract_listings(payload)
    assert listing.apply_url == "" and listing.source_url == "https://jobright.ai/jobs/info/q1"


def test_unrelated_json_yields_nothing():
    assert jobright.extract_listings({"user": {"id": 1, "name": "x"}, "menu": [{"title": "Home"}]}) == []
    assert jobright.extract_listings(None) == [] and jobright.extract_listings("text") == []


_DETAIL = {
    "jobResult": {
        "jobTitle": "Backend Engineer", "workModel": "Remote", "employmentType": "Full-time",
        "jobSeniority": "Senior", "jobLocation": "New York, NY", "minYearsOfExperience": 5,
        "isRemote": True, "minSalary": 150000, "maxSalary": 180000,
        "applyLink": "https://boards.greenhouse.io/acme/jobs/42?jr_id=1&utm_source=jobright",
        "jobSummary": "Build the APIs.",
        "coreResponsibilities": ["Design services", "Mentor; and teach"],
        "qualifications": {"mustHave": ["Python"], "preferredHave": ["Go"]},
        "educationSummaries": ["BS in CS"],
        "jdCoreSkills": [{"skill": "Python"}, {"skill": "Kafka"}],
        "benefitsSummaries": ["Health insurance"],
    },
    "companyResult": {
        "companyName": "Acme", "companyDesc": "Acme builds tools.", "companySize": "51-200",
        "companyFoundYear": "2018", "companyLocation": "NYC", "companyURL": "https://acme.example",
        "companyCategories": "Software,AI", "fundraisingCurrentStage": "Series B",
    },
}


def test_the_job_page_record_is_composed_the_way_the_extension_composes_it():
    built = jobright.compose_from_data(_DETAIL)
    assert built["title"] == "Backend Engineer" and built["company"] == "Acme"
    assert built["salary"] == "$150,000 - $180,000/yr"  # salaryDesc was empty, so the numbers are used
    assert built["apply"] == "https://boards.greenhouse.io/acme/jobs/42"
    text = built["description"]
    assert text.startswith("Backend Engineer — Acme")
    assert "Remote · Full-time · Senior · New York, NY · 5+ years experience" in text
    assert "Salary: $150,000 - $180,000/yr" in text
    assert "Summary\nBuild the APIs." in text
    assert "• Design services\n• Mentor\n• and teach" in text  # a run-on item becomes two bullets
    assert "Required\n• Python\n• BS in CS" in text and "Preferred\n• Go" in text
    assert "Key skills: Python, Kafka" in text
    assert "Benefits\n• Health insurance" in text
    assert "Size: 51-200" in text and "Industries: Software, AI" in text and "Stage: Series B" in text
    assert jobright.is_full_posting(text)


def test_a_stated_salary_and_a_jobright_company_name_are_handled():
    stated = {"jobResult": {"jobTitle": "Dev", "salaryDesc": "$150K/yr", "jobSummary": "Build things."},
              "companyResult": {"companyName": "Jobright"}}
    built = jobright.compose_from_data(stated)
    assert built["salary"] == "$150K/yr" and built["company"] == ""
    assert built["description"].startswith("Dev\n") and "Summary\nBuild things." in built["description"]


def test_the_list_blurb_does_not_survive_a_page_that_had_the_posting():
    base = Listing("a1", title="Card title", company="Card Co", salary_text="$1",
                   description="A summary from the list. " * 20,
                   apply_url="https://boards.greenhouse.io/acme/jobs/1", posted_at="2 hours ago")
    found = jobright.listing_from_detail(base, {"kind": "data", "ds": _DETAIL})
    assert found.external_id == "a1" and found.posted_at == "2 hours ago"
    assert (found.title, found.company, found.location) == ("Backend Engineer", "Acme", "New York, NY")
    assert found.description.startswith("Backend Engineer — Acme") and "list" not in found.description
    assert found.apply_url == "https://boards.greenhouse.io/acme/jobs/42"
    assert found.salary_text == "$150,000 - $180,000/yr"


def test_a_page_with_nothing_on_it_does_not_invent_a_description_from_the_list_blurb():
    base = Listing("a1", title="Dev", company="Acme", description="A summary. " * 40,
                   apply_url="https://boards.greenhouse.io/acme/jobs/1")
    found = jobright.listing_from_detail(base, {"kind": "empty"})
    assert found.description == "" and found.title == "Dev" and found.company == "Acme"
    assert found.apply_url == ""  # read_detail puts the list's link back only after searching the page


def test_a_long_list_description_is_kept_when_the_page_could_not_be_read():
    long = "The posting. " * 200
    found = jobright.listing_from_detail(Listing("a1", title="Dev", description=long), None)
    assert found.description == long


def test_the_visible_page_is_composed_when_the_embedded_record_was_stale():
    built = jobright.compose_from_page({
        "title": "Platform Engineer", "company": "Visible Co", "salary": "$160K/yr",
        "apply": "https://boards.greenhouse.io/visible/jobs/7?jr_id=9",
        "sections": [{"title": "Responsibilities", "body": "Own the platform."}],
        "company_text": "Visible builds infrastructure.",
    })
    assert built["apply"] == "https://boards.greenhouse.io/visible/jobs/7"
    assert "Old Role" not in built["description"]
    assert "Responsibilities\nOwn the platform." in built["description"]
    assert "Company\nVisible builds infrastructure." in built["description"]
    assert jobright.usable_description("Platform Engineer — Visible Co") == ""
    assert jobright.usable_description(built["description"]) == built["description"]


# ------------------------------------------------------------------ the page reader

pytest.importorskip("playwright")
from core.browser_host import BrowserConfig, BrowserError, BrowserHost, find_browser  # noqa: E402

try:
    find_browser("chrome")
except BrowserError:  # pragma: no cover
    pytest.skip("Chrome is not installed", allow_module_level=True)


def _job(n: int, hours: int, apply: str = "") -> dict:
    return {"jobResult": {"jobId": f"j{n}", "jobTitle": f"Engineer {n}", "jobLocation": "Remote",
                          "publishTime": (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(),
                          "applyLink": apply},
            "companyResult": {"companyName": f"Company {n}"}}


PAGE_ONE = [_job(1, 1), _job(2, 2), _job(3, 3)]
PAGE_TWO = [_job(4, 20), _job(5, 30)]
PAGE_THREE = [_job(6, 100), _job(7, 110)]  # older than 72 hours

INNER_HTML = """<html><head><title>Jobs</title></head><body style="margin:0;overflow:hidden">
<div id="side" style="position:fixed;left:0;top:0;width:200px;height:100%">menu</div>
<div id="box" style="margin-left:220px;height:500px;overflow-y:auto"><div style="height:3000px" id="list">loading</div></div>
<script>
let page = 1, busy = false;
async function load() { if (busy) return; busy = true; const r = await fetch("/swan/jobs?page=" + page); await r.json(); page += 1; busy = false; }
load();
const box = document.getElementById("box");
box.addEventListener("scroll", () => { if (box.scrollTop + box.clientHeight >= box.scrollHeight - 30) load(); });  // only the very bottom loads more
</script></body></html>"""

LIST_HTML = """<html><head><title>Jobs</title></head><body style="min-height:4000px">
<div id="list">loading</div>
<script>
let page = 1, busy = false;
async function load() {
  if (busy) return; busy = true;
  const r = await fetch('/swan/jobs?page=' + page); const j = await r.json();
  page += 1; busy = false;
  document.getElementById('list').innerText += ' ' + JSON.stringify(j).length;
}
fetch('/swan/profile').then(() => setTimeout(load, 700));  // account data arrives first, the job list later
window.addEventListener('scroll', () => { if (window.scrollY > 200 && page === 2) load(); });
</script></body></html>"""


class _Fixture(BaseHTTPRequestHandler):
    employer_port = 0
    jobs_ctype = "application/json"
    empty = False
    inner = False
    signed_in = True

    def _send(self, body: str, ctype="text/html", code=200, headers=()):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    detail = ""

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path.startswith("/jobs/info/") and self.detail == "data":
            body = ("<html><body><h1>Backend Engineer</h1>"
                    '<script id="jobright-helper-job-detail-info" type="application/json">'
                    + json.dumps(_PAGE_DS) + "</script></body></html>")
            return self._send(body)
        if path.startswith("/jobs/info/") and self.detail == "stale":
            return self._send(_STALE_HTML)
        if path == "/jobs/recommend":
            if not self.signed_in:
                return self._send("", code=302, headers=[("Location", "/login")])
            return self._send(INNER_HTML if self.inner else LIST_HTML)
        if path == "/login":
            return self._send("<html><body><h1>Sign in</h1><input type=password></body></html>")
        if path == "/swan/profile":
            return self._send(json.dumps({"user": {"id": 1, "title": "Not a job"}}), "application/json")
        if path == "/swan/jobs":
            page = int(self.path.split("page=")[-1] or 1)
            jobs = [] if self.empty else PAGE_ONE if page == 1 else PAGE_TWO if page == 2 else PAGE_THREE if page == 3 and self.inner else []
            return self._send(json.dumps({"result": {"jobList": jobs}}), self.jobs_ctype)
        if path.startswith("/jobs/info/"):
            return self._send(
                f'<html><body><a href="{path}">Share</a>'
                f'<a href="http://127.0.0.1:{self.employer_port}/redir?to=/boards/acme/jobs/1">Apply now</a>'
                f'<a href="http://127.0.0.1:{self.employer_port}/about">About the company</a></body></html>')
        self._send("not found", code=404)

    def log_message(self, *a):
        pass


class _Employer(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/redir"):
            self.send_response(302)
            self.send_header("Location", "/boards/acme/jobs/1?utm=x")
            self.end_headers()
            return
        body = b"<html><body>employer</body></html>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def world(tmp_path):
    employer = _serve(_Employer)
    handler = type("H", (_Fixture,), {"employer_port": employer.server_address[1], "signed_in": True})
    site = _serve(handler)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    host = BrowserHost(BrowserConfig(profile_dir=tmp_path / "p", headless=True, debug_port=port, mode="attach"))
    host.start()
    base = f"http://127.0.0.1:{site.server_address[1]}"
    yield host, base, handler
    host.stop()
    site.shutdown()
    employer.shutdown()


def test_the_list_is_read_newest_first_and_scrolling_loads_more(world):
    host, base, _ = world
    result = host.run(lambda ctx: jobright.read_new_listings(ctx, base + "/jobs/recommend", host_hint="127.0.0.1"))
    assert [l.external_id for l in result.listings] == ["j1", "j2", "j3", "j4", "j5"]
    assert result.pages_scrolled >= 1 and result.from_json == 5
    assert result.listings[0].company == "Company 1"


def test_reading_stops_when_a_screen_shows_nothing_new(world):
    host, base, _ = world
    known = {"j1", "j2", "j3"}  # a poll that finds only what it has already handled does not keep scrolling
    result = host.run(lambda ctx: jobright.read_new_listings(
        ctx, base + "/jobs/recommend", host_hint="127.0.0.1", is_known=known.__contains__))
    assert {l.external_id for l in result.listings} >= {"j1", "j2", "j3"}
    assert result.pages_scrolled <= 2


def test_a_signed_out_browser_is_reported_not_misread(world):
    host, base, handler = world
    handler.signed_in = False
    with pytest.raises(jobright.LoginRequired):
        host.run(lambda ctx: jobright.read_new_listings(ctx, base + "/jobs/recommend", host_hint="127.0.0.1"))


def test_the_employers_page_is_found_through_the_redirect(world):
    host, base, _ = world
    listing = Listing(external_id="j1", source_url=base + "/jobs/info/j1")
    url = host.run(lambda ctx: jobright.resolve_apply_url(ctx, listing, host_hint="jobright-fixture"))
    assert url.endswith("/boards/acme/jobs/1?utm=x"), url
    # the link that merely names the company is not the application
    assert "about" not in url


def test_a_link_already_known_to_be_an_ats_is_used_without_visiting_anything(world):
    host, _, _ = world
    listing = Listing(external_id="j1", apply_url="https://boards.greenhouse.io/acme/jobs/9", source_url="http://127.0.0.1:1/x")
    assert host.run(lambda ctx: jobright.resolve_apply_url(ctx, listing)) == "https://boards.greenhouse.io/acme/jobs/9"


def test_a_list_with_an_unusual_label_arriving_after_other_account_data_is_still_read(world):
    host, base, handler = world
    handler.jobs_ctype = "text/plain"  # not labelled as JSON, and it comes after the account data
    result = host.run(lambda ctx: jobright.read_new_listings(ctx, base + "/jobs/recommend", host_hint="127.0.0.1"))
    assert [l.external_id for l in result.listings] == ["j1", "j2", "j3", "j4", "j5"]


def test_each_read_leaves_a_trace_of_what_the_page_sent(world):
    host, base, _ = world
    result = host.run(lambda ctx: jobright.read_new_listings(ctx, base + "/jobs/recommend", host_hint="127.0.0.1"))
    text = "\n".join(result.trace)
    assert text.startswith("page http://127.0.0.1") and "state ok" in text
    assert "other data responses (no jobs): /swan/profile" in text  # the account data was seen and is not a job list
    assert "/swan/jobs" in text and "3 job(s)" in text and "jobList" in text  # the shape, so a renamed field shows
    assert "scroll 1:" in text
    assert "Not a job" not in text and "Company 1" not in text  # structure only: no values are logged


def test_an_empty_read_says_so_and_keeps_a_screenshot(world, tmp_path):
    host, base, handler = world
    handler.empty = True
    shots = tmp_path / "debug"
    result = host.run(lambda ctx: jobright.read_new_listings(
        ctx, base + "/jobs/recommend", host_hint="127.0.0.1", debug_dir=shots))
    assert result.listings == []
    assert any("no jobs in any response" in line for line in result.trace)
    assert (shots / "jobright_last.png").stat().st_size > 1000


def test_a_signed_out_error_carries_its_trace(world):
    host, base, handler = world
    handler.signed_in = False
    with pytest.raises(jobright.LoginRequired) as caught:
        host.run(lambda ctx: jobright.read_new_listings(ctx, base + "/jobs/recommend", host_hint="127.0.0.1"))
    assert any("state login" in line for line in caught.value.trace)


def test_the_shape_of_json_hides_its_values():
    shape = jobright.shape({"a": {"b": [{"name": "secret", "n": 3}], "c": "text"}, "d": None})
    assert shape == {"a": {"b": ["len=1", {"name": "str", "n": "int"}], "c": "str"}, "d": "NoneType"}


def test_every_page_of_a_list_is_loaded_by_reaching_the_bottom_until_postings_get_too_old(world):
    host, base, handler = world
    handler.inner = True  # the page itself does not scroll; the list panel does, and only its very bottom loads more
    result = host.run(lambda ctx: jobright.read_new_listings(
        ctx, base + "/jobs/recommend", host_hint="127.0.0.1", max_age_hours=72))
    # three pages were read: the first two are recent; the third is entirely older than 72 hours, which ends the read
    assert [l.external_id for l in result.listings] == ["j1", "j2", "j3", "j4", "j5", "j6", "j7"]
    text = "\n".join(result.trace)
    assert "list panel" in text and "older than 72h" in text


def test_without_an_age_limit_it_reads_until_no_more_load(world):
    host, base, handler = world
    handler.inner = True
    result = host.run(lambda ctx: jobright.read_new_listings(ctx, base + "/jobs/recommend", host_hint="127.0.0.1"))
    assert len(result.listings) == 7
    assert any("loaded no more jobs" in line for line in result.trace)


_PAGE_DS = {
    "jobResult": {
        "jobId": "page-1", "jobTitle": "Backend Engineer", "jobLocation": "Remote",
        "salaryDesc": "$150K/yr - $180K/yr",
        "applyLink": "https://boards.greenhouse.io/acme/jobs/42?jr_id=1&utm_source=jobright",
        "jobSummary": "Build the APIs.",
        "coreResponsibilities": ["Design services"],
        "qualifications": {"mustHave": ["Python"], "preferredHave": []},
        "jdCoreSkills": [{"skill": "Python"}, {"skill": "Kafka"}],
        "benefitsSummaries": ["Health insurance"],
    },
    "companyResult": {"companyName": "Acme", "companyDesc": "Acme builds tools."},
}

_STALE_HTML = """<html><body>
<h1>Platform Engineer</h1>
<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"dataSource":{"jobResult":{"jobId":"old","jobTitle":"Old Role","jobSummary":"Stale summary that must not be used."},"companyResult":{"companyName":"OldCo"}}}}}</script>
<div class="company-name">Visible Co</div>
<div data-time-filter-field="salaryDesc">$160K/yr</div>
<section><h2 title="Responsibilities">Responsibilities</h2><p>Own the platform.</p></section>
<a class="index_origin" href="https://boards.greenhouse.io/visible/jobs/7?jr_id=9">Original</a>
</body></html>"""


def test_opening_the_job_page_reads_it_like_the_extension(world):
    host, base, handler = world
    handler.detail = "data"
    listing = Listing("j1", title="Card title", company="Card Co", salary_text="$1",
                      description="A summary from the list.",
                      source_url=base + "/jobs/info/j1",
                      apply_url="https://boards.greenhouse.io/acme/jobs/1")
    found = host.run(lambda ctx: jobright.read_detail(ctx, listing))
    assert found.external_id == "j1"
    assert (found.title, found.company) == ("Backend Engineer", "Acme")
    assert found.salary_text == "$150K/yr - $180K/yr"
    assert "Summary\nBuild the APIs." in found.description and "Design services" in found.description
    assert "Key skills: Python, Kafka" in found.description and "Acme builds tools." in found.description
    assert "list" not in found.description
    assert found.apply_url == "https://boards.greenhouse.io/acme/jobs/42"


def test_stale_embedded_data_is_ignored_for_the_job_on_screen(world):
    host, base, handler = world
    handler.detail = "stale"
    listing = Listing("j1", title="Card title", company="Card Co", source_url=base + "/jobs/info/j1")
    found = host.run(lambda ctx: jobright.read_detail(ctx, listing))
    assert found.title == "Platform Engineer" and found.company == "Visible Co"
    assert found.salary_text == "$160K/yr"
    assert "Old Role" not in found.description and "Own the platform." in found.description
    assert found.apply_url == "https://boards.greenhouse.io/visible/jobs/7"


def test_a_poll_that_finds_only_what_it_already_has_does_not_scroll_at_all(world):
    host, base, handler = world
    handler.inner = True
    known = {"j1", "j2", "j3"}
    result = host.run(lambda ctx: jobright.read_new_listings(
        ctx, base + "/jobs/recommend", host_hint="127.0.0.1", is_known=known.__contains__, max_age_hours=72))
    assert [l.external_id for l in result.listings] == ["j1", "j2", "j3"] and result.pages_scrolled == 0
    assert any("already seen" in line for line in result.trace)


def test_the_job_list_request_is_asked_for_newest_first():
    url = "https://jobright.ai/swan/recommend/list/jobs?refresh=true&sortCondition=0&position=0&count=20"
    assert "sortCondition=1" in jobright.newest_first(url)
    assert "position=0" in jobright.newest_first(url)
    other = "https://jobright.ai/swan/filter/get/filter?sortCondition=0"
    assert jobright.newest_first(other) == other


def test_an_unordered_screen_is_recognised_so_the_age_limit_does_not_cut_it_short():
    now = datetime.now(timezone.utc)

    def at(h):
        return jobright.Listing(external_id=str(h), title="t", company="c",
                                posted_at=(now - timedelta(hours=h)).isoformat())
    assert jobright.newest_first_order([at(1), at(2), at(5)], now)
    assert not jobright.newest_first_order([at(30), at(1), at(5)], now)
