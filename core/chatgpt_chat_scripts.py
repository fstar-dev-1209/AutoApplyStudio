"""JavaScript helpers for ChatGPT web chat automation (chatgpt.com).

Written against ChatGPT's page structure as last known (``#prompt-textarea`` composer, ``data-testid`` send/stop
buttons, ``data-message-author-role`` messages). Those details change without notice: every selector lives in
this file so a change on the site is a one-file fix, and ``scripts/diagnose_chat.py`` prints what the live page
actually exposes.
"""

from core.chat_script_common import JSON_HELPERS

_COMPOSER = (
    "'#prompt-textarea, div[contenteditable=\"true\"].ProseMirror, "
    "div[contenteditable=\"true\"][role=\"textbox\"], textarea[name=\"prompt-textarea\"], "
    "div[contenteditable=\"true\"], textarea'"
)

_COMMON = (
    JSON_HELPERS
    + r"""
  const COMPOSER = """
    + _COMPOSER
    + r""";
  // A conversation page can hold a hidden editor before the real one: the visible one is the message box.
  function composerEl() {
    const all = Array.from(document.querySelectorAll(COMPOSER));
    return all.find(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; }) || all[0] || null;
  }
  function composerText(c) {
    if (!c) return '';
    return (c.value !== undefined && c.tagName === 'TEXTAREA') ? c.value : (c.innerText || c.textContent || '');
  }
  function pastedCards(c) {
    if (!c) return 0;
    const form = c.closest('form') || (c.parentElement && c.parentElement.parentElement && c.parentElement.parentElement.parentElement);
    if (!form) return 0;
    return Array.from(form.querySelectorAll('*')).filter(
      e => e.children.length === 0 && /^pasted text$/i.test((e.textContent || '').trim())).length;
  }
  function conversationRoot() {
    return document.querySelector('main, [role="main"]') || document.body;
  }
  function isInSidebar(el) {
    return !!(el.closest('nav') || el.closest('aside') || el.closest('[data-testid="sidebar"]'));
  }
  function outermost(els) { return els.filter(e => !els.some(o => o !== e && o.contains(e))); }
  function assistantEls() {
    const root = conversationRoot();
    // 1. messages marked with their author
    // (the second form is ChatGPT's page as of November 2026: replies carry data-markdown-text-style="assistant-message")
    let els = Array.from(root.querySelectorAll(
      '[data-message-author-role="assistant"], [data-markdown-text-style="assistant-message"]'))
      .filter(e => !isInSidebar(e) && (e.innerText || e.textContent || '').trim().length > 0);
    if (els.length) return outermost(els);
    // 2. conversation turns headed "ChatGPT said:" (a visually hidden heading in each turn), anywhere on the page
    els = Array.from(document.querySelectorAll('article, section, [data-testid^="conversation-turn"]')).filter(t => {
      if (isInSidebar(t)) return false;
      const heads = Array.from(t.querySelectorAll('h1,h2,h3,h4,h5,h6,[class*="sr-only"]')).map(h => (h.textContent || '').toLowerCase());
      return heads.some(h => h.includes('chatgpt said'));
    });
    if (els.length) return outermost(els);
    // 3. the rendered markdown of a reply
    els = Array.from(document.querySelectorAll('.markdown, [class*="markdown"]')).filter(e =>
      !isInSidebar(e) && !e.closest('[data-message-author-role="user"]'));
    return outermost(els);
  }
  // Last resort when no element can be identified as a reply: the page's text without the sidebar, the composer and
  // the user's own message (which holds the prompt, schema included).
  function fallbackText() {
    const clone = document.body.cloneNode(true);
    clone.querySelectorAll('nav, aside, form, textarea, [contenteditable="true"], [data-message-author-role="user"], script, style, button')
      .forEach(n => n.remove());
    clone.querySelectorAll('article, section, [data-testid^="conversation-turn"]').forEach(t => {
      const heads = Array.from(t.querySelectorAll('h1,h2,h3,h4,h5,h6,[class*="sr-only"]')).map(h => (h.textContent || '').toLowerCase());
      if (heads.some(h => h.startsWith('you said')) && !heads.some(h => h.includes('chatgpt said'))) t.remove();
    });
    return (clone.textContent || '').trim();
  }
  function textOf(el) { return (el.innerText || el.textContent || '').trim(); }
  function stopButton() {
    return Array.from(document.querySelectorAll('button')).find(b => {
      const label = (b.getAttribute('aria-label') || '').toLowerCase();
      const testid = (b.getAttribute('data-testid') || '').toLowerCase();
      return !b.disabled && (testid === 'stop-button' || label.includes('stop streaming') ||
                             label.includes('stop generating') || label === 'stop');
    });
  }
  function isGenerating() {
    return !!(stopButton() || document.querySelector('.result-streaming, [data-testid="streaming-indicator"]'));
  }
  function noticeText() {
    return Array.from(document.querySelectorAll(
      '[role="alert"], [role="status"], [data-testid*="limit"], [data-testid*="banner"]'
    )).map(n => (n.innerText || n.textContent || '').slice(0, 500)).join('\n');
  }
  function looksLimited(text) {
    return /usage limit|rate limit|message limit|too many requests|try again later|you.ve reached|you have reached|hit your limit|limit reached/i.test(text || '');
  }
  function blockedReason() {
    const title = document.title || '';
    if (title.toLowerCase().includes('just a moment') || document.querySelector(
        '#challenge-form, iframe[src*="challenges.cloudflare.com"], #cf-challenge-running')) return 'challenge';
    if (looksLimited(title + '\n' + noticeText())) return 'limit';
    if (/\/auth\/|auth\.openai\.com/.test(location.href)) return 'login';
    if (document.querySelector(
        '[data-testid="login-button"], [data-testid="signup-button"], [data-testid="welcome-login-button"], ' +
        'a[href*="/auth/login"]')) return 'login';
    return '';
  }
  function continueButton() {
    return Array.from(document.querySelectorAll('button')).find(b => {
      if (b.disabled) return false;
      const label = ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')).toLowerCase().replace(/\s+/g, ' ').trim();
      return label.includes('continue generat') || label.includes('continue response') || label === 'continue';
    }) || null;
  }
  function collectReplyCandidates() {
    const root = conversationRoot();
    const out = [];
    const seen = new Set();
    function push(t) {
      t = (t || '').trim();
      if (t.length < 80 || seen.has(t)) return;
      seen.add(t);
      out.push(t);
    }
    assistantEls().forEach(el => push(textOf(el)));
    document.querySelectorAll('pre, code').forEach(el => {
      if (isInSidebar(el) || el.closest('[data-message-author-role="user"]')) return;
      const t = textOf(el);
      if (t.length > 200 && t.includes('{')) push(t);
    });
    if (!out.length) push(fallbackText());
    return out;
  }
  function lastAssistantText() {
    const els = assistantEls();
    return els.length ? textOf(els[els.length - 1]) : fallbackText();
  }
  function blockedNow() {
    const reason = blockedReason();
    if (reason) return reason;
    const last = lastAssistantText();
    if (last && last.length < 500 && looksLimited(last)) return 'limit';
    return '';
  }
  function pickBestReply(candidates) {
    let best = '';
    for (const t of candidates) {
      const looksJson = t.includes('{') && (
        t.includes('"company_name"') || t.includes('"skill_groups"') || t.includes('"cover_letter"'));
      if (looksJson && t.length > best.length) best = t;
    }
    if (best) return best;
    return candidates.length ? candidates[candidates.length - 1] : '';
  }
  function sendButton() {
    // The composer button is often labeled only "Send", and an older disabled copy can sit earlier in the page.
    const all = Array.from(document.querySelectorAll('button, [role="button"]')).filter((b) => {
      const label = (b.getAttribute('aria-label') || '').toLowerCase().replace(/\s+/g, ' ').trim();
      const testid = (b.getAttribute('data-testid') || '').toLowerCase();
      if (/voice|dictat|\bmic\b|attach|upload|\bstop\b/.test(label)) return false;
      return testid === 'send-button' || testid.endsWith('send-button') ||
        label === 'send' || label === 'send prompt' || label === 'send message';
    });
    const enabled = all.filter(sendEnabled);
    return (enabled.length ? enabled : all).at(-1) || null;
  }
  function sendEnabled(button) {
    return !!button && !button.disabled && button.getAttribute('aria-disabled') !== 'true';
  }
  function proseMirrorView(el) {
    let node = el;
    for (let i = 0; node && i < 8; i++, node = node.parentElement) {
      if (node.pmViewDesc && node.pmViewDesc.view) return node.pmViewDesc.view;
    }
    return null;
  }
"""
)

