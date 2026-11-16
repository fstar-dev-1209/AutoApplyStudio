"""The salary asked for on an application form: one figure per job, chosen inside the job's own pay range.

How it works
  1. The limits are facts, worked out here: the range the posting shows (yearly, in dollars), and your minimum.
  2. The chat that wrote this job's resume is asked once for a single figure, aiming at the lower-middle of the range
     (40-55% of the way from the low end to the high end by default). It sees the job description, so it can judge
     the seniority and the scope.
  3. The figure is checked and kept inside the limits (never above the posted maximum, never below your minimum),
     then written out in whatever shape each question needs: digits, "$112,000", a low-high range, an hourly rate, or the
     option of a dropdown that contains it. The same figure is used for every salary question of the form.
If the chat cannot be reached the figure is worked out here (the middle of the band), and a job with no posted range
uses the default saved in your answers. A job that pays below your minimum is skipped at discovery.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.chat_questions import END, START
from core.salary import parse_salary

HOURS_PER_YEAR = 2080
_PER_YEAR = {"year": 1, "month": 12, "week": 52, "day": 260, "hour": HOURS_PER_YEAR}
_HOURLY_QUESTION = re.compile(r"hourly|per hour|an hour|/\s*h(?:ou)?r\b", re.I)
_RANGE_QUESTION = re.compile(r"\brange\b|\bbetween\b|min(?:imum)?\b.{0,30}\bmax|\bfrom\b.{0,20}\bto\b", re.I)
_STATED_PAY = re.compile(
    r"(?:\$|USD\s?)\s?\d{2,3}(?:,\d{3}|(?:\.\d)?\s?[kK])\s*(?:-|–|—|to)\s*(?:\$|USD\s?)?\s?\d{2,3}(?:,\d{3}|(?:\.\d)?\s?[kK])"
    r"(?:\s*(?:/|per\s)\s*(?:yr|year|annum|hr|hour))?", re.I)


@dataclass
class PayContext:
    """What is known about the pay of one job, and the rules for choosing a figure. ``target`` and ``basis`` are
    filled in the first time a salary question is answered and reused for the rest of the form."""

    low: int = 0  # the posted range, in the posting's own period
    high: int = 0
    period: str = ""
    currency: str = ""
    text: str = ""  # the posting's own wording, for the record
    floor: int = 80_000  # your minimum, per year
    band_low: int = 40  # aim at this share of the way from the low end to the high end ...
    band_high: int = 55  # ... up to this one
    fallback: str = ""  # your saved default ("$140,000"), used when no range is posted
    target: int = 0
    basis: str = ""

    # ------------------------------------------------------------------ the limits

    def posted(self) -> tuple[int, int] | None:
        """The posted range as dollars per year, or None when there is none (or it is not in dollars)."""
        if not self.high or self.currency not in ("", "USD"):
            return None
        period = self.period or ("year" if self.high >= 1000 else "hour" if self.high < 500 else "")
        factor = _PER_YEAR.get(period)
        if not factor:
            return None
        return int((self.low or self.high) * factor), int(self.high * factor)

    def limits(self) -> tuple[int, int] | None:
        """The yearly range an answer must stay in: the posted range, never lower than your minimum."""
        posted = self.posted()
        if posted is None:
            return None
        return max(posted[0], self.floor), max(posted[1], self.floor)

    def clamp(self, value: int) -> int:
        limits = self.limits()
        if limits is None:
            return max(int(value), self.floor)
        return min(max(int(value), limits[0]), limits[1])

    # ------------------------------------------------------------------ a figure without the chat

    def aim(self) -> int:
        """The figure worked out here: the middle of the band inside the posted range, else your saved default."""
        posted = self.posted()
        if posted is not None:
            share = (self.band_low + self.band_high) / 200
            return self.clamp(tidy(posted[0] + (posted[1] - posted[0]) * share))
        return self.clamp(figure_in(self.fallback)) if figure_in(self.fallback) else 0

    def describe_posted(self) -> str:
        posted = self.posted()
        if posted is None:
            return ""
        lo, hi = posted
        return f"${lo:,}" if lo == hi else f"${lo:,} - ${hi:,}"

    def position(self) -> int | None:
        """Where the figure sits in the posted range, in percent (None without a range)."""
        posted = self.posted()
        if posted is None or posted[1] <= posted[0] or not self.target:
            return None
        return round(100 * (self.target - posted[0]) / (posted[1] - posted[0]))


def tidy(value: float) -> int:
    """A clean figure: the nearest $1,000, or the nearest $5,000 from $100,000 up."""
    step = 5000 if value >= 100_000 else 1000
    return int(round(value / step) * step)


def figure_in(text: str) -> int:
    """The yearly dollar figure in a piece of text ("$112,000", "112k", "54 per hour"); 0 when there is none."""
    sal = parse_salary(text or "")
    amount = sal.high or sal.low
    if not amount:
        digits = re.search(r"\d[\d,]*", text or "")
        amount = int(digits.group(0).replace(",", "")) if digits else 0
    if not amount:
        return 0
    period = sal.period or ("hour" if amount < 500 else "year")
    return int(amount * _PER_YEAR.get(period, 1))


def range_in_text(description: str) -> tuple[str, int, int, str]:
    """A pay range stated in the job description ("$120,000 - $150,000"): (text, low, high, period) or empty."""
    for m in _STATED_PAY.finditer(description or ""):
        near = (description[max(0, m.start() - 160): m.end() + 80]).casefold()
        if re.search(r"salary|compensation|pay|range|base|wage|rate", near):
            sal = parse_salary(m.group(0))
            if sal.high:
                return sal.text, sal.low, sal.high, sal.period
    return "", 0, 0, ""


# ------------------------------------------------------------------ writing the figure for a field

def wants_range(label: str) -> bool:
    return bool(_RANGE_QUESTION.search(label or ""))


def wants_hourly(label: str) -> bool:
    return bool(_HOURLY_QUESTION.search(label or ""))


def render(pay: PayContext, label: str, kind: str, max_chars: int = 0) -> str:
    """The figure in the shape the field needs. ``kind`` is the form field's kind (text, number, textarea ...)."""
    target = pay.target
    if wants_hourly(label):
        value = round(target / HOURS_PER_YEAR)
        return str(value) if kind == "number" else f"${value} per hour"
    if kind == "number":
        return str(target)
    if wants_range(label):
        low, high = pay.clamp(tidy(target * 0.95)), pay.clamp(tidy(target * 1.10))
        text = f"${low:,} - ${high:,}" if low != high else f"${low:,}"
    else:
        text = f"${target:,}"
    if max_chars and len(text) > max_chars:
        text = str(target)
    return text[:max_chars] if max_chars else text


