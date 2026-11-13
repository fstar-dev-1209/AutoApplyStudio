"""Typing answers into an application form the way a person would: focus, type, pick from the list."""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from core.form_answers import Decision, Field, Option

Pause = Callable[[float, float], None]


class FillError(RuntimeError):
    """A field could not be filled; the message says which and why."""


def _sleep(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


@dataclass
class Files:
    resume: str = ""  # absolute paths of the generated documents
    cover: str = ""


def _loc(page, ref: str):
    return page.locator(f'[data-aas="{ref}"]').first


def fill_field(page, f: Field, d: Decision, files: Files, *, pause: Pause = _sleep) -> None:
    """Carry out one ``fill`` decision. Raises FillError if the page did not take it."""
    try:
        {
            "text": _fill_text, "textarea": _fill_text, "number": _fill_text,
            "select": _fill_select, "combobox": _fill_combobox, "radio": _fill_radio,
            "checkbox": _fill_checkbox, "checkgroup": _fill_checkgroup, "file": _fill_file,
        }[f.kind](page, f, d, files, pause)
    except FillError:
        raise
    except Exception as exc:  # noqa: BLE001 - Playwright timeouts etc. become a readable field error
        raise FillError(f"{f.label or f.ref}: {type(exc).__name__}: {str(exc).splitlines()[0][:160]}") from exc


def _fill_text(page, f: Field, d: Decision, files: Files, pause: Pause) -> None:
    box = _loc(page, f.ref)
    box.scroll_into_view_if_needed(timeout=8000)
    box.click(timeout=8000)
    value = d.value[: f.max_chars] if f.max_chars else d.value
    if len(value) <= 40:
        box.fill("")
        box.press_sequentially(value, delay=random.randint(18, 45))  # short answers are typed key by key
    else:
        box.fill(value)  # long answers are pasted in one go, as a person would
    pause(0.15, 0.5)
    got = box.input_value(timeout=3000)
    if _squash(got) != _squash(value):
        # a page that re-drew the field mid-typing loses the keys: put the whole answer in again, once
        box.click(timeout=8000)
        box.fill(value)
        pause(0.2, 0.5)
        got = box.input_value(timeout=3000)
    if _squash(got) != _squash(value):
        raise FillError(f"{f.label}: the page kept '{got[:40]}' instead of the answer")


def _fill_select(page, f: Field, d: Decision, files: Files, pause: Pause) -> None:
    box = _loc(page, f.ref)
    box.scroll_into_view_if_needed(timeout=8000)
    box.select_option(label=d.value, timeout=8000)
    pause(0.1, 0.35)


class _NotOpened(Exception):
    """This way of opening the dropdown did not apply to the widget."""


_OUTLINE_JS = r"""
(ref) => {
  const el = document.querySelector('[data-aas="' + ref + '"]');
  if (!el) return 'the field is gone';
  const describe = (n) => n.tagName.toLowerCase() + (n.id ? '#' + n.id : '') + '[' + Array.from(n.attributes)
    .filter(a => /^(role|aria-|data-|tabindex|type|readonly|disabled)/.test(a.name)).map(a => a.name + '=' + String(a.value).slice(0, 30))
    .slice(0, 6).join(' ') + '].' + String(n.className || '').split(/\s+/).slice(0, 3).join('.');
  const up = []; for (let n = el, i = 0; n && i < 5; n = n.parentElement, i++) up.push(describe(n));
  const r = el.getBoundingClientRect();
  const menus = Array.from(document.querySelectorAll('[role="listbox"], [class*="menu" i], [class*="option" i]')).slice(0, 6)
    .map(m => describe(m) + ' children=' + m.children.length + ' visible=' + (m.getBoundingClientRect().height > 0));
  return JSON.stringify({ size: Math.round(r.width) + 'x' + Math.round(r.height), up, menus_on_page: menus });
}
"""

# Options that belong to the open dropdown. The phone field's country list is a separate widget and is never read.
_MENU = "[class*='select__menu']:not([class*='iti'])"
_OPTION_SELECTOR = "[role='option'], [class*='select__option']"


def _openers(page, box):
    """How a closed dropdown is asked to open, in the order a person would try them.

    The arrow on the control is what Greenhouse's widget opens on. A click on its tiny input only focuses it, and
    focusing it before the click closes a menu the focus just opened. The other attempts cover widgets that open on
    the control, the field, or a key."""
    return [
        ("the dropdown arrow", lambda: _click_indicator(box)),
        ("a click on its control", lambda: _click_control(box)),
        ("a click on the field", lambda: box.click(timeout=4000)),
        ("the arrow key", lambda: box.press("ArrowDown")),
        ("the space bar", lambda: box.press("Space")),
    ]


def probe_combobox(page, f: Field, notes: list[str] | None = None) -> list[Option]:
    """Open a dropdown just long enough to read its choices (a type-ahead that lists nothing yet returns []).

    If none of the attempts shows a list, the field's structure is added to ``notes`` (names and attributes only)."""
    box = _loc(page, f.ref)
    openers = _openers(page, box)
    opened = False  # the list came up, even if it had nothing in it yet (a city or country lookup)
    try:
        box.scroll_into_view_if_needed(timeout=6000)
    except Exception:  # noqa: BLE001
        pass
    for name, opener in openers:
        try:
            opener()
            if not _wait_for_menu(page, box):
                _close(page, box)
                continue
            options = _visible_options(page)
            opened = True
        except _NotOpened:
            continue
        except Exception as exc:  # noqa: BLE001 - this way of opening did not work: try the next
            options = []
            if notes is not None:
                notes.append(f"{f.label}: {name} failed ({type(exc).__name__})")
        if options:
            _close(page, box)
            if notes is not None and name != openers[0][0]:
                notes.append(f"{f.label}: choices read after {name}")
            return [Option(label=t) for t, _ in options][:60]
        _close(page, box)
    if opened:
        return []  # open, but empty: the answer is typed and a suggestion picked, so there is nothing to record
    if notes is not None:
        try:
            notes.append(f"{f.label}: no choices appeared. Structure: {page.evaluate(_OUTLINE_JS, f.ref)}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{f.label}: no choices appeared (outline failed: {type(exc).__name__})")
    return []


def _control_of(box):
    """The dropdown's visible control (the wide box with the arrow), not the 3-pixel input inside it. A dropdown widget
    opens on a mouse press on that control; a click on its input only focuses it."""
    for xpath in ("ancestor::*[contains(@class,'select__control')][1]",
                  "ancestor::*[@role='combobox' or contains(@class,'-control') or contains(@class,'control')][1]"):
        control = box.locator("xpath=" + xpath).first
        if control.count():
            return control
    return box


def _click_indicator(box) -> None:
    """The arrow at the right of the control. Greenhouse's list opens on this and not on its input."""
    indicator = _control_of(box).locator("[class*='dropdown-indicator']").first
    if not indicator.count():
        raise _NotOpened()
    indicator.scroll_into_view_if_needed(timeout=6000)
    indicator.click(timeout=4000)


def _click_control(box) -> None:
    control = _control_of(box)
    control.scroll_into_view_if_needed(timeout=6000)
    control.click(timeout=4000)


def _wait_for_menu(page, box, timeout_ms: int = 1200) -> bool:
    """True once this dropdown's own list is on screen (not the phone field's country list)."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if box.get_attribute("aria-expanded") == "true" or _visible_options(page):
            return True
        page.wait_for_timeout(100)
    return False


def _close(page, box) -> None:
    try:
        box.press("Escape")
        page.wait_for_timeout(150)
    except Exception:  # noqa: BLE001
        pass


def _visible_options(page) -> list[tuple[str, object]]:
    """Choices in an open dropdown menu. The phone widget's country list is a different list and is left alone."""
    items = page.locator(_MENU).locator(_OPTION_SELECTOR)
    out, seen = [], set()
    for i in range(min(items.count(), 120)):
        item = items.nth(i)
        try:
            if not item.is_visible():
                continue
            text = re.sub(r"\s+", " ", item.inner_text(timeout=1000)).strip()
            if text and len(text) <= 200 and text not in seen:  # a container and its child give the same text once
                seen.add(text)
                out.append((text, item))
        except Exception:  # noqa: BLE001 - an option that vanished while reading
            continue
    return out


def _open_menu(page, box) -> None:
    """Open the dropdown, trying each way until its own list is on screen."""
    try:
        box.scroll_into_view_if_needed(timeout=8000)
    except Exception:  # noqa: BLE001
        pass
    for _name, opener in _openers(page, box):
        try:
            opener()
        except _NotOpened:
            continue
        except Exception:  # noqa: BLE001
            continue
        if _wait_for_menu(page, box):
            return
        _close(page, box)


def _fill_combobox(page, f: Field, d: Decision, files: Files, pause: Pause) -> None:
    box = _loc(page, f.ref)
    shown = _shown_value(page, f.ref)
    if shown and _squash(d.value) in _squash(shown):
        return  # the control already shows this answer
    _open_menu(page, box)
    wanted = d.value
    typed = False
    options = _visible_options(page)
    if not options or len(options) > 12 or not any(_squash(wanted) in _squash(t) for t, _ in options):
        query = wanted.split(",")[0].strip() if "," in wanted else wanted
        typed = True
        # The suggestions come over the network. Behind a slow proxy the first lookup sometimes returns nothing in
        # time, so the text is typed once more and given longer before the field is reported as unanswerable.
        for wait_ms in (2500, 9000):
            box.click(timeout=4000)
            box.press("Control+A")
            box.press("Backspace")
            box.press_sequentially(query[:40], delay=random.randint(35, 70))  # type-ahead (the city of "Austin, TX")
            options = _wait_for_match(page, wanted, timeout_ms=wait_ms)
            if _best_option(wanted, options):
                break
    pick = _best_option(wanted, options)
    if pick is None:
        box.press("Escape")
        raise FillError(f"{f.label}: no option matches '{wanted}'" + (f" (saw {len(options)} choices)" if typed else ""))
    pick[1].click(timeout=5000)
    pause(0.2, 0.5)
    now = _shown_value(page, f.ref)
    if not _choice_kept(pick[0], now):
        raise FillError(f"{f.label}: the choice '{pick[0]}' was not kept")


def _wait_for_match(page, wanted: str, timeout_ms: int = 2000):
    """Suggestions after typing. A lookup answers over the network, so the list is polled rather than read once.

    Greenhouse's city lookup shows \"Loading...\" for several seconds before the cities arrive. That is not an empty
    list, so the wait runs longer while that word is all the menu contains."""
    deadline = time.monotonic() + timeout_ms / 1000
    cap = time.monotonic() + 12
    options: list = []
    while time.monotonic() < deadline:
        if _menu_loading(page) and deadline < cap:
            deadline = min(cap, deadline + 0.4)
            page.wait_for_timeout(200)
            continue
        options = _visible_options(page)
        if _best_option(wanted, options):
            return options
        page.wait_for_timeout(150)
    return options


def _menu_loading(page) -> bool:
    try:
        menu = page.locator(_MENU)
        if not menu.count():
            return False
        text = menu.first.inner_text(timeout=500)
    except Exception:  # noqa: BLE001
        return False
    return bool(re.search(r"loading", text or "", re.I)) and not _visible_options(page)


def _choice_kept(picked: str, shown: str) -> bool:
    """The control shows the choice. 'United States +1' kept as 'United States' still counts."""
    a, b = _squash(picked), _squash(shown)
    if not b or b in {"select", "select...", "please select"}:
        return False
    if a in b or b in a:
        return True
    head = re.split(r"\s*\+\s*|\s*:\s*", a)[0].strip()
    return bool(head) and head in b


def _shown_value(page, ref: str) -> str:
    return page.evaluate(
        "(ref) => { let n = document.querySelector('[data-aas=\"' + ref + '\"]'); "
        "for (let i = 0; i < 6 && n; i++, n = n.parentElement) { const nodes = n.querySelectorAll && "
        "n.querySelectorAll(\"[class*='single-value'], [class*='SingleValue'], [class*='multi-value__label']\"); "
        "if (!nodes) continue; const text = Array.from(nodes).map(v => (v.textContent || '').trim()).filter(Boolean); "
        "if (text.length) return text.join(', '); } return ''; }",
        ref) or ""


US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
    "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
    "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york", "NC": "north carolina",
    "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
    "RI": "rhode island", "SC": "south carolina", "SD": "south dakota", "TN": "tennessee", "TX": "texas",
    "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
}


