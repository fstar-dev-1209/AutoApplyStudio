"""The salary asked for on a form: one figure per job, inside its posted range, above your minimum, in the shape each
question needs; and jobs that pay below your minimum never being queued."""

from __future__ import annotations

from core.application_record import format_record, rows_from_plan
from core.chat_questions import Question, build_question_prompt
from core.discovery import DiscoveryRules, Listing, posted_maximum, reason_kind
from core.form_answers import CHAT, FILL, NEEDS, SKIP, Context, Field, Option, decide
from core.greenhouse import GreenhouseApplier
from core.models import JobItem
from core.salary_answer import (
    PayContext,
    build_salary_prompt,
    figure_in,
    parse_bucket,
    pay_context_for,
    pick_bucket,
    range_in_text,
    render,
    tidy,
)
from core.settings_store import AppSettings, ProfileSettings


def pay(low=100_000, high=110_000, period="year", **kw) -> PayContext:
    return PayContext(low=low, high=high, period=period, currency="USD", **kw)


# ------------------------------------------------------------------ the limits

def test_the_posted_range_is_read_per_year_whatever_period_the_posting_uses():
    assert pay().posted() == (100_000, 110_000)
    assert pay(40, 60, "hour").posted() == (83_200, 124_800)
    assert pay(8_000, 10_000, "month").posted() == (96_000, 120_000)
    assert PayContext().posted() is None  # nothing posted
    assert PayContext(low=50, high=60, period="year", currency="GBP").posted() is None  # not dollars: not compared


def test_an_answer_never_goes_above_the_posted_maximum_or_below_your_minimum():
    p = pay(100_000, 150_000, floor=120_000)
    assert p.limits() == (120_000, 150_000)  # the minimum lifts the bottom of the range
    assert p.clamp(90_000) == 120_000 and p.clamp(200_000) == 150_000 and p.clamp(130_000) == 130_000
    assert pay(60_000, 70_000, floor=80_000).limits() == (80_000, 80_000)  # the whole range is below the minimum
    assert PayContext(floor=80_000).clamp(50_000) == 80_000  # no range posted: still never below the minimum


def test_the_figure_worked_out_without_the_chat_is_the_middle_of_the_band_and_tidy():
    assert pay(100_000, 110_000).aim() == 105_000  # 47.5% of the way
    assert pay(130_000, 187_000).aim() == 155_000  # 130,000 + 0.475 * 57,000 = 157,075 -> nearest $5,000
    assert pay(100_000, 110_000, band_low=0, band_high=0).aim() == 100_000
    assert PayContext(fallback="$140,000").aim() == 140_000  # no range posted: your saved default
    assert PayContext().aim() == 0  # nothing at all: nothing is invented
    assert (tidy(104_750), tidy(87_400), tidy(156_000)) == (105_000, 87_000, 155_000)


def test_a_range_in_the_description_is_used_when_the_posting_page_gave_none():
    text, low, high, period = range_in_text("About the role.\nThe base salary range for this role is $135,000 - $165,000 per year plus equity.")
    assert (low, high, period) == (135_000, 165_000, "year")
    assert range_in_text("We raised $50,000 - $60,000 from angels.")[2] == 0  # not about pay: money in another sense
    job = JobItem.create("https://x.example/1")
    job.job_description = "Pay range: $90K - $110K per year."
    context = pay_context_for(job, AppSettings(salary_floor=85_000))
    assert (context.low, context.high, context.floor) == (90_000, 110_000, 85_000)
    job.salary_min, job.salary_max, job.salary_period, job.salary_currency = 70_000, 80_000, "year", "USD"
    assert pay_context_for(job, AppSettings()).high == 80_000  # what the posting page said wins


def test_the_figure_in_a_reply_is_read_whatever_way_it_is_written():
    assert [figure_in(t) for t in ("105000", "$105,000", "105k", "105K per year", "$50 per hour", "")] == [
        105_000, 105_000, 105_000, 105_000, 104_000, 0]