_NUM = re.compile(r"(\d+(?:\.\d+)?)\s*([km])?\b")
_BELOW = re.compile(r"under|less than|below|up to|fewer than|<|≤")
_ABOVE = re.compile(r"over|more than|above|at least|\+|>|≥|or more|and up")


def parse_bucket(label: str) -> tuple[float, float] | None:
    """The yearly range of a dropdown choice such as "$100,000 - $120,000", "100-120K", "Under $80K" or "$150K+"."""
    text = (label or "").casefold().replace(",", "")
    hourly = bool(re.search(r"/\s*h(?:ou)?r|per hour|hourly|\bph\b", text))
    numbers = []
    for m in _NUM.finditer(text):
        value = float(m.group(1))
        if m.group(2) == "k":
            value *= 1000
        elif m.group(2) == "m":
            value *= 1_000_000
        numbers.append(value)
    if not numbers:
        return None
    if hourly:
        numbers = [n * HOURS_PER_YEAR for n in numbers]
    elif max(numbers) < 1000:
        numbers = [n * 1000 for n in numbers]  # "100 - 120" in a salary list means thousands
    if len(numbers) >= 2:
        return min(numbers[:2]), max(numbers[:2])
    n = numbers[0]
    if _BELOW.search(text):
        return 0.0, n
    if _ABOVE.search(text):
        return n, float("inf")
    return n, n