def _best_option(wanted: str, options: list[tuple[str, object]]):
    w = _squash(wanted)
    for text, item in options:
        if _squash(text) == w:
            return text, item
    city, comma, rest = w.partition(",")
    if comma and rest.strip():
        # "Austin, TX": the city AND its state must both be in the suggestion, so it can never land on another state
        state = rest.strip()
        abbr = next((code.lower() for code, name in US_STATES.items() if name == state), "")
        names = {state, US_STATES.get(state.upper(), ""), abbr} - {""}
        for text, item in options:
            t = _squash(text)
            if t.startswith(city.strip()) and any(re.search(rf"(?<![a-z]){re.escape(n)}(?![a-z])", t) for n in names):
                return text, item
        return None
    for text, item in options:
        if _squash(text).startswith(w) or w.startswith(_squash(text)):
            return text, item
    contains = [(t, i) for t, i in options if w in _squash(t)]
    return contains[0] if contains else None


def _fill_radio(page, f: Field, d: Decision, files: Files, pause: Pause) -> None:
    option = next((o for o in f.options if o.label == d.value), None)
    if option is None:
        raise FillError(f"{f.label}: no choice named '{d.value}'")
    _click_input(page, option.ref)
    pause(0.15, 0.4)
    if not _loc(page, option.ref).is_checked():
        raise FillError(f"{f.label}: the choice '{option.label}' was not kept")


