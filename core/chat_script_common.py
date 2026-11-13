"""JavaScript shared by every web-chat driver (Claude, ChatGPT): pulling resume JSON out of reply text."""

from core.claude_limits import CLAUDE_MAX_SCRAPED_TEXT_CHARS

# ``clipText`` bounds what is sent back across the Qt bridge; the rest finds the largest balanced JSON object that
# looks like a resume inside a block of text.
JSON_HELPERS = (
    r"""
  const CLAUDE_MAX_TEXT = """
    + str(CLAUDE_MAX_SCRAPED_TEXT_CHARS)
    + r""";
  function clipText(t) {
    return (t || '').slice(0, CLAUDE_MAX_TEXT);
  }
  function sliceBalancedJson(s, from) {
    let depth = 0;
    let inStr = false;
    let esc = false;
    for (let i = from; i < s.length; i++) {
      const c = s[i];
      if (inStr) {
        if (esc) esc = false;
        else if (c === '\\') esc = true;
        else if (c === '"') inStr = false;
        continue;
      }
      if (c === '"') { inStr = true; continue; }
      if (c === '{') depth++;
      else if (c === '}') {
        depth--;
        if (depth === 0) return s.slice(from, i + 1);
      }
    }
    return '';
  }
  function scoreResumeJson(text) {
    if (!text || text.length < 40) return null;
    try {
      const o = JSON.parse(text);
      const exp = Array.isArray(o.experience) ? o.experience.length : 0;
      const skills = Array.isArray(o.skill_groups) ? o.skill_groups.length : 0;
      if (exp === 0 && skills === 0) return null;
      return { text: text, len: text.length, exp: exp, skills: skills };
    } catch (e) {
      return null;
    }
  }
  function bestJsonFromBlob(blob) {
    const markers = ['"company_name"', '"skill_groups"', '"cover_letter"'];
    let best = null;
    for (const marker of markers) {
      let searchFrom = blob.length;
      while (searchFrom > 0) {
        const idx = blob.lastIndexOf(marker, searchFrom - 1);
        if (idx < 0) break;
        searchFrom = idx;
        const start = blob.lastIndexOf('{', idx);
        if (start < 0) continue;
        const chunk = sliceBalancedJson(blob, start);
        const scored = scoreResumeJson(chunk);
        if (scored && (!best || scored.len > best.len)) best = scored;
      }
    }
    return best ? best.text : '';
  }
"""
)


# What a chat page is made of, without any of its text: attribute values, counts and the shape around the longest
# JSON-looking block. Saved when a reply cannot be found, so the selectors can be fixed from facts.
DOM_OUTLINE_JS = r"""
(() => {
  const count = (list) => { const m = {}; list.forEach(v => { if (v) m[v] = (m[v] || 0) + 1; });
    return Object.fromEntries(Object.entries(m).sort((a, b) => b[1] - a[1]).slice(0, 25)); };
  const all = Array.from(document.querySelectorAll('*'));
  let best = null, bestLen = 0;
  all.forEach(el => { if (el.children.length < 40) { const t = el.textContent || '';
    if (t.length > bestLen && t.includes('"company_name"') && t.length < 60000) { best = el; bestLen = t.length; } } });
  const chain = [];
  for (let n = best; n && n !== document.body && chain.length < 8; n = n.parentElement)
    chain.push(n.tagName.toLowerCase() + (n.id ? '#' + n.id : '') + '[' + Array.from(n.attributes).map(a => a.name +
      (/^(data-|role|aria-label)/.test(a.name) ? '=' + String(a.value).slice(0, 40) : '')).slice(0, 8).join(' ') + ']' +
      '.' + String(n.className || '').split(/\s+/).slice(0, 4).join('.'));
  return JSON.stringify({
    url: location.href, title: document.title,
    testids: count(all.map(e => e.getAttribute('data-testid'))),
    roles: count(all.map(e => e.getAttribute('data-message-author-role'))),
    tags: count(['main','article','section','pre','code','nav','aside','form','textarea'].flatMap(t =>
      Array.from(document.getElementsByTagName(t)).map(() => t))),
    headings: Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6')).map(h => (h.textContent || '').trim().slice(0, 30)).slice(0, 12),
    json_block_chain_from_inside_out: chain,
    json_block_length: bestLen,
  });
})()
"""
