"""Greenhouse links: the board and job come from the right part of the address."""

from __future__ import annotations

import httpx

from core.scraper import _scrape_greenhouse

POSTING = {"title": "Senior Data Engineer", "content": "<p>" + "Build data platforms for health. " * 10 + "</p>"}


def client_for(seen: list[str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=POSTING)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_an_embed_link_uses_the_board_and_job_from_its_query_not_the_word_embed():
    seen: list[str] = []
    result = _scrape_greenhouse(client_for(seen), "https://job-boards.greenhouse.io/embed/job_app?for=uniteus&token=8840770002")
    assert seen == ["https://boards-api.greenhouse.io/v1/boards/uniteus/jobs/8840770002?pay_transparency=true"]
    assert result.ok and result.company == "Uniteus" and result.role_title == "Senior Data Engineer"


def test_a_normal_board_link_is_unchanged():
    seen: list[str] = []
    result = _scrape_greenhouse(client_for(seen), "https://boards.greenhouse.io/acme-corp/jobs/123456?gh_src=x")
    assert seen == ["https://boards-api.greenhouse.io/v1/boards/acme-corp/jobs/123456?pay_transparency=true"]
    assert result.company == "Acme Corp"


ESCAPED = {
    "title": "Data Scientist", "company_name": "Empower Pharmacy", "location": {"name": "US Remote"},
    "content": "&lt;div class=&quot;content-intro&quot;&gt;&lt;p&gt;Empower Pharmacy is a visionary healthcare company "
               "&amp;amp; more. " + "We transform care. " * 10 + "&lt;/p&gt;&lt;/div&gt;&lt;h3&gt;RESPONSIBILITIES:&lt;/h3&gt;"
               "&lt;ul&gt;&lt;li&gt;Build models&lt;/li&gt;&lt;li&gt;Share insights&lt;/li&gt;&lt;/ul&gt;",
    "pay_input_ranges": [{"min_cents": 15000000, "max_cents": 18000000, "currency_type": "USD", "title": "Base"}],
}


def escaped_client(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=ESCAPED)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_the_escaped_html_greenhouse_sends_becomes_plain_text_not_raw_tags():
    seen: list[str] = []
    result = _scrape_greenhouse(escaped_client(seen), "https://job-boards.greenhouse.io/embed/job_app?for=empowerpharmacy&token=4371957009")
    text = result.job_description
    assert "<" not in text and "&lt;" not in text and "&quot;" not in text and "&amp;" not in text
    assert text.startswith("Location: US Remote") and "RESPONSIBILITIES:" in text
    assert "Build models" in text and "Share insights" in text and "healthcare company & more." in text


def test_the_company_name_and_pay_come_from_the_api():
    result = _scrape_greenhouse(escaped_client([]), "https://boards.greenhouse.io/empowerpharmacy/jobs/4371957009")
    assert result.company == "Empower Pharmacy"  # not the slug "Empowerpharmacy"
    assert result.salary_text == "$150,000 - $180,000 USD per year"


def test_missing_or_nonsense_pay_ranges_give_no_salary():
    from core.scraper import _greenhouse_pay

    assert _greenhouse_pay([]) == "" and _greenhouse_pay(None) == "" and _greenhouse_pay([{"min_cents": 0, "max_cents": 0}]) == ""
    assert _greenhouse_pay([{"min_cents": "x", "max_cents": 5}]) == ""


def test_a_page_scrape_stops_at_the_application_form_and_drops_the_page_chrome():
    from core.scraper import trim_application_form

    posting = "Back to jobs\nData Scientist\nUS Remote\nApply Now\n" + "Empower Pharmacy builds things for patients. " * 12
    page = (posting + "\nRESPONSIBILITIES:\nAnalytical Ownership\n" + "Translate questions into analyses. " * 8 +
            "\nCreate a Job Alert\nInterested in building your career?\nApply for this job\nFirst Name\n*\nLast Name\n"
            "Voluntary Self-Identification\nGender\nSelect...\nSubmit application")
    out = trim_application_form(page)
    assert out.startswith("Data Scientist\nUS Remote") and "RESPONSIBILITIES:" in out
    for junk in ("Create a Job Alert", "First Name", "Voluntary Self-Identification", "Submit application", "Back to jobs"):
        assert junk not in out
    # short pages are left alone: a phrase near the very top is not the start of a form
    assert trim_application_form("Apply for this job\nShort") == "Apply for this job\nShort"


def test_json_ld_descriptions_delivered_escaped_are_unescaped_too():
    from core.scraper import html_to_text

    assert html_to_text("&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;One&lt;/li&gt;&lt;/ul&gt;") == "Hello & welcome\nOne"
    # inline tags stay in the sentence; block tags start a new line
    assert html_to_text("<p>Already <b>real</b> markup</p><p>Next</p>") == "Already real markup\nNext"
    assert html_to_text("") == "" and html_to_text(None) == ""


def test_what_counts_as_a_broken_saved_description():
    from core.scraper import description_looks_broken

    assert description_looks_broken("<div><p>a</p><ul><li>b</li></ul></div>")
    assert description_looks_broken("posting\nApply for this job\nFirst Name\n*")
    assert description_looks_broken("posting text\nVoluntary Self-Identification\nGender")
    assert not description_looks_broken("We use <div> tags in React and C# generics like List<int>.")
    assert not description_looks_broken("Build data platforms.\nRESPONSIBILITIES:\nOwn the roadmap.")
    assert not description_looks_broken("")
