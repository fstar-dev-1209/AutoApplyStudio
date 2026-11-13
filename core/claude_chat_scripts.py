"""JavaScript helpers for Claude web chat automation (claude.ai)."""

from core.chat_script_common import JSON_HELPERS

# Shared extractor inlined in both scripts below.
_EXTRACT_REPLY = (
    JSON_HELPERS
    + r"""
  function conversationRoot() {
    return document.querySelector(
      '[data-testid="conversation-turn-list"], [data-testid="conversation"], main, [role="main"]'
    ) || document.body;
  }
  function isInSidebar(el) {
    return !!(el.closest('nav') || el.closest('[data-testid="sidebar"]') || el.closest('aside'));
  }
  function isUserTurn(el) {
    return !!(
      el.closest('[data-testid="user-message"]') ||
      el.querySelector('[data-testid="user-message"]') ||
      el.getAttribute('data-testid') === 'user-message' ||
      el.getAttribute('data-is-human') === 'true'
    );
  }
  function lastAssistantTurnText() {
    const root = conversationRoot();
    const turns = Array.from(root.querySelectorAll(
      '[data-testid*="conversation-turn"], [data-testid="assistant-message"]'
    ));
    for (let i = turns.length - 1; i >= 0; i--) {
      const el = turns[i];
      if (isInSidebar(el) || isUserTurn(el)) continue;
      const t = (el.innerText || el.textContent || '').trim();
      if (t.length > 80) return t;
    }
    return '';
  }
  function collectReplyCandidates() {
    const root = conversationRoot();
    const out = [];
    const seen = new Set();
    function pushText(t) {
      t = (t || '').trim();
      if (t.length < 80 || seen.has(t)) return;
      seen.add(t);
      out.push(t);
    }
    const fullTurn = lastAssistantTurnText();
    if (fullTurn) pushText(fullTurn);
    const assistantSelectors = [
      '[data-testid="assistant-message"]',
      '[data-testid*="assistant-message"]',
      '.font-claude-message',
      '[class*="font-claude"]'
    ];
    assistantSelectors.forEach(sel => {
      root.querySelectorAll(sel).forEach(el => {
        if (isInSidebar(el) || isUserTurn(el)) return;
        pushText(el.innerText || el.textContent || '');
      });
    });
    root.querySelectorAll(
      '[data-testid*="conversation-turn"], article, [data-is-streaming="true"]'
    ).forEach(el => {
      if (isInSidebar(el) || isUserTurn(el)) return;
      pushText(el.innerText || el.textContent || '');
    });
    root.querySelectorAll(
      'pre, code, textarea[readonly], [data-testid*="artifact"], [class*="artifact"]'
    ).forEach(el => {
      if (isInSidebar(el) || isUserTurn(el)) return;
      const t = (el.innerText || el.textContent || el.value || '').trim();
      if (t.length > 200 && t.includes('{')) pushText(t);
    });
    return out;
  }
  function pickBestReply(candidates) {
    let best = '';
    for (const t of candidates) {
      const looksJson = t.includes('{') && (
        t.includes('"company_name"') || t.includes('"skill_groups"') ||
        t.includes('"cover_letter"')
      );
      if (looksJson && t.length > best.length) best = t;
    }
    if (best) return best;
    if (candidates.length) return candidates[candidates.length - 1];
    return '';
  }
  function noticeText() {
    return Array.from(document.querySelectorAll(
      '[role="alert"], [role="status"], [data-testid*="limit"], [data-testid*="banner"]'
    )).map(n => (n.innerText || n.textContent || '').slice(0, 500)).join('\n');
  }
  function looksLimited(text) {
    return /usage limit|rate limit|message limit|too many requests|try again later|you.ve reached|you have reached|hit your limit|limit reached/i.test(text || '');
  }
  function continueButton() {
    return Array.from(document.querySelectorAll('button')).find(b => {
      if (b.disabled) return false;
      const label = ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')).toLowerCase().replace(/\s+/g, ' ').trim();
      return label.includes('continue generat') || label.includes('continue response') || label === 'continue';
    }) || null;
  }
  function blockedFrom(extra) {
    const title = document.title || '';
    if (title.toLowerCase().includes('just a moment') || document.querySelector(
        '#challenge-form, iframe[src*="challenges.cloudflare.com"], #cf-challenge-running')) return 'challenge';
    if (looksLimited(title + '\n' + noticeText())) return 'limit';
    const tail = (extra || '').trim();
    if (tail && tail.length < 500 && looksLimited(tail)) return 'limit';
    if (document.querySelector('input[type="email"], form[action*="login"], a[href*="login"]')) return 'login';
    return '';
  }
"""
)

