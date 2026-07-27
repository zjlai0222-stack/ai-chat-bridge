/* AI Chat Bridge — background service worker
 *
 * Connects to a local WebSocket server (default ws://127.0.0.1:8765/ws),
 * receives JSON commands, drives ChatGPT / Gemini / Qwen tabs via the
 * Chrome DevTools Protocol (chrome.debugger), and returns JSON results.
 *
 * Protocol (one message per line of thought):
 *   server -> extension : {"id":"...","action":"ask","site":"chatgpt","prompt":"...","timeoutSec":240}
 *   extension -> server : {"id":"...","ok":true,"result":{...}}  or  {"id":"...","ok":false,"error":"..."}
 *
 * Supported actions: status | ask | read | new_chat | navigate | open | ping
 */

"use strict";

// ---------------------------------------------------------------------------
// Site adapters. Selectors drift as the vendors ship UI updates — tweak here.
// `composer` / `sendBtn` / `stopBtn` / `newChat` are comma-separated fallback
// lists; the first visible match wins.
// ---------------------------------------------------------------------------
const SITES = {
  chatgpt: {
    label: "ChatGPT",
    urlPatterns: ["*://chatgpt.com/*", "*://chat.openai.com/*"],
    home: "https://chatgpt.com/",
    composer: "#prompt-textarea, div.ProseMirror[contenteditable='true'], textarea[data-id='root'], textarea",
    sendBtn: "button[data-testid='send-button'], button[aria-label*='Send' i], button[aria-label*='傳送' i], button[aria-label*='发送' i]",
    stopBtn: "button[data-testid='stop-button'], button[aria-label*='Stop' i], button[aria-label*='停止' i]",
    newChat: "a[href='/'], button[aria-label*='New chat' i]",
    messages: ["[data-message-author-role='assistant']", "article"],
    generatingHints: "button[data-testid='stop-button'], [data-testid='thinking-indicator'], .result-streaming",
  },
  gemini: {
    label: "Gemini",
    urlPatterns: ["*://gemini.google.com/*"],
    home: "https://gemini.google.com/app",
    composer: "div.ql-editor[contenteditable='true'], rich-textarea div[contenteditable='true'], div[contenteditable='true'][role='textbox'], div[contenteditable='true']",
    sendBtn: "button.send-button, button[aria-label*='Send' i], button[aria-label*='傳送' i], button[aria-label*='提交' i]",
    stopBtn: "button[aria-label*='Stop' i], button[aria-label*='停止' i]",
    newChat: "a[href='/app'], button[aria-label*='New chat' i]",
    messages: ["message-content", ".response-container", ".markdown.markdown-main-panel"],
    generatingHints: "",
    modelSwitch: "button.input-area-switch",
  },
  qwen: {
    label: "Qwen",
    urlPatterns: ["*://chat.qwen.ai/*", "*://www.qianwen.com/*", "*://qianwen.com/*", "*://www.tongyi.com/*"],
    home: "https://chat.qwen.ai/",
    composer: "textarea.chat-input, textarea[placeholder], div[contenteditable='true']",
    sendBtn: "button[class*='send'], button[aria-label*='发送' i], button[aria-label*='傳送' i], button[aria-label*='Send' i]",
    stopBtn: "button[class*='stop'], button[aria-label*='停止' i]",
    newChat: "a[href='/'], button[class*='new-chat'], button[aria-label*='新对话' i]",
    messages: [".answer-item", ".markdown-body", ".message-content"],
    generatingHints: "button[class*='stop'], .loading, .cursor-blink",
  },
};

const DEFAULT_WS_URL = "ws://127.0.0.1:8765/ws";

// ---------------------------------------------------------------------------
// WebSocket client with reconnect + keepalive (alarms reset the MV3 idle timer)
// ---------------------------------------------------------------------------
let ws = null;
let wsState = "disconnected"; // disconnected | connecting | connected
let reconnectTimer = null;
let reconnectDelay = 1000;

async function getServerUrl() {
  const data = await chrome.storage.local.get(["serverUrl"]);
  return data.serverUrl || DEFAULT_WS_URL;
}