CHATGPT_PAGE_STATE_JS = (
    r"""
(() => {
"""
    + _COMMON
    + r"""
  const blocked = blockedNow();
  const composer = composerEl();
  const sendBtn = sendEnabled(sendButton()) ? sendButton() : null;
  const candidates = collectReplyCandidates();
  const last = lastAssistantText();
  return JSON.stringify({
    loggedIn: !!composer && !blocked,
    blocked: blocked,
    hasComposer: !!composer,
    hasContinue: !!continueButton(),
    isGenerating: isGenerating(),
    assistantCount: assistantEls().length,
    longestAssistant: candidates.reduce((m, t) => Math.max(m, t.length), 0),
    fullTurnText: clipText(last),
    lastAssistantText: clipText(pickBestReply(candidates)),
    replyText: clipText(last || pickBestReply(candidates)),
    canSend: !!composer && !!sendBtn,
    composerLength: composerText(composer).length,
    // ChatGPT turns a long paste into a "Pasted text" card above the box and leaves the box itself empty
    attachments: pastedCards(composer),
    url: location.href
  });
})()
"""
)

# Resume JSON from assistant replies only (never from the pasted job description).
CHATGPT_EXTRACT_JSON_JS = (
    r"""
(() => {
"""
    + _COMMON
    + r"""
  try { window.scrollTo(0, document.body.scrollHeight); } catch (e) {}
  const candidates = collectReplyCandidates();
  let best = '';
  for (const t of candidates) {
    let j = bestJsonFromBlob(t);
    if (j.length > best.length) best = j;
    const re = /```(?:json)?\s*([\s\S]*?)```/gi;
    let m;
    while ((m = re.exec(t))) {
      j = bestJsonFromBlob(m[1]);
      if (j.length > best.length) best = j;
    }
  }
  const plain = pickBestReply(candidates);
  if (plain.length > best.length) {
    const j2 = bestJsonFromBlob(plain);
    if (j2.length > best.length) best = j2;
  }
  return JSON.stringify({
    ok: best.length > 0,
    text: best,
    length: best.length,
    assistantBlocks: candidates.length,
    longestAssistant: candidates.reduce((m, t) => Math.max(m, t.length), 0),
    plainText: clipText(plain)
  });
})()
"""
)

