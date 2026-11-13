"""A stand-in Greenhouse board served on localhost, for testing the application flow end to end in a real Chrome.

It imitates the behaviour the bot has to cope with on job-boards.greenhouse.io: type-ahead "react-select" dropdowns,
a city lookup that answers after a delay, file inputs, per-field validation, a security-code step after sending, a
visible captcha frame, closed postings and a company page that embeds the form. It records what is submitted.
It is NOT Greenhouse: ``scripts/diagnose_apply.py`` is the check against a real posting (a dry run, nothing sent).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

COMBO = """
<div class="input-wrapper"><label for="{id}" id="{id}-label">{label}{star}</label>
 <div class="select__container" data-opts='{opts}' data-async="{async_}">
  <div class="select__control"><div class="select__value-container"><div class="select__single-value" style="display:none"></div>
   <div class="select__input-container"><input id="{id}" class="select__input" role="combobox" aria-autocomplete="list"
     aria-expanded="false" aria-labelledby="{id}-label" type="text" aria-required="{req}" autocomplete="off"></div></div>
   <div class="select__dropdown-indicator" aria-hidden="true" style="display:inline-block;width:20px;height:20px">▾</div></div>
  <div class="select__menu" style="display:none"></div>
  <input tabindex="-1" autocomplete="off" required value="" style="opacity:0;position:absolute;height:0;width:100%;pointer-events:none"></div></div>"""


def combo(id_, label, options, required=True, async_=False):
    return COMBO.format(id=id_, label=label, star="*" if required else "", opts=json.dumps(options).replace("'", "&#39;"),
                        async_="1" if async_ else "", req="true" if required else "false")


def form_html(*, code_step: bool, captcha: bool, extra_question: bool, solve_ms: int = 0, stubborn: int = 0,
              late_race: bool = False, captcha_message: bool = False) -> str:
    extra = combo("question_9", "Which office would you prefer?", ["Austin", "Remote"]) if extra_question else ""
    phone_list = ""
    if stubborn == 3:
        # The phone widget's country list. It is a different dropdown and must never be read as a job question.
        phone_list = """<ul id="iti-0__country-listbox" class="iti__country-list" role="listbox" aria-label="List of countries" style="display:none">
  <li role="option">United States</li><li role="option">Canada</li></ul>"""
    return f"""<!doctype html><html><head><title>Job Application for Backend Engineer at Acme</title>