def _fill_checkgroup(page, f: Field, d: Decision, files: Files, pause: Pause) -> None:
    """A group of checkboxes answering one question (e.g. a range of years): tick exactly the chosen one."""
    option = next((o for o in f.options if o.label == d.value), None)
    if option is None:
        raise FillError(f"{f.label}: no choice named '{d.value}'")
    for other in f.options:
        if other is not option and _loc(page, other.ref).is_checked():
            _click_input(page, other.ref)  # untick anything already ticked
    if not _loc(page, option.ref).is_checked():
        _click_input(page, option.ref)
    pause(0.15, 0.4)
    if not _loc(page, option.ref).is_checked():
        raise FillError(f"{f.label}: the choice '{option.label}' was not kept")


def _fill_checkbox(page, f: Field, d: Decision, files: Files, pause: Pause) -> None:
    _click_input(page, f.ref)
    pause(0.1, 0.3)
    if not _loc(page, f.ref).is_checked():
        raise FillError(f"{f.label}: the box did not stay ticked")


def _click_input(page, ref: str) -> None:
    box = _loc(page, ref)
    box.scroll_into_view_if_needed(timeout=8000)
    try:
        box.check(timeout=2500)
    except Exception:  # noqa: BLE001 - a visually hidden input: click what the person would, its label
        label = page.locator(f'label[for="{box.get_attribute("id")}"]').first
        if box.get_attribute("id") and label.count():
            label.click(timeout=5000)
        else:
            box.check(force=True, timeout=5000)