CHATGPT_INSERT_PROMPT_JS = (
    r"""
((promptText) => {
"""
    + _COMMON
    + r"""
  const composer = composerEl();
  if (!composer) {
    return JSON.stringify({ ok: false, error: 'Composer not found. Sign in at chatgpt.com first.' });
  }
  composer.focus();
  const view = proseMirrorView(composer);
  if (view) {
    try {
      const size = view.state.doc.content.size;
      view.dispatch(view.state.tr.insertText(promptText, 1, Math.max(1, size - 1)));
      try { view.focus(); } catch (e) {}
    } catch (e) {}
  }
  if (composer.tagName === 'TEXTAREA' && composerText(composer).length < promptText.length) {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
    setter.call(composer, promptText);
    composer.dispatchEvent(new Event('input', { bubbles: true }));
  }
  // Select only the composer. selectAll would select the whole page, and deleting that makes ChatGPT reload.
  if (composer.tagName !== 'TEXTAREA' && composerText(composer).length < Math.min(20, promptText.length)) {
    try {
      composer.focus();
      const range = document.createRange();
      range.selectNodeContents(composer);
      const sel = window.getSelection();
      if (sel) { sel.removeAllRanges(); sel.addRange(range); }
      const chunk = 6000;
      for (let i = 0; i < promptText.length; i += chunk) {
        if (!document.execCommand('insertText', false, promptText.slice(i, i + chunk))) break;
      }
    } catch (e) {}
  }
  const length = composerText(composer).length;
  return JSON.stringify({
    ok: length > 0,
    length: length,
    expected: promptText.length,
    canSend: sendEnabled(sendButton()),
    error: length > 0 ? '' : 'ChatGPT did not accept the prompt.'
  });
})(%PROMPT%)
"""
)