async function setWsState(state) {
  wsState = state;
  await chrome.storage.local.set({ wsState: state, wsStateAt: Date.now() });
  try {
    chrome.action.setBadgeText({ text: state === "connected" ? "ON" : "" });
    chrome.action.setBadgeBackgroundColor({ color: "#0a7f3f" });
  } catch (_) {}
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, 15000);
}

async function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  const url = await getServerUrl();
  setWsState("connecting");
  try {
    ws = new WebSocket(url);
  } catch (e) {
    setWsState("disconnected");
    scheduleReconnect();
    return;
  }
  ws.onopen = () => {
    reconnectDelay = 1000;
    setWsState("connected");
    ws.send(JSON.stringify({ type: "hello", client: "ai-chat-bridge-extension", version: "1.0.0" }));
  };
  ws.onmessage = (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    handleCommand(msg);
  };
  ws.onclose = () => {
    ws = null;
    setWsState("disconnected");
    scheduleReconnect();
  };
  ws.onerror = () => {
    try { ws.close(); } catch (_) {}
  };
}

chrome.alarms.create("keepalive", { periodInMinutes: 0.3 }); // ~18s
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== "keepalive") return;
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify({ type: "ping", at: Date.now() })); } catch (_) {}
  } else {
    connect();
  }
});
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();

// ---------------------------------------------------------------------------
// Debugger helpers (CDP), modelled after the reference WebBridge extension
// ---------------------------------------------------------------------------
const attachedTabs = new Set();
chrome.tabs.onRemoved.addListener((tabId) => attachedTabs.delete(tabId));
chrome.debugger.onDetach.addListener((source) => {
  if (source.tabId) attachedTabs.delete(source.tabId);
});

async function attach(tabId) {
  if (attachedTabs.has(tabId)) return;
  try { await chrome.debugger.detach({ tabId }); } catch (_) {}
  await chrome.debugger.attach({ tabId }, "1.3");
  attachedTabs.add(tabId);
}

async function detach(tabId) {
  if (!attachedTabs.has(tabId)) return;
  try { await chrome.debugger.detach({ tabId }); } catch (_) {}
  attachedTabs.delete(tabId);
}

async function evalInTab(tabId, expression, awaitPromise = false) {
  await attach(tabId);
  const res = await chrome.debugger.sendCommand({ tabId }, "Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise,
  });
  if (res.exceptionDetails) {
    throw new Error("page eval failed: " + (res.exceptionDetails.exception?.description || res.exceptionDetails.text));
  }
  return res.result ? res.result.value : undefined;
}

// ---------------------------------------------------------------------------
// Tab management
// ---------------------------------------------------------------------------
async function findSiteTab(site) {
  for (const pattern of site.urlPatterns) {
    const tabs = await chrome.tabs.query({ url: pattern });
    if (tabs.length) return tabs[0];
  }
  return null;
}

async function ensureSiteTab(siteKey, activate = false) {
  const site = SITES[siteKey];
  if (!site) throw new Error(`unknown site "${siteKey}". Known: ${Object.keys(SITES).join(", ")}`);
  let tab = await findSiteTab(site);
  if (!tab) {
    tab = await chrome.tabs.create({ url: site.home, active: activate });
    await waitForTabLoaded(tab.id, 30000);
    tab = await chrome.tabs.get(tab.id);
  } else if (activate) {
    await chrome.tabs.update(tab.id, { active: true });
  }
  return tab;
}

