// Injected into the current tab (all frames). The value of the last expression is returned to the popup.
// Jobright and HiringCafe each have their own reader (their pages share one layout per site);
// every other page goes through the generic reader at the bottom.
(() => {
  const clean = (t) =>
    (t || "")
      .replace(/ /g, " ")
      .replace(/[ \t]+\n/g, "\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();

  const htmlToText = (html) => {
    const doc = new DOMParser().parseFromString(html || "", "text/html");
    doc.querySelectorAll("br").forEach((n) => n.replaceWith("\n"));
    doc.querySelectorAll("li").forEach((n) => n.prepend("• "));
    doc.querySelectorAll("p,div,li,h1,h2,h3,h4,h5,tr,ul,ol").forEach((n) => n.append("\n"));
    return clean(doc.body.textContent);
  };

  const meta = (sel) => {
    const n = document.querySelector(sel);
    return n ? (n.getAttribute("content") || "").trim() : "";
  };

  const cleanTitle = (t) => {
    let text = (t || "").replace(/\s+/g, " ").trim();
    for (const sep of [" | ", " · ", " — ", " - "]) {
      if (text.includes(sep)) {
        text = text.split(sep)[0].trim();
        break;
      }
    }
    return text.slice(0, 200);
  };

  const text = (node) => clean(node ? node.innerText || node.textContent || "" : "");
  const one = (s) => String(s || "").replace(/\s+/g, " ").trim();

  const host = location.hostname.replace(/^www\./, "");
  const site = /(^|\.)jobright\.ai$/.test(host) ? "jobright" : /(^|\.)hiring\.cafe$|(^|\.)hiringcafe\.com$/.test(host) ? "hiringcafe" : "";

  // ---- salary -------------------------------------------------------------------------------
  const SALARY_RE = new RegExp(
    "(?:US\\$|CA\\$|A\\$|[$£€₹])\\s?\\d[\\d,]*(?:\\.\\d+)?\\s?[kK]?" +
      "(?:\\s?(?:\\/\\s?(?:yr|year|hr|hour|mo|month)\\b)?\\s?(?:-|–|—|to)\\s?(?:US\\$|CA\\$|A\\$|[$£€₹])?\\s?\\d[\\d,]*(?:\\.\\d+)?\\s?[kK]?)?" +
      "(?:\\s?(?:\\/|per\\s|a\\s)\\s?(?:yr|year|annum|hr|hour|mo|month|week|wk|day))?",
    "i"
  );
  const salaryFromText = (t) => {
    const m = SALARY_RE.exec(t || "");
    return m ? m[0].replace(/\s+/g, " ").trim() : "";
  };
  const money = (n, cur) => {
    const sym = { USD: "$", CAD: "CA$", GBP: "£", EUR: "€", AUD: "A$", INR: "₹" }[cur] || "$";
    return sym + Number(n).toLocaleString("en-US");
  };
  const salaryFromNumbers = (low, high, period, cur) => {
    low = Number(low) || 0;
    high = Number(high) || 0;
    if (!low && !high) return "";
    const unit = { yearly: "yr", year: "yr", annual: "yr", hourly: "hr", hour: "hr", monthly: "mo", month: "mo" }[
      String(period || "").toLowerCase()
    ];
    const span = low && high && low !== high ? `${money(low, cur)} - ${money(high, cur)}` : money(low || high, cur);
    return span + (unit ? `/${unit}` : "");
  };

  const embeddedJson = () => {
    const out = [];
    document.querySelectorAll('script#__NEXT_DATA__, script[type="application/json"]').forEach((s) => {
      try {
        out.push(JSON.parse(s.textContent));
      } catch (_) {
        /* not JSON */
      }
    });
    return out;
  };

  const absolute = (href) => {
    try {
      return new URL(href, location.href).href;
    } catch (_) {
      return "";
    }
  };

  // The site's own tracking parameter is not part of the company's address.
  const withoutTracking = (href) => {
    try {
      const u = new URL(href, location.href);
      ["jr_id", "jr_src", "utm_source", "utm_medium", "utm_campaign"].forEach((k) => u.searchParams.delete(k));
      return u.href;
    } catch (_) {
      return "";
    }
  };

  // Text blocks the app hands to Claude: a heading line, then the body.
  const block = (title, body) => (body ? `${title}\n${body}` : "");
  const bullets = (list) =>
    (list || [])
      .flatMap((s) => String(s || "").split(/;\s+(?=[a-z])/)) // long run-on items become one bullet each
      .map(one)
      .filter(Boolean)
      .map((s) => "• " + s)
      .join("\n");

  // ---- Jobright -------------------------------------------------------------------------------
  const jobrightData = () => {
    for (const sel of ["script#__NEXT_DATA__", "script#jobright-helper-job-detail-info"]) {
      const node = document.querySelector(sel);
      if (!node) continue;
      try {
        const j = JSON.parse(node.textContent);
        const ds = j.jobResult ? j : j.props && j.props.pageProps && j.props.pageProps.dataSource;
        if (!ds || !ds.jobResult) continue;
        // After in-app navigation the embedded data can still be the previous job's.
        const shown = one((document.querySelector("h1") || {}).innerText).toLowerCase();
        const title = one(ds.jobResult.jobTitle).toLowerCase();
        if (shown && title && !shown.includes(title.slice(0, 40)) && !title.includes(shown.slice(0, 40))) continue;
        return ds;
      } catch (_) {
        /* try the next source */
      }
    }
    return null;
  };

  const jobrightFromData = (ds) => {
    const j = ds.jobResult;
    const c = ds.companyResult || {};
    const q = j.qualifications || {};
    const company = one(c.companyName);
    const facts = [
      j.workModel || (j.isRemote ? "Remote" : ""),
      j.employmentType,
      j.jobSeniority,
      j.jobLocation,
      j.minYearsOfExperience ? `${j.minYearsOfExperience}+ years experience` : "",
    ]
      .filter(Boolean)
      .join(" · ");
    const salary = one(j.salaryDesc) || salaryFromNumbers(j.minSalary, j.maxSalary, "year", "USD");
    const skills = (j.jdCoreSkills || []).map((s) => s.skill).filter(Boolean).join(", ");
    const companyLines = [
      c.companyDesc,
      c.gptShortDescription && c.gptShortDescription !== c.companyDesc ? c.gptShortDescription : "",
      c.companySize && `Size: ${c.companySize}`,
      c.companyFoundYear && `Founded: ${c.companyFoundYear}`,
      c.companyLocation && `Headquarters: ${c.companyLocation}`,
      c.companyURL && `Website: ${c.companyURL}`,
      c.companyCategories && `Industries: ${String(c.companyCategories).split(",").join(", ")}`,
      c.fundraisingCurrentStage && `Stage: ${c.fundraisingCurrentStage}`,
    ]
      .filter(Boolean)
      .join("\n");
    const required = [bullets(q.mustHave), bullets(j.educationSummaries)].filter(Boolean).join("\n");
    const qualifications = [
      block("Required", required),
      block("Preferred", bullets(q.preferredHave)),
      skills ? `Key skills: ${skills}` : "",
    ]
      .filter(Boolean)
      .join("\n\n");
    const description = [
      `${one(j.jobTitle)}${company ? " — " + company : ""}`,
      facts,
      salary ? `Salary: ${salary}` : "",
      "",
      block("Summary", one(j.jobSummary)),
      block("Responsibilities", bullets(j.coreResponsibilities)),
      block("Qualifications", qualifications),
      block("Benefits", bullets(j.benefitsSummaries)),
      block("Company", companyLines),
    ]
      .filter((s) => s !== undefined)
      .join("\n\n")
      .replace(/\n{3,}/g, "\n\n");
    const apply = j.applyLink || j.originalUrl || "";
    return {
      title: one(j.jobTitle),
      company,
      salary,
      apply: apply ? withoutTracking(apply) : "",
      description: clean(description),
      source: "embedded json",
    };
  };

  // What the page shows, section by section (used when the embedded data is missing or stale).
  const jobrightFromPage = () => {
    const sections = [];
    document.querySelectorAll("h2[title]").forEach((h) => {
      const name = h.getAttribute("title");
      if (!/^(Responsibilities|Qualification|Benefits)$/i.test(name)) return;
      const box = h.closest("section") || (h.parentElement && h.parentElement.parentElement);
      const body = text(box).replace(new RegExp("^" + name + "\\s*", "i"), "");
      if (body) sections.push(block(name === "Qualification" ? "Qualifications" : name, body));
    });
    const intro = document.querySelector("[class*='companyIntroduction']");
    if (intro) sections.push(block("Company", text(intro)));
    const title = one((document.querySelector("h1") || {}).innerText);
    const company = one(text(document.querySelector("[class*='company-name']")));
    const link = document.querySelector("a[class*='index_origin']");
    return {
      title,
      company,
      salary: one(text(document.querySelector('[data-time-filter-field="salaryDesc"]'))),
      apply: link ? withoutTracking(link.getAttribute("href")) : "",
      description: clean([`${title}${company ? " — " + company : ""}`, ...sections].join("\n\n")),
      source: "visible page",
    };
  };

  // ---- HiringCafe -------------------------------------------------------------------------------
  // The job opens in a side panel on top of the search results (or as its own /job/... page).
  const requisitionFromLink = (href) => {
    const m = /\/job\/[^/?#]*-([a-z0-9]{12,})(?:[/?#]|$)/i.exec(href || "");
    return m ? m[1] : "";
  };

  const findPosting = (id) => {
    if (!id) return null;
    let hit = null;
    const seen = new Set();
    const visit = (n, depth) => {
      if (hit || !n || typeof n !== "object" || depth > 9 || seen.has(n)) return;
      seen.add(n);
      if (!Array.isArray(n) && n.requisition_id === id && n.apply_url) {
        hit = n;
        return;
      }
      Object.values(n).forEach((v) => visit(v, depth + 1));
    };
    embeddedJson().forEach((j) => visit(j, 0));
    return hit;
  };

  const hiringcafeFromPage = () => {
    const heading = [...document.querySelectorAll("h1")].find((h) => h.closest("[role='dialog']")) || document.querySelector("h1");
    const panel = heading && heading.closest("[role='dialog']");
    const onJobPage = /\/(job|viewjob)\//.test(location.pathname);
    if (!heading || (!panel && !onJobPage)) return null; // a results list, not a job
    const root = panel || heading.closest("main") || document.body;

    const fullView = [...root.querySelectorAll("a[href*='/job/']")].find((a) => requisitionFromLink(a.href));
    const jobLink = fullView ? fullView.href : onJobPage ? location.href : "";
    const posting = findPosting(requisitionFromLink(jobLink));
    const v5 = (posting && posting.v5_processed_job_data) || {};

    const tagged = [...root.querySelectorAll("span")].find((s) => !s.children.length && /^@\s*\S/.test(s.textContent.trim()));
    const company = one(tagged ? tagged.textContent.replace(/^\s*@\s*/, "") : v5.company_name);
    const title = one(heading.innerText);

    const badges = [...root.querySelectorAll("span")]
      .filter((s) => !s.children.length && /\brounded\b/.test(s.className) && s.textContent.trim().length < 40)
      .map((s) => one(s.textContent))
      .filter((b) => !/talent agent|^job description$/i.test(b));
    const salary =
      badges.map(salaryFromText).find(Boolean) ||
      salaryFromNumbers(v5.yearly_min_compensation, v5.yearly_max_compensation, "year", v5.listed_compensation_currency);

    const summaries = [];
    root.querySelectorAll("span").forEach((s) => {
      const label = one(s.textContent);
      const name = { "Responsibilities:": "Responsibilities (summary)", "Requirements Summary:": "Requirements (summary)", "Technical Tools Mentioned:": "Tools" }[label];
      if (!name || s.children.length) return;
      const next = s.nextElementSibling;
      if (next) summaries.push(block(name, text(next)));
    });
    const article = root.querySelector("article");
    const description = [
      `${title}${company ? " — " + company : ""}`,
      badges.length ? badges.join(" · ") : "",
      summaries.join("\n\n"),
      block("Job description", text(article)),
    ]
      .filter(Boolean)
      .join("\n\n");

    return {
      title,
      company,
      salary,
      apply: posting ? posting.apply_url : jobLink,
      description: clean(description),
      source: posting ? "visible page + page data" : "visible page",
    };
  };

  // ---- result ---------------------------------------------------------------------------------
  const result = {
    url: location.href,
    title: "",
    company: "",
    description: "",
    salary: "",
    apply_url: "",
    site,
    method: "page",
    selection: false,
  };

  const selected = clean(String(window.getSelection ? window.getSelection() : ""));
  if (selected.length > 80) {
    result.description = selected;
    result.method = "selection";
    result.selection = true;
  }

  if (site) {
    let info = null;
    if (site === "jobright") {
      const ds = jobrightData();
      info = ds ? jobrightFromData(ds) : jobrightFromPage();
    } else {
      info = hiringcafeFromPage();
    }
    if (!info) {
      result.method = "site-empty";
      result.debug = { site, note: "no job is open on this page" };
      return result;
    }
    result.title = info.title;
    result.company = /^(jobright|hiring\s?cafe)/i.test(info.company) ? "" : info.company;
    result.salary = info.salary || salaryFromText(info.description.slice(0, 1500));
    result.apply_url = info.apply;
    if (!result.selection) {
      result.description = info.description.slice(0, 60000);
      result.method = info.source === "embedded json" ? "site-data" : "site-page";
    }
    const has = (label) => new RegExp("(^|\\n)" + label, "i").test(info.description);
    result.debug = {
      site,
      source: info.source,
      found: {
        title: !!result.title,
        company: !!result.company,
        salary: !!result.salary,
        original_link: !!result.apply_url,
        description_chars: info.description.length,
        sections: ["Summary", "Responsibilities", "Qualifications", "Required", "Preferred", "Benefits", "Company", "Job description"].filter(has),
      },
    };
    return result;
  }

  // ---- any other site -------------------------------------------------------------------------
  const jobPostings = () => {
    const found = [];
    const visit = (node) => {
      if (!node || typeof node !== "object") return;
      if (Array.isArray(node)) return node.forEach(visit);
      const type = node["@type"];
      if (type === "JobPosting" || (Array.isArray(type) && type.includes("JobPosting"))) found.push(node);
      if (node["@graph"]) visit(node["@graph"]);
    };
    document.querySelectorAll('script[type="application/ld+json"]').forEach((s) => {
      try {
        visit(JSON.parse(s.textContent));
      } catch (_) {
        /* ignore malformed JSON-LD */
      }
    });
    return found;
  };

  const h1 = document.querySelector("h1");
  result.title = cleanTitle((h1 && h1.innerText) || meta('meta[property="og:title"]') || document.title);
  result.company = meta('meta[property="og:site_name"]');

  const posting = jobPostings().find((p) => htmlToText(p.description).length > 80);
  if (posting) {
    result.title = String(posting.title || result.title);
    const org = posting.hiringOrganization;
    result.company = String((org && (org.name || org)) || result.company);
    const base = posting.baseSalary && posting.baseSalary.value;
    if (base && (base.minValue || base.maxValue || base.value)) {
      const unit = String(base.unitText || "").toLowerCase();
      result.salary = salaryFromNumbers(base.minValue || base.value, base.maxValue || base.value, unit, posting.baseSalary.currency);
    }
    if (!result.selection) {
      result.description = htmlToText(posting.description);
      result.method = "jsonld";
    }
  }

  if (!result.description) {
    let best = "";
    for (const sel of [
      "[class*='job-description']",
      "[class*='jobDescription']",
      "[id*='job-description']",
      "[id*='jobDescription']",
      "[data-testid*='description']",
      "[class*='description']",
      "article",
      "main",
      "[role='main']",
    ]) {
      const node = document.querySelector(sel);
      const t = node ? clean(node.innerText) : "";
      if (t.length > Math.max(best.length, 200)) best = t;
      if (best.length > 1500) break;
    }
    result.description = best || clean(document.body ? document.body.innerText : "");
  }
  if (!result.salary) result.salary = salaryFromText(result.description.slice(0, 4000));

  result.description = result.description.slice(0, 60000);
  return result;
})();
