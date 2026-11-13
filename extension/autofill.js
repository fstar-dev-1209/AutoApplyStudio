/* Application Studio - fills an employer's application form. It NEVER submits, clicks Next/Apply, solves a CAPTCHA
 * or creates an account. Only empty fields are filled; anything it is unsure about is listed for the person.
 * Injected by the popup; the app token never reaches this script or the page. */
(function (root) {
  "use strict";

  const SITES = [{ name: "greenhouse", host: /(^|\.)greenhouse\.io$/i }];

  // Voluntary self-identification and similar questions are never answered by software.
  const NEVER = /gender|\brace\b|ethnic|hispanic|latino|veteran|disabilit|sexual orientation|pronoun|transgender|self-identif|demographic/i;

  const TEXT_RULES = [
    { key: "first_name", label: "First name", ids: ["first_name"], auto: ["given-name"], labels: [/^first\b/i, /legal first/i] },
    { key: "last_name", label: "Last name", ids: ["last_name"], auto: ["family-name"], labels: [/^last\b/i, /surname|family name/i] },
    { key: "full_name", label: "Full name", ids: ["name"], auto: ["name"], labels: [/^(full |legal |your )?name\b/i] },
    { key: "email", label: "Email", ids: ["email"], auto: ["email"], types: ["email"], labels: [/e-?mail/i] },
    { key: "phone", label: "Phone", ids: ["phone"], auto: ["tel"], types: ["tel"], labels: [/^phone|mobile|telephone/i] },
    { key: "linkedin_url", label: "LinkedIn", labels: [/linkedin/i] },
    { key: "github_url", label: "GitHub", labels: [/github/i] },
    { key: "website_url", label: "Website", labels: [/website|portfolio|personal (site|url)/i] },
    { key: "location", label: "Location", ids: ["candidate-location"], labels: [/^(current |your )?(location|city)\b/i] },
  ];

  const CHOICE_RULES = [
    { key: "work_authorization", label: "Work authorization", labels: [/authori[sz]ed to work|eligible to work|work authori[sz]ation|right to work/i] },
    { key: "needs_sponsorship", label: "Sponsorship", labels: [/sponsor|visa/i] },
  ];

  const FILE_RULES = [
    { slot: "cover", label: "Cover letter", test: /cover/i },
    { slot: "resume", label: "Resume", test: /resume|\bcv\b|curriculum/i },
  ];

  const clean = (s) => String(s || "").replace(/\s+/g, " ").replace(/[*✱]/g, "").replace(/\(required\)|\brequired\b/gi, "").trim();

  function labelOf(el) {
    const doc = el.ownerDocument;
    const parts = [];
    if (el.id) {
      const l = doc.querySelector('label[for="' + String(el.id).replace(/"/g, '\\"') + '"]');
      if (l) parts.push(l.textContent);
    }
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      by.split(/\s+/).forEach((id) => {
        const n = doc.getElementById(id);
        if (n) parts.push(n.textContent);
      });
    }
    if (el.getAttribute("aria-label")) parts.push(el.getAttribute("aria-label"));
    const wrap = el.closest("label");
    if (wrap) parts.push(wrap.textContent);
    if (!parts.length) {
      const box = el.closest(".field, .application-field, [class*='field'], .form-group, div");
      const l = box && box.querySelector("label, legend");
      if (l) parts.push(l.textContent);
    }
    return clean(parts.join(" "));
  }

  function groupLabel(radio) {
    const set = radio.closest("fieldset");
    const legend = set && set.querySelector("legend");
    if (legend) return clean(legend.textContent);
    const by = radio.closest("[aria-labelledby]");
    if (by) {
      const n = radio.ownerDocument.getElementById(by.getAttribute("aria-labelledby"));
      if (n) return clean(n.textContent);
    }
    const box = radio.closest("[class*='field'], .form-group, div");
    const l = box && box.parentElement && box.parentElement.querySelector("label, legend");
    return clean(l ? l.textContent : "");
  }

  const isVisible = (el) => !el.disabled && el.type !== "hidden" && el.getAttribute("aria-hidden") !== "true" && !el.closest("[hidden]");

  function setValue(el, value) {
    const win = el.ownerDocument.defaultView;
    const proto = el.tagName === "TEXTAREA" ? win.HTMLTextAreaElement.prototype : win.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    el.focus();
    setter.call(el, value); // the native setter, so React-style controlled inputs notice the change
    el.dispatchEvent(new win.Event("input", { bubbles: true }));
    el.dispatchEvent(new win.Event("change", { bubbles: true }));
    el.dispatchEvent(new win.Event("blur", { bubbles: true }));
  }

  function chooseOption(select, wanted) {
    const re = wanted === "Yes" ? /^yes\b/i : /^no\b/i;
    const option = Array.from(select.options).find((o) => re.test(clean(o.textContent)));
    if (!option) return false;
    select.value = option.value;
    select.dispatchEvent(new select.ownerDocument.defaultView.Event("change", { bubbles: true }));
    return true;
  }

  function attachFile(input, file) {
    const win = input.ownerDocument.defaultView;
    const bytes = Uint8Array.from(win.atob(file.base64), (c) => c.charCodeAt(0));
    const transfer = new win.DataTransfer();
    transfer.items.add(new win.File([bytes], file.name, { type: file.mime }));
    input.files = transfer.files;
    input.dispatchEvent(new win.Event("input", { bubbles: true }));
    input.dispatchEvent(new win.Event("change", { bubbles: true }));
  }

  function accepts(input, file) {
    const accept = (input.getAttribute("accept") || "").toLowerCase();
    if (!accept) return true;
    const ext = "." + file.name.split(".").pop().toLowerCase();
    return accept.split(",").map((s) => s.trim()).some((a) => a === ext || a === file.mime || (a.endsWith("/*") && file.mime.startsWith(a.slice(0, -1))));
  }

  function detectSite(hostname) {
    return SITES.find((s) => s.host.test(hostname || "")) || null;
  }

  function findScope(doc) {
    return (
      doc.querySelector("form#application_form, form#application-form, form[action*='applications']") ||
      Array.from(doc.querySelectorAll("form")).find((f) => f.querySelector("#first_name, #resume, input[type=file]")) ||
      null
    );
  }

  function matchText(el, label) {
    const id = (el.id || "").toLowerCase();
    const auto = (el.getAttribute("autocomplete") || "").toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    return TEXT_RULES.find(
      (r) =>
        (r.ids && r.ids.includes(id)) ||
        (r.auto && r.auto.includes(auto)) ||
        (r.types && r.types.includes(type)) ||
        r.labels.some((re) => re.test(label)),
    );
  }

  function fill(doc, kit, files) {
    const report = { matched: false, site: "", page: "", filled: [], needs_you: [], files: [], skipped: [] };
    const win = doc.defaultView;
    const site = detectSite(win && win.location.hostname);
    const scope = site && findScope(doc);
    if (!site) {
      report.reason = "unsupported_site";
      return report;
    }
    if (!scope) {
      report.reason = "no_form";
      return report;
    }
    report.matched = true;
    report.site = site.name;
    report.page = win.location.origin + win.location.pathname;
    const profile = kit.profile || {};
    const note = (list, text) => list.includes(text) || list.push(text);

    // ---- text inputs and textareas
    const controls = Array.from(scope.querySelectorAll("input, textarea, select")).filter(isVisible);
    controls.forEach((el) => {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (["hidden", "file", "submit", "button", "checkbox", "radio", "password"].includes(type)) return;
      const label = labelOf(el);
      if (NEVER.test(label)) return note(report.skipped, label || el.name || "Self-identification");
      if (el.tagName === "SELECT") {
        const rule = CHOICE_RULES.filter((r) => r.labels.some((re) => re.test(label)));
        if (rule.length === 1 && profile[rule[0].key]) {
          if (el.value === "" || /select|choose/i.test(el.options[el.selectedIndex].textContent)) {
            if (chooseOption(el, profile[rule[0].key])) note(report.filled, rule[0].label);
            else note(report.needs_you, label || rule[0].label);
          }
        }
        return;
      }
      if (el.getAttribute("role") === "combobox" || el.getAttribute("aria-autocomplete")) {
        // Type-ahead widgets need a click on a suggestion; fill nothing and ask the person.
        const rule = matchText(el, label);
        if (rule && !el.value) note(report.needs_you, label || rule.label);
        return;
      }
      const rule = matchText(el, label);
      if (!rule) return;
      if (el.value && el.value.trim()) return; // never overwrite what the person already typed
      let value = profile[rule.key];
      if (rule.key === "full_name") value = profile.full_name;
      if (!value) return;
      setValue(el, value);
      note(report.filled, rule.label);
    });

    // ---- yes/no radio groups
    const groups = new Map();
    controls
      .filter((el) => el.type === "radio")
      .forEach((r) => groups.set(r.name || r.id, (groups.get(r.name || r.id) || []).concat(r)));
    groups.forEach((radios) => {
      const label = groupLabel(radios[0]);
      if (!label) return;
      if (NEVER.test(label)) return note(report.skipped, label);
      const rules = CHOICE_RULES.filter((r) => r.labels.some((re) => re.test(label)));
      if (rules.length !== 1) {
        if (rules.length > 1) note(report.needs_you, label);
        return;
      }
      const wanted = profile[rules[0].key];
      if (!wanted) return note(report.needs_you, label);
      if (radios.some((r) => r.checked)) return;
      const re = wanted === "Yes" ? /^yes\b/i : /^no\b/i;
      const pick = radios.find((r) => re.test(labelOf(r)) || re.test(clean(r.value)));
      if (!pick) return note(report.needs_you, label);
      pick.click();
      note(report.filled, rules[0].label);
    });

    // ---- files
    const inputs = Array.from(scope.querySelectorAll("input[type=file]")).filter((el) => !el.disabled);
    inputs.forEach((input) => {
      const box = input.closest("[data-field], [id*='resume'], [id*='cover'], fieldset");
      const context = [input.id, input.name, input.getAttribute("aria-label"), labelOf(input),
        box ? box.id + " " + (box.getAttribute("data-field") || "") : ""].join(" ");
      const rule = FILE_RULES.find((r) => r.test.test(context));
      if (!rule) return;
      if (input.files && input.files.length) return;
      const order = rule.slot === "resume" ? ["pdf", "resume"] : ["cover"];
      const file = order.map((k) => files[k]).filter(Boolean).find((f) => accepts(input, f));
      if (!file) return note(report.needs_you, rule.label + " (attach it yourself)");
      attachFile(input, file);
      note(report.files, rule.label + ": " + file.name);
    });

    // ---- required fields still empty
    scope.querySelectorAll("input[required], textarea[required], select[required], [aria-required='true']").forEach((el) => {
      if (!isVisible(el) || ["hidden", "checkbox", "radio", "submit", "button"].includes(el.type)) return;
      const empty = el.type === "file" ? !(el.files && el.files.length) : !String(el.value || "").trim();
      const label = labelOf(el) || el.name || el.id;
      if (empty && label && !NEVER.test(label)) note(report.needs_you, label);
    });
    return report;
  }

  const api = { fill, detectSite, SITES };
  root.__appStudioAutofill = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
