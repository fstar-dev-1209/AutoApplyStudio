"""The bot's service container: one browser, reopened only when it must be, and the company rules it shares."""

from __future__ import annotations

import pytest

from core import bot_services
from core.applications import ApplicationService
from core.bot_services import BotServices, browser_config
from core.models import JobItem, QueueStore
from core.paths import company_dir
from core.settings_store import SettingsStore


class FakeHost:
    instances: list["FakeHost"] = []

    def __init__(self, config):
        self.config, self.running, self.started, self.stopped = config, False, 0, 0
        FakeHost.instances.append(self)

    def start(self):
        self.running, self.started = True, self.started + 1

    def stop(self):
        self.running, self.stopped = False, self.stopped + 1


@pytest.fixture
def services(tmp_path, monkeypatch):
    FakeHost.instances = []
    monkeypatch.setattr(bot_services, "BrowserHost", FakeHost)
    store = QueueStore(tmp_path / "studio.db")
    settings = SettingsStore(tmp_path / "settings.json")
    settings.settings.output_root = str(tmp_path / "Remote")
    bot = BotServices(store, ApplicationService(store.database), settings, data_dir=tmp_path)
    yield bot, settings, store
    store.database.close()


def test_the_browser_is_started_once_and_reused(services):
    bot, _, _ = services
    first = bot.host()
    assert bot.host() is first and first.started == 1 and bot.browser_running
    assert len(FakeHost.instances) == 1


def test_changing_the_proxy_or_browser_reopens_it_but_other_settings_do_not(services):
    bot, settings, _ = services
    first = bot.host()
    settings.settings.discovery_interval_minutes = 5  # nothing to do with the browser
    assert bot.host() is first
    settings.settings.web_proxy = "http://1.2.3.4:8080"
    second = bot.host()
    assert second is not first and first.stopped == 1 and second.config.proxy == "http://1.2.3.4:8080"
    settings.set_proxy_credentials("user", "pw")
    third = bot.host()
    assert third is not second and third.config.proxy_user == "user" and third.config.effective_mode == "launch"


def test_a_browser_that_died_is_started_again(services):
    bot, _, _ = services
    host = bot.host()
    host.running = False
    assert bot.host() is host and host.started == 2


def test_the_browser_profile_is_the_bots_own_folder(services, tmp_path):
    bot, settings, _ = services
    cfg = browser_config(settings.settings, ("", ""), tmp_path / "browser" / "bot")
    assert cfg.profile_dir == tmp_path / "browser" / "bot" and cfg.channel == "chrome"
    assert bot.host().config.profile_dir == tmp_path / "browser" / "bot"


def test_discovery_skips_companies_already_applied_to_or_already_in_the_output_folder(services):
    bot, settings, store = services
    applied = JobItem.create("https://boards.greenhouse.io/acme/jobs/1")
    applied.company = "Acme"
    store.add_many([applied])
    ApplicationService(store.database).mark_applied(applied.id)
    assert "Already applied to Acme" in bot._company_reason("ACME")

    assert bot._company_reason("Zeta") == ""
    company_dir(settings.settings.resolved_output_root(), settings.settings.profile.name, "Zeta").mkdir(parents=True)
    assert "folder already exists" in bot._company_reason("Zeta")
    assert bot._company_reason("Nobody") == ""


def test_notifications_read_their_secrets_when_sending(services):
    bot, settings, _ = services
    settings.set_notify_secret("discord_webhook", "https://discord.com/api/webhooks/1/abc")
    settings.settings.notify_telegram_chat = "42"
    settings.set_notify_secret("telegram_token", "9:TOKEN")
    assert bot.notifier._discord() == "https://discord.com/api/webhooks/1/abc"
    assert bot.notifier._telegram() == ("9:TOKEN", "42")
