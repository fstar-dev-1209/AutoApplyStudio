from __future__ import annotations

import pytest

from core.salary import format_salary, parse_salary


@pytest.mark.parametrize(
    "text, low, high, period, currency",
    [
        ("$120K - $150K/yr", 120000, 150000, "year", "USD"),
        ("$120,000 - $150,000 per year", 120000, 150000, "year", "USD"),
        ("$95,000", 95000, 95000, "year", "USD"),
        ("$55 - $65/hr", 55, 65, "hour", "USD"),
        ("CA$55 - 65 per hour", 55, 65, "hour", "CAD"),
        ("£70k to £85k a year", 70000, 85000, "year", "GBP"),
        ("$8,000/mo", 8000, 8000, "month", "USD"),
    ],
)
def test_parse_salary_reads_ranges_periods_and_currency(text, low, high, period, currency):
    pay = parse_salary(text)
    assert (pay.low, pay.high, pay.period, pay.currency) == (low, high, period, currency)
    assert pay.text == text


def test_text_without_money_keeps_the_words_but_no_range():
    pay = parse_salary("Competitive")
    assert pay.text == "Competitive" and (pay.low, pay.high) == (0, 0)
    assert not parse_salary("")


def test_a_plain_number_is_not_mistaken_for_pay():
    assert parse_salary("5 days a week").low == 0


def test_format_prefers_the_postings_own_wording():
    assert format_salary("$120K - $150K/yr", 1, 2) == "$120K - $150K/yr"
    assert format_salary("", 120000, 150000, "year", "USD") == "$120,000 – $150,000 / year"
    assert format_salary("") == ""