# ------------------------------------------------------------------ writing the figure

def test_the_figure_is_written_in_the_shape_the_question_needs():
    p = pay()
    p.target = 105_000
    assert render(p, "What are your salary expectations?", "text") == "$105,000"
    assert render(p, "Desired salary", "number") == "105000"
    assert render(p, "What is your desired salary range?", "text") == "$100,000 - $110,000"  # inside the posted range
    assert render(p, "Expected hourly rate", "text") == "$50 per hour" and render(p, "Hourly rate", "number") == "50"
    assert render(p, "Salary expectations", "text", max_chars=5) == "10500"  # too long with the $ and comma: digits
    assert render(p, "Salary expectations", "text", max_chars=4) == "1050"


def test_a_dropdown_gets_the_option_that_holds_the_figure():
    assert pick_bucket(["Under $80K", "$80K - $100K", "$100,000 - $120,000", "$120K+"], 105_000) == "$100,000 - $120,000"
    assert pick_bucket(["< 90", "90-110", "110+"], 105_000) == "90-110"  # bare numbers in a salary list are thousands
    assert pick_bucket(["$40 - $50 / hour", "$50 - $60 / hour"], 104_000) == "$50 - $60 / hour"
    assert pick_bucket(["Under $80K", "$80K - $100K"], 250_000) == "$80K - $100K"  # none holds it: the closest
    assert pick_bucket(["Competitive", "Negotiable"], 105_000) == ""  # not amounts: the chat is asked instead
    assert parse_bucket("$150K+") == (150_000.0, float("inf")) and parse_bucket("Under $80K") == (0.0, 80_000.0)


# ------------------------------------------------------------------ the prompt

def test_the_prompt_states_the_facts_the_goal_and_the_exact_output_format():
    facts = {"low": 100_000, "high": 110_000, "period": "year", "currency": "USD", "floor": 80_000,
             "band_low": 40, "band_high": 55}
    text = build_question_prompt(Question("Minimum base salary?", kind="salary", extra=facts))
    assert "Posted pay range for this job: 100,000 - 110,000 per year, USD." in text
    assert "My minimum acceptable pay: 80,000 per year. Never go below it." in text
    assert "about 40-55% of the way from the low end to the high end" in text
    assert "Never go above the posted high end. Never go below my minimum." in text
    assert "digits only, per year" in text and "[[ANSWER_START]] and [[ANSWER_END]]" in text
    none = build_salary_prompt({"floor": 80_000, "band_low": 40, "band_high": 55}, "Salary?")
    assert "No pay range is posted for this job." in none and "estimate what this role typically pays" in none
    choice = build_question_prompt(Question("Salary band?", options=["< 90", "90-110"], kind="salary", figure=104_000, extra=facts))
    assert "My figure is $104,000 per year." in choice and "Options: < 90 | 90-110" in choice and "digits only" not in choice


# ------------------------------------------------------------------ the answer rule

def field(label, kind="text", options=None, required=True, **kw) -> Field:
    return Field(ref="f", kind=kind, label=label, required=required, options=[Option(o) for o in options or []], **kw)


def ctx(p=None, **kw) -> Context:
    return Context(profile=ProfileSettings(name="Mary Berg"), facts={"salary_expectation": "$140,000"}, pay=p, **kw)


def test_a_salary_question_is_left_to_the_salary_rule_only_when_the_job_has_pay_facts():
    q = field("What are your salary expectations?")
    assert decide(q, ctx(pay())).source == "salary" and decide(q, ctx(pay())).action == CHAT
    assert (decide(q, ctx()).action, decide(q, ctx()).value) == (FILL, "$140,000")  # no pay context: the saved fact, as before
    current = field("What is your current salary?")
    assert decide(current, ctx(pay())).action in {SKIP, NEEDS}  # never answered, never sent to a chat
    optional = field("Salary expectations", required=False)
    assert decide(optional, Context(profile=ProfileSettings(), pay=pay(), skip_optional=True)).action == SKIP


