"""Alerts: delivered to the right places, not repeated, and never leaking the secret address."""

from __future__ import annotations

import httpx

from core.notify import Notifier, valid_discord_webhook

HOOK = "https://discord.com/api/webhooks/123456789/abcDEF-ghi_jkl"


def make(handler, **kw):
    sent = []

    def respond(request: httpx.Request):
        sent.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    return Notifier(client=client, background=False, **kw), sent


def test_an_alert_goes_to_the_app_discord_and_telegram():
    shown = []
    n, sent = make(lambda r: httpx.Response(204), discord_webhook=lambda: HOOK,
                   telegram=lambda: ("123:ABC", "555"), in_app=lambda t, b: shown.append((t, b)))
    assert n.send("Needs you: Acme", "Which office?") is True
    assert shown == [("Needs you: Acme", "Which office?")]
    urls = [str(r.url) for r in sent]
    assert HOOK in urls and "https://api.telegram.org/bot123:ABC/sendMessage" in urls
    discord = next(r for r in sent if str(r.url) == HOOK)
    assert b"Needs you: Acme" in discord.content and b"Which office?" in discord.content
    telegram = next(r for r in sent if "telegram" in str(r.url))
    assert b"chat_id=555" in telegram.content


def test_the_same_alert_is_not_repeated_within_ten_minutes():
    now = [1000.0]
    n, sent = make(lambda r: httpx.Response(204), discord_webhook=lambda: HOOK, clock=lambda: now[0])
    assert n.send("Jobright needs you to sign in", "x") and not n.send("Jobright needs you to sign in", "x")
    assert len(sent) == 1
    now[0] += 601
    assert n.send("Jobright needs you to sign in", "x") and len(sent) == 2
    assert n.send("A different problem")  # a different alert is never held back


def test_nothing_configured_means_in_app_only_and_nothing_raises():
    shown = []
    n, sent = make(lambda r: httpx.Response(204), in_app=lambda t, b: shown.append(t))
    assert n.send("hello") and shown == ["hello"] and sent == []


def test_delivery_problems_are_recorded_without_the_secret():
    def boom(request):
        raise httpx.ConnectError("no route")

    n, _ = make(boom, discord_webhook=lambda: HOOK, telegram=lambda: ("123:SECRET", "5"))
    assert n.send("x")  # does not raise
    assert len(n.failures) == 2
    assert all("SECRET" not in f and "abcDEF" not in f for f in n.failures)

    refused, _ = make(lambda r: httpx.Response(401), discord_webhook=lambda: HOOK)
    refused.send("y")
    assert "HTTP 401" in refused.failures[0]


def test_only_real_discord_webhook_addresses_are_used():
    assert valid_discord_webhook(HOOK)
    for bad in ("", "http://discord.com/api/webhooks/1/a", "https://evil.example/api/webhooks/1/a",
                "https://discord.com.evil.example/api/webhooks/1/a", "https://discord.com/api/webhooks/x/a"):
        assert not valid_discord_webhook(bad)
    n, sent = make(lambda r: httpx.Response(204), discord_webhook=lambda: "https://evil.example/hook")
    n.send("z")
    assert sent == [] and "not valid" in n.failures[0]  # a mistyped address is never posted to