async function waitForTabLoaded(tabId, timeoutMs) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (tab.status === "complete") return;
    } catch (_) {
      throw new Error("tab closed while loading");
    }
    await sleep(250);
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------------------
// Page-side script: one self-contained function, cfg injected per site.
// Actions: status | count | send | poll | read | new_chat
// ---------------------------------------------------------------------------
function pageScript(cfgJson, argsJson) {
  return `(async () => {
  const cfg = ${cfgJson};
  const args = ${argsJson};
  const toList = (x) => Array.isArray(x) ? x : String(x).split(',').map((s) => s.trim()).filter(Boolean);
  const firstVisible = (list) => {
    for (const sel of toList(list)) {
      let els;
      try { els = document.querySelectorAll(sel); } catch (e) { continue; }
      for (const el of els) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) return el;
      }
    }
    return null;
  };
  const allVisible = (list) => {
    const out = [];
    for (const sel of toList(list)) {
      let els;
      try { els = document.querySelectorAll(sel); } catch (e) { continue; }
      for (const el of els) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) out.push(el);
      }
    }
    return out;
  };
  const composer = () => {
    for (const sel of toList(cfg.composer)) {
      let els;
      try { els = document.querySelectorAll(sel); } catch (e) { continue; }
      for (const el of els) {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        if (el.isContentEditable || el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') return el;
      }
    }
    return null;
  };
  const insertText = (el, text) => {
    el.focus();
    if (el.isContentEditable) {
      const sel = window.getSelection();
      if (sel) {
        const range = document.createRange();
        range.selectNodeContents(el);
        sel.removeAllRanges();
        sel.addRange(range);
      }
      let inserted = false;
      try { inserted = document.execCommand('insertText', false, text); } catch (e) { inserted = false; }
      if (!inserted) {
        el.textContent = text;
        el.dispatchEvent(new InputEvent('input', { inputType: 'insertText', data: text, bubbles: true }));
      }
    } else {
      const proto = el instanceof window.HTMLTextAreaElement ? window.HTMLTextAreaElement.prototype
        : el instanceof window.HTMLInputElement ? window.HTMLInputElement.prototype : null;
      const setter = proto ? Object.getOwnPropertyDescriptor(proto, 'value')?.set : null;
      if (setter) setter.call(el, text); else el.value = text;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }
  };
  // Some sites render custom elements (e.g. Gemini's <message-content>) whose
  // OWN box is 0x0 while their children render the content. Treat such a
  // container as shown when any descendant has a real box; exclude
  // display:none / visibility:hidden outright.
  const isShown = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) return true;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    if (!(el.innerText || '').trim()) return false; // hidden template placeholder
    const kids = el.querySelectorAll('*');
    for (const k of kids) {
      const kr = k.getBoundingClientRect();
      if (kr.width > 0 && kr.height > 0) return true;
    }
    return false;
  };
  // Use the FIRST selector that yields visible matches, so message order stays
  // true DOM order (concatenating matches across selectors scrambles order and
  // can return a stale message as "latest").
  const messageEls = () => {
    for (const sel of toList(cfg.messages)) {
      let els;
      try { els = document.querySelectorAll(sel); } catch (e) { continue; }
      const vis = [];
      for (const el of els) {
        if (isShown(el)) vis.push(el);
      }
      if (vis.length) return vis;
    }
    return [];
  };
  const lastText = () => {
    const els = messageEls();
    if (!els.length) return '';
    let t = (els[els.length - 1].innerText || '').trim();
    // Strip screen-reader-only prefixes (e.g. Gemini's "Gemini 說了" a11y label),
    // which otherwise look like a complete (tiny) reply during early streaming.
    t = t.replace(/^(Gemini 說了|你說了|Gemini said|You said)\s*/i, '').trim();
    return t;
  };
  const isGenerating = () => {
    if (firstVisible(cfg.stopBtn)) return true;
    for (const sel of toList(cfg.generatingHints)) {
      try { if (firstVisible([sel])) return true; } catch (e) {}
    }
    return false;
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  if (args.action === 'status') {
    const els = messageEls();
    return {
      url: location.href,
      title: document.title,
      composerFound: !!composer(),
      messageCount: els.length,
      generating: isGenerating(),
    };
  }

  if (args.action === 'count') {
    return { count: messageEls().length, generating: isGenerating() };
  }

  if (args.action === 'read') {
    return { text: lastText(), count: messageEls().length, generating: isGenerating() };
  }

  if (args.action === 'send') {
    const el = composer();
    if (!el) return { sent: false, error: 'composer not found (not logged in? page not loaded?)' };
    insertText(el, args.prompt);
    // The send button may only render AFTER text is inserted (Angular re-render);
    // poll up to 4s for an enabled button instead of giving up immediately.
    const btnDeadline = Date.now() + 4000;
    while (Date.now() < btnDeadline) {
      await sleep(300);
      const btn = firstVisible(cfg.sendBtn);
      if (btn && !btn.disabled) {
        btn.click();
        await sleep(300);
        return { sent: true, via: 'button' };
      }
    }
    el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
    el.dispatchEvent(new KeyboardEvent('keypress', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
    el.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
    await sleep(300);
    return { sent: true, via: 'enter-fallback' };
  }

  if (args.action === 'set_model') {
    const want = String(args.modelLabel || '').toLowerCase();
    if (!want) return { ok: false, error: 'modelLabel is required' };
    // The model switch renders late on a fresh page (Angular boot) — wait for it.
    let sw = null;
    const swDeadline = Date.now() + 8000;
    while (Date.now() < swDeadline) {
      sw = firstVisible(cfg.modelSwitch || 'button.input-area-switch');
      if (sw) break;
      await sleep(500);
    }
    if (!sw) return { ok: false, error: 'model switch not found on this site' };
    sw.click();
    // Menu items render asynchronously — poll until they appear.
    let items = [];
    const menuDeadline = Date.now() + 4000;
    while (Date.now() < menuDeadline) {
      await sleep(400);
      items = [...document.querySelectorAll('gem-menu-item[role=menuitem], [role=menuitem]')].filter((it) => {
        const r = it.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      });
      if (items.length) break;
    }
    const labels = items.map((it) => (((it.querySelector('.label') || it).textContent) || '').trim());
    for (let i = 0; i < items.length; i++) {
      if (labels[i].toLowerCase().includes(want)) {
        items[i].click();
        await sleep(400);
        return { ok: true, selected: labels[i] };
      }
    }
    // Close the menu so we don't leave the UI in a weird state.
    try { document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); } catch (e) {}
    return { ok: false, error: 'model option not found: ' + args.modelLabel, available: labels };
  }

  if (args.action === 'poll') {
    const els = messageEls();
    return { count: els.length, text: lastText(), generating: isGenerating() };
  }

  if (args.action === 'new_chat') {
    const btn = firstVisible(cfg.newChat);
    if (btn) { btn.click(); await sleep(300); return { ok: true, via: 'button' }; }
    return { ok: false, error: 'new-chat control not found' };
  }

  return { error: 'unknown page action: ' + args.action };
})()`;
}

