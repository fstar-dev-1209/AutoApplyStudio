"""Reading Greenhouse's emailed security code: the text parsing, and the inbox reader against a stand-in mailbox."""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import mail_code

BODY = ("Hi Mary, Copy and paste this code into the security code field on your application: 3Fz8Qk2L "
        "After you enter the code, resubmit your application. Good luck! Greenhouse")


@pytest.mark.parametrize("text, code", [
    (BODY, "3Fz8Qk2L"),
    ("Greenhouse Security code for your application to Acme Copy and paste this code into the security code field "
     "on your application: aBcDeFgH After you", "aBcDeFgH"),
    ("Security code for your application. Complete Continue Copy and paste this code ... application: AB12CD34", "AB12CD34"),
    ("Your application: QWERTYUI", "QWERTYUI"),  # all capitals still counts, but a digit beats it when present
    ("Newsletter: continue complete received reminder confirm", ""),
    ("", ""),
])
def test_the_code_is_found_among_ordinary_words(text, code):
    assert mail_code.parse_security_code(text) == code


def test_a_digit_beats_an_earlier_word_that_only_looks_like_a_code():
    text = "Security code Received ABCDEFGH or rather x9Yz3WvU is your code"
    assert mail_code.parse_security_code(text) == "x9Yz3WvU"


def test_only_code_mails_from_greenhouse_are_considered():
    assert mail_code.looks_like_code_mail("Greenhouse: Security code for your application to Acme")
    assert not mail_code.looks_like_code_mail("Your weekly digest from LinkedIn")
    assert not mail_code.looks_like_code_mail("Security code for your bank login")  # nothing about an application


pytest.importorskip("playwright")
from core.browser_host import BrowserConfig, BrowserError, BrowserHost, find_browser  # noqa: E402

try:
    find_browser("chrome")
except BrowserError:  # pragma: no cover
    pytest.skip("Chrome is not installed", allow_module_level=True)

INBOX = """<html><body><div role="listbox" id="list">
<div role="option" aria-label="LinkedIn Your weekly digest Jobs for you">LinkedIn</div>
<div role="option" aria-label="Greenhouse Security code for your application to OldCo Copy and paste this code into the security code field on your application: OLD0CODE">old</div>
</div>
<div aria-label="Message body" id="body"></div>
<script>
const list = document.getElementById('list');
function add(label, body) {
  const d = document.createElement('div'); d.setAttribute('role', 'option'); d.setAttribute('aria-label', label); d.textContent = label;
  d.addEventListener('click', () => { document.getElementById('body').textContent = body || ''; });
  list.insertBefore(d, list.firstChild);
}
// a second company's code mail, then Acme's whose preview is cut before the code (so the message must be opened)
setTimeout(() => add('Greenhouse Security code for your application to OtherCo Copy and paste this code into the security code field on your application: Oth3rCde'), 1500);
setTimeout(() => add('Greenhouse Security code for your application to Acme Copy and paste this code ...',
  'Copy and paste this code into the security code field on your application: Acm3C0de After you enter the code'), 2500);
</script></body></html>"""


class _Mail(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        data = INBOX.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def mailbox(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Mail)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    host = BrowserHost(BrowserConfig(profile_dir=tmp_path / "p", headless=True, debug_port=port, mode="attach"))
    host.start()
    yield host, f"http://127.0.0.1:{server.server_address[1]}/outlook.html"
    host.stop()
    server.shutdown()


def test_a_new_code_mail_is_found_and_old_ones_and_other_companies_are_ignored(mailbox):
    host, home = mailbox
    before = host.run(lambda ctx: mail_code.snapshot(ctx, home))
    assert len(before) == 2  # what was in the inbox before sending
    code = host.run(lambda ctx: mail_code.fetch_code(ctx, before=before, company="Acme", timeout_s=30, poll_s=0.5, home=home))
    assert code == "Acm3C0de"  # not the old mail, not OtherCo's, and read from the opened message


def test_no_new_mail_means_no_code(mailbox):
    host, home = mailbox
    everything = host.run(lambda ctx: mail_code.snapshot(ctx, home))
    # wait for the page to add its mails, then treat them as already seen
    host.run(lambda ctx: ctx.pages[0].wait_for_timeout(3500))
    everything = host.run(lambda ctx: mail_code.snapshot(ctx, home))
    code = host.run(lambda ctx: mail_code.fetch_code(ctx, before=everything, company="Acme", timeout_s=3, poll_s=0.5, home=home))
    assert code == ""


def test_a_different_company_never_gets_anothers_code(mailbox):
    host, home = mailbox
    before = host.run(lambda ctx: mail_code.snapshot(ctx, home))
    code = host.run(lambda ctx: mail_code.fetch_code(ctx, before=before, company="Zeta Systems", timeout_s=6, poll_s=0.5, home=home))
    assert code == ""  # two new code mails, neither is Zeta's: ambiguous, so none is used


def test_company_names_match_loosely_but_not_carelessly():
    mail = "Greenhouse Security code for your application to Acme, Inc."
    assert mail_code.company_matches("Acme", mail)
    assert mail_code.company_matches("Acme Corp", mail)
    assert mail_code.company_matches("ACME Inc.", mail)
    assert not mail_code.company_matches("Zeta Systems", mail)
    assert not mail_code.company_matches("Inc", mail) and not mail_code.company_matches("", mail)
    assert mail_code.company_matches("ESO", "Greenhouse Security code for your application to ESO")
    assert not mail_code.company_matches("ESO", "Greenhouse Security code for your application to Espresso Labs")