def _file_state(page, ref: str, slot: str) -> dict:
    """plain: the file stays on the input. upload: Greenhouse copies it to storage and only then shows the name.

    Greenhouse replaces the file input while the upload runs, so the marker on the original input disappears.
    The upload group stays. Reading the page directly avoids waiting 30s for a node that is already gone."""
    return page.evaluate(
        """({ref, slot}) => {
          const text = (node) => {
            if (!node) return '';
            const box = node.getBoundingClientRect();
            if (box.width === 0 && box.height === 0) return '';
            return (node.innerText || '').trim();
          };
          const fileName = (group) => {
            if (!group) return '';
            const nodes = group.querySelectorAll('.file-upload__filename, [class*="filename"], [class*="file-name"]');
            let hidden = '';
            for (const node of nodes) {
              const raw = (node.innerText || node.textContent || '').trim();
              if (!raw) continue;
              const box = node.getBoundingClientRect();
              if (box.width > 0 || box.height > 0) return raw;
              hidden = raw;
            }
            const match = (group.innerText || '').match(/[^\\n]{1,180}\\.(pdf|docx?|rtf|txt)/i);
            return (match && match[0].trim()) || hidden;
          };
          const pack = (el, group) => {
            if (!group) return {mode: 'plain', name: '', uploading: false, error: '',
                                files: el && el.files ? el.files.length : 0};
            return {
              mode: 'upload',
              name: fileName(group),
              uploading: !!text(group.querySelector('[role="progressbar"]')),
              error: text(group.querySelector('.helper-text--error')),
              files: el && el.files ? el.files.length : 0,
            };
          };
          const el = document.querySelector(`[data-aas="${ref}"]`);
          if (el) return pack(el, el.closest('.file-upload'));
          const want = slot === 'cover' ? /cover/i : /resume|\\bcv\\b|curriculum/i;
          for (const group of document.querySelectorAll('.file-upload')) {
            const input = group.querySelector('input[type="file"]');
            const lab = document.getElementById(group.getAttribute('aria-labelledby') || '');
            const blob = ((lab && lab.textContent) || '') + ' ' + ((input && input.id) || '') + ' ' + ((input && input.name) || '');
            if (want.test(blob)) return pack(input, group);
          }
          return {mode: 'missing', name: '', uploading: false, error: '', files: 0};
        }""",
        {"ref": ref, "slot": slot},
    )