CLAUDE_PAGE_STATE_JS = (
    r"""
(() => {
"""
    + _EXTRACT_REPLY
    + r"""
  const composer = document.querySelector(
    'div[contenteditable="true"][data-testid="chat-input"], ' +
    'div[contenteditable="true"][role="textbox"], ' +
    'div[contenteditable="true"].ProseMirror, ' +
    'div[contenteditable="true"]'
  );
  const buttons = Array.from(document.querySelectorAll('button'));
  const sendBtn = buttons.find(b => {
    const label = (b.getAttribute('aria-label') || '').toLowerCase();
    const testid = (b.getAttribute('data-testid') || '').toLowerCase();
    if (b.disabled) return false;
    return label.includes('send') || testid.includes('send');
  });
  const stopBtn = buttons.find(b => {
    const label = (b.getAttribute('aria-label') || '').toLowerCase();
    return label.includes('stop') && !b.disabled;
  });
  const streaming = document.querySelector(
    '[data-is-streaming="true"], [data-testid="streaming-indicator"]'
  );
  const candidates = collectReplyCandidates();
  const replyText = pickBestReply(candidates);
  const fullTurn = lastAssistantTurnText();
  const blocked = blockedFrom(fullTurn || replyText);
  const composerLen = composer
    ? (composer.innerText || composer.textContent || '').length
    : 0;
  return JSON.stringify({
    loggedIn: !!composer && !blocked,
    blocked: blocked,
    hasComposer: !!composer,
    hasContinue: !!continueButton(),
    isGenerating: !!(stopBtn || streaming),
    assistantCount: candidates.length,
    longestAssistant: candidates.reduce((m, t) => Math.max(m, t.length), 0),
    fullTurnText: clipText(fullTurn),
    lastAssistantText: clipText(replyText),
    replyText: clipText(fullTurn || replyText),
    canSend: !!composer && !!sendBtn,
    composerLength: composerLen,
    url: location.href
  });
})()
"""
)

# Resume JSON from assistant replies only (never from the pasted job description).
CLAUDE_EXTRACT_JSON_JS = (
    r"""
(() => {
"""
    + _EXTRACT_REPLY
    + r"""
  try {
    window.scrollTo(0, document.body.scrollHeight);
  } catch (e) {}
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
  const longestAssistant = candidates.reduce((m, t) => Math.max(m, t.length), 0);
  const plainBest = pickBestReply(candidates);
  return JSON.stringify({
    ok: best.length > 0,
    text: best,
    length: best.length,
    assistantBlocks: candidates.length,
    longestAssistant: longestAssistant,
    plainText: clipText(plainBest)
  });
})()
"""
)

CLAUDE_INSERT_PROMPT_JS = r"""
((promptText) => {
  const composer = document.querySelector(
    'div[contenteditable="true"][data-testid="chat-input"], ' +
    'div[contenteditable="true"][role="textbox"], ' +
    'div[contenteditable="true"].ProseMirror, ' +
    'div[contenteditable="true"]'
  );
  if (!composer) {
    return JSON.stringify({ ok: false, error: 'Composer not found. Sign in at claude.ai first.' });
  }
  composer.focus();
  try {
    document.execCommand('selectAll', false, null);
    document.execCommand('delete', false, null);
  } catch (e) {
    composer.textContent = '';
  }
  const chunk = 6000;
  try {
    for (let i = 0; i < promptText.length; i += chunk) {
      const part = promptText.slice(i, i + chunk);
      if (!document.execCommand('insertText', false, part)) {
        composer.textContent = (composer.textContent || '') + part;
      }
    }
  } catch (e) {
    composer.textContent = promptText;
  }
  composer.dispatchEvent(new InputEvent('input', { bubbles: true }));
  const len = (composer.innerText || composer.textContent || '').length;
  return JSON.stringify({ ok: true, length: len, expected: promptText.length });
})(%PROMPT%)
"""

CLAUDE_CLICK_COPY_LAST_JS = r"""
(() => {
  const root = document.querySelector(
    '[data-testid="conversation-turn-list"], [data-testid="conversation"], main'
  ) || document.body;
  const turns = Array.from(root.querySelectorAll(
    '[data-testid*="conversation-turn"], article, [data-testid="assistant-message"]'
  ));
  const searchRoots = turns.length ? turns.slice(-3) : [root];
  const buttons = [];
  searchRoots.forEach(el => {
    el.querySelectorAll('button').forEach(b => buttons.push(b));
  });
  if (!buttons.length) {
    document.querySelectorAll('button').forEach(b => buttons.push(b));
  }
  const copyBtn = buttons.reverse().find(b => {
    const label = (b.getAttribute('aria-label') || '').toLowerCase();
    const testid = (b.getAttribute('data-testid') || '').toLowerCase();
    return label.includes('copy') || testid.includes('copy');
  });
  if (!copyBtn) {
    return JSON.stringify({ ok: false, error: 'Copy button not found.' });
  }
  copyBtn.click();
  return JSON.stringify({ ok: true });
})()
"""

