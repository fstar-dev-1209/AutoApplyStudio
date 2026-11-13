"""The browser host against a real Chrome (headless) and a local web server."""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.browser_host import BrowserConfig, BrowserError, BrowserHost, browser_args, browser_closed, find_browser

try:
    find_browser("chrome")
except BrowserError:  # pragma: no cover - CI without Chrome
    pytest.skip("Chrome is not installed", allow_module_level=True)

pytest.importorskip("playwright")


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"<html><head><title>Hello bot</title></head><body><h1 id=x>hi</h1></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _config(tmp_path, **kw) -> BrowserConfig:
    return BrowserConfig(profile_dir=tmp_path / "profile", headless=True, debug_port=_free_port(), **kw)


def _title(url):
    def task(context):
        page = context.new_page()
        try:
            page.goto(url)
            return page.title()
        finally:
            page.close()
    return task


def test_attach_mode_starts_chrome_runs_tasks_on_one_thread_and_stops(tmp_path, site):
    host = BrowserHost(_config(tmp_path, mode="attach"))
    host.start()
    try:
        assert host.running
        assert host.run(_title(site)) == "Hello bot"
        threads = {host.run(lambda ctx: threading.get_ident()) for _ in range(3)}
        assert len(threads) == 1, "every task must run on the browser thread"
        # a profile of its own was created for the bot
        assert (tmp_path / "profile").is_dir()
    finally:
        host.stop()
    assert not host.running
    with pytest.raises(BrowserError):
        host.run(lambda ctx: 1)


def test_launch_mode_works_too(tmp_path, site):
    host = BrowserHost(_config(tmp_path, mode="launch"))
    host.start()
    try:
        assert host.run(_title(site)) == "Hello bot"
    finally:
        host.stop()


def test_a_failing_task_reports_its_error_and_the_browser_keeps_working(tmp_path, site):
    host = BrowserHost(_config(tmp_path, mode="attach"))
    host.start()
    try:
        def boom(ctx):
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            host.run(boom)
        assert host.run(_title(site)) == "Hello bot"
    finally:
        host.stop()


def test_a_closed_browser_is_recognised_from_the_driver_error():
    assert browser_closed(RuntimeError("Page.goto: Target page, context or browser has been closed"))
    assert not browser_closed(RuntimeError("net::ERR_TIMED_OUT"))


def test_mode_follows_the_proxy_login():
    base = {"profile_dir": None}
    assert BrowserConfig(**base).effective_mode == "attach"
    assert BrowserConfig(**base, proxy="http://h:1").effective_mode == "attach"
    assert BrowserConfig(**base, proxy="http://h:1", proxy_user="u").effective_mode == "launch"
    assert BrowserConfig(**base, proxy_user="u", mode="attach").effective_mode == "attach"


def test_browser_switches_keep_the_real_address_private_behind_a_proxy():
    args = browser_args(BrowserConfig(profile_dir=None, proxy="http://1.2.3.4:8080"))
    assert "--proxy-server=http://1.2.3.4:8080" in args
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args
    assert not any("proxy" in a or "webrtc" in a for a in browser_args(BrowserConfig(profile_dir=None)))