def _safe_file_state(page, f: Field) -> dict:
    try:
        return _file_state(page, f.ref, f.slot)
    except Exception:  # noqa: BLE001 - the page is mid-update; the upload group is checked again
        return {"mode": "missing", "name": "", "uploading": False, "error": "", "files": 0}


def _file_ready(state: dict) -> bool:
    """The form is showing the attached file. A timeout from the file input does not change that."""
    if state.get("mode") == "plain":
        return bool(state.get("files"))
    return state.get("mode") == "upload" and bool(state.get("name")) and not state.get("uploading")


def _fill_file(page, f: Field, d: Decision, files: Files, pause: Pause) -> None:
    path = files.resume if d.value == "@resume" else files.cover
    if not path:
        raise FillError(f"{f.label}: the document to attach is missing")
    if _file_ready(_safe_file_state(page, f)):
        pause(0.2, 0.4)
        return
    loc = _loc(page, f.ref)
    # set_input_files waits until the input settles. Greenhouse removes that input once the name is on
    # screen, so the call times out even though the upload succeeded. The name on the page is what counts.
    placed = False
    for _ in range(2):
        try:
            loc.set_input_files(path, timeout=5000)
            placed = True
            break
        except Exception:  # noqa: BLE001
            state = _safe_file_state(page, f)
            if _file_ready(state) or state.get("uploading") or state.get("files"):
                placed = True
                break
            page.wait_for_timeout(300)
    if not placed and not (_file_ready(_safe_file_state(page, f))):
        raise FillError(f"{f.label}: the file input did not take the document")
    # The new boards show the file name only after their upload finishes. Submitting before that
    # comes back as "Resume/CV is required" even though the name appears a moment later.
    deadline = time.monotonic() + 30
    while True:
        state = _safe_file_state(page, f)
        if state["mode"] == "plain":
            if not state["files"]:
                raise FillError(f"{f.label}: the file did not stay attached")
            break
        if _file_ready(state):
            break
        if state["mode"] == "upload" and state["error"] and not state["uploading"]:
            raise FillError(f"{f.label}: {state['error']}")
        if time.monotonic() >= deadline:
            raise FillError(f"{f.label}: the file did not finish uploading")
        page.wait_for_timeout(400)
    pause(0.4, 1.0)


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().casefold()