class Chat:
    def __init__(self, *replies):
        self.replies, self.asked = list(replies), []

    def __call__(self, question: Question) -> str:
        self.asked.append(question)
        return self.replies.pop(0) if self.replies else "0"


def ask(chat, p, f):
    return GreenhouseApplier(answer=chat)._ask_salary(f, ctx(p))


def test_the_chat_picks_the_figure_and_it_is_kept_inside_the_limits():
    chat = Chat("107000")
    p = pay()
    d = ask(chat, p, field("Salary expectations"))
    assert (d.action, d.value, d.source) == (FILL, "$107,000", "salary")
    assert d.reason == "Posted $100,000 - $110,000; aimed at 70% of the range (ChatGPT's figure)"
    q = chat.asked[0]
    assert q.kind == "salary" and q.extra["high"] == 110_000 and q.extra["floor"] == 80_000
    assert ask(Chat("150000"), pay(), field("Salary")).value == "$110,000"  # too high: the posted maximum
    assert ask(Chat("50000"), pay(), field("Salary")).value == "$100,000"  # too low: the bottom of the range
    assert ask(Chat("50000"), pay(60_000, 70_000), field("Salary")).value == "$80,000"  # range below your minimum: the minimum


def test_one_figure_serves_every_salary_question_of_the_form_and_the_chat_is_asked_once():
    chat = Chat("105000")
    p = pay()
    applier = GreenhouseApplier(answer=chat)
    first = applier._ask_salary(field("Minimum base salary", "number"), ctx(p))
    second = applier._ask_salary(field("Desired salary range"), ctx(p))
    third = applier._ask_salary(field("Hourly rate"), ctx(p))
    assert (first.value, second.value, third.value) == ("105000", "$100,000 - $110,000", "$50 per hour")
    assert len(chat.asked) == 1


def test_without_the_chat_the_figure_is_worked_out_here_and_a_job_with_no_range_uses_your_default():
    no_chat = GreenhouseApplier(answer=None)
    assert no_chat._ask_salary(field("Salary"), ctx(pay())).value == "$105,000"
    d = no_chat._ask_salary(field("Salary"), ctx(PayContext(fallback="$140,000")))
    assert d.value == "$140,000" and "No range posted" in d.reason
    nothing = no_chat._ask_salary(field("Salary"), ctx(PayContext()))
    assert nothing.action == NEEDS and "No pay range is posted" in nothing.reason
    assert no_chat._ask_salary(field("Salary", required=False), ctx(PayContext())).action == SKIP
    broken = GreenhouseApplier(answer=lambda q: (_ for _ in ()).throw(RuntimeError("chat down")))
    assert broken._ask_salary(field("Salary"), ctx(pay())).value == "$105,000"  # the chat failing does not stop it


def test_a_job_with_no_posted_range_lets_the_chat_estimate_but_never_below_the_minimum():
    d = ask(Chat("95000"), PayContext(floor=80_000, fallback="$140,000"), field("Salary"))
    assert d.value == "$95,000" and d.reason.startswith("No range posted") and "your minimum of $80,000" in d.reason
    assert ask(Chat("60000"), PayContext(floor=80_000), field("Salary")).value == "$80,000"


def test_a_salary_dropdown_is_answered_by_the_option_that_holds_the_figure():
    options = ["Under $80K", "$80K - $100K", "$100,000 - $120,000", "$120K+"]
    chat = Chat("105000")
    d = ask(chat, pay(), field("Salary range", "select", options))
    assert (d.action, d.value) == (FILL, "$100,000 - $120,000") and len(chat.asked) == 1  # only the figure was asked
    odd = Chat("105000", "Competitive")
    d2 = ask(odd, pay(), field("Salary band", "select", ["Competitive", "Negotiable", "Other"]))
    assert d2.value == "Competitive" and odd.asked[1].options == ["Competitive", "Negotiable", "Other"]
    assert odd.asked[1].figure == 105_000  # the chat is told the figure the option must fit
    lost = ask(Chat("105000", "no idea"), pay(), field("Salary band", "select", ["Competitive", "Negotiable"]))
    assert lost.action == NEEDS and "No salary option fits $105,000" in lost.reason


