import {
  buildControllerAppUrl,
  getStoredOrigin,
  isValidSheetId,
  MSG_TYPES,
  OAUTH_TOKEN_KEY,
  ORIGIN_KEY,
  PENDING_PROMPT_KEY,
} from './constants.js';
import { createIframeBridge } from './bridge.js';

function $(id) {
  return document.getElementById(id);
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function escapeAttr(s) {
  return escapeHtml(s).replace(/'/g, '&#39;');
}

function shortenSheetId(id) {
  const s = String(id);
  if (s.length <= 14) return s;
  return `${s.slice(0, 6)}…${s.slice(-6)}`;
}

function renderContext(hintEl, dot, detectedSheetId) {
  const hasSheet = detectedSheetId && isValidSheetId(detectedSheetId);

  if (hasSheet) {
    dot.classList.remove('off');
    dot.classList.add('on');
    hintEl.innerHTML = `Sheet · <strong title="${escapeAttr(detectedSheetId)}">${escapeHtml(
      shortenSheetId(detectedSheetId)
    )}</strong>`;
    hintEl.title =
      'Sheet ID is pre-filled below. Sign in with your API token in the frame.';
  } else {
    dot.classList.remove('on');
    dot.classList.add('off');
    hintEl.textContent = 'Open a Smartsheet tab to detect sheet ID.';
    hintEl.title =
      'Browse to app.smartsheet.com/sheets/… or enter the ID manually in the app.';
  }
}

async function pushOAuthToken(bridge) {
  const { [OAUTH_TOKEN_KEY]: token } = await chrome.storage.local.get(OAUTH_TOKEN_KEY);
  if (token && String(token).trim()) {
    bridge.post(MSG_TYPES.APPLY_TOKEN, { token: String(token).trim() });
  }
}

async function pushPendingPrompt(bridge) {
  const { [PENDING_PROMPT_KEY]: text } = await chrome.storage.session.get(PENDING_PROMPT_KEY);
  if (text && String(text).trim()) {
    bridge.post(MSG_TYPES.INSERT_PROMPT, { text: String(text).trim() });
    await chrome.storage.session.remove(PENDING_PROMPT_KEY);
  }
}

async function onFrameReady(bridge) {
  await pushOAuthToken(bridge);
  await pushPendingPrompt(bridge);
}

async function main() {
  const hintEl = $('hint');
  const dot = $('status-dot');
  const btnOpts = $('btn-opts');
  const btnReload = $('btn-reload');
  const frame = $('app');

  let origin = await getStoredOrigin();
  let bridge = createIframeBridge(frame, origin);

  const session = await chrome.storage.session.get(['detectedSheetId']);
  let detectedSheetId = session.detectedSheetId || null;

  function reloadPanel() {
    frame.src = buildControllerAppUrl(origin, detectedSheetId);
    renderContext(hintEl, dot, detectedSheetId);
  }

  bridge.onReady = () => {
    onFrameReady(bridge);
  };

  reloadPanel();

  btnOpts.addEventListener('click', () => chrome.runtime.openOptionsPage());
  btnReload.addEventListener('click', () => reloadPanel());

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'sync' && changes[ORIGIN_KEY]) {
      origin = changes[ORIGIN_KEY].newValue
        ? String(changes[ORIGIN_KEY].newValue).replace(/\/$/, '')
        : origin;
      bridge.destroy();
      bridge = createIframeBridge(frame, origin);
      bridge.onReady = () => onFrameReady(bridge);
      reloadPanel();
      return;
    }

    if (area === 'session' && changes.detectedSheetId) {
      detectedSheetId = changes.detectedSheetId.newValue ?? null;
      reloadPanel();
      return;
    }

    if (area === 'session' && changes[PENDING_PROMPT_KEY]?.newValue) {
      bridge.post(MSG_TYPES.INSERT_PROMPT, {
        text: String(changes[PENDING_PROMPT_KEY].newValue).trim(),
      });
      chrome.storage.session.remove(PENDING_PROMPT_KEY);
    }

    if (area === 'local' && changes[OAUTH_TOKEN_KEY]?.newValue) {
      bridge.post(MSG_TYPES.APPLY_TOKEN, {
        token: String(changes[OAUTH_TOKEN_KEY].newValue).trim(),
      });
    }
  });

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type === 'reload_panel') {
      reloadPanel();
      sendResponse({ ok: true });
      return true;
    }
    if (msg?.type === 'send_prompt_to_panel' && msg.text) {
      bridge.post(MSG_TYPES.INSERT_PROMPT, { text: String(msg.text).trim() });
      sendResponse({ ok: true });
      return true;
    }
    if (msg?.type === 'oauth_token_updated') {
      pushOAuthToken(bridge);
      sendResponse({ ok: true });
      return true;
    }
    return false;
  });
}

main().catch((err) => {
  const h = $('hint');
  const dot = $('status-dot');
  if (dot) {
    dot.classList.remove('on');
    dot.classList.add('off');
  }
  if (h) {
    h.textContent =
      err && err.message ? `Error: ${err.message}` : 'Failed to load.';
    h.title = '';
  }
});