CHATGPT_CLICK_SEND_JS = (
    r"""
(() => {
"""
    + _COMMON
    + r"""
  const composer = composerEl();
  if (!composer) return JSON.stringify({ ok: false, error: 'Composer not found.' });
  composer.focus();
  const send = sendButton();
  if (!sendEnabled(send)) {
    return JSON.stringify({
      ok: false,
      error: 'Send is disabled. The prompt is in the box, but ChatGPT did not accept it as typing.'
    });
  }
  send.click();
  return JSON.stringify({ ok: true, method: 'click' });
})()
"""
)

CHATGPT_CLICK_COPY_LAST_JS = (
    r"""
(() => {
"""
    + _COMMON
    + r"""
  // The Copy button under the last reply ("Copy message" is the one under your own message). ChatGPT no longer puts
  // the text of a reply into the page, so this button is how the reply is read.
  const replyCopies = Array.from(document.querySelectorAll('main button[aria-label="Copy"]'));
  if (replyCopies.length) {
    replyCopies[replyCopies.length - 1].click();
    return JSON.stringify({ ok: true });
  }
  const els = assistantEls();
  const last = els.length ? els[els.length - 1] : null;
  const scope = (last && (last.closest('article') || last.parentElement)) || document;
  const btn = Array.from(scope.querySelectorAll('button')).reverse().find(b => {
    const label = (b.getAttribute('aria-label') || '').toLowerCase();
    const testid = (b.getAttribute('data-testid') || '').toLowerCase();
    return testid === 'copy-turn-action-button' || label.includes('copy');
  });
  if (!btn) return JSON.stringify({ ok: false, error: 'Copy button not found.' });
  btn.click();
  return JSON.stringify({ ok: true });
})()
"""
)

CHATGPT_CLICK_CONTINUE_JS = (
    r"""
(() => {
"""
    + _COMMON
    + r"""
  const button = continueButton();
  if (!button) return JSON.stringify({ ok: false, error: 'Continue button not found.' });
  button.click();
  return JSON.stringify({ ok: true });
})()
"""
)

# The last assistant message as plain text, for short follow-up answers (no JSON involved).
CHATGPT_LAST_REPLY_JS = (
    r"""
(() => {
"""
    + _COMMON
    + r"""
  // An element marked as a reply but with no text (a placeholder the page keeps around) is not a reply: when no
  // readable reply element exists, the page's own text is used and the caller is told so.
  const els = assistantEls().filter(e => textOf(e).length > 0);
  return JSON.stringify({
    blocked: blockedNow(),
    hasComposer: !!composerEl(),
    turns: els.length,
    fallback: !els.length,
    generating: isGenerating(),
    text: els.length ? textOf(els[els.length - 1]) : fallbackText().slice(-20000),
    // What a person sees in the conversation, whatever the page is built from. Fenced answers are read from this
    // when no reply element can be identified.
    pageText: ((document.querySelector('main, [role="main"]') || document.body).innerText || '').slice(-30000),
    // Where the last answer marker sits (tags and attribute names only), so the selectors above can be repaired.
    markerPath: (() => {
      try {
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node, last = null;
        while ((node = walker.nextNode())) if ((node.nodeValue || '').includes('ANSWER_START')) last = node;
        const out = [];
        for (let e = last && last.parentElement; e && e !== document.body && out.length < 12; e = e.parentElement) {
          const attrs = Array.from(e.attributes).filter(a => a.name !== 'class' && a.name !== 'style')
            .map(a => a.name + (/^(data-|role|aria-label)/.test(a.name) ? '=' + a.value.slice(0, 30) : '')).join(',');
          out.push(e.tagName.toLowerCase() + (attrs ? '[' + attrs + ']' : ''));
        }
        return out.join(' < ');
      } catch (e) { return ''; }
    })(),
    url: location.href
  });
})()
"""
)