def pick_bucket(options: list[str], target: int) -> str:
    """The option whose range holds ``target`` (the closest one when none does); '' when the options are not amounts."""
    parsed = [(o, parse_bucket(o)) for o in options]
    parsed = [(o, b) for o, b in parsed if b is not None]
    if len(parsed) < 2:
        return ""
    for option, (lo, hi) in parsed:  # a figure on the line between two buckets belongs to the upper one
        if lo <= target < hi:
            return option
    for option, (lo, hi) in parsed:
        if lo <= target <= hi:
            return option
    return min(parsed, key=lambda ob: min(abs(target - ob[1][0]), abs(target - ob[1][1])))[0]


# ------------------------------------------------------------------ the prompt

def build_salary_prompt(extra: dict, question: str, options: list[str] | None = None, figure: int = 0) -> str:
    """The question put to the chat. ``extra`` carries the facts: low, high, period, currency, floor, band_low, band_high.
    With ``options`` the chat chooses a dropdown option that holds ``figure``; without, it names the figure."""
    low, high = int(extra.get("low") or 0), int(extra.get("high") or 0)
    period, currency = extra.get("period") or "year", extra.get("currency") or "USD"
    if high:
        posted = (f"- Posted pay range for this job: {low:,} - {high:,} per {period}, {currency}." if low != high
                  else f"- Posted pay for this job: {high:,} per {period}, {currency}.")
    else:
        posted = "- No pay range is posted for this job."
    floor = int(extra.get("floor") or 0)
    band_low, band_high = int(extra.get("band_low") or 40), int(extra.get("band_high") or 55)
    lines = [
        "You are choosing the salary figure for this job's application form.",
        "",
        "FACTS (given by me, do not change them):",
        posted,
        f"- My minimum acceptable pay: {floor:,} per year. Never go below it.",
        "",
        "GOAL",
        "Pick ONE realistic figure for THIS role, using its seniority, scope, location and the experience the job "
        "description asks for.",
        f"The figure should be in the LOWER-MIDDLE of the posted range: about {band_low}-{band_high}% of the way from the "
        "low end to the high end. It should be competitive enough to get a recruiter reply, and it should never look "
        "like a stretch. Never go above the posted high end. Never go below my minimum.",
        "If no range is posted: estimate what this role typically pays in the US for this seniority and location, aim "
        "at the middle of it, and still stay at or above my minimum.",
        "Round to a clean number: the nearest $1,000, or the nearest $5,000 above $100,000.",
        "",
        "THE FORM FIELD",
        f'Question: "{question.strip()}"',
    ]
    if options:
        lines += [f"My figure is ${figure:,} per year." if figure else "",
                  "Options: " + " | ".join(options),
                  "Choose exactly one option, copied word for word, whose range contains my figure (the closest one if none does).",
                  "", "OUTPUT FORMAT", "The option text, exactly as written. No explanations, no extra words."]
    else:
        lines += ["Field type: number", "",
                  "OUTPUT FORMAT",
                  "The figure as digits only, per year: no $, no commas, no words. "
                  "If the question asks for an hourly rate, still answer per year (it is converted afterwards).",
                  "No explanations, no extra words."]
    lines += ["", f"Reply with ONLY the answer between the markers {START} and {END}."]
    return "\n".join(line for i, line in enumerate(lines) if line != "" or (i and lines[i - 1] != ""))


def pay_context_for(job, settings) -> PayContext:
    """The pay facts of one job and your rules (from Settings), ready for the form filler."""
    low, high, period, currency, text = job.salary_min, job.salary_max, job.salary_period, job.salary_currency, job.salary_text
    if not high:
        stated, lo, hi, per = range_in_text(job.job_description)
        if hi:
            low, high, period, currency, text = lo, hi, per, "USD", stated
    return PayContext(
        low=low, high=high, period=period, currency=currency, text=text, floor=settings.salary_floor,
        band_low=settings.salary_band_low, band_high=settings.salary_band_high,
        fallback=str((settings.apply_facts or {}).get("salary_expectation") or ""))