<style>.select__menu{{border:1px solid #888;background:#fff}} .helper-text--error{{color:#b00}}</style></head><body>
{phone_list}
<h1>Backend Engineer</h1><p>Thank you for your interest in Acme. We build things.</p>
<form id="application-form" novalidate>
 <div class="input-wrapper"><label for="first_name">First Name*</label><input id="first_name" type="text" aria-required="true" required></div>
 <div class="input-wrapper"><label for="last_name">Last Name*</label><input id="last_name" type="text" aria-required="true" required></div>
 <div class="input-wrapper"><label for="email">Email*</label><input id="email" type="text" aria-required="true" required></div>
 <div class="input-wrapper"><label for="phone">Phone</label><input id="phone" type="tel"></div>
 {combo("candidate-location", "Location (City)", [], True, True)}
 <div class="file-upload" role="group" aria-labelledby="upload-label-resume" aria-required="true">
   <div id="upload-label-resume" class="label">Resume/CV*</div>
   <div class="file-upload__wrapper"><button type="button">Attach</button>
     <label class="visually-hidden" for="resume">Attach</label>
     <input id="resume" class="visually-hidden" type="file" accept=".pdf,.doc,.docx,.txt,.rtf"></div></div>
 <div class="input-wrapper"><label id="cover-label">Cover Letter</label><input id="cover_letter" type="file" aria-labelledby="cover-label"></div>
 <div class="input-wrapper"><label for="question_1">LinkedIn Profile</label><input id="question_1" type="text"></div>
 {combo("question_2", "Are you legally authorized to work in the United States?", ["Yes", "No"])}
 {combo("question_3", "Will you now or in the future require sponsorship for employment visa status?", ["Yes", "No"])}
 {combo("question_4", "How did you hear about this job?", ["LinkedIn", "Indeed", "Other"])}
 <div class="input-wrapper"><label for="question_5">Describe your experience building RAG systems.*</label>
   <textarea id="question_5" maxlength="400" aria-required="true" required></textarea></div>
 {extra}
 <fieldset><legend>Do you require a visa to work here?*</legend>
   <label for="v_yes"><input type="radio" name="visa" id="v_yes" value="1"> Yes</label>
   <label for="v_no"><input type="radio" name="visa" id="v_no" value="0"> No</label></fieldset>
  <fieldset><legend>How many years of experience do you have with Snowflake?*</legend>
   <label><input type="checkbox" name="snow" value="0-1" aria-required="true"> Less than 1 year</label>
   <label><input type="checkbox" name="snow" value="1-3"> 1-3 years</label>
   <label><input type="checkbox" name="snow" value="3-5"> 3-5 years</label>
   <label><input type="checkbox" name="snow" value="5+"> 5+ years</label></fieldset>
 {combo("gender", "Gender", ["Male", "Female", "Decline To Self Identify"], False)}
 {('<div class="input-wrapper" id="race-wrap" style="display:none"><label for="race">Please identify your race</label>'
   '<select id="race"><option value="">Select...</option>'
   '<option>Decline To Self Identify</option><option>Asian</option><option>White</option></select></div>') if late_race else ''}
 {combo("veteran_status", "Veteran Status", ["I am a protected veteran", "I am not a protected veteran", "I don't wish to answer"], False)}
 <label><input type="checkbox" id="privacy" required> I acknowledge the Privacy Notice*</label>
 <button type="submit" id="submit">Submit application</button>
</form>
<div id="code-step" style="display:none"><p>Enter the security code sent to your email</p>
  {''.join(f'<input id="security-input-{i}" maxlength="1" size="1">' for i in range(8))}
  <button id="code-submit" type="button">Submit application</button></div>
<iframe id="captcha" src="/recaptcha/api2/bframe" style="display:none;width:300px;height:400px"></iframe>
<script>
const CODE_STEP = {str(code_step).lower()}, CAPTCHA = {str(captcha).lower()}, CAPTCHA_MSG = {str(captcha_message).lower()}, SOLVE_MS = {solve_ms}, STUBBORN = {stubborn}, LATE_RACE = {str(late_race).lower()};  // 0 click or arrow, 1 arrow key only, 2 never, 3 the arrow on the control (menu drawn on the page body)
document.querySelectorAll('.select__container').forEach((box) => {{
  const input = box.querySelector('input'), single = box.querySelector('.select__single-value');
  let menu = box.querySelector('.select__menu');
  if (STUBBORN === 3) {{ menu = document.createElement('div'); menu.className = 'select__menu'; menu.dataset.for = input.id;
    menu.style.cssText = 'display:none;position:absolute;z-index:9999;background:#fff'; document.body.appendChild(menu); }}
  const base = JSON.parse(box.dataset.opts || '[]');
  const show = (items, keepOpen) => {{ menu.innerHTML = ''; items.forEach((t) => {{ const o = document.createElement('div');
    o.className = 'select__option'; o.setAttribute('role', 'option'); o.textContent = t;
    o.addEventListener('mousedown', (e) => e.preventDefault());
    o.addEventListener('click', () => {{ single.textContent = t; single.style.display = ''; input.value = ''; menu.style.display = 'none';
      input.setAttribute('aria-expanded', 'false');
      if (LATE_RACE && input.id === 'gender') {{ const race = document.getElementById('race-wrap'); if (race) race.style.display = ''; }} }});
    menu.appendChild(o); }}); const open = items.length || keepOpen; menu.style.display = open ? 'block' : 'none';
    input.setAttribute('aria-expanded', open ? 'true' : 'false'); }};
  const refresh = () => {{
    const q = input.value.trim().toLowerCase();
    if (box.dataset.async) {{ if (q.length < 2) return show([], STUBBORN === 3);
      setTimeout(() => show(['Austin, Texas, United States', 'Austin, Minnesota, United States'].filter((c) => c.toLowerCase().includes(q))), 300); }}
    else show(STUBBORN === 2 && !q ? [] : base.filter((t) => t.toLowerCase().includes(q)));
  }};
  if (STUBBORN === 0) {{ input.addEventListener('click', refresh); input.addEventListener('focus', refresh); }}
  input.addEventListener('input', refresh);
  if (STUBBORN === 1) input.addEventListener('keydown', (e) => {{ if (e.key === 'ArrowDown') refresh(); }});
  box.querySelector('.select__dropdown-indicator').addEventListener('mousedown', (e) => {{ e.preventDefault(); e.stopPropagation();
    if (STUBBORN === 0 || STUBBORN === 3) refresh(); }});
  const hide = () => {{ menu.style.display = 'none'; input.setAttribute('aria-expanded', 'false'); }};
  input.addEventListener('blur', () => setTimeout(hide, 120));  // like react-select
  input.addEventListener('keydown', (e) => {{ if (e.key === 'Escape') hide(); }});
  box.querySelector('.select__control').addEventListener('click', () => input.focus());
}});
const val = (id) => {{ const el = document.getElementById(id); if (!el) return ''; if (el.type === 'file') return el.files.length ? el.files[0].name : '';
  const box = el.closest('.select__container'); if (box) return box.querySelector('.select__single-value').textContent; return el.value; }};
document.getElementById('resume').addEventListener('change', () => {{
  const input = document.getElementById('resume'), file = input.files[0];
  if (!file) return;
  setTimeout(() => {{ let named = document.querySelector('.file-upload__filename');
    if (!named) {{ named = document.createElement('div'); named.className = 'file-upload__filename'; input.closest('.file-upload').appendChild(named); }}
    named.textContent = file.name; }}, 700);
}});
document.getElementById('application-form').addEventListener('submit', async (e) => {{
  e.preventDefault();
  document.querySelectorAll('.helper-text--error').forEach((n) => n.remove());
  let bad = false;
  document.querySelectorAll('[aria-required=true], [required]').forEach((el) => {{
    const empty = el.type === 'checkbox' ? !el.checked : !val(el.id);
    if (empty && el.id) {{ bad = true; const m = document.createElement('div'); m.className = 'helper-text--error'; m.setAttribute('role', 'alert');
      m.textContent = (document.querySelector('label[for="' + el.id + '"]') || {{textContent: el.id}}).textContent.replace('*','').trim() + ' is required'; el.parentElement.appendChild(m); }}
  }});
  if (!document.querySelector('input[name=snow]:checked')) {{ bad = true; const m = document.createElement('div'); m.className = 'helper-text--error'; m.setAttribute('role','alert'); m.textContent = 'Snowflake experience is required'; document.body.appendChild(m); }}
  if (!document.querySelector('input[name=visa]:checked')) {{ bad = true; const m = document.createElement('div'); m.className = 'helper-text--error'; m.setAttribute('role','alert'); m.textContent = 'Visa question is required'; document.body.appendChild(m); }}
  if (bad) return;
  if (document.querySelector('.file-upload') && !document.querySelector('.file-upload__filename')) {{
    const m = document.createElement('div'); m.className = 'helper-text--error'; m.setAttribute('role', 'alert');
    m.textContent = 'Resume/CV is required.'; document.body.appendChild(m); return;
  }}
  if (CAPTCHA_MSG && !window.__solved) {{
    const m = document.createElement('div'); m.className = 'helper-text--error'; m.setAttribute('role', 'alert'); m.id = 'captcha-msg';
    m.textContent = 'Please complete the reCAPTCHA and resubmit your application.'; document.body.appendChild(m);
    if (SOLVE_MS) setTimeout(() => {{ window.__solved = true; const n = document.getElementById('captcha-msg'); if (n) n.remove(); }}, SOLVE_MS);
    return;
  }}
  if (CAPTCHA && !window.__solved) {{ document.getElementById('captcha').style.display = 'block'; window.__pending = true; if (SOLVE_MS) setTimeout(() => {{ document.getElementById("captcha").style.display = "none"; }}, SOLVE_MS); await window.__wait(); }}
  await send(null);
}});
window.__wait = () => new Promise((res) => {{ const t = setInterval(() => {{ if (document.getElementById('captcha').style.display === 'none') {{ clearInterval(t); res(); }} }}, 400); }});
async function send(code) {{
  const fields = {{}}; ['first_name','last_name','email','phone','candidate-location','resume','cover_letter','question_1','question_2','question_3',
    'question_4','question_5','question_9','gender','veteran_status','race'].forEach((id) => {{ if (document.getElementById(id)) fields[id] = val(id); }});
  fields.visa = (document.querySelector('input[name=visa]:checked') || {{}}).value || '';
  fields.snow = Array.from(document.querySelectorAll('input[name=snow]:checked')).map((c) => c.value).join(',');
  fields.privacy = document.getElementById('privacy').checked;
  const r = await fetch('/api/submit' + (code ? '?code=' + encodeURIComponent(code) : ''), {{method: 'POST', body: JSON.stringify(fields)}});
  const j = await r.json();
  if (j.need_code) {{ document.getElementById('application-form').style.display = 'none'; document.getElementById('code-step').style.display = 'block'; return; }}
  if (j.ok) location.href = location.pathname.replace(/\\/$/, '') + '/confirmation';
}}
document.getElementById('code-submit').addEventListener('click', () => {{
  const code = Array.from(document.querySelectorAll('[id^=security-input-]')).map((i) => i.value).join('');
  send(code);
}});
document.querySelectorAll('[id^=security-input-]').forEach((box, i, all) => box.addEventListener('input', () => {{ if (box.value && all[i + 1]) all[i + 1].focus(); }}));
</script></body></html>"""


class Board:
    """The server plus its settings and the record of what arrived."""

    def __init__(self) -> None:
        self.code_step = False
        self.captcha = False
        self.captcha_message = False  # the error text, with no large challenge frame
        self.captcha_solve_ms = 0  # 0 = nobody ever solves it
        self.extra_question = False
        self.late_race = False
        self.stubborn = 0  # how the dropdown widgets open: see form_html
        self.valid_code = "Ab3dE6gH"
        self.submissions: list[dict] = []
        self.codes_seen: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self.port = 0

    def start(self) -> "Board":
        board = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, body: str, code=200, ctype="text/html"):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                parts = urlsplit(self.path)
                path = parts.path
                if path == "/recaptcha/api2/bframe":
                    return self._send("<html><body>I am not a robot</body></html>")
                if path.endswith("/confirmation"):
                    return self._send("<html><body><h1>Thank you for applying.</h1><p>Your application has been submitted.</p></body></html>")
                if path == "/closed/jobs/9":
                    return self._send("<html><body><h1>Sorry</h1>The job you are looking for is no longer available.</body></html>", 200)
                if path == "/gone/jobs/404":
                    return self._send("not found", 404)
                if path == "/careers":
                    return self._send(f'<html><body><h1>Careers</h1><iframe src="http://127.0.0.1:{board.port}/embed/job_app?for=acme&token=1" width="800" height="900"></iframe></body></html>')
                if path == "/api/boards/toast/jobs/8224111":  # Greenhouse's public job API, for one board only
                    return self._send(json.dumps({"id": 8224111, "title": "Software Engineer II"}), ctype="application/json")
                if path == "/own-form":
                    return self._send("<html><body><h1>Apply now</h1><input placeholder='Legal First Name'></body></html>")
                if path == "/cookie-careers":  # an Apply button under a cookie pop-up, like careers.toasttab.com
                    frame = f"http://127.0.0.1:{board.port}/embed/job_app?for=acme&token=1"
                    return self._send(
                        '<html><body><h1>Careers</h1><p>About the role</p><a id="apply" href="#">Apply Now</a>'
                        '<div id="slot"></div>'
                        '<div id="cookies" style="position:fixed;inset:0;background:rgba(0,0,0,.4)">'
                        '<p>We use cookies on this website.</p><button onclick="this.parentNode.remove()">I Do Not Accept</button>'
                        '<button onclick="this.parentNode.remove()">I Accept</button></div>'
                        '<script>document.getElementById("apply").onclick = (e) => { e.preventDefault(); '
                        f'document.getElementById("slot").innerHTML = `<iframe src="{frame}" width="800" height="900"></iframe>`; }};'
                        '</script></body></html>')
                if path in ("/acme/jobs/1", "/embed/job_app"):
                    return self._send(form_html(code_step=board.code_step, captcha=board.captcha, extra_question=board.extra_question, solve_ms=board.captcha_solve_ms, stubborn=board.stubborn, late_race=board.late_race, captcha_message=board.captcha_message))
                if path == "/api/solve":
                    return self._send("ok")
                self._send("not found", 404)

            def do_POST(self):  # noqa: N802
                parts = urlsplit(self.path)
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                code = (parse_qs(parts.query).get("code") or [""])[0]
                if board.code_step and not code:
                    return self._send(json.dumps({"need_code": True}), ctype="application/json")
                if board.code_step:
                    board.codes_seen.append(code)
                    if code != board.valid_code:
                        return self._send(json.dumps({"need_code": True}), ctype="application/json")
                board.submissions.append(body)
                self._send(json.dumps({"ok": True}), ctype="application/json")

            def log_message(self, *a):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
