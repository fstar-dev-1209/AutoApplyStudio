/* Job apply assist, popup side. The popup holds the app token; the page only ever receives the data to fill.
 * Nothing here submits a form, and a job is marked applied only when the person presses "I submitted it". */
const ApplyAssist = (() => {
  const SITE = /(^|\.)greenhouse\.io$/i; // phase 1: Greenhouse only; keep in step with autofill.js
  const view = { tab: null, kit: null, report: null };
  const key = (tabId) => `apply:${tabId}`;

  const isApplyPage = (url) => {
    try {
      return SITE.test(new URL(url).hostname);
    } catch (_) {
      return false;
    }
  };

  const sessionGet = async (k) => (await chrome.storage.session.get(k))[k];
  const sessionSet = (k, v) => chrome.storage.session.set({ [k]: v });

  function applyBanner(kind, title, text) {
    const box = $("apply-banner");
    box.hidden = !kind;
    if (!kind) return;
    box.className = `banner ${kind}`;
    box.replaceChildren();
    const head = document.createElement("b");
    head.textContent = title;
    box.append(head, document.createTextNode(text || ""));
  }

  function list(id, items) {
    const ul = $(id);
    ul.replaceChildren(...items.map((t) => Object.assign(document.createElement("li"), { textContent: t })));
    $(`${id}-h`).hidden = ul.hidden = !items.length;
  }

  function showReport(report) {
    $("apply-report").hidden = false;
    list("apply-filled", report.filled);
    list("apply-files", report.files);
    list("apply-needs", report.needs_you);
    list("apply-skipped", report.skipped);
  }

  async function fetchKit(extra = {}) {
    const { status, data } = await api("/api/apply/kit", {
      url: view.tab.url, company: guessCompany(view.tab), ...extra,
    });
    if (status === 401) return showSetup("The token is missing or wrong.");
    return { status, data };
  }

  // Greenhouse puts the company in the page title ("Job Application for X at Acme") and the board in the path.
  function guessCompany(tab) {
    const m = /\bat\s+(.+?)(?:\s*[|-].*)?$/i.exec(tab.title || "");
    if (m) return m[1].trim();
    try {
      return new URL(tab.url).pathname.split("/").filter(Boolean)[0] || "";
    } catch (_) {
      return "";
    }
  }

  function describe(job) {
    return `${job.company || "Unknown company"}${job.role ? " - " + job.role : ""}`;
  }

  async function loadKit(extra) {
    const result = await fetchKit(extra);
    if (!result) return false;
    const { status, data } = result;
    $("apply-pick").hidden = true;
    if (status === 200) {
      view.kit = data;
      applyBanner("ok", "Ready to fill", describe(data.job));
      $("apply-fill").disabled = false;
      return true;
    }
    view.kit = null;
    $("apply-fill").disabled = true;
    if (status === 423) applyBanner("bad", "Application Studio is locked", "Unlock it with your password, then reopen this popup.");
    else if (status === 404 && (data.candidates || []).length) {
      applyBanner("warn", "Which job is this?", "No finished job matches this page by link or company. Pick one.");
      const select = $("apply-job");
      select.replaceChildren(...data.candidates.map((c) => Object.assign(document.createElement("option"), { value: c.id, textContent: describe(c) })));
      $("apply-pick").hidden = false;
      $("apply-fill").disabled = false;
      view.picking = true;
    } else if (status === 404) applyBanner("warn", "No finished job to apply with", "Generate documents for this job in Application Studio first.");
    else applyBanner("bad", "Could not prepare the form", data.error || "Unexpected reply from the app.");
    return false;
  }

  async function fetchFiles(jobId, files) {
    const out = {};
    for (const f of files) {
      const { status, data } = await api(`/api/apply/file?job=${encodeURIComponent(jobId)}&kind=${f.kind}`);
      if (status === 200) out[f.kind] = data;
    }
    return out;
  }

  function merge(reports) {
    const matched = reports.filter((r) => r && r.matched);
    if (!matched.length) return { matched: false, reason: (reports.find((r) => r && r.reason) || {}).reason || "no_form" };
    const join = (k) => [...new Set(matched.flatMap((r) => r[k] || []))];
    return {
      matched: true, site: matched[0].site, page: matched[0].page,
      filled: join("filled"), files: join("files"), skipped: join("skipped"),
      needs_you: join("needs_you"),
    };
  }

  async function fill() {
    $("apply-fill").disabled = true;
    setStatus("", "Filling…");
    try {
      if (view.picking && !view.kit) {
        if (!(await loadKit({ job_id: $("apply-job").value }))) return;
        view.picking = false;
      }
      const kit = view.kit;
      const files = await fetchFiles(kit.job.id, kit.files);
      const target = { tabId: view.tab.id, allFrames: true };
      await chrome.scripting.executeScript({ target, files: ["autofill.js"] });
      const results = await chrome.scripting.executeScript({
        target,
        func: (k, f) => globalThis.__appStudioAutofill.fill(document, k, f),
        args: [{ job: kit.job, profile: kit.profile }, files],
      });
      const report = merge(results.map((r) => r.result));
      if (!report.matched) {
        applyBanner("warn", "No application form found",
          report.reason === "unsupported_site" ? "This site is not supported yet." : "Open the application form (the Apply button on the posting), then try again.");
        setStatus("", "");
        return;
      }
      view.report = report;
      showReport(report);
      await api("/api/apply/event", { job_id: kit.job.id, event: "autofilled", report });
      await sessionSet(key(view.tab.id), { job_id: kit.job.id, label: describe(kit.job) });
      $("apply-done").hidden = false;
      applyBanner("ok", "Form filled - not submitted", "Check every field, finish the ones listed under Needs you, then press Submit on the site.");
      setStatus("", "");
    } catch (_) {
      setStatus("bad", "Could not fill this page. Is Application Studio running?");
    } finally {
      $("apply-fill").disabled = false;
    }
  }

  async function submitted() {
    const saved = await sessionGet(key(view.tab.id));
    if (!saved) return;
    $("apply-submitted").disabled = true;
    const { status, data } = await api("/api/apply/event", { job_id: saved.job_id, event: "submitted_confirmed" });
    if (status === 200 || status === 409) {
      await chrome.storage.session.remove(key(view.tab.id));
      $("apply-done").hidden = true;
      setStatus("ok", status === 200 ? "Marked as applied." : "That job was already marked as applied.");
      setTimeout(() => window.close(), 1400);
    } else {
      $("apply-submitted").disabled = false;
      setStatus("bad", data.error || "Could not update the app.");
    }
  }

  async function start(tab) {
    view.tab = tab;
    $("form").hidden = true;
    $("apply").hidden = false;
    document.querySelector("h1").textContent = "Fill application";
    const saved = await sessionGet(key(tab.id));
    $("apply-done").hidden = !saved;
    if (saved) applyBanner("warn", "Filled earlier", `${saved.label}. If you submitted it, press "I submitted it" below.`);
    try {
      await loadKit();
    } catch (_) {
      applyBanner("bad", "Application Studio is not running", "Open the app on this computer, then reopen this popup.");
      $("apply-fill").disabled = true;
    }
    $("apply-fill").addEventListener("click", fill);
    $("apply-submitted").addEventListener("click", submitted);
    $("apply-close").addEventListener("click", () => window.close());
    $("apply-add-instead").addEventListener("click", () => {
      $("apply").hidden = true;
      $("form").hidden = false;
      document.querySelector("h1").textContent = "Add job to Application Studio";
      init(true);
    });
  }

  return { isApplyPage, start };
})();