async function runPageAction(tabId, siteKey, action, extra = {}, overrides = null) {
  // Per-call selector overrides let the server fix drifting site DOMs without
  // reloading the extension: {"messages": [...], "composer": "...", ...}
  const cfg = overrides && typeof overrides === "object"
    ? { ...SITES[siteKey], ...overrides }
    : SITES[siteKey];
  const expr = pageScript(JSON.stringify(cfg), JSON.stringify({ action, ...extra }));
  return await evalInTab(tabId, expr, true);
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------
async function cmdStatus(siteKey, overrides) {
  const site = SITES[siteKey];
  const tab = await findSiteTab(site);
  if (!tab) return { site: siteKey, tabOpen: false };
  try {
    const page = await runPageAction(tab.id, siteKey, "status", {}, overrides);
    return { site: siteKey, tabOpen: true, tabId: tab.id, ...page };
  } finally {
    await detach(tab.id);
  }
}

async function cmdRead(siteKey, overrides) {
  const tab = await ensureSiteTab(siteKey);
  try {
    const page = await runPageAction(tab.id, siteKey, "read", {}, overrides);
    return { site: siteKey, tabId: tab.id, ...page };
  } finally {
    await detach(tab.id);
  }
}

async function cmdEval(siteKey, expression) {
  if (!expression) throw new Error("eval: expression is required");
  const tab = await ensureSiteTab(siteKey);
  try {
    const value = await evalInTab(tab.id, expression, true);
    return { site: siteKey, tabId: tab.id, url: tab.url, value };
  } finally {
    await detach(tab.id);
  }
}

async function cmdNewChat(siteKey) {
  const tab = await ensureSiteTab(siteKey, true);
  try {
    const page = await runPageAction(tab.id, siteKey, "new_chat");
    if (!page || page.ok !== true) {
      // Fallback: navigate to the site home (usually starts a fresh chat).
      await chrome.debugger.sendCommand({ tabId: tab.id }, "Page.navigate", { url: SITES[siteKey].home });
      await waitForTabLoaded(tab.id, 20000);
      return { site: siteKey, ok: true, via: "navigate" };
    }
    return { site: siteKey, ...page };
  } finally {
    await detach(tab.id);
  }
}

async function cmdNavigate(url) {
  if (!url) throw new Error("navigate: url is required");
  const tab = await chrome.tabs.create({ url, active: true });
  await waitForTabLoaded(tab.id, 30000);
  return { tabId: tab.id, url };
}

async function cmdOpen(siteKey) {
  const tab = await ensureSiteTab(siteKey, true);
  return { site: siteKey, tabId: tab.id, url: tab.url };
}

async function cmdRestart(siteKey) {
  const site = SITES[siteKey];
  if (!site) throw new Error(`unknown site "${siteKey}". Known: ${Object.keys(SITES).join(", ")}`);
  // Close every existing tab of this site, then open a fresh one at home.
  let closed = 0;
  for (const pattern of site.urlPatterns) {
    const tabs = await chrome.tabs.query({ url: pattern });
    for (const t of tabs) {
      try { await chrome.tabs.remove(t.id); closed += 1; } catch (_) {}
    }
  }
  const tab = await chrome.tabs.create({ url: site.home, active: true });
  await waitForTabLoaded(tab.id, 30000);
  return { site: siteKey, closedTabs: closed, tabId: tab.id, url: tab.url };
}

async function cmdAsk(siteKey, prompt, timeoutSec, overrides, modelLabel) {
  if (!prompt || !String(prompt).trim()) throw new Error("ask: prompt is required");
  const timeoutMs = Math.max(10, timeoutSec || 240) * 1000;
  const tab = await ensureSiteTab(siteKey);

  let modelInfo = null;
  if (modelLabel) {
    try {
      modelInfo = await runPageAction(tab.id, siteKey, "set_model", { modelLabel }, overrides);
    } catch (e) {
      modelInfo = { ok: false, error: String(e && e.message || e) };
    }
  }

  let baseline = 0;
  try {
    const before = await runPageAction(tab.id, siteKey, "count", {}, overrides);
    baseline = before && typeof before.count === "number" ? before.count : 0;
  } catch (_) {}

  const sent = await runPageAction(tab.id, siteKey, "send", { prompt: String(prompt) }, overrides);
  if (!sent || sent.sent !== true) {
    await detach(tab.id);
    throw new Error("ask: failed to send prompt — " + ((sent && sent.error) || "unknown error"));
  }

  const deadline = Date.now() + timeoutMs;
  const sendAt = Date.now();
  let lastText = "";
  let stableRounds = 0;
  let sawNewMessage = false;

  while (Date.now() < deadline) {
    await sleep(800);
    let state;
    try {
      state = await runPageAction(tab.id, siteKey, "poll", {}, overrides);
    } catch (e) {
      continue; // page may be navigating; keep polling
    }
    if (!state) continue;
    const count = typeof state.count === "number" ? state.count : 0;
    if (count > baseline) sawNewMessage = true;
    const text = state.text || "";
    const gen = !!state.generating;
    // Fail fast only when NOTHING is generating: if the stop button / loading
    // animation is visible, the model is still thinking — keep waiting.
    if (!sawNewMessage && !gen && Date.now() - sendAt > 20000) {
      await detach(tab.id);
      return {
        site: siteKey,
        tabId: tab.id,
        promptChars: String(prompt).length,
        replyChars: 0,
        text: "",
        completed: false,
        model: modelInfo,
        warning: `no reply and no generating indicator within 20s (send via ${sent.via || "?"}) — prompt may not have been submitted`,
      };
    }
    // Completion = new message exists AND generation indicator gone AND text
    // stable for 3 polls (~2.4s). While the indicator is present we never
    // complete (covers thinking pauses with stable intermediate text).
    if (sawNewMessage && !gen && text && text === lastText) {
      stableRounds += 1;
      if (stableRounds >= 3) {
        await detach(tab.id);
        return {
          site: siteKey,
          tabId: tab.id,
          promptChars: String(prompt).length,
          replyChars: text.length,
          text,
          completed: true,
          model: modelInfo,
        };
      }
    } else {
      stableRounds = 0;
      lastText = text;
    }
  }

  // Timeout: return whatever we have.
  await detach(tab.id);
  return {
    site: siteKey,
    tabId: tab.id,
    promptChars: String(prompt).length,
    replyChars: lastText.length,
    text: lastText,
    completed: false,
    model: modelInfo,
    warning: "timeout waiting for generation to finish; returning partial/last text",
  };
}

// ---------------------------------------------------------------------------
// Granular actions for server-side ask orchestration (resumable across
// service-worker restarts — each command is short-lived).
// ---------------------------------------------------------------------------
async function cmdCount(siteKey, overrides) {
  const tab = await ensureSiteTab(siteKey);
  try {
    return await runPageAction(tab.id, siteKey, "count", {}, overrides);
  } finally {
    await detach(tab.id);
  }
}

async function cmdSendPrompt(siteKey, prompt, overrides) {
  if (!prompt || !String(prompt).trim()) throw new Error("send_prompt: prompt is required");
  const tab = await ensureSiteTab(siteKey);
  try {
    return await runPageAction(tab.id, siteKey, "send", { prompt: String(prompt) }, overrides);
  } finally {
    await detach(tab.id);
  }
}

async function cmdPollState(siteKey, overrides) {
  const tab = await ensureSiteTab(siteKey);
  try {
    return await runPageAction(tab.id, siteKey, "poll", {}, overrides);
  } finally {
    await detach(tab.id);
  }
}

async function cmdSetModel(siteKey, modelLabel, overrides) {
  const tab = await ensureSiteTab(siteKey);
  try {
    return await runPageAction(tab.id, siteKey, "set_model", { modelLabel }, overrides);
  } finally {
    await detach(tab.id);
  }
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------
async function handleCommand(msg) {
  const reply = (ok, payload) => {
    if (!ws || ws.readyState !== WebSocket.OPEN || !msg.id) return;
    const out = ok ? { id: msg.id, ok: true, result: payload } : { id: msg.id, ok: false, error: String(payload) };
    try { ws.send(JSON.stringify(out)); } catch (_) {}
  };
  try {
    switch (msg.action) {
      case "ping":
        return reply(true, { pong: Date.now(), wsState });
      case "status":
        return reply(true, await cmdStatus(msg.site, msg.selectors));
      case "ask":
        if (msg.fresh) { try { await cmdRestart(msg.site); } catch (_) {} }
        return reply(true, await cmdAsk(msg.site, msg.prompt, msg.timeoutSec, msg.selectors, msg.model));
      case "read":
        return reply(true, await cmdRead(msg.site, msg.selectors));
      case "eval":
        return reply(true, await cmdEval(msg.site, msg.expression));
      case "new_chat":
        return reply(true, await cmdNewChat(msg.site));
      case "open":
        return reply(true, await cmdOpen(msg.site));
      case "restart":
        return reply(true, await cmdRestart(msg.site));
      case "count":
        return reply(true, await cmdCount(msg.site, msg.selectors));
      case "send_prompt":
        return reply(true, await cmdSendPrompt(msg.site, msg.prompt, msg.selectors));
      case "poll_state":
        return reply(true, await cmdPollState(msg.site, msg.selectors));
      case "set_model":
        return reply(true, await cmdSetModel(msg.site, msg.model, msg.selectors));
      case "navigate":
        return reply(true, await cmdNavigate(msg.url));
      default:
        return reply(false, `unknown action "${msg.action}"`);
    }
  } catch (e) {
    return reply(false, e && e.message ? e.message : String(e));
  }
}

// ---------------------------------------------------------------------------
// Popup messaging
// ---------------------------------------------------------------------------
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg && msg.type === "popup:getState") {
      const data = await chrome.storage.local.get(["serverUrl"]);
      sendResponse({ wsState, serverUrl: data.serverUrl || DEFAULT_WS_URL });
    } else if (msg && msg.type === "popup:connect") {
      connect();
      sendResponse({ ok: true });
    } else if (msg && msg.type === "popup:disconnect") {
      if (ws) { try { ws.close(); } catch (_) {} }
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      sendResponse({ ok: true });
    } else if (msg && msg.type === "popup:setServerUrl") {
      await chrome.storage.local.set({ serverUrl: msg.url });
      sendResponse({ ok: true });
    } else if (msg && msg.type === "popup:openSite") {
      try {
        const tab = await ensureSiteTab(msg.site, true);
        sendResponse({ ok: true, tabId: tab.id });
      } catch (e) {
        sendResponse({ ok: false, error: String(e && e.message || e) });
      }
    }
  })();
  return true; // async sendResponse
});
