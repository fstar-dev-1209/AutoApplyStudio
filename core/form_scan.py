"""Reading an application form: every field with its label, kind, options and current value.

The scan runs inside the page and tags each control with a ``data-aas`` reference so the filler can find it again.
It covers the markup of classic Greenhouse boards (plain inputs) and the newer React boards (type-ahead
"combobox" widgets). It never changes anything on the page except adding those attributes.
"""

from __future__ import annotations

from typing import Any

from core.form_answers import Field, Option

SCAN_JS = r"""
() => {
  const clean = (s) => String(s || "").replace(/\s+/g, " ").replace(/[*✱]/g, "").replace(/\(required\)/ig, "").trim();
  const visible = (el) => {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return cs.visibility !== "hidden" && cs.display !== "none" && (r.width > 0 || r.height > 0);
  };
  const forms = Array.from(document.querySelectorAll("form"));
  const form = forms.find((f) => f.querySelector("#first_name, input[name*='first_name'], input[type=file]")) || forms[0];
  if (!form) return { ok: false, reason: "no_form" };

  const labelFor = (el) => {
    const nodes = [];  // each label element once, even when it is reachable both by "for" and by aria-labelledby
    const add = (n) => { if (n && !nodes.includes(n)) nodes.push(n); };
    if (el.id) add(document.querySelector('label[for="' + el.id.replace(/"/g, '\\"') + '"]'));
    (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean).forEach((id) => add(document.getElementById(id)));
    const parts = nodes.map((n) => n.textContent);
    if (!parts.length && el.getAttribute("aria-label")) parts.push(el.getAttribute("aria-label"));
    if (!parts.length) {
      const wrap = el.closest("label");
      if (wrap) parts.push(wrap.textContent);
    }
    if (!parts.length) {
      const box = el.closest(".field, .application-field, .input-wrapper, [class*='field'], .form-group, fieldset, div");
      const l = box && box.querySelector("label, legend");
      if (l) parts.push(l.textContent);
    }
    return parts.join(" ");
  };
  const rawRequired = (el, label) => el.required || el.getAttribute("aria-required") === "true" || /\*/.test(label);
  const groupLegend = (el) => {
    const set = el.closest("fieldset");
    const legend = set && set.querySelector("legend");
    if (legend) return legend.textContent;
    const by = el.closest("[aria-labelledby]");
    if (by) { const n = document.getElementById(by.getAttribute("aria-labelledby")); if (n) return n.textContent; }
    const box = el.closest(".input-wrapper, .field, [class*='field'], .form-group");
    const l = box && box.querySelector("label, legend");
    return l ? l.textContent : "";
  };
  const optionLabel = (el) => {
    const parts = [];
    if (el.id) { const l = document.querySelector('label[for="' + el.id.replace(/"/g, '\\"') + '"]'); if (l) { parts.push(l.textContent); l.setAttribute("data-aas-label", "1"); } }
    if (!parts.length) { const w = el.closest("label"); if (w) parts.push(w.textContent); }
    return clean(parts.join(" ")) || clean(el.value);
  };
  const comboValue = (el) => {
    let node = el;
    for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
      const nodes = node.querySelectorAll && node.querySelectorAll("[class*='single-value'], [class*='SingleValue'], [class*='multi-value__label']");
      if (!nodes) continue;
      const text = Array.from(nodes).map((v) => clean(v.textContent)).filter((t) => t && !/^select(\.\.\.|…)?$/i.test(t));
      if (text.length) return text.join(", ");
    }
    return "";
  };

  const fields = [];
  let n = 0;
  const tag = (el, prefix) => { const ref = prefix + (++n); el.setAttribute("data-aas", ref); return ref; };
  const radios = new Map(), boxes = new Map();

  form.querySelectorAll("input, textarea, select").forEach((el) => {
    const type = (el.getAttribute("type") || (el.tagName === "SELECT" ? "select" : el.tagName === "TEXTAREA" ? "textarea" : "text")).toLowerCase();
    if (["hidden", "submit", "button", "image", "reset", "password", "search"].includes(type)) return;
    if (el.closest("[class*='select__menu']") || el.getAttribute("aria-hidden") === "true" && type !== "file") return;
    if (type === "file") {
      const group = el.closest(".file-upload");
      const groupLabel = group && document.getElementById(group.getAttribute("aria-labelledby") || "");
      const raw = (groupLabel && groupLabel.textContent) || labelFor(el);
      const label = clean(raw || (el.closest("[id],[data-field]") || {}).id || el.name || el.id);
      const ctx = (label + " " + el.id + " " + el.name).toLowerCase();
      const slot = /cover/.test(ctx) ? "cover" : /resume|\bcv\b|curriculum/.test(ctx) ? "resume" : "other";
      const named = group && group.querySelector(".file-upload__filename");
      const bar = group && group.querySelector('[role="progressbar"]');
      const uploading = !!(bar && bar.getBoundingClientRect().height > 0);
      const attached = group ? !!(named && (named.textContent || "").trim() && !uploading) : !!(el.files && el.files.length);
      const groupRequired = !!(group && group.getAttribute("aria-required") === "true");
      fields.push({ ref: tag(el, "f"), kind: "file", label, required: rawRequired(el, raw) || groupRequired, slot,
        accept: el.getAttribute("accept") || "", value: attached ? "attached" : "" });
      return;
    }
    if (!visible(el) && type !== "radio" && type !== "checkbox") return;
    // dropdown widgets add an invisible stand-in input (tabindex -1, opacity 0) only to drive browser validation
    if (type !== "radio" && type !== "checkbox" && el.tabIndex === -1 && getComputedStyle(el).opacity === "0") return;
    if (type === "radio") { const k = el.name || el.id; (radios.get(k) || radios.set(k, []).get(k)).push(el); return; }
    if (type === "checkbox") { const k = el.name || el.id; (boxes.get(k) || boxes.set(k, []).get(k)).push(el); return; }
    const raw = labelFor(el), label = clean(raw);
    const base = { required: rawRequired(el, raw), label, input_type: type, max_chars: el.maxLength > 0 ? el.maxLength : 0 };
    if (el.tagName === "SELECT") {
      const options = Array.from(el.options).filter((o) => o.value !== "" && !/^(select|choose|please select|--)/i.test(o.textContent.trim()))
        .map((o) => ({ label: clean(o.textContent), value: o.value }));
      const sel = el.options[el.selectedIndex];
      fields.push({ ...base, ref: tag(el, "s"), kind: "select", options, value: sel && sel.value ? clean(sel.textContent) : "" });
    } else if (el.getAttribute("role") === "combobox" || el.getAttribute("aria-autocomplete") === "list" || /select__input/.test(el.className)) {
      fields.push({ ...base, ref: tag(el, "c"), kind: "combobox", options: [], value: comboValue(el) || (el.value || "").trim() });
    } else if (el.tagName === "TEXTAREA") {
      fields.push({ ...base, ref: tag(el, "t"), kind: "textarea", value: el.value || "" });
    } else {
      const kind = type === "number" ? "number" : "text";
      fields.push({ ...base, ref: tag(el, "i"), kind, value: el.value || "" });
    }
  });

  radios.forEach((group) => {
    const legend = groupLegend(group[0]);
    fields.push({
      ref: tag(group[0], "g"), kind: "radio", label: clean(legend), required: group.some((r) => rawRequired(r, legend)),
      value: group.some((r) => r.checked) ? "checked" : "", input_type: "radio", max_chars: 0,
      options: group.map((r) => ({ label: optionLabel(r), ref: tag(r, "o"), value: r.value })),
    });
  });
  boxes.forEach((group) => {
    if (group.length === 1) {
      const el = group[0], raw = labelFor(el);
      fields.push({ ref: tag(el, "k"), kind: "checkbox", label: optionLabel(el) || clean(raw), required: rawRequired(el, raw) || el.required,
        value: el.checked ? "checked" : "", input_type: "checkbox", max_chars: 0, options: [] });
    } else {
      const legend = groupLegend(group[0]);
      fields.push({ ref: tag(group[0], "m"), kind: "checkgroup", label: clean(legend), required: group.some((c) => c.required || c.getAttribute('aria-required') === 'true'),
        value: group.some((c) => c.checked) ? "checked" : "", input_type: "checkbox", max_chars: 0,
        options: group.map((c) => ({ label: optionLabel(c), ref: tag(c, "o"), value: c.value })) });
    }
  });

  const submit = Array.from(form.querySelectorAll("button, input[type=submit]")).find((b) =>
    /submit|apply/i.test((b.textContent || b.value || "").trim()) && !b.disabled);
  if (submit) submit.setAttribute("data-aas", "submit");
  const errors = Array.from(document.querySelectorAll("[role=alert], .helper-text--error, [class*='error-message'], [class*='field-error'], .field_with_errors .error"))
    .filter(visible).map((e) => clean(e.textContent)).filter(Boolean);
  return {
    ok: true, fields, has_submit: !!submit, errors,
    title: clean((document.querySelector("h1") || {}).textContent || document.title),
    url: location.href,
  };
}
"""


class ScanError(RuntimeError):
    pass


def to_field(raw: dict[str, Any]) -> Field:
    return Field(
        ref=raw["ref"], kind=raw["kind"], label=raw.get("label", ""), required=bool(raw.get("required")),
        value=raw.get("value", ""), max_chars=int(raw.get("max_chars") or 0), slot=raw.get("slot", ""),
        input_type=raw.get("input_type", ""), accept=raw.get("accept", ""),
        options=[Option(label=o["label"], ref=o.get("ref", ""), value=o.get("value", "")) for o in raw.get("options", [])],
    )


def scan(page_or_frame) -> tuple[list[Field], dict[str, Any]]:
    """The form's fields in page order, plus {'has_submit', 'errors', 'title', 'url'}."""
    data = page_or_frame.evaluate(SCAN_JS)
    if not data.get("ok"):
        raise ScanError(data.get("reason", "no_form"))
    return [to_field(f) for f in data["fields"]], {k: data[k] for k in ("has_submit", "errors", "title", "url")}
