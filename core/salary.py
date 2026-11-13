"""Read pay out of the text a job posting shows, e.g. "$120K - $150K/yr" or "CA$55 - 65 per hour"."""

from __future__ import annotations

import re
from dataclasses import dataclass

_CURRENCY = {"$": "USD", "US$": "USD", "USD": "USD", "CA$": "CAD", "C$": "CAD", "CAD": "CAD",
             "£": "GBP", "GBP": "GBP", "€": "EUR", "EUR": "EUR", "A$": "AUD", "AUD": "AUD",
             "₹": "INR", "INR": "INR"}
_PERIODS = (
    ("hour", r"/\s*h(?:ou)?r\b|per\s+hour|hourly|an\s+hour|\bph\b"),
    ("day", r"/\s*day\b|per\s+day|daily"),
    ("week", r"/\s*w(?:ee)?k\b|per\s+week|weekly"),
    ("month", r"/\s*mo(?:nth)?\b|per\s+month|monthly"),
    ("year", r"/\s*y(?:ea)?r\b|per\s+year|per\s+annum|annual(?:ly)?|\bp\.?a\.?\b|a\s+year"),
)
_AMOUNT = re.compile(
    r"(?P<cur>US\$|CA\$|A\$|C\$|[$£€₹]|\b(?:USD|CAD|GBP|EUR|AUD|INR)\b)?\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<k>[kK]\b)?",
)


@dataclass(frozen=True)
class Salary:
    text: str = ""
    low: int = 0
    high: int = 0
    period: str = ""
    currency: str = ""

    def __bool__(self) -> bool:
        return bool(self.text)


def _period(text: str) -> str:
    for name, pattern in _PERIODS:
        if re.search(pattern, text, re.I):
            return name
    return ""


def parse_salary(text: str) -> Salary:
    """Keeps the original wording; the range is filled only when it is clear."""
    raw = " ".join((text or "").split())[:200]
    if not raw:
        return Salary()
    period = _period(raw)
    amounts: list[tuple[float, str]] = []
    for m in _AMOUNT.finditer(raw):
        value = float(m.group("num").replace(",", ""))
        if m.group("k"):
            value *= 1000
        cur = (m.group("cur") or "").upper()
        # A bare number only counts when a currency sign or a "k" shows it is money
        if not cur and not m.group("k") and not amounts:
            continue
        amounts.append((value, _CURRENCY.get(cur, "")))
    if not amounts:
        return Salary(text=raw, period=period)
    values = [v for v, _ in amounts[:2]]
    currency = next((c for _, c in amounts if c), "USD" if "$" in raw else "")
    if not period:  # "$120,000 - $150,000" with no unit is a yearly figure; small numbers are not
        period = "year" if max(values) >= 1000 else ("hour" if max(values) < 500 else "")
    low, high = (min(values), max(values)) if len(values) > 1 else (values[0], values[0])
    return Salary(text=raw, low=int(round(low)), high=int(round(high)), period=period, currency=currency)


def format_salary(text: str, low: int = 0, high: int = 0, period: str = "", currency: str = "") -> str:
    """What the app shows: the posting's own wording when there is any, else the stored range."""
    if text:
        return text
    if not low and not high:
        return ""
    sym = {"USD": "$", "CAD": "CA$", "GBP": "£", "EUR": "€", "AUD": "A$", "INR": "₹"}.get(currency, "")
    span = f"{sym}{low:,}" if low == high else f"{sym}{low:,} – {sym}{high:,}"
    return span + (f" / {period}" if period else "")