# ------------------------------------------------------------------ the record

def test_the_record_shows_the_salary_with_its_basis_and_it_is_not_treated_as_private():
    plan = [{"label": "What are your salary expectations?", "kind": "text", "action": "fill", "source": "salary",
             "required": True, "value": "$105,000",
             "reason": "Posted $100,000 - $110,000; aimed at 50% of the range (ChatGPT's figure)"}]
    (row,), _ = rows_from_plan(plan)
    assert row.source == "salary" and not row.private and row.note.startswith("Posted $100,000")
    job = JobItem.create("https://x.example/1")
    full = format_record(job, plan, __import__("datetime").datetime.now(), private=False)
    assert "$105,000" in full and "Basis: Posted $100,000 - $110,000" in full  # visible even in the Telegram copy
    from core import report_format as fmt

    text = fmt.applied(job, plan, "FILE").text
    assert "💰 $105,000" in text and "aimed at 50% of the range" in text and "🔒" not in text


# ------------------------------------------------------------------ discovery: jobs that pay too little

def test_a_job_whose_posted_pay_tops_out_below_your_minimum_is_not_queued():
    rules = DiscoveryRules.from_text("", "", 80_000)
    low = Listing("a", title="Dev", company="Co", salary_text="$60K/yr - $70K/yr")
    ok = Listing("b", title="Dev", company="Co", salary_text="$70K/yr - $95K/yr")  # the top is above the minimum
    hourly = Listing("c", title="Dev", company="Co", salary_text="$30 - $35 per hour")  # 72,800 a year at most
    unknown = Listing("d", title="Dev", company="Co", salary_text="")
    other = Listing("e", title="Dev", company="Co", salary_text="£40,000 - £50,000")  # not dollars: not compared
    assert "tops out at $70,000 a year, below your minimum of $80,000" in rules.reject_reason(low)
    assert [rules.reject_reason(x) for x in (ok, unknown, other)] == ["", "", ""]
    assert "$72,800" in rules.reject_reason(hourly)
    assert DiscoveryRules.from_text("", "", 0).reject_reason(low) == ""  # switched off
    assert posted_maximum("$60K/yr - $70K/yr") == 70_000 and posted_maximum("") == 0
    assert reason_kind("filtered", rules.reject_reason(low)) == "pay below your minimum"


def test_the_settings_for_pay_have_safe_defaults_and_limits():
    s = AppSettings()
    assert (s.salary_floor, s.salary_band_low, s.salary_band_high, s.discovery_skip_below_floor) == (80_000, 40, 55, True)
    bad = AppSettings.from_dict({"salary_floor": "x", "salary_band_low": 70, "salary_band_high": 20})
    assert bad.salary_floor == 80_000 and bad.salary_band_low == 70 and bad.salary_band_high == 70  # high is never below low
    assert AppSettings.from_dict({"salary_band_low": 500}).salary_band_low == 100
    assert AppSettings.from_dict(AppSettings(salary_floor=95_000).to_dict()).salary_floor == 95_000


def test_a_job_is_only_left_out_for_its_pay_when_the_posting_clearly_gives_a_ceiling():
    for text in ("", "$1", "$30 - $35", "$60K+", "from $60K", "starting at $50,000", "£40,000 - £50,000", "Competitive"):
        assert posted_maximum(text) == 0, text  # unknown: never a reason to drop a job
    assert posted_maximum("$60K/yr - $70K/yr") == 70_000
    assert posted_maximum("$30 - $35 per hour") == 72_800
    assert posted_maximum("$4,000 - $5,000 per month") == 60_000
    assert posted_maximum("Up to $90,000 a year") == 90_000
