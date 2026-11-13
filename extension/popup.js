const DEFAULT_PORT = 47821;
const $ = (id) => document.getElementById(id);

const state = { token: "", base: `http://127.0.0.1:${DEFAULT_PORT}`, tab: null, applied: false, inQueue: false, companyQueued: false, reachable: true, origin: "", debug: null };

const storage = {
  get: () => chrome.storage.local.get({ token: "", port: DEFAULT_PORT, base: "" }),
  set: (values) => chrome.storage.local.set(values),
};

// "vps.example:47821" -> "http://vps.example:47821"; anything after host:port is dropped. "" if not a web address.
function normalizeBase(text) {
  let value = (text || "").trim();
  if (!value) return "";
  if (value.includes("://") && !/^https?:\/\//i.test(value)) return "";
  if (!/^https?:\/\//i.test(value)) value = `http://${value}`;
  try {
    const url = new URL(value);
    return url.hostname ? url.origin : "";
  } catch (_) {
    return "";
  }
}

async function api(path, body) {
  const res = await fetch(`${state.base}${path}`, {
    method: body ? "POST" : "GET",
    headers: { Authorization: `Bearer ${state.token}`, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try {
    data = await res.json();
  } catch (_) {
    /* non-JSON reply */
  }
  return { status: res.status, data };
}

function banner(kind, title, text) {
  const box = $("banner");
  box.hidden = !kind;
  if (!kind) return;
  box.className = `banner ${kind}`;
  box.replaceChildren();
  const head = document.createElement("b");
  head.textContent = title;
  box.append(head, document.createTextNode(text || ""));
}

function setStatus(kind, text) {
  const s = $("status");
  s.hidden = !text;
  s.className = `status ${kind || ""}`;
  s.textContent = text || "";
}

function refreshAddButton() {
  const long = $("description").value.trim().length >= 80;
  const url = /^https?:\/\//i.test($("url").value.trim());
  $("add").disabled = !(state.reachable && long && url && !state.applied && !state.inQueue && !state.companyQueued);
  $("count").textContent = `(${$("description").value.trim().length.toLocaleString()} characters)`;
}

let checkTimer = null;
function scheduleCheck() {
  clearTimeout(checkTimer);
  checkTimer = setTimeout(runCheck, 250);
}

async function runCheck() {
  try {
    const { status, data } = await api("/api/check", {
      url: $("url").value.trim(), company: $("company").value.trim(),
    });
    if (status === 401) return showSetup("The token is missing or wrong.");
    state.reachable = true;
    state.applied = !!data.applied;
    state.inQueue = !!data.in_queue;
    state.companyQueued = !!data.company_queued;
    if (state.applied) {
      const m = (data.matches || [])[0] || {};
      const when = (m.applied_at || "").slice(0, 10);
      banner("bad", "Already applied to this company",
        `${m.company || $("company").value}${m.role ? " — " + m.role : ""}${when ? " (" + when + ")" : ""}. It will not be added.`);
    } else if (state.inQueue) {
      banner("warn", "Already in your queue", "This job link has been added before.");
    } else if (state.companyQueued) {
      const m = (data.queued_matches || [])[0] || {};
      banner("bad", "This company is already in your queue",
        `${m.company || $("company").value}${m.role ? " — " + m.role : ""}${m.status ? " (" + m.status + ")" : ""}. It will not be added.`);
    } else if (!$("company").value.trim()) {
      banner("warn", "Company not detected", "Type the company name so it can be checked against your applied jobs.");
    } else {
      banner("ok", "No application to this company yet", "Safe to add.");
    }
  } catch (_) {
    state.reachable = false;
    banner("bad", "Application Studio is not running", "Open the app on this computer, then reopen this popup.");
  }
  refreshAddButton();
}

function showSetup(message) {
  $("form").hidden = true;
  $("setup").hidden = false;
  $("token").value = state.token;
  $("server").value = /^http:\/\/127\.0\.0\.1:\d+$/.test(state.base) ? "" : state.base;
  setStatus(message ? "bad" : "", message || "");
}

async function readPage() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  state.tab = tab;
  let frames = [];
  try {
    const injected = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      files: ["extract.js"],
    });
    frames = injected.map((r) => r.result).filter(Boolean);
  } catch (_) {
    /* restricted page (chrome://, store, PDF viewer) */
  }
  if (!frames.length) return null;
  const pick =
    frames.find((f) => f.selection) ||
    frames
      .filter((f) => f.method === "jsonld" || f.method === "site-data")
      .sort((a, b) => b.description.length - a.description.length)[0] ||
    [...frames].sort((a, b) => b.description.length - a.description.length)[0];
  const top = frames.find((f) => f.url === tab.url) || frames[0];
  return {
    url: tab.url,
    apply_url: pick.apply_url || top.apply_url || "",
    title: pick.title || top.title,
    company: pick.company || top.company,
    salary: pick.salary || top.salary || "",
    site: pick.site || top.site || "",
    description: pick.description,
    method: pick.method,
    debug: pick.debug || top.debug || null,
  };
}

const METHOD_TEXT = {
  selection: "Using the text you selected on the page.",
  jsonld: "Read from the page's structured job data.",
  "site-data": "Read from the job data on the page.",
  "site-page": "Read from what the page shows. Check each field.",
  page: "Read from the page content. Check it, or select the description on the page and reopen this popup.",
};

async function init(skipApply = false) {
  const saved = await storage.get();
  state.token = saved.token;
  state.base = saved.base || `http://127.0.0.1:${saved.port || DEFAULT_PORT}`;
  if (!state.token) return showSetup("");

  if (!skipApply) {
    const [current] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (current && ApplyAssist.isApplyPage(current.url || "")) return ApplyAssist.start(current);
  }
  $("form").hidden = false;
  const page = await readPage();
  if (!page) {
    banner("warn", "Cannot read this page", "Open the job posting in a normal tab and try again.");
    return;
  }
  // On Jobright / HiringCafe only the original job link is kept (the company's own address when the page shows one)
  state.origin = page.url;
  state.debug = { url: page.url, site: page.site, method: page.method, ...(page.debug || {}),
    apply_url: page.apply_url, company: page.company, title: page.title, salary: page.salary };
  $("url").value = page.apply_url || page.url;
  $("url-note").textContent = page.apply_url ? "(original job link)" : "";
  $("company").value = page.company || "";
  $("role").value = page.title || "";
  $("salary").value = page.salary || "";
  $("description").value = page.description || "";
  $("method").textContent = METHOD_TEXT[page.method] || "";
  refreshAddButton();
  await runCheck();
}

async function add() {
  $("add").disabled = true;
  setStatus("", "Adding…");
  try {
    const { status, data } = await api("/api/jobs", {
      url: $("url").value.trim(),
      company: $("company").value.trim(),
      role_title: $("role").value.trim(),
      job_description: $("description").value.trim(),
      salary: $("salary").value.trim(),
      source: new URL(state.origin || $("url").value.trim()).hostname.replace(/^www\./, ""),
    });
    if (status === 200) {
      setStatus("ok", data.started ? "Added — the queue has started." : "Added to your queue.");
      setTimeout(() => window.close(), 1400);
      return;
    }
    if (status === 401) return showSetup("The token is missing or wrong.");
    setStatus("bad", data.error || "The app refused this job.");
    await runCheck();
  } catch (_) {
    state.reachable = false;
    setStatus("bad", "Could not reach Application Studio. Is it running?");
    refreshAddButton();
  }
}

$("copy-debug").addEventListener("click", async () => {
  const text = JSON.stringify(state.debug || {}, null, 2);
  try {
    await navigator.clipboard.writeText(text);
    setStatus("ok", "Detection details copied.");
  } catch (_) {
    setStatus("bad", "Could not copy.");
  }
});
$("add").addEventListener("click", add);
$("cancel").addEventListener("click", () => window.close());
$("gear").addEventListener("click", () => showSetup(""));
$("cancel-setup").addEventListener("click", () => window.close());
$("save-setup").addEventListener("click", async () => {
  const typed = $("server").value.trim();
  const base = typed ? normalizeBase(typed) : `http://127.0.0.1:${DEFAULT_PORT}`;
  if (!base) return setStatus("bad", "That server address is not a valid web address.");
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:|$)/.test(base)) {
    // A remote controller: Chrome must be allowed to talk to that one address (asked once, on this click).
    const granted = await chrome.permissions.request({ origins: [`${base}/*`] });
    if (!granted) return setStatus("bad", "Permission to reach that server was not granted.");
  }
  state.token = $("token").value.trim();
  state.base = base;
  await storage.set({ token: state.token, base });
  $("setup").hidden = true;
  setStatus("", "");
  init();
});
$("url").addEventListener("input", scheduleCheck);
$("company").addEventListener("input", scheduleCheck);
$("description").addEventListener("input", refreshAddButton);

init();