CLAUDE_CLICK_SEND_JS = r"""
(() => {
  const composer = document.querySelector(
    'div[contenteditable="true"][data-testid="chat-input"], ' +
    'div[contenteditable="true"][role="textbox"], ' +
    'div[contenteditable="true"].ProseMirror, ' +
    'div[contenteditable="true"]'
  );
  if (!composer) {
    return JSON.stringify({ ok: false, error: 'Composer not found.' });
  }
  composer.focus();
  const buttons = Array.from(document.querySelectorAll('button'));
  const sendBtn = buttons.find(b => {
    if (b.disabled) return false;
    const label = (b.getAttribute('aria-label') || '').toLowerCase();
    const testid = (b.getAttribute('data-testid') || '').toLowerCase();
    return label.includes('send message') || label === 'send' || testid.includes('send');
  });
  if (sendBtn) {
    sendBtn.click();
    return JSON.stringify({ ok: true, method: 'click' });
  }
  const enter = new KeyboardEvent('keydown', {
    key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true
  });
  composer.dispatchEvent(enter);
  const ctrlEnter = new KeyboardEvent('keydown', {
    key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true, ctrlKey: true
  });
  composer.dispatchEvent(ctrlEnter);
  return JSON.stringify({ ok: true, method: 'keyboard' });
})()
"""

CLAUDE_CLICK_CONTINUE_JS = r"""
(() => {
  function continueButton() {
    return Array.from(document.querySelectorAll('button')).find(b => {
      if (b.disabled) return false;
      const label = ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')).toLowerCase().replace(/\s+/g, ' ').trim();
      return label.includes('continue generat') || label.includes('continue response') || label === 'continue';
    }) || null;
  }
  const button = continueButton();
  if (!button) return JSON.stringify({ ok: false, error: 'Continue button not found.' });
  button.click();
  return JSON.stringify({ ok: true });
})()
"""

# The last assistant message as plain text, for short follow-up answers (no JSON involved).
CLAUDE_LAST_REPLY_JS = r"""
(() => {
  const root = document.querySelector(
    '[data-testid="conversation-turn-list"], [data-testid="conversation"], main, [role="main"]'
  ) || document.body;
  const selectors = [
    '[data-testid="assistant-message"]', '[data-testid*="assistant-message"]',
    '.font-claude-message', '[class*="font-claude-message"]'
  ];
  let els = [];
  selectors.forEach(s => root.querySelectorAll(s).forEach(e => { if (!els.includes(e)) els.push(e); }));
  els = els.filter(e => !(e.closest('nav') || e.closest('aside') || e.closest('[data-testid="sidebar"]') ||
                          e.closest('[data-testid="user-message"]')));
  els = els.filter(e => !els.some(o => o !== e && o.contains(e)));
  els.sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);
  const buttons = Array.from(document.querySelectorAll('button'));
  const stop = buttons.find(b => ((b.getAttribute('aria-label') || '').toLowerCase().includes('stop')) && !b.disabled);
  const streaming = document.querySelector('[data-is-streaming="true"], [data-testid="streaming-indicator"]');
  const composer = document.querySelector('div[contenteditable="true"]');
  const last = els.length ? els[els.length - 1] : null;
  const lastText = last ? (last.innerText || last.textContent || '').trim() : '';
  function noticeText() {
    return Array.from(document.querySelectorAll(
      '[role="alert"], [role="status"], [data-testid*="limit"], [data-testid*="banner"]'
    )).map(n => (n.innerText || n.textContent || '').slice(0, 500)).join('\n');
  }
  function looksLimited(text) {
    return /usage limit|rate limit|message limit|too many requests|try again later|you.ve reached|you have reached|hit your limit|limit reached/i.test(text || '');
  }
  const title = document.title || '';
  let blocked = '';
  if (title.toLowerCase().includes('just a moment') || document.querySelector(
      '#challenge-form, iframe[src*="challenges.cloudflare.com"], #cf-challenge-running')) blocked = 'challenge';
  else if (looksLimited(title + '\n' + noticeText()) || (lastText && lastText.length < 500 && looksLimited(lastText))) blocked = 'limit';
  else if (document.querySelector('input[type="email"], form[action*="login"], a[href*="login"]')) blocked = 'login';
  return JSON.stringify({
    blocked: blocked,
    hasComposer: !!composer,
    turns: els.length,
    generating: !!(stop || streaming),
    text: last ? (last.innerText || last.textContent || '').trim() : '',
    url: location.href
  });
})()
"""
