let ws = null;
let sessionId = null;
let wsToken = null;
let authCookie = null;
let allSheets = [];
let currentSheetId = null;
let currentConversationId = null;
let conversationMessages = [];
let isAgentRunning = false;
let availableProviders = {};
let currentUser = null;
let validatedToken = null;
let validatedSheets = [];

// ═══════ Toast ═══════
function showToast(msg, duration = 2000) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('visible');
  setTimeout(() => t.classList.remove('visible'), duration);
}

function sessionAuthHeaders(extra = {}) {
  const h = { ...extra };
  if (wsToken) h['X-WS-Token'] = wsToken;
  if (authCookie) h['X-Auth-Cookie'] = authCookie;
  return h;
}

function sessionAuthQuery(params = {}) {
  const q = new URLSearchParams(params);
  if (sessionId) q.set('session_id', sessionId);
  if (wsToken) q.set('ws_token', wsToken);
  if (authCookie) q.set('auth_cookie', authCookie);
  return q.toString();
}

function renderMarkdown(text) {
  const src = text || '';
  if (typeof marked === 'undefined') return escapeHtml(src);
  const raw = marked.parse(src);
  if (typeof DOMPurify !== 'undefined') return DOMPurify.sanitize(raw);
  return raw;
}

// ═══════ Auto-Reconnect ═══════
function saveSessionInfo(data) {
  try {
    localStorage.setItem('ss_ctrl_session', JSON.stringify({
      session_id: data.session_id,
      sheet: data.sheet,
      user: data.user,
      savedAt: Date.now(),
    }));
  } catch {}
}

function clearSessionInfo() {
  localStorage.removeItem('ss_ctrl_session');
}

// Remembered-token storage (opt-in only; localStorage — client-side risk)
const REMEMBER_KEY = 'ss_ctrl_remembered_token';
function saveRememberedToken(t) {
  try { localStorage.setItem(REMEMBER_KEY, t); } catch {}
}
function getRememberedToken() {
  try { return localStorage.getItem(REMEMBER_KEY) || ''; } catch { return ''; }
}
function clearRememberedToken() {
  try { localStorage.removeItem(REMEMBER_KEY); } catch {}
}

let wsRetryCount = 0;
const WS_MAX_RETRIES = 3;

function connectWebSocket() {
  const wsProtocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const tokenParam = wsToken ? ('?token=' + encodeURIComponent(wsToken)) : '';
  ws = new WebSocket(wsProtocol + '//' + location.host + '/ws/' + sessionId + tokenParam);

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    handleEvent(msg);
  };

  ws.onopen = () => {
    wsRetryCount = 0;
    document.getElementById('header-status').style.background = 'var(--success)';
    hideReconnectBanner();
    restoreWatchStateForSheet();
  };

  ws.onclose = () => {
    document.getElementById('header-status').style.background = 'var(--error)';
    if (wsRetryCount < WS_MAX_RETRIES && sessionId) {
      wsRetryCount++;
      const delay = Math.min(1000 * Math.pow(2, wsRetryCount), 8000);
      showToast('Connection lost. Reconnecting in ' + (delay/1000) + 's...');
      setTimeout(() => {
        if (sessionId) connectWebSocket();
      }, delay);
    } else {
      showReconnectBanner();
    }
  };
}

function showReconnectBanner() {
  let banner = document.getElementById('reconnect-banner');
  if (banner) { banner.classList.add('visible'); return; }
  banner = document.createElement('div');
  banner.id = 'reconnect-banner';
  banner.className = 'reconnect-banner visible';
  banner.setAttribute('role', 'alert');
  banner.innerHTML =
    '<span class="reconnect-icon">\u26A0</span>' +
    '<span class="reconnect-text">Connection lost.</span>' +
    '<button id="reconnect-btn" type="button" class="reconnect-btn">Reconnect</button>' +
    '<button id="reconnect-dismiss" type="button" class="reconnect-dismiss" aria-label="Dismiss">\u00D7</button>';
  document.body.appendChild(banner);
  document.getElementById('reconnect-btn').onclick = () => {
    wsRetryCount = 0;
    hideReconnectBanner();
    if (sessionId) {
      showToast('Reconnecting...');
      connectWebSocket();
    } else {
      showToast('Session expired. Reloading...');
      setTimeout(() => location.reload(), 800);
    }
  };
  document.getElementById('reconnect-dismiss').onclick = hideReconnectBanner;
}
function hideReconnectBanner() {
  const banner = document.getElementById('reconnect-banner');
  if (banner) banner.classList.remove('visible');
}

// ═══════ Quick Connect / Connect ═══════
async function checkEnvReady() {
  try {
    const res = await fetch('/api/env-status');
    const data = await res.json();
    if (data.ready) {
      document.getElementById('quick-connect-panel').style.display = 'block';
    }
  } catch (e) {}
}

async function quickConnect() {
  const btn = document.getElementById('quick-connect-btn');
  const errEl = document.getElementById('setup-error-qc');
  btn.disabled = true;
  btn.querySelector('.qc-icon + span').textContent = 'Connecting...';
  errEl.style.display = 'none';

  try {
    const res = await fetch('/api/quick-connect', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Quick connect failed');
    openChat(data);
  } catch (e) {
    errEl.textContent = e.message;
    errEl.style.display = 'block';
    btn.disabled = false;
    btn.querySelector('.qc-icon + span').textContent = 'Quick Connect';
  }
}

// Map raw error text -> friendly contextual message
function friendlyTokenError(rawMessage, statusCode) {
  const m = (rawMessage || '').toLowerCase();
  if (statusCode === 0 || m.includes('failed to fetch') || m.includes('network')) {
    return {
      title: 'Network unreachable',
      hint: 'Check your internet connection or that the server at this URL is running.',
    };
  }
  if (statusCode === 401 || m.includes('unauthorized') || m.includes('invalid token') || m.includes('access token')) {
    return {
      title: 'Token rejected by Smartsheet',
      hint: 'The token is invalid, expired, or revoked. Generate a new one in Smartsheet → Account → Personal Settings → API Access.',
    };
  }
  if (statusCode === 403 || m.includes('forbidden') || m.includes('no access')) {
    return {
      title: 'Token has no sheet access',
      hint: 'This token can authenticate but cannot list sheets. Re-create it with at least the READ_SHEETS scope.',
    };
  }
  if (statusCode === 429 || m.includes('rate limit')) {
    return {
      title: 'Smartsheet rate limit hit',
      hint: 'Too many requests in a short period. Wait ~30s and try again.',
    };
  }
  if (statusCode >= 500) {
    return {
      title: 'Smartsheet server error',
      hint: 'Smartsheet API returned an error. Try again in a moment.',
    };
  }
  return {
    title: 'Validation failed',
    hint: rawMessage || 'Unexpected error.',
  };
}

function renderTokenError(errEl, title, hint) {
  errEl.innerHTML =
    '<div style="font-weight:600;margin-bottom:4px;">' + escapeHtml(title) + '</div>' +
    '<div style="font-size:12px;opacity:0.9;">' + escapeHtml(hint) + '</div>';
  errEl.style.display = 'block';
}

// Live token shape feedback (non-network, non-blocking)
function onTokenInput() {
  const tok = (document.getElementById('ss-token').value || '').trim();
  const badge = document.getElementById('token-shape-badge');
  if (!badge) return;
  if (tok.length === 0) { badge.style.display = 'none'; return; }
  if (tok.length < 16) {
    badge.textContent = 'Too short';
    badge.className = 'token-shape-badge warn';
    badge.style.display = 'inline-flex';
  } else if (tok.length > 200 || /\s/.test(tok)) {
    badge.textContent = 'Looks malformed';
    badge.className = 'token-shape-badge warn';
    badge.style.display = 'inline-flex';
  } else {
    badge.textContent = 'Looks valid';
    badge.className = 'token-shape-badge ok';
    badge.style.display = 'inline-flex';
  }
}

function showValidateSkeleton(show) {
  const skel = document.getElementById('validate-skeleton');
  if (skel) skel.style.display = show ? 'block' : 'none';
}

// Step 1: validate token -> fetch sheets + user info
async function validateToken() {
  const tokenInput = document.getElementById('ss-token');
  const rememberChk = document.getElementById('remember-token');
  const btn = document.getElementById('validate-btn');
  const errEl = document.getElementById('setup-error');

  const token = tokenInput.value.trim();
  if (!token) {
    renderTokenError(errEl, 'Token required', 'Please paste your Smartsheet API token first.');
    tokenInput.focus();
    return;
  }
  if (token.length < 16) {
    renderTokenError(errEl, 'Token too short', 'A Smartsheet token is typically 30+ characters. Make sure you copied the full string.');
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Validating...';
  errEl.style.display = 'none';
  showValidateSkeleton(true);

  let res, data;
  try {
    res = await fetch('/api/validate-token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ smartsheet_token: token }),
    });
    data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const friendly = friendlyTokenError(data.error || res.statusText, res.status);
      renderTokenError(errEl, friendly.title, friendly.hint);
      return;
    }

    validatedToken = token;
    validatedSheets = data.sheets || [];
    availableProviders = data.available_providers || {};

    if (validatedSheets.length === 0) {
      renderTokenError(
        errEl,
        'No sheets accessible',
        'Token is valid but no sheets are visible. Ask a colleague to share a sheet with you, or open one of your own first.'
      );
      return;
    }

    if (rememberChk.checked) saveRememberedToken(token);
    else clearRememberedToken();

    // Build confirmation card
    const u = data.user || {};
    const initials = ((u.firstName || '?').charAt(0) + (u.lastName || '').charAt(0)).toUpperCase();
    const fullName = [u.firstName, u.lastName].filter(Boolean).join(' ') || (u.email || 'Connected');
    document.getElementById('account-confirmation').innerHTML =
      '<div class="avatar">' + escapeHtml(initials) + '</div>' +
      '<div class="info"><div class="name">' + escapeHtml(fullName) + '</div>' +
      '<div class="email">' + escapeHtml(u.email || '') + (u.account ? ' \u00B7 ' + escapeHtml(u.account) : '') + '</div></div>';

    renderSheetDropdown(validatedSheets);
    populateStep2Providers();
    updateApiKeyBadge();

    document.getElementById('connect-step-1').style.display = 'none';
    document.getElementById('connect-step-2').style.display = 'block';
    const ind = document.getElementById('step-indicator');
    ind.querySelectorAll('.step').forEach(s => {
      const n = parseInt(s.dataset.step, 10);
      s.classList.toggle('active', n === 2);
      s.classList.toggle('done', n < 2);
    });
  } catch (e) {
    const friendly = friendlyTokenError(e.message, 0);
    renderTokenError(errEl, friendly.title, friendly.hint);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Validate & Browse Sheets';
    showValidateSkeleton(false);
  }
}

function backToStep1() {
  document.getElementById('connect-step-1').style.display = 'block';
  document.getElementById('connect-step-2').style.display = 'none';
  const ind = document.getElementById('step-indicator');
  ind.querySelectorAll('.step').forEach(s => {
    const n = parseInt(s.dataset.step, 10);
    s.classList.toggle('active', n === 1);
    s.classList.remove('done');
  });
}

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
}

function renderSheetDropdown(sheets) {
  const dd = document.getElementById('sheet-dropdown');
  const countLabel = document.getElementById('sheet-count-label');
  countLabel.textContent = sheets.length + ' sheet' + (sheets.length === 1 ? '' : 's');
  if (!sheets.length) {
    dd.innerHTML = '<div class="sheet-option empty">No sheets available on this account</div>';
    return;
  }
  dd.innerHTML = sheets.map(s =>
    '<div class="sheet-option" data-id="' + escapeHtml(s.id) + '" data-name="' + escapeHtml(s.name) + '" onclick="pickSheet(this)">' + escapeHtml(s.name) + '</div>'
  ).join('');
}

function filterSheets() {
  const q = (document.getElementById('sheet-filter').value || '').toLowerCase();
  const filtered = q
    ? validatedSheets.filter(s => String(s.name).toLowerCase().includes(q))
    : validatedSheets;
  renderSheetDropdown(filtered);
  // Restore current selection highlight
  const currentId = document.getElementById('ss-sheet-id').value;
  if (currentId) {
    const opt = document.querySelector('.sheet-option[data-id="' + currentId + '"]');
    if (opt) opt.classList.add('selected');
  }
}

function pickSheet(el) {
  document.querySelectorAll('.sheet-option').forEach(o => o.classList.remove('selected'));
  el.classList.add('selected');
  document.getElementById('ss-sheet-id').value = el.dataset.id;
  document.getElementById('sheet-filter').value = el.dataset.name;
  // Wipe success messages from sibling tabs so the user sees a single source of truth
  const byidOut = document.getElementById('sheet-byid-result');
  const createOut = document.getElementById('sheet-create-result');
  if (byidOut) byidOut.innerHTML = '';
  if (createOut) createOut.innerHTML = '';
}

function switchSheetTab(name) {
  document.querySelectorAll('.sheet-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.sheet-tab-panel').forEach(p => {
    p.style.display = (p.dataset.tab === name) ? 'block' : 'none';
  });
  // Hide step2 error when switching modes — context changes
  const errEl = document.getElementById('setup-error-step2');
  if (errEl) errEl.style.display = 'none';
}

function onByIdInput() {
  const el = document.getElementById('sheet-byid-input');
  const raw = el.value || '';
  // Strip whitespace and quotes silently — common when copy-pasting from "File > Properties"
  const cleaned = raw.replace(/[\s"'`]/g, '');
  if (cleaned !== raw) el.value = cleaned;
  document.getElementById('sheet-byid-btn').disabled = !/^\d{6,}$/.test(cleaned);
  // Wipe stale success/error block so the user knows the previous lookup is no longer valid
  const out = document.getElementById('sheet-byid-result');
  if (out && out.innerHTML) out.innerHTML = '';
  document.getElementById('ss-sheet-id').value = '';
}

function onCreateInput() {
  const v = (document.getElementById('sheet-create-name').value || '').trim();
  document.getElementById('sheet-create-btn').disabled = v.length === 0;
}

async function lookupSheetById() {
  const input = document.getElementById('sheet-byid-input');
  const btn = document.getElementById('sheet-byid-btn');
  const out = document.getElementById('sheet-byid-result');
  const id = (input.value || '').trim();
  out.innerHTML = '';
  if (!/^\d+$/.test(id)) {
    out.innerHTML = '<div class="lookup-error">Sheet ID must be numeric.</div>';
    return;
  }
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = 'Looking up...';
  try {
    const r = await fetch('/api/lookup-sheet', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ smartsheet_token: validatedToken, sheet_id: id })
    });
    const data = await r.json();
    if (!r.ok || data.error) {
      out.innerHTML = '<div class="lookup-error">' + escapeHtml(data.error || 'Sheet not found.') + '</div>';
      document.getElementById('ss-sheet-id').value = '';
      return;
    }
    document.getElementById('ss-sheet-id').value = data.id;
    const meta = (data.row_count != null && data.column_count != null)
      ? data.row_count + ' rows &middot; ' + data.column_count + ' columns'
      : '';
    out.innerHTML =
      '<div class="lookup-success">' +
      '  <div>Found <strong>' + escapeHtml(data.name || ('Sheet ' + data.id)) + '</strong></div>' +
      (meta ? '  <div class="lookup-success-meta">' + meta + '</div>' : '') +
      '  <button type="button" class="btn-talk-to-sheet" onclick="talkToSelectedSheet(this)"><span>Talk to this sheet</span><span class="arrow">&rarr;</span></button>' +
      '</div>';
  } catch (e) {
    out.innerHTML = '<div class="lookup-error">Network error: ' + escapeHtml(String(e && e.message || e)) + '</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

function talkToSelectedSheet(btn) {
  // Visual feedback on the inline CTA — finalConnect() handles its own button state for the main one
  if (btn) {
    btn.disabled = true;
    const label = btn.querySelector('span:first-child');
    if (label) label.textContent = 'Connecting...';
    const arrow = btn.querySelector('.arrow');
    if (arrow) arrow.textContent = '...';
  }
  // Re-use the canonical connect flow (validates sheet ID + LLM + opens session)
  finalConnect();
}

async function createBlankSheet() {
  const input = document.getElementById('sheet-create-name');
  const btn = document.getElementById('sheet-create-btn');
  const out = document.getElementById('sheet-create-result');
  const name = (input.value || '').trim();
  out.innerHTML = '';
  if (!name) {
    out.innerHTML = '<div class="lookup-error">Please give the new sheet a name.</div>';
    return;
  }
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = 'Creating...';
  try {
    const r = await fetch('/api/create-sheet', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ smartsheet_token: validatedToken, name: name })
    });
    const data = await r.json();
    if (!r.ok || data.error) {
      out.innerHTML = '<div class="lookup-error">' + escapeHtml(data.error || 'Could not create sheet.') + '</div>';
      document.getElementById('ss-sheet-id').value = '';
      return;
    }
    document.getElementById('ss-sheet-id').value = data.id;
    const permaLink = data.permalink
      ? ' &middot; <a href="' + escapeHtml(data.permalink) + '" target="_blank" rel="noopener">Open in Smartsheet &#8599;</a>'
      : '';
    out.innerHTML =
      '<div class="lookup-success">' +
      '  <div>Created <strong>' + escapeHtml(data.name) + '</strong></div>' +
      '  <div class="lookup-success-meta">ID ' + escapeHtml(data.id) + permaLink + '</div>' +
      '  <button type="button" class="btn-talk-to-sheet" onclick="talkToSelectedSheet(this)"><span>Talk to this sheet</span><span class="arrow">&rarr;</span></button>' +
      '</div>';
    // Add to validatedSheets so it shows up in the Browse tab too
    if (typeof validatedSheets !== 'undefined' && Array.isArray(validatedSheets)) {
      validatedSheets.unshift({ id: data.id, name: data.name });
      const countLabel = document.getElementById('sheet-count-label');
      if (countLabel) countLabel.textContent = validatedSheets.length + ' sheet' + (validatedSheets.length === 1 ? '' : 's');
    }
  } catch (e) {
    out.innerHTML = '<div class="lookup-error">Network error: ' + escapeHtml(String(e && e.message || e)) + '</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

function populateStep2Providers() {
  const providerSel = document.getElementById('llm-provider');
  // Mark providers with server env key with suffix
  Array.from(providerSel.options).forEach(opt => {
    const has = !!availableProviders[opt.value];
    const base = opt.textContent.replace(/\s*·\s*env$/, '');
    opt.textContent = has ? base + ' · env' : base;
  });
  // Preselect first available
  const firstAvail = Object.keys(availableProviders)[0];
  if (firstAvail) providerSel.value = firstAvail;
  onProviderChange();
}

function onProviderChange() {
  const p = document.getElementById('llm-provider').value;
  const info = availableProviders[p];
  const modelInput = document.getElementById('llm-model');
  if (info && info.default_model) {
    modelInput.value = info.default_model;
  } else {
    const defaults = {
      openai: 'gpt-4o-mini', anthropic: 'claude-sonnet-4-20250514',
      google: 'gemini-2.0-flash', groq: 'llama-3.3-70b-versatile',
      mistral: 'mistral-large-latest', deepseek: 'deepseek-chat',
      openrouter: 'anthropic/claude-sonnet-4-20250514',
    };
    modelInput.value = defaults[p] || '';
  }
  updateApiKeyBadge();
}

function toggleApiKeyField() {
  const f = document.getElementById('llm-key');
  const arrow = document.getElementById('api-key-toggle');
  const isOpen = f.style.display !== 'none';
  f.style.display = isOpen ? 'none' : 'block';
  arrow.classList.toggle('open', !isOpen);
  if (!isOpen) setTimeout(() => f.focus(), 50);
}

function updateApiKeyBadge() {
  const p = document.getElementById('llm-provider').value;
  const userKey = document.getElementById('llm-key').value.trim();
  const hasEnv = !!availableProviders[p];
  const badge = document.getElementById('api-key-status-badge');
  if (userKey) {
    badge.className = 'api-badge user';
    badge.textContent = 'your key';
  } else if (hasEnv) {
    badge.className = 'api-badge env';
    badge.textContent = 'server .env';
  } else {
    badge.className = 'api-badge missing';
    badge.textContent = 'no key set';
  }
}

// Step 2: actually create the session
async function finalConnect() {
  const btn = document.getElementById('connect-btn');
  const errEl = document.getElementById('setup-error-step2');
  const sheetId = document.getElementById('ss-sheet-id').value.trim();

  if (!sheetId) {
    errEl.textContent = 'Please select a sheet from the list above.';
    errEl.style.display = 'block';
    return;
  }
  btn.disabled = true;
  btn.textContent = 'Connecting...';
  errEl.style.display = 'none';

  const body = {
    smartsheet_token: validatedToken,
    sheet_id: sheetId,
    llm_provider: document.getElementById('llm-provider').value,
    llm_model: document.getElementById('llm-model').value.trim(),
    llm_api_key: document.getElementById('llm-key').value.trim(),
  };

  try {
    const res = await fetch('/api/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Connection failed');
    openChat(data);
  } catch (e) {
    errEl.textContent = e.message;
    errEl.style.display = 'block';
    btn.disabled = false;
    btn.textContent = 'Connect & Start Chatting';
  }
}

// ═══════ Open Chat ═══════
function openChat(data) {
  sessionId = data.session_id;
  wsToken = data.ws_token;
  authCookie = data.auth_cookie || null;
  allSheets = data.all_sheets || [];
  currentUser = data.user || null;

  saveSessionInfo(data);

  const badge = document.getElementById('sheet-badge');
  badge.textContent = data.sheet.name + ' \u00B7 ' + data.sheet.columnCount + ' cols \u00B7 ' + data.sheet.totalRowCount + ' rows';
  badge.style.display = 'inline';
  document.getElementById('header-status').style.display = 'block';

  // User badge
  if (currentUser && currentUser.email) {
    const ub = document.getElementById('user-badge');
    const initials = (((currentUser.firstName || currentUser.email).charAt(0) || '?')).toUpperCase();
    document.getElementById('user-avatar-letter').textContent = initials;
    document.getElementById('user-email-label').textContent = currentUser.email;
    ub.style.display = 'inline-flex';
  }

  document.getElementById('btn-export').style.display = 'flex';
  document.getElementById('btn-export-pdf').style.display = 'flex';
  document.getElementById('btn-history-toggle').style.display = 'flex';
  document.getElementById('btn-shortcuts').style.display = 'flex';
  document.getElementById('btn-settings').style.display = 'flex';
  document.getElementById('btn-watch').style.display = 'flex';
  const bps = document.getElementById('btn-prompt-sidebar'); if (bps) bps.style.display = 'flex';
  const bl = document.getElementById('btn-logout'); if (bl) bl.style.display = 'flex';
  const burger = document.getElementById('btn-burger');
  if (burger) burger.style.display = '';

  availableProviders = data.available_providers || {};
  populateModelSelector(data.current_provider, data.current_model);
  populateSheetSwitcher(data.sheet);

  const setupEl = document.getElementById('setup');
  const chatEl = document.getElementById('chat');

  setupEl.classList.add('screen-exit');
  setTimeout(() => {
    setupEl.style.display = 'none';
    chatEl.style.display = 'flex';
    chatEl.classList.add('screen-enter');
    setTimeout(() => chatEl.classList.remove('screen-enter'), 500);
    initPromptSidebar();
  }, 380);

  currentConversationId = 'conv-' + Date.now();
  conversationMessages = [];
  currentSheetId = data.sheet && data.sheet.id ? String(data.sheet.id) : null;

  connectWebSocket();
  registerActiveConversation();
  startWebhookPolling();
  if (data.db_user_id) maybeMigrateLocalHistory();

  addToFavorites(data.sheet.id || '', data.sheet.name || 'Unknown');

  if (data.welcome) {
    setTimeout(() => { handleEvent(data.welcome); }, 600);
  }

  setTimeout(() => document.getElementById('user-input').focus(), 700);
  const _ui = document.getElementById('user-input');
  if (_ui && !_ui._slashListenerBound) {
    _ui.addEventListener('input', updateSlashMenu);
    _ui.addEventListener('blur', () => setTimeout(() => { _slashMenuState.open = false; renderSlashMenu(); }, 150));
    _ui._slashListenerBound = true;
  }
  startFirstMessageHint();
  renderHistoryList();
  renderPinnedList();
  renderFavorites();

  // First-time onboarding
  if (!localStorage.getItem('ss_ctrl_tour_done')) {
    setTimeout(() => startTour(), 1400);
  }
}

// ═══════ Sheet Switcher ═══════
function populateSheetSwitcher(currentSheet) {
  if (allSheets.length <= 1) return;

  const switcher = document.getElementById('sheet-switcher');
  const select = document.getElementById('sheet-select');
  switcher.style.display = 'block';

  select.innerHTML = '';
  allSheets.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.name;
    if (String(s.id) === String(currentSheet.id)) opt.selected = true;
    select.appendChild(opt);
  });
}

async function switchSheet(sheetId) {
  if (!sessionId) return;
  showToast('Switching sheet...');

  try {
    const res = await fetch('/api/switch-sheet', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, sheet_id: sheetId }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Switch failed');

    // Update badge
    const badge = document.getElementById('sheet-badge');
    badge.textContent = data.sheet.name + ' \u00B7 ' + data.sheet.columnCount + ' cols \u00B7 ' + data.sheet.totalRowCount + ' rows';

    // Save current conversation, start new
    saveCurrentConversation();
    currentConversationId = 'conv-' + Date.now();
    conversationMessages = [];
    registerActiveConversation();

    // Stop watch (state per-sheet) — will be restored if new sheet had it on
    if (watchActive) stopWatch();
    currentSheetId = String(sheetId);
    restoreWatchStateForSheet();

    // Clear messages
    document.getElementById('messages').innerHTML = '';

    // Show welcome for new sheet
    if (data.welcome) {
      handleEvent(data.welcome);
    }

    showToast('Connected to ' + data.sheet.name);
    renderHistoryList();
  } catch (e) {
    showToast('Error: ' + e.message);
    // Reset select
    const select = document.getElementById('sheet-select');
    for (let opt of select.options) {
      if (document.getElementById('sheet-badge').textContent.startsWith(opt.textContent)) {
        opt.selected = true;
        break;
      }
    }
  }
}

// ═══════ Streaming State ═══════
let streamingGroup = null;
let streamingBubble = null;
let streamingContent = '';

// ═══════ Event Handling ═══════
function handleEvent(event) {
  if (event.type === 'stream_delta') {
    removeTyping();
    if (!streamingGroup) {
      removeSuggestions();
      const container = document.getElementById('messages');
      streamingGroup = document.createElement('div');
      streamingGroup.className = 'msg-group assistant';

      const label = document.createElement('div');
      label.className = 'msg-label';
      label.textContent = 'Assistant';

      streamingBubble = document.createElement('div');
      streamingBubble.className = 'msg assistant';
      streamingContent = '';

      streamingGroup.appendChild(label);
      streamingGroup.appendChild(streamingBubble);
      container.appendChild(streamingGroup);
    }
    streamingContent += event.content;
    if (typeof marked !== 'undefined') {
      streamingBubble.innerHTML = renderMarkdown(streamingContent);
    } else {
      streamingBubble.textContent = streamingContent;
    }
    const container = document.getElementById('messages');
    container.scrollTop = container.scrollHeight;
    return;
  }

  if (event.type === 'stream_end') {
    removeTyping();
    removeSuggestions();
    setAgentRunning(false);
    const finalContent = event.content || streamingContent;
    const ts = Date.now();

    if (streamingGroup && streamingBubble) {
      if (typeof marked !== 'undefined') {
        streamingBubble.innerHTML = renderMarkdown(finalContent);
        enhanceAssistantBubble(streamingBubble);
      } else {
        streamingBubble.textContent = finalContent;
      }
      const copyBtn = document.createElement('button');
      copyBtn.className = 'msg-copy-btn';
      copyBtn.title = 'Copy';
      copyBtn.setAttribute('aria-label', 'Copy message');
      copyBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';
      copyBtn.onclick = (e) => { e.stopPropagation(); copyText(finalContent, copyBtn); };
      streamingBubble.appendChild(copyBtn);
      streamingBubble.appendChild(createTtsButton(finalContent));
      streamingBubble.appendChild(createPinButton(finalContent, ts));
    } else {
      addMessage('assistant', finalContent);
    }

    conversationMessages.push({ role: 'assistant', content: finalContent, time: ts });
    saveCurrentConversation();

    streamingGroup = null;
    streamingBubble = null;
    streamingContent = '';

    if (event.try_cards && event.try_cards.length) {
      addTryCards(event.try_cards);
    } else if (event.suggestions && event.suggestions.length) {
      addSuggestions(event.suggestions);
    }
    return;
  }

  if (event.type === 'cancelled') {
    removeTyping();
    setAgentRunning(false);
    if (event.content) addMessage('assistant', event.content);
    streamingGroup = null;
    streamingBubble = null;
    streamingContent = '';
    return;
  }

  removeTyping();
  removeSuggestions();
  streamingGroup = null;
  streamingBubble = null;
  streamingContent = '';

  if (event.type === 'confirm_action') {
    addConfirmCard(event);
    return;
  }

  if (event.type === 'chart') {
    addChart(event.spec);
    return;
  }

  if (event.type === 'notification') {
    const changes = Array.isArray(event.changes) ? event.changes.join(', ') : (event.changes || 'Data updated');
    const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    showNotification('Sheet change detected', time + ' — ' + changes);
    addToolMessage('watch', 'Detected: ' + changes);
    return;
  }

  if (event.type === 'response') {
    setAgentRunning(false);
    addMessage('assistant', event.content);
    if (event.try_cards && event.try_cards.length) {
      addTryCards(event.try_cards);
    } else if (event.suggestions && event.suggestions.length) {
      addSuggestions(event.suggestions);
    }
  } else if (event.type === 'image') {
    addImageMessage(event.url, event.caption || '');
  } else if (event.type === 'tool_call') {
    const argsStr = JSON.stringify(event.arguments, null, 2);
    addToolMessage('calling ' + event.name, argsStr);
    showTyping();
  } else if (event.type === 'tool_result') {
    addToolMessage(event.name + ' result', event.result);
    showTyping();
  } else if (event.type === 'agent_hint') {
    addAgentHint(event);
  }
}

// ═══════ Agent reliability hints (P3.4) ═══════
// Show a discrete, color-coded banner when the safety-net catches a model
// mistake. Helps the user understand WHY the agent is correcting itself
// instead of seeing only a wall of failing tool calls.
function addAgentHint(evt) {
  if (!chatBox) return;
  const level = evt.level || 'info';
  const code = evt.code || 'AGENT_HINT';
  const tool = evt.tool ? ' · ' + evt.tool : '';
  const message = evt.message || '';

  const wrap = document.createElement('div');
  wrap.className = 'agent-hint agent-hint-' + level;
  const icon = level === 'warn' ? '⚠' : (level === 'error' ? '✕' : 'ⓘ');
  wrap.innerHTML =
    '<span class="agent-hint-icon">' + icon + '</span>' +
    '<span class="agent-hint-body">' +
      '<span class="agent-hint-code">' + code + tool + '</span>' +
      '<span class="agent-hint-msg">' + message.replace(/</g, '&lt;') + '</span>' +
    '</span>';
  chatBox.appendChild(wrap);
  chatBox.scrollTop = chatBox.scrollHeight;
}

// ═══════ TTS / Read Aloud ═══════
let _ttsCurrent = null;

function _stripMarkdownForTts(md) {
  if (!md) return '';
  let s = String(md);
  s = s.replace(/```[\s\S]*?```/g, ' code block. ');
  s = s.replace(/`[^`]+`/g, m => m.slice(1, -1));
  s = s.replace(/!\[([^\]]*)\]\([^)]+\)/g, '$1');
  s = s.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
  s = s.replace(/^\s{0,3}>\s?/gm, '');
  s = s.replace(/^#{1,6}\s+/gm, '');
  s = s.replace(/(\*\*|__)(.*?)\1/g, '$2');
  s = s.replace(/(\*|_)(.*?)\1/g, '$2');
  s = s.replace(/^\s*[-*+]\s+/gm, '');
  s = s.replace(/\|/g, ' ');
  s = s.replace(/\n{2,}/g, '. ');
  s = s.replace(/\s+/g, ' ').trim();
  return s.slice(0, 4500);
}

function _detectLang(text) {
  if (!text) return navigator.language || 'en-US';
  const sample = text.slice(0, 400).toLowerCase();
  const frHits = (sample.match(/\b(le|la|les|des|une|est|pour|avec|dans|vous|nous|cette|votre|c'est|s'il)\b/g) || []).length;
  const enHits = (sample.match(/\b(the|and|with|for|that|this|your|have|from|please|click|sheet)\b/g) || []).length;
  if (frHits > enHits + 1) return 'fr-FR';
  if (enHits > 0) return 'en-US';
  return navigator.language || 'en-US';
}

function _pickVoice(lang) {
  const all = window.speechSynthesis.getVoices() || [];
  if (!all.length) return null;
  const exact = all.find(v => v.lang === lang);
  if (exact) return exact;
  const prefix = lang.split('-')[0];
  return all.find(v => v.lang.startsWith(prefix)) || all[0];
}

function speakAssistantMessage(content, btn) {
  if (!('speechSynthesis' in window)) {
    showToast('Voice playback not supported in this browser');
    return;
  }
  if (_ttsCurrent && _ttsCurrent.btn === btn) {
    window.speechSynthesis.cancel();
    btn.classList.remove('playing');
    _ttsCurrent = null;
    return;
  }
  if (_ttsCurrent) {
    window.speechSynthesis.cancel();
    _ttsCurrent.btn?.classList.remove('playing');
    _ttsCurrent = null;
  }
  const text = _stripMarkdownForTts(content);
  if (!text) { showToast('Nothing to read'); return; }
  const lang = _detectLang(text);
  const ut = new SpeechSynthesisUtterance(text);
  ut.lang = lang;
  ut.rate = 1.0;
  ut.pitch = 1.0;
  const v = _pickVoice(lang);
  if (v) ut.voice = v;
  ut.onend = () => { btn.classList.remove('playing'); _ttsCurrent = null; };
  ut.onerror = () => { btn.classList.remove('playing'); _ttsCurrent = null; };
  btn.classList.add('playing');
  _ttsCurrent = { btn, ut };
  window.speechSynthesis.speak(ut);
}

function createTtsButton(content) {
  const btn = document.createElement('button');
  btn.className = 'msg-tts-btn';
  btn.title = 'Read aloud';
  btn.setAttribute('aria-label', 'Read message aloud');
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>';
  btn.onclick = (e) => { e.stopPropagation(); speakAssistantMessage(content, btn); };
  return btn;
}

// Pre-load voices (some browsers need this)
if ('speechSynthesis' in window) {
  window.speechSynthesis.onvoiceschanged = () => {};
  try { window.speechSynthesis.getVoices(); } catch {}
}

// ═══════ CSV Drag & Drop ═══════
let _csvDragCounter = 0;
let _csvParsed = null;

function _isCsvFile(f) {
  if (!f) return false;
  const n = (f.name || '').toLowerCase();
  return n.endsWith('.csv') || n.endsWith('.tsv') || f.type === 'text/csv';
}

function _parseCsv(text, delimiter) {
  const rows = [];
  let cur = [];
  let field = '';
  let inQuotes = false;
  let i = 0;
  const d = delimiter || (text.split('\n')[0].includes('\t') ? '\t' : ',');
  while (i < text.length) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i += 2; continue; }
        inQuotes = false; i++; continue;
      }
      field += c; i++; continue;
    }
    if (c === '"') { inQuotes = true; i++; continue; }
    if (c === d) { cur.push(field); field = ''; i++; continue; }
    if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      cur.push(field);
      rows.push(cur);
      cur = []; field = '';
      i++; continue;
    }
    field += c; i++;
  }
  if (field.length > 0 || cur.length > 0) { cur.push(field); rows.push(cur); }
  return rows.filter(r => r.length > 1 || (r.length === 1 && r[0] !== ''));
}

function initCsvDragDrop() {
  const overlay = document.getElementById('csv-drop-overlay');
  if (!overlay) return;

  window.addEventListener('dragenter', (e) => {
    if (!sessionId) return;
    if (!Array.from(e.dataTransfer?.types || []).includes('Files')) return;
    _csvDragCounter++;
    overlay.classList.add('active');
  });
  window.addEventListener('dragover', (e) => {
    if (!sessionId) return;
    if (!Array.from(e.dataTransfer?.types || []).includes('Files')) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  window.addEventListener('dragleave', (e) => {
    if (!sessionId) return;
    _csvDragCounter--;
    if (_csvDragCounter <= 0) {
      _csvDragCounter = 0;
      overlay.classList.remove('active');
    }
  });
  window.addEventListener('drop', (e) => {
    if (!sessionId) return;
    e.preventDefault();
    _csvDragCounter = 0;
    overlay.classList.remove('active');
    const file = Array.from(e.dataTransfer?.files || [])[0];
    if (!file) return;
    if (!_isCsvFile(file)) {
      showToast('Only .csv or .tsv files are supported');
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      showToast('File too large (max 10 MB)');
      return;
    }
    const reader = new FileReader();
    reader.onload = () => openCsvPreview(file.name, reader.result);
    reader.onerror = () => showToast('Failed to read file');
    reader.readAsText(file);
  });
}

function openCsvPreview(filename, text) {
  const rows = _parseCsv(String(text || ''));
  if (rows.length < 1) {
    showToast('CSV looks empty');
    return;
  }
  const headers = rows[0].map((h, i) => (h || '').trim() || `Column ${i + 1}`);
  const dataRows = rows.slice(1);
  if (headers.length === 0) {
    showToast('No columns detected');
    return;
  }
  _csvParsed = { headers, rows: dataRows };

  const defaultName = filename.replace(/\.[^.]+$/, '').replace(/[_-]/g, ' ').slice(0, 80) || 'Imported CSV';
  document.getElementById('csv-preview-name').value = defaultName;
  document.getElementById('csv-preview-meta').textContent = `${dataRows.length} row${dataRows.length === 1 ? '' : 's'} · ${headers.length} column${headers.length === 1 ? '' : 's'}`;

  const table = document.getElementById('csv-preview-table');
  const headHtml = '<thead><tr>' + headers.map(h => '<th>' + escapeHtml(h) + '</th>').join('') + '</tr></thead>';
  const bodyHtml = '<tbody>' + dataRows.slice(0, 10).map(r =>
    '<tr>' + headers.map((_, i) => '<td>' + escapeHtml(r[i] == null ? '' : r[i]) + '</td>').join('') + '</tr>'
  ).join('') + '</tbody>';
  table.innerHTML = headHtml + bodyHtml;

  document.getElementById('csv-preview-overlay').classList.remove('hidden');
}

function closeCsvPreview() {
  document.getElementById('csv-preview-overlay')?.classList.add('hidden');
  _csvParsed = null;
}

async function confirmCsvImport() {
  if (!_csvParsed || !sessionId) return;
  const name = document.getElementById('csv-preview-name').value.trim();
  if (!name) { showToast('Sheet name required'); return; }
  const btn = document.getElementById('csv-import-btn');
  btn.disabled = true;
  btn.textContent = 'Creating...';
  try {
    const res = await fetch('/api/csv-to-sheet', {
      method: 'POST',
      headers: sessionAuthHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        session_id: sessionId,
        name,
        headers: _csvParsed.headers,
        rows: _csvParsed.rows,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    closeCsvPreview();
    showToast(`Sheet created (${data.rows_added} rows). Switching...`);
    if (typeof refreshSheetsList === 'function') {
      try { await refreshSheetsList(); } catch {}
    }
    if (typeof switchSheet === 'function') {
      setTimeout(() => switchSheet(data.sheet_id), 500);
    }
  } catch (e) {
    showToast('Import failed: ' + (e.message || e));
  } finally {
    btn.disabled = false;
    btn.textContent = 'Create sheet';
  }
}

// ═══════ Templates / Saved Prompts ═══════
const TEMPLATES_KEY = 'ss_ctrl_templates_v1';

function loadTemplates() {
  try {
    const raw = localStorage.getItem(TEMPLATES_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch { return []; }
}

function saveTemplatesArr(arr) {
  try { localStorage.setItem(TEMPLATES_KEY, JSON.stringify(arr)); } catch {}
}

function renderTemplatesList() {
  const list = document.getElementById('settings-templates-list');
  if (!list) return;
  const tpls = loadTemplates();
  if (tpls.length === 0) {
    list.innerHTML = '<span style="color:var(--text-muted);font-size:12px;">No templates yet. Click <strong>Add template</strong> to create one.</span>';
    return;
  }
  list.innerHTML = tpls.map(t => `
    <div class="template-item">
      <div class="template-name" title="${escapeHtml(t.prompt)}">${escapeHtml(t.name)}</div>
      <div class="template-actions">
        <button onclick="useTemplate('${escapeAttr(t.id)}')" title="Use">Use</button>
        <button onclick="openTemplateEditor('${escapeAttr(t.id)}')" title="Edit">Edit</button>
        <button class="danger" onclick="deleteTemplate('${escapeAttr(t.id)}')" title="Delete">×</button>
      </div>
    </div>
  `).join('');
}

function escapeAttr(s) { return escapeHtml(s); }

function openTemplateEditor(id) {
  const overlay = document.getElementById('template-editor-overlay');
  const title = document.getElementById('template-editor-title');
  const idEl = document.getElementById('template-edit-id');
  const nameEl = document.getElementById('template-edit-name');
  const promptEl = document.getElementById('template-edit-prompt');
  if (id) {
    const t = loadTemplates().find(x => x.id === id);
    if (!t) return;
    title.textContent = 'Edit template';
    idEl.value = t.id;
    nameEl.value = t.name;
    promptEl.value = t.prompt;
  } else {
    title.textContent = 'New template';
    idEl.value = '';
    nameEl.value = '';
    promptEl.value = '';
  }
  overlay.classList.remove('hidden');
  setTimeout(() => nameEl.focus(), 50);
}

function closeTemplateEditor() {
  document.getElementById('template-editor-overlay')?.classList.add('hidden');
}

function saveTemplate() {
  const id = document.getElementById('template-edit-id').value.trim();
  const name = document.getElementById('template-edit-name').value.trim().replace(/\s+/g, '-').toLowerCase();
  const prompt = document.getElementById('template-edit-prompt').value.trim();
  if (!name || !prompt) {
    showToast('Name and prompt are required');
    return;
  }
  if (!/^[a-z0-9._-]+$/i.test(name)) {
    showToast('Name: letters, numbers, - _ . only');
    return;
  }
  const tpls = loadTemplates();
  if (id) {
    const i = tpls.findIndex(t => t.id === id);
    if (i >= 0) tpls[i] = { ...tpls[i], name, prompt, updatedAt: Date.now() };
  } else {
    if (tpls.some(t => t.name === name)) {
      showToast(`Template "${name}" already exists`);
      return;
    }
    tpls.unshift({ id: 'tpl_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6), name, prompt, createdAt: Date.now() });
  }
  saveTemplatesArr(tpls);
  closeTemplateEditor();
  renderTemplatesList();
  showToast('Template saved');
}

function deleteTemplate(id) {
  const tpls = loadTemplates();
  const t = tpls.find(x => x.id === id);
  if (!t) return;
  if (!confirm(`Delete template "${t.name}"?`)) return;
  saveTemplatesArr(tpls.filter(x => x.id !== id));
  renderTemplatesList();
  showToast('Template deleted');
}

function useTemplate(id) {
  const t = loadTemplates().find(x => x.id === id);
  if (!t) return;
  let prompt = t.prompt;
  const vars = Array.from(prompt.matchAll(/\{\{\s*([a-zA-Z0-9_-]+)\s*\}\}/g)).map(m => m[1]);
  const unique = [...new Set(vars)];
  for (const v of unique) {
    const val = window.prompt(`Value for {{${v}}}:`, '');
    if (val === null) return;
    prompt = prompt.replace(new RegExp(`\\{\\{\\s*${v}\\s*\\}\\}`, 'g'), val);
  }
  toggleSettingsModal();
  const input = document.getElementById('user-input');
  if (input) {
    input.value = prompt;
    input.focus();
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
  }
  showToast(`Loaded: ${t.name}`);
}

// ═══════ Slash Commands ═══════
const SLASH_COMMANDS = [
  {
    cmd: 'summarize',
    icon: '📋',
    args: '',
    desc: 'Concise overview of the active sheet',
    hasArgs: false,
    expand: () => 'Provide a concise summary of this sheet: row count, key columns, dominant statuses, anything that stands out.',
  },
  {
    cmd: 'find',
    icon: '🔍',
    args: '<query>',
    desc: 'Search rows matching a term',
    hasArgs: true,
    expand: (args) => `Search rows that match: ${args}. Show the matching rows in a markdown table with their row IDs.`,
  },
  {
    cmd: 'formula',
    icon: '🧮',
    args: '<description>',
    desc: 'Generate a Smartsheet formula',
    hasArgs: true,
    expand: (args) => `Generate a Smartsheet formula for: ${args}. Explain it briefly and show example usage.`,
  },
  {
    cmd: 'chart',
    icon: '📊',
    args: '<type> <column>',
    desc: 'Visualize a column (line/bar/pie)',
    hasArgs: true,
    expand: (args) => `Build a ${args} chart from the sheet. Return the data as a markdown table so it can be visualized inline.`,
  },
  {
    cmd: 'analyze',
    icon: '🔬',
    args: '',
    desc: 'Detect patterns, anomalies, problems',
    hasArgs: false,
    expand: () => 'Analyze this sheet thoroughly: detect patterns, anomalies, missing data, and call out anything that looks wrong.',
  },
  {
    cmd: 'template',
    icon: '⭐',
    args: '<name>',
    desc: 'Run a saved template prompt',
    hasArgs: true,
    expand: (args) => {
      const t = (typeof loadTemplates === 'function') ? loadTemplates().find(x => x.name.toLowerCase() === (args || '').trim().toLowerCase()) : null;
      return t ? t.prompt : `Run template named "${args}".`;
    },
  },
  {
    cmd: 'help',
    icon: '❓',
    args: '',
    desc: 'List every slash command',
    hasArgs: false,
    expand: () => '__HELP__', // sentinel: handled locally, never sent to LLM
  },
  {
    cmd: 'clear',
    icon: '🧹',
    args: '',
    desc: 'Clear the current conversation',
    hasArgs: false,
    expand: () => '__CLEAR__',
  },
];

let _slashMenuState = { open: false, filtered: [], selected: 0 };

function _slashMenuEl() { return document.getElementById('slash-menu'); }

function renderSlashMenu() {
  const menu = _slashMenuEl();
  if (!menu) return;
  if (!_slashMenuState.open || _slashMenuState.filtered.length === 0) {
    menu.classList.add('hidden');
    menu.innerHTML = '';
    return;
  }
  const html = [
    '<div class="slash-menu-header">Slash commands · ↑ ↓ to navigate, ⏎ to insert, Esc to close</div>',
    ..._slashMenuState.filtered.map((c, i) => `
      <div class="slash-item ${i === _slashMenuState.selected ? 'active' : ''}" role="option" data-cmd="${c.cmd}">
        <div class="slash-item-icon">${c.icon}</div>
        <div class="slash-item-body">
          <div class="slash-item-cmd">/${c.cmd}${c.args ? `<span class="slash-args">${c.args}</span>` : ''}</div>
          <div class="slash-item-desc">${c.desc}</div>
        </div>
        <div class="slash-item-hint">${c.hasArgs ? 'Tab' : '⏎'}</div>
      </div>
    `),
  ].join('');
  menu.innerHTML = html;
  menu.classList.remove('hidden');
  Array.from(menu.querySelectorAll('.slash-item')).forEach((el, i) => {
    el.addEventListener('mouseenter', () => { _slashMenuState.selected = i; renderSlashMenu(); });
    el.addEventListener('mousedown', (e) => { e.preventDefault(); _slashMenuState.selected = i; pickSlashCommand(); });
  });
}

function updateSlashMenu() {
  const input = document.getElementById('user-input');
  if (!input) return;
  const v = input.value;
  const showMenu = v.startsWith('/') && !v.includes('\n');
  if (!showMenu) {
    _slashMenuState.open = false;
    renderSlashMenu();
    return;
  }
  const firstSpace = v.indexOf(' ');
  const typed = firstSpace === -1 ? v.slice(1) : v.slice(1, firstSpace);
  if (firstSpace !== -1) {
    _slashMenuState.open = false;
    renderSlashMenu();
    return;
  }
  const t = typed.toLowerCase();
  const filtered = SLASH_COMMANDS.filter(c => c.cmd.startsWith(t));
  _slashMenuState.open = true;
  _slashMenuState.filtered = filtered;
  if (_slashMenuState.selected >= filtered.length) _slashMenuState.selected = 0;
  renderSlashMenu();
}

function pickSlashCommand() {
  if (!_slashMenuState.open || _slashMenuState.filtered.length === 0) return false;
  const cmd = _slashMenuState.filtered[_slashMenuState.selected];
  const input = document.getElementById('user-input');
  if (!input) return false;
  if (cmd.hasArgs) {
    input.value = `/${cmd.cmd} `;
    _slashMenuState.open = false;
    renderSlashMenu();
    input.focus();
    setTimeout(() => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 120) + 'px'; }, 0);
    return true;
  }
  input.value = `/${cmd.cmd}`;
  _slashMenuState.open = false;
  renderSlashMenu();
  send();
  return true;
}

function maybeExpandSlash(text) {
  if (!text.startsWith('/')) return { send: text };
  const firstSpace = text.indexOf(' ');
  const cmdName = (firstSpace === -1 ? text.slice(1) : text.slice(1, firstSpace)).toLowerCase();
  const args = firstSpace === -1 ? '' : text.slice(firstSpace + 1).trim();
  const cmd = SLASH_COMMANDS.find(c => c.cmd === cmdName);
  if (!cmd) return { send: text };
  const expanded = cmd.expand(args);
  if (expanded === '__HELP__') {
    const helpMd = [
      '### Available slash commands',
      '',
      '| Command | What it does |',
      '|---|---|',
      ...SLASH_COMMANDS.map(c => `| \`/${c.cmd}${c.args ? ' ' + c.args : ''}\` | ${c.desc} |`),
    ].join('\n');
    addMessage('assistant', helpMd);
    return { send: null };
  }
  if (expanded === '__CLEAR__') {
    document.getElementById('messages').innerHTML = '';
    conversationMessages = [];
    showToast('Conversation cleared');
    return { send: null };
  }
  return { send: expanded, displayed: text };
}

// ═══════ Send / Input ═══════
function send() {
  if (isRecording) stopVoice();
  const input = document.getElementById('user-input');
  const text = input.value.trim();
  if (!text || !ws) return;

  const expansion = maybeExpandSlash(text);
  input.value = '';
  input.style.height = 'auto';
  _slashMenuState.open = false;
  renderSlashMenu();

  if (expansion.send === null) return;

  cancelFirstMessageHint();
  removeSuggestions();
  addMessage('user', expansion.displayed || expansion.send);
  ws.send(JSON.stringify({ message: expansion.send }));
  showTyping();
  setAgentRunning(true);
}

// ═══════ First-message hint ═══════
let _firstMsgHintTimer = null;
function startFirstMessageHint() {
  cancelFirstMessageHint();
  const input = document.getElementById('user-input');
  if (!input) return;
  const onActivity = () => cancelFirstMessageHint();
  input.addEventListener('input', onActivity, { once: true });
  _firstMsgHintTimer = setTimeout(() => {
    const cards = document.getElementById('current-try-cards');
    if (cards) cards.classList.add('hint-pulse');
  }, 10000);
}
function cancelFirstMessageHint() {
  if (_firstMsgHintTimer) {
    clearTimeout(_firstMsgHintTimer);
    _firstMsgHintTimer = null;
  }
  const cards = document.getElementById('current-try-cards');
  if (cards) cards.classList.remove('hint-pulse');
}

function handleKey(e) {
  if (_slashMenuState.open && _slashMenuState.filtered.length) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      _slashMenuState.selected = (_slashMenuState.selected + 1) % _slashMenuState.filtered.length;
      renderSlashMenu();
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      _slashMenuState.selected = (_slashMenuState.selected - 1 + _slashMenuState.filtered.length) % _slashMenuState.filtered.length;
      renderSlashMenu();
      return;
    }
    if (e.key === 'Tab') {
      e.preventDefault();
      pickSlashCommand();
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      _slashMenuState.open = false;
      renderSlashMenu();
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      pickSlashCommand();
      return;
    }
  }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
  setTimeout(() => {
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
    updateSlashMenu();
  }, 0);
}

// ═══════ Quick Actions ═══════
function quickAction(text) {
  if (!ws) return;
  removeSuggestions();
  addMessage('user', text);
  ws.send(JSON.stringify({ message: text }));
  showTyping();
  setAgentRunning(true);
}

// ═══════ Suggestions ═══════
function addSuggestions(suggestions) {
  const container = document.getElementById('messages');
  const row = document.createElement('div');
  row.className = 'suggestions';
  row.id = 'current-suggestions';

  suggestions.forEach(text => {
    const chip = document.createElement('button');
    chip.className = 'suggestion-chip';
    chip.textContent = text;
    chip.onclick = () => {
      removeSuggestions();
      addMessage('user', text);
      ws.send(JSON.stringify({ message: text }));
      showTyping();
    };
    row.appendChild(chip);
  });

  container.appendChild(row);
  container.scrollTop = container.scrollHeight;
}

function removeSuggestions() {
  const el = document.getElementById('current-suggestions');
  if (el) el.remove();
  const tc = document.getElementById('current-try-cards');
  if (tc) tc.remove();
}

const TRY_CARD_ICONS = {
  compass: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/></svg>',
  alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  rows: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>',
  share: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="8.5" cy="7" r="4"/><path d="M20 8v6M23 11h-6"/></svg>',
  bolt: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
  chart: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>',
};

function addTryCards(cards) {
  const container = document.getElementById('messages');
  const wrap = document.createElement('div');
  wrap.className = 'try-cards-grid';
  wrap.id = 'current-try-cards';
  wrap.setAttribute('role', 'group');
  wrap.setAttribute('aria-label', 'Suggested actions');

  cards.forEach(card => {
    const btn = document.createElement('button');
    btn.className = 'try-card';
    btn.type = 'button';
    btn.setAttribute('aria-label', card.title + ': ' + card.desc);
    const iconSvg = TRY_CARD_ICONS[card.icon] || TRY_CARD_ICONS.bolt;
    btn.innerHTML =
      '<div class="try-card-icon">' + iconSvg + '</div>' +
      '<div class="try-card-body">' +
        '<div class="try-card-title">' + escapeHtml(card.title || '') + '</div>' +
        '<div class="try-card-desc">' + escapeHtml(card.desc || '') + '</div>' +
      '</div>';
    btn.onclick = () => {
      removeSuggestions();
      const text = card.prompt || card.title;
      addMessage('user', text);
      ws.send(JSON.stringify({ message: text }));
      showTyping();
    };
    wrap.appendChild(btn);
  });

  container.appendChild(wrap);
  container.scrollTop = container.scrollHeight;
}

// ═══════ Messages ═══════
// Post-process assistant bubble: syntax-highlight Smartsheet formulas + add CSV btn on tables
function enhanceAssistantBubble(bubble) {
  if (!bubble) return;
  try { highlightFormulasIn(bubble); } catch {}
  try { addCsvButtonsIn(bubble); } catch {}
  try { addSparklinesIn(bubble); } catch {}
}

function _parseNumeric(raw) {
  if (raw == null) return NaN;
  let s = String(raw).trim();
  if (!s) return NaN;
  s = s.replace(/[%$€£¥\s]/g, '');
  if (/,\d{1,2}$/.test(s) && !/\./.test(s)) s = s.replace(/\./g, '').replace(',', '.');
  else s = s.replace(/,/g, '');
  const n = parseFloat(s);
  return isNaN(n) ? NaN : n;
}

function _renderSparkSvg(values, opts) {
  const w = (opts && opts.w) || 80;
  const h = (opts && opts.h) || 22;
  const pad = 1.5;
  const min = Math.min.apply(null, values);
  const max = Math.max.apply(null, values);
  const range = max - min || 1;
  const step = (w - pad * 2) / (values.length - 1 || 1);
  const points = values.map((v, i) => {
    const x = pad + i * step;
    const y = h - pad - ((v - min) / range) * (h - pad * 2);
    return [x, y];
  });
  const path = points.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
  const last = points[points.length - 1];
  const trend = values[values.length - 1] >= values[0] ? 'up' : 'down';
  const color = trend === 'up' ? '#10b981' : '#f97316';
  return (
    `<svg class="spark-svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-label="trend ${trend}">` +
      `<path d="${path}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>` +
      `<circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="1.8" fill="${color}"/>` +
    `</svg>`
  );
}

function addSparklinesIn(root) {
  root.querySelectorAll('table').forEach(table => {
    if (table.dataset.sparkReady) return;
    table.dataset.sparkReady = '1';
    const head = table.querySelector('thead tr');
    const bodyRows = Array.from(table.querySelectorAll('tbody tr'));
    if (!head || bodyRows.length < 3) return;
    const headers = Array.from(head.children);
    const numCols = headers.length;
    let added = false;
    const summaryCells = [];
    for (let c = 0; c < numCols; c++) {
      const colVals = bodyRows.map(r => r.children[c]?.textContent || '').map(_parseNumeric).filter(v => !isNaN(v));
      if (colVals.length >= Math.max(3, Math.ceil(bodyRows.length * 0.7)) && colVals.length === bodyRows.length) {
        const min = Math.min.apply(null, colVals);
        const max = Math.max.apply(null, colVals);
        const sum = colVals.reduce((a, b) => a + b, 0);
        const avg = sum / colVals.length;
        const trend = colVals[colVals.length - 1] >= colVals[0] ? '▲' : '▼';
        const trendCls = colVals[colVals.length - 1] >= colVals[0] ? 'up' : 'down';
        const fmt = (v) => Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) : v.toLocaleString(undefined, { maximumFractionDigits: 2 });
        summaryCells[c] = (
          `<div class="spark-cell">` +
            _renderSparkSvg(colVals) +
            `<div class="spark-meta">` +
              `<span class="spark-trend ${trendCls}">${trend}</span> ` +
              `<span title="min">${fmt(min)}</span> · ` +
              `<span title="avg">${fmt(avg)}</span> · ` +
              `<span title="max">${fmt(max)}</span>` +
            `</div>` +
          `</div>`
        );
        added = true;
      } else {
        summaryCells[c] = '';
      }
    }
    if (!added) return;
    let tfoot = table.querySelector('tfoot');
    if (!tfoot) {
      tfoot = document.createElement('tfoot');
      table.appendChild(tfoot);
    }
    const tr = document.createElement('tr');
    tr.className = 'spark-row';
    for (let c = 0; c < numCols; c++) {
      const td = document.createElement('td');
      td.innerHTML = summaryCells[c];
      tr.appendChild(td);
    }
    tfoot.appendChild(tr);
  });
}

function highlightFormulasIn(root) {
  // Only touch inline <code> that looks like a Smartsheet formula (=FUNC(...))
  root.querySelectorAll('code').forEach(code => {
    if (code.dataset.shHl) return;
    const txt = code.textContent || '';
    if (!/^\s*=[A-Z_]+\s*\(/.test(txt)) return;
    code.dataset.shHl = '1';
    code.classList.add('sh-formula');
    code.innerHTML = txt
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/\b([A-Z][A-Z0-9_]+)(?=\s*\()/g, '<span class="sh-fn">$1</span>')
      .replace(/(\[[^\]]+\]@row|\[[^\]]+\]\d*|\{\{[^}]+\}\}|@row|@cell)/g, '<span class="sh-ref">$1</span>')
      .replace(/"([^"]*)"/g, '<span class="sh-str">"$1"</span>')
      .replace(/\b(\d+(?:\.\d+)?)\b/g, '<span class="sh-num">$1</span>');
  });
}

function addCsvButtonsIn(root) {
  root.querySelectorAll('table').forEach(table => {
    if (table.dataset.csvReady) return;
    table.dataset.csvReady = '1';
    const wrap = document.createElement('div');
    wrap.className = 'table-wrap';
    table.parentNode.insertBefore(wrap, table);
    wrap.appendChild(table);
    const btn = document.createElement('button');
    btn.className = 'table-csv-btn';
    btn.textContent = 'Download CSV';
    btn.onclick = (e) => { e.stopPropagation(); downloadTableAsCsv(table); };
    wrap.appendChild(btn);
  });
}

function downloadTableAsCsv(table) {
  const rows = Array.from(table.querySelectorAll('tr'));
  const lines = rows.map(tr =>
    Array.from(tr.querySelectorAll('th,td')).map(c => {
      const t = (c.textContent || '').replace(/\r?\n/g, ' ').trim();
      return /[",\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
    }).join(',')
  );
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'smartsheet-table-' + Date.now() + '.csv';
  a.click();
  URL.revokeObjectURL(url);
  showToast('CSV downloaded');
}

function addMessage(role, content) {
  const container = document.getElementById('messages');
  const group = document.createElement('div');
  group.className = 'msg-group ' + role;

  const label = document.createElement('div');
  label.className = 'msg-label';
  label.textContent = role === 'user' ? 'You' : 'Assistant';

  const bubble = document.createElement('div');
  bubble.className = 'msg ' + role;

  const ts = Date.now();
  group.dataset.ts = String(ts);

  if (role === 'assistant' && typeof marked !== 'undefined') {
    bubble.innerHTML = renderMarkdown(content);
    enhanceAssistantBubble(bubble);

    const copyBtn = document.createElement('button');
    copyBtn.className = 'msg-copy-btn';
    copyBtn.title = 'Copy';
    copyBtn.setAttribute('aria-label', 'Copy message');
    copyBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';
    copyBtn.onclick = (e) => { e.stopPropagation(); copyText(content, copyBtn); };
    bubble.appendChild(copyBtn);
    bubble.appendChild(createTtsButton(content));

    bubble.appendChild(createPinButton(content, ts));
  } else {
    bubble.textContent = content;
  }

  group.appendChild(label);
  group.appendChild(bubble);
  container.appendChild(group);
  container.scrollTop = container.scrollHeight;

  conversationMessages.push({ role, content, time: ts });
  saveCurrentConversation();
}

function addImageMessage(imageUrl, caption) {
  const container = document.getElementById('messages');
  const group = document.createElement('div');
  group.className = 'msg-group assistant';

  const label = document.createElement('div');
  label.className = 'msg-label';
  label.textContent = 'Assistant';

  const bubble = document.createElement('div');
  bubble.className = 'msg assistant';

  const img = document.createElement('img');
  img.src = imageUrl;
  img.className = 'chat-image';
  img.alt = caption || 'Generated image';
  bubble.appendChild(img);

  if (caption) {
    const cap = document.createElement('div');
    cap.className = 'image-caption';
    cap.textContent = caption;
    bubble.appendChild(cap);
  }

  group.appendChild(label);
  group.appendChild(bubble);
  container.appendChild(group);
  container.scrollTop = container.scrollHeight;

  conversationMessages.push({ role: 'assistant', content: '[Image] ' + (caption || ''), time: Date.now() });
  saveCurrentConversation();
}

function addToolMessage(header, body) {
  const container = document.getElementById('messages');
  const group = document.createElement('div');
  group.className = 'msg-group tool';

  const bubble = document.createElement('div');
  bubble.className = 'msg tool';
  bubble.innerHTML =
    '<div class="tool-header">' + escapeHtml(header) + '</div>' +
    '<div class="tool-body">' + escapeHtml(body) + '</div>';

  group.appendChild(bubble);
  container.appendChild(group);
  container.scrollTop = container.scrollHeight;
}

// ═══════ Typing Indicator ═══════
function showTyping() {
  removeTyping();
  const container = document.getElementById('messages');
  const el = document.createElement('div');
  el.className = 'typing-indicator';
  el.id = 'typing';
  el.innerHTML =
    '<div class="typing-dots"><span></span><span></span><span></span></div>' +
    '<span class="typing-text">Thinking...</span>';
  container.appendChild(el);
  container.scrollTop = container.scrollHeight;
}

function removeTyping() {
  const el = document.getElementById('typing');
  if (el) el.remove();
}

function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    btn.classList.add('copied');
    btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
    setTimeout(() => {
      btn.classList.remove('copied');
      btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';
    }, 1500);
  });
}

// ═══════ Cancel / Stop ═══════
function cancelRequest() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'cancel' }));
  }
  removeTyping();
  removeSuggestions();
  if (streamingGroup) {
    streamingContent += '\n\n*[Interrupted]*';
    if (streamingBubble && typeof marked !== 'undefined') {
      streamingBubble.innerHTML = renderMarkdown(streamingContent);
    }
    conversationMessages.push({ role: 'assistant', content: streamingContent, time: Date.now() });
    saveCurrentConversation();
    streamingGroup = null;
    streamingBubble = null;
    streamingContent = '';
  }
  setAgentRunning(false);
}

function setAgentRunning(running) {
  isAgentRunning = running;
  const sendBtn = document.getElementById('send-btn');
  const stopBtn = document.getElementById('stop-btn');
  if (running) {
    sendBtn.style.display = 'none';
    stopBtn.style.display = 'flex';
  } else {
    sendBtn.style.display = 'flex';
    stopBtn.style.display = 'none';
  }
}

// ═══════ Model Selector ═══════
function populateModelSelector(currentProvider, currentModel) {
  const selector = document.getElementById('model-selector');
  const select = document.getElementById('model-select');
  if (!Object.keys(availableProviders).length) return;

  selector.style.display = 'block';
  select.innerHTML = '';

  for (const [provider, info] of Object.entries(availableProviders)) {
    const group = document.createElement('optgroup');
    group.label = provider.charAt(0).toUpperCase() + provider.slice(1);
    (info.models || []).forEach(m => {
      const opt = document.createElement('option');
      opt.value = provider + '::' + m;
      opt.textContent = m;
      if (provider === currentProvider && m === currentModel) opt.selected = true;
      group.appendChild(opt);
    });
    select.appendChild(group);
  }
}

async function switchModel() {
  const select = document.getElementById('model-select');
  const val = select.value;
  if (!val || !sessionId) return;

  const [provider, model] = val.split('::');
  showToast('Switching to ' + model + '...');

  try {
    const res = await fetch('/api/switch-model', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, provider, model }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Switch failed');
    showToast('Model: ' + data.model);
  } catch (e) {
    showToast('Error: ' + e.message);
  }
}

// ═══════ History (localStorage) ═══════
function getHistoryKey() {
  return 'ss_ctrl_history';
}

function loadHistory() {
  try {
    return JSON.parse(localStorage.getItem(getHistoryKey()) || '[]');
  } catch { return []; }
}

function saveHistory(history) {
  try {
    const trimmed = history.slice(0, 30);
    localStorage.setItem(getHistoryKey(), JSON.stringify(trimmed));
  } catch {}
}

function saveCurrentConversation() {
  if (!currentConversationId || conversationMessages.length === 0) return;

  const history = loadHistory();
  const existing = history.findIndex(h => h.id === currentConversationId);

  // Preserve existing title if it was auto-generated by LLM
  let title = null;
  if (existing >= 0 && history[existing].titleAuto) {
    title = history[existing].title;
  }
  if (!title) {
    const firstUserMsg = conversationMessages.find(m => m.role === 'user');
    title = firstUserMsg
      ? firstUserMsg.content.substring(0, 60) + (firstUserMsg.content.length > 60 ? '...' : '')
      : 'New conversation';
  }

  const entry = {
    id: currentConversationId,
    title,
    titleAuto: existing >= 0 ? !!history[existing].titleAuto : false,
    messages: conversationMessages,
    updatedAt: Date.now(),
  };

  if (existing >= 0) {
    history[existing] = entry;
  } else {
    history.unshift(entry);
  }

  saveHistory(history);

  // Trigger LLM title generation once after 3+ messages
  if (!entry.titleAuto && conversationMessages.length >= 3) {
    maybeGenerateTitle(currentConversationId);
  }
}

// ═══════ Auto-Titles (LLM-generated) ═══════
let _titleInFlight = new Set();
async function maybeGenerateTitle(convId) {
  if (_titleInFlight.has(convId)) return;
  _titleInFlight.add(convId);
  try {
    if (!sessionId) return;
    const snippet = conversationMessages
      .filter(m => m.role === 'user' || m.role === 'assistant')
      .slice(0, 6)
      .map(m => m.role + ': ' + m.content.substring(0, 240))
      .join('\n');
    const res = await fetch('/api/generate-title', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, snippet }),
    });
    if (!res.ok) return;
    const data = await res.json();
    const title = (data && data.title || '').trim();
    if (!title) return;
    const history = loadHistory();
    const idx = history.findIndex(h => h.id === convId);
    if (idx < 0) return;
    history[idx].title = title.substring(0, 80);
    history[idx].titleAuto = true;
    saveHistory(history);
    renderHistoryList();
  } catch {
    // Silent fail; heuristic title stays
  } finally {
    _titleInFlight.delete(convId);
  }
}

function renderHistoryList() {
  const list = document.getElementById('history-list');
  const searchEl = document.getElementById('history-search');
  const q = (searchEl && searchEl.value || '').trim().toLowerCase();
  let history = loadHistory();

  if (q) {
    history = history.filter(conv => {
      if ((conv.title || '').toLowerCase().includes(q)) return true;
      return (conv.messages || []).some(m => (m.content || '').toLowerCase().includes(q));
    });
  }

  if (history.length === 0) {
    list.innerHTML = '<div class="history-empty">' + (q ? 'No matches' : 'No conversations yet') + '</div>';
    return;
  }

  list.innerHTML = '';
  history.forEach(conv => {
    const item = document.createElement('div');
    item.className = 'history-item' + (conv.id === currentConversationId ? ' active' : '');

    const ago = timeAgo(conv.updatedAt);
    item.innerHTML = escapeHtml(conv.title) + '<span class="history-time">' + ago + '</span>';

    item.onclick = () => loadConversation(conv.id);
    list.appendChild(item);
  });
}

function filterHistory() {
  renderHistoryList();
}

function loadConversation(convId) {
  const history = loadHistory();
  const conv = history.find(h => h.id === convId);
  if (!conv) return;

  currentConversationId = convId;
  conversationMessages = conv.messages || [];

  const container = document.getElementById('messages');
  container.innerHTML = '';

  conversationMessages.forEach(m => {
    if (m.role === 'user' || m.role === 'assistant') {
      const group = document.createElement('div');
      group.className = 'msg-group ' + m.role;
      const ts = m.time || Date.now();
      group.dataset.ts = String(ts);

      const label = document.createElement('div');
      label.className = 'msg-label';
      label.textContent = m.role === 'user' ? 'You' : 'Assistant';

      const bubble = document.createElement('div');
      bubble.className = 'msg ' + m.role;

      if (m.role === 'assistant' && typeof marked !== 'undefined') {
        bubble.innerHTML = renderMarkdown(m.content);
        enhanceAssistantBubble(bubble);

        // Parity with live: copy + pin buttons
        const copyBtn = document.createElement('button');
        copyBtn.className = 'msg-copy-btn';
        copyBtn.title = 'Copy';
        copyBtn.setAttribute('aria-label', 'Copy message');
        copyBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';
        copyBtn.onclick = (e) => { e.stopPropagation(); copyText(m.content, copyBtn); };
        bubble.appendChild(copyBtn);
        bubble.appendChild(createTtsButton(m.content));

        bubble.appendChild(createPinButton(m.content, ts));
      } else {
        bubble.textContent = m.content;
      }

      group.appendChild(label);
      group.appendChild(bubble);
      container.appendChild(group);
    }
  });

  container.scrollTop = container.scrollHeight;
  renderHistoryList();
}

function newConversation() {
  saveCurrentConversation();
  currentConversationId = 'conv-' + Date.now();
  conversationMessages = [];
  document.getElementById('messages').innerHTML = '';
  renderHistoryList();
  document.getElementById('user-input').focus();
}

function toggleMobileHeaderMenu() {
  const header = document.querySelector('header');
  const btn = document.getElementById('btn-burger');
  if (!header) return;
  header.classList.toggle('mobile-menu-open');
  if (btn) btn.setAttribute('aria-expanded', header.classList.contains('mobile-menu-open') ? 'true' : 'false');
}

function toggleHistory() {
  const sidebar = document.getElementById('history-sidebar');
  const isMobile = window.matchMedia('(max-width: 900px)').matches;
  const btn = document.getElementById('btn-history-toggle');

  if (isMobile) {
    let backdrop = document.getElementById('mobile-drawer-backdrop');
    const isOpen = sidebar.classList.contains('mobile-open');
    if (isOpen) {
      sidebar.classList.remove('mobile-open');
      if (backdrop) backdrop.remove();
      if (btn) btn.setAttribute('aria-expanded', 'false');
    } else {
      sidebar.classList.add('mobile-open');
      if (!backdrop) {
        backdrop = document.createElement('div');
        backdrop.id = 'mobile-drawer-backdrop';
        backdrop.className = 'mobile-drawer-backdrop';
        backdrop.onclick = () => toggleHistory();
        document.body.appendChild(backdrop);
        backdrop.style.display = 'block';
      }
      if (btn) btn.setAttribute('aria-expanded', 'true');
    }
  } else {
    sidebar.classList.toggle('collapsed');
    if (btn) btn.setAttribute('aria-expanded', sidebar.classList.contains('collapsed') ? 'false' : 'true');
  }
  renderHistoryList();
}

function timeAgo(ts) {
  const diff = Date.now() - ts;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'Just now';
  if (mins < 60) return mins + 'm ago';
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours + 'h ago';
  const days = Math.floor(hours / 24);
  return days + 'd ago';
}

// ═══════ Export Conversation ═══════
function exportConversation() {
  if (conversationMessages.length === 0) {
    showToast('No messages to export');
    return;
  }

  let md = '# Smartsheet Controller — Conversation\n';
  md += '_Exported: ' + new Date().toLocaleString() + '_\n\n---\n\n';

  conversationMessages.forEach(m => {
    if (m.role === 'user') {
      md += '**You:**\n' + m.content + '\n\n';
    } else if (m.role === 'assistant') {
      md += '**Assistant:**\n' + m.content + '\n\n';
    }
  });

  const blob = new Blob([md], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'smartsheet-conversation-' + new Date().toISOString().slice(0,10) + '.md';
  a.click();
  URL.revokeObjectURL(url);

  showToast('Conversation exported!');
}

// ═══════ Voice Dictation (Web Speech API) ═══════
let recognition = null;
let isRecording = false;

function initVoice() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    document.getElementById('mic-btn').classList.add('mic-unsupported');
    return;
  }

  recognition = new SpeechRecognition();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = navigator.language || 'fr-FR';

  let finalTranscript = '';

  recognition.onresult = (event) => {
    let interim = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const t = event.results[i][0].transcript;
      if (event.results[i].isFinal) {
        finalTranscript += t + ' ';
      } else {
        interim += t;
      }
    }
    const input = document.getElementById('user-input');
    input.value = finalTranscript + interim;
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
  };

  recognition.onend = () => {
    if (isRecording) {
      recognition.start();
    } else {
      finalTranscript = '';
    }
  };

  recognition.onerror = (event) => {
    if (event.error !== 'no-speech' && event.error !== 'aborted') {
      console.error('Speech recognition error:', event.error);
      stopVoice();
    }
  };
}

function toggleVoice() {
  if (!recognition) return;
  if (isRecording) stopVoice();
  else startVoice();
}

function startVoice() {
  if (!recognition) return;
  isRecording = true;
  document.getElementById('mic-btn').classList.add('recording');
  document.getElementById('voice-indicator').classList.add('active');
  document.getElementById('user-input').placeholder = 'Listening... speak now';
  try { recognition.start(); } catch (e) {}
}

function stopVoice() {
  isRecording = false;
  document.getElementById('mic-btn').classList.remove('recording');
  document.getElementById('voice-indicator').classList.remove('active');
  document.getElementById('user-input').placeholder = 'Type or use the mic to speak...';
  if (recognition) { try { recognition.stop(); } catch (e) {} }
}

// ═══════ Confirmation UI ═══════
function _renderDiffBody(diff, fallbackArgs) {
  if (!diff || !diff.kind) {
    return '<div class="confirm-card-body">' + escapeHtml(JSON.stringify(fallbackArgs, null, 2)) + '</div>';
  }
  const total = diff.total || (diff.items || []).length;
  const shown = (diff.items || []).length;
  const verb = ({ update_rows: 'Updating', delete_rows: 'Deleting', add_rows: 'Adding' })[diff.kind] || 'Changing';
  const summary = `<div class="diff-summary">${verb} <strong>${total}</strong> row${total === 1 ? '' : 's'}${shown < total ? ` <span class="diff-truncated">(showing first ${shown})</span>` : ''}</div>`;

  if (diff.kind === 'update_rows') {
    const rowsHtml = diff.items.map(it => {
      if (!it.changes || it.changes.length === 0) {
        return `<div class="diff-row"><div class="diff-row-head">Row ${it.rowId}</div><div class="diff-empty">No detectable changes</div></div>`;
      }
      const cells = it.changes.map(ch => `
        <tr>
          <td class="diff-col">${escapeHtml(ch.column)}</td>
          <td class="diff-old"><span class="diff-strike">${escapeHtml(String(ch.old == null ? '∅' : ch.old))}</span></td>
          <td class="diff-arrow">→</td>
          <td class="diff-new">${escapeHtml(String(ch.new == null ? '∅' : ch.new))}</td>
        </tr>
      `).join('');
      return `<div class="diff-row"><div class="diff-row-head">Row ${it.rowId} <span class="diff-cell-count">· ${it.changes.length} cell${it.changes.length === 1 ? '' : 's'}</span></div><table class="diff-table">${cells}</table></div>`;
    }).join('');
    return `<div class="confirm-card-body diff-body">${summary}${rowsHtml}</div>`;
  }

  if (diff.kind === 'delete_rows') {
    const rowsHtml = diff.items.map(it => {
      if (it.error) return `<div class="diff-row deleting"><div class="diff-row-head">Row ${it.rowId}</div><div class="diff-empty">(${escapeHtml(it.error)})</div></div>`;
      const cells = (it.cells || []).map(c => `<span class="diff-pill"><span class="diff-pill-key">${escapeHtml(c.column)}:</span> ${escapeHtml(String(c.value))}</span>`).join(' ');
      return `<div class="diff-row deleting"><div class="diff-row-head">Row ${it.rowId}</div><div class="diff-cells">${cells || '<span class="diff-empty">empty row</span>'}</div></div>`;
    }).join('');
    return `<div class="confirm-card-body diff-body danger">${summary}${rowsHtml}</div>`;
  }

  if (diff.kind === 'add_rows') {
    const rowsHtml = diff.items.map((it, i) => {
      const cells = (it.cells || []).map(c => `<span class="diff-pill adding"><span class="diff-pill-key">${escapeHtml(c.column)}:</span> ${escapeHtml(String(c.value == null ? '' : c.value))}</span>`).join(' ');
      return `<div class="diff-row adding"><div class="diff-row-head">New row ${i + 1}</div><div class="diff-cells">${cells || '<span class="diff-empty">empty row</span>'}</div></div>`;
    }).join('');
    return `<div class="confirm-card-body diff-body adding">${summary}${rowsHtml}</div>`;
  }

  return '<div class="confirm-card-body">' + escapeHtml(JSON.stringify(fallbackArgs, null, 2)) + '</div>';
}

function addConfirmCard(event) {
  const container = document.getElementById('messages');
  removeTyping();

  const group = document.createElement('div');
  group.className = 'msg-group assistant';
  group.id = 'confirm-' + event.tool_call_id;

  const card = document.createElement('div');
  card.className = 'confirm-card';
  if (event.tool === 'delete_rows' || event.tool === 'delete_sheet' || event.tool === 'delete_column') {
    card.classList.add('danger');
  }

  const body = _renderDiffBody(event.diff, event.arguments);
  const showRaw = !event.diff;

  card.innerHTML =
    '<div class="confirm-card-header">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' +
      'Confirm: <strong>' + escapeHtml(event.tool) + '</strong>' +
    '</div>' +
    body +
    (showRaw ? '' : '<details class="diff-raw"><summary>Show raw arguments</summary><pre>' + escapeHtml(JSON.stringify(event.arguments, null, 2)) + '</pre></details>') +
    '<div class="confirm-card-actions">' +
      '<button class="btn-approve" onclick="respondConfirm(\'' + event.tool_call_id + '\', true)">Approve</button>' +
      '<button class="btn-reject" onclick="respondConfirm(\'' + event.tool_call_id + '\', false)">Reject</button>' +
    '</div>';

  group.appendChild(card);
  container.appendChild(group);
  container.scrollTop = container.scrollHeight;
}

function respondConfirm(toolCallId, approved) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: approved ? 'confirm' : 'reject', tool_call_id: toolCallId }));

  const card = document.getElementById('confirm-' + toolCallId);
  if (card) {
    const actions = card.querySelector('.confirm-card-actions');
    if (actions) {
      actions.innerHTML = approved
        ? '<span style="color:var(--success);font-size:12px;font-weight:600">Approved</span>'
        : '<span style="color:var(--error);font-size:12px;font-weight:600">Rejected</span>';
    }
  }
  if (approved) showTyping();
}

// ═══════ Chart Rendering ═══════
function addChart(spec) {
  const container = document.getElementById('messages');
  const group = document.createElement('div');
  group.className = 'msg-group assistant';

  const wrapper = document.createElement('div');
  wrapper.className = 'chart-container';

  const canvas = document.createElement('canvas');
  wrapper.appendChild(canvas);
  group.appendChild(wrapper);
  container.appendChild(group);
  container.scrollTop = container.scrollHeight;

  try {
    new Chart(canvas, spec);
  } catch (e) {
    wrapper.textContent = 'Chart render error: ' + e.message;
  }
}

// ═══════ Favorites / Recent Sheets ═══════
function getFavorites() {
  try { return JSON.parse(localStorage.getItem('ss_ctrl_sheets') || '[]'); }
  catch { return []; }
}

function saveFavorites(list) {
  try { localStorage.setItem('ss_ctrl_sheets', JSON.stringify(list.slice(0, 20))); }
  catch {}
}

function addToFavorites(sheetId, sheetName, starred) {
  const favs = getFavorites();
  const idx = favs.findIndex(f => String(f.id) === String(sheetId));
  const entry = {
    id: sheetId,
    name: sheetName,
    last_used: Date.now(),
    starred: starred || false,
  };
  if (idx >= 0) {
    entry.starred = starred !== undefined ? starred : favs[idx].starred;
    favs[idx] = entry;
  } else {
    favs.unshift(entry);
  }
  favs.sort((a, b) => (b.starred ? 1 : 0) - (a.starred ? 1 : 0) || b.last_used - a.last_used);
  saveFavorites(favs);
}

function toggleFavStar(sheetId) {
  const favs = getFavorites();
  const item = favs.find(f => String(f.id) === String(sheetId));
  if (item) { item.starred = !item.starred; saveFavorites(favs); renderFavorites(); }
}

function renderFavorites() {
  const section = document.getElementById('favorites-section');
  const grid = document.getElementById('favorites-grid');
  if (!section || !grid) return;

  const favs = getFavorites();
  if (favs.length === 0) { section.style.display = 'none'; return; }

  section.style.display = 'block';
  grid.innerHTML = '';

  favs.forEach(f => {
    const card = document.createElement('div');
    card.className = 'fav-card';
    card.onclick = () => quickConnectSheet(f.id);

    card.innerHTML =
      '<div class="fav-card-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 9h18"/><path d="M9 3v18"/></svg></div>' +
      '<div class="fav-card-info"><div class="fav-card-name">' + escapeHtml(f.name) + '</div>' +
      '<div class="fav-card-meta">' + timeAgo(f.last_used) + '</div></div>' +
      '<button class="fav-star' + (f.starred ? ' active' : '') + '" onclick="event.stopPropagation();toggleFavStar(\'' + f.id + '\')">' +
      '<svg viewBox="0 0 24 24" fill="' + (f.starred ? 'currentColor' : 'none') + '" stroke="currentColor" stroke-width="2"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26"/></svg>' +
      '</button>';

    grid.appendChild(card);
  });
}

async function quickConnectSheet(sheetId) {
  showToast('Connecting...');
  try {
    const res = await fetch('/api/quick-connect', { method: 'POST' });
    if (!res.ok) throw new Error('Connection failed');
    const data = await res.json();
    openChat(data);
    if (String(data.sheet.id) !== String(sheetId)) {
      await switchSheet(sheetId);
    }
  } catch (e) {
    showToast('Error: ' + e.message);
  }
}

// ═══════ Export PDF ═══════
function exportPDF() {
  if (conversationMessages.length === 0) {
    showToast('No messages to export');
    return;
  }
  window.print();
}

// ═══════ Keyboard Shortcuts ═══════
function initKeyboardShortcuts() {
  document.addEventListener('keydown', (e) => {
    const modal = document.getElementById('shortcuts-modal');
    const isModalOpen = modal && !modal.classList.contains('hidden');

    if (e.key === 'Escape') {
      if (_ttsCurrent) {
        try { window.speechSynthesis.cancel(); } catch {}
        _ttsCurrent.btn?.classList.remove('playing');
        _ttsCurrent = null;
        return;
      }
      const csvOverlay = document.getElementById('csv-preview-overlay');
      if (csvOverlay && !csvOverlay.classList.contains('hidden')) { closeCsvPreview(); return; }
      const tplOverlay = document.getElementById('template-editor-overlay');
      if (tplOverlay && !tplOverlay.classList.contains('hidden')) { closeTemplateEditor(); return; }
      if (isModalOpen) { toggleShortcutsModal(); return; }
      const helpModal = document.getElementById('help-modal');
      if (helpModal && !helpModal.classList.contains('hidden')) { closeHelpModal(); return; }
      const bugModal = document.getElementById('bug-report-modal');
      if (bugModal && !bugModal.classList.contains('hidden')) { closeBugReportModal(); return; }
      const settingsModal = document.getElementById('settings-modal');
      if (settingsModal && !settingsModal.classList.contains('hidden')) { toggleSettingsModal(); return; }
      const tourPop = document.getElementById('tour-popover');
      if (tourPop && tourPop.style.display !== 'none' && tourPop.style.display !== '') { endTour(); return; }
      if (isAgentRunning) { cancelRequest(); return; }
      return;
    }

    if (e.key === '?' && !e.ctrlKey && !e.metaKey && !isInputFocused()) {
      e.preventDefault();
      toggleShortcutsModal();
      return;
    }

    const ctrl = e.ctrlKey || e.metaKey;

    if (ctrl && e.key === 'k') {
      e.preventDefault();
      const input = document.getElementById('user-input');
      if (input) input.focus();
      return;
    }

    if (ctrl && e.shiftKey) {
      switch (e.key.toUpperCase()) {
        case 'N': e.preventDefault(); newConversation(); break;
        case 'E': e.preventDefault(); exportConversation(); break;
        case 'P': e.preventDefault(); exportPDF(); break;
        case 'H': e.preventDefault(); toggleHistory(); break;
        case 'B': e.preventDefault(); openBugReportModal(); break;
        case 'L': e.preventDefault(); openHelpModal(); break;
        case 'K': e.preventDefault(); togglePromptSidebar(); break;
        case 'Q': {
          e.preventDefault();
          const bl = document.getElementById('btn-logout');
          if (bl && bl.style.display !== 'none') disconnectSession();
          break;
        }
      }
    }
  });
}

function isInputFocused() {
  const el = document.activeElement;
  return el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT');
}

function toggleShortcutsModal() {
  const modal = document.getElementById('shortcuts-modal');
  if (!modal) return;
  const willOpen = modal.classList.contains('hidden');
  modal.classList.toggle('hidden');
  if (willOpen) trapFocus(modal); else releaseFocus('shortcuts-modal');
}

// ═══════ Bug Report Modal ═══════
function _safe(get) { try { return get(); } catch { return null; } }

function collectBugContext() {
  // Lightweight, JSON-safe snapshot of useful client state.
  const lastMessages = (() => {
    try {
      const arr = Array.isArray(conversationMessages) ? conversationMessages : [];
      return arr.slice(-6).map(m => ({
        role: m && m.role,
        content: typeof m?.content === 'string' ? m.content.slice(0, 1200) : null,
        ts: m && m.ts,
      }));
    } catch { return []; }
  })();

  return {
    app: 'smartsheet-controller',
    captured_at: new Date().toISOString(),
    page_url: location.href,
    user_agent: navigator.userAgent,
    language: navigator.language,
    viewport: { w: window.innerWidth, h: window.innerHeight },
    session_id: _safe(() => sessionId) || null,
    sheet_id: _safe(() => currentSheetId) || null,
    conversation_id: _safe(() => currentConversationId) || null,
    user: _safe(() => (currentUser ? {
      email: currentUser.email,
      first_name: currentUser.firstName || currentUser.first_name,
      last_name: currentUser.lastName || currentUser.last_name,
    } : null)),
    last_messages: lastMessages,
    is_agent_running: _safe(() => !!isAgentRunning),
    ws_state: _safe(() => (ws ? ws.readyState : null)),
  };
}

function openBugReportModal() {
  const modal = document.getElementById('bug-report-modal');
  if (!modal) return;
  // Reset form state every open
  const form = document.getElementById('bug-report-form');
  if (form) form.reset();
  const desc = document.getElementById('bug-description');
  const counter = document.getElementById('bug-desc-counter');
  if (desc && counter) {
    counter.textContent = '0 / 8000';
    desc.oninput = () => { counter.textContent = `${desc.value.length} / 8000`; };
  }
  const fb = document.getElementById('bug-feedback');
  if (fb) { fb.textContent = ''; fb.className = 'bug-feedback'; }
  // Refresh & show context preview
  const ctx = collectBugContext();
  const pre = document.getElementById('bug-context-preview');
  if (pre) pre.textContent = JSON.stringify(ctx, null, 2);
  // Stash on dataset so submit reads the EXACT same snapshot
  modal.dataset.bugContext = JSON.stringify(ctx);
  modal.classList.remove('hidden');
  trapFocus(modal);
  setTimeout(() => { if (desc) desc.focus(); }, 50);
}

function closeBugReportModal() {
  const modal = document.getElementById('bug-report-modal');
  if (!modal) return;
  modal.classList.add('hidden');
  releaseFocus('bug-report-modal');
}

async function submitBugReport() {
  const modal = document.getElementById('bug-report-modal');
  const desc = document.getElementById('bug-description');
  const steps = document.getElementById('bug-steps');
  const btn = document.getElementById('bug-submit-btn');
  const fb = document.getElementById('bug-feedback');
  const sevEl = document.querySelector('input[name="bug-severity"]:checked');
  if (!desc || !btn || !fb) return;

  const description = (desc.value || '').trim();
  if (!description) {
    fb.textContent = 'Please describe what happened.';
    fb.className = 'bug-feedback is-error';
    desc.focus();
    return;
  }

  let context = {};
  try { context = JSON.parse(modal.dataset.bugContext || '{}'); } catch {}

  const payload = {
    description,
    steps: steps && steps.value ? steps.value.trim() : null,
    severity: sevEl ? sevEl.value : 'normal',
    session_id: _safe(() => sessionId) || null,
    reporter_email: _safe(() => currentUser && currentUser.email) || null,
    reporter_name: _safe(() => currentUser
      ? [currentUser.firstName || currentUser.first_name,
         currentUser.lastName || currentUser.last_name]
        .filter(Boolean).join(' ').trim() || null
      : null),
    context,
  };

  btn.disabled = true;
  const lbl = btn.querySelector('.bug-submit-label');
  const spn = btn.querySelector('.bug-submit-spinner');
  if (lbl) lbl.hidden = true;
  if (spn) spn.hidden = false;
  fb.textContent = '';
  fb.className = 'bug-feedback';

  try {
    const res = await fetch('/api/bug-reports', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    fb.textContent = `Thanks! Report #${data.id} sent — we'll take a look.`;
    fb.className = 'bug-feedback is-success';
    if (typeof showToast === 'function') showToast('Bug report sent — thank you!', 2400);
    setTimeout(() => closeBugReportModal(), 1200);
  } catch (err) {
    fb.textContent = `Could not send the report: ${err.message || err}. Please retry.`;
    fb.className = 'bug-feedback is-error';
  } finally {
    btn.disabled = false;
    if (lbl) lbl.hidden = false;
    if (spn) spn.hidden = true;
  }
}

// ═══════ Help / Prompts library Modal ═══════
//
// Loads `frontend/data/prompts.json` via `/api/prompts`, renders
// collapsible categories with prompt cards, and supports live
// search + copy-to-clipboard + insert-into-chat. The catalogue is
// cached in-memory after the first load so repeated openings stay
// snappy. Failure to fetch falls back to an inline error message
// rather than breaking the rest of the UI.

let _helpCatalogue = null;
let _helpLoaded = false;

const HELP_ICON_SVG = {
  search:  '<circle cx="11" cy="11" r="8"/><path d="M21 21l-4.3-4.3"/>',
  rows:    '<line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>',
  columns: '<line x1="6" y1="3" x2="6" y2="21"/><line x1="12" y1="3" x2="12" y2="21"/><line x1="18" y1="3" x2="18" y2="21"/>',
  link:    '<path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71"/>',
  sigma:   '<path d="M18 4H6l7 8-7 8h12"/>',
  users:   '<path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/>',
  bolt:    '<path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>',
  wrench:  '<path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94L9 17.59a2 2 0 11-2.83-2.83l5.12-5.12a6 6 0 017.94-7.94l-3.76 3.76z"/>',
  tree:    '<circle cx="12" cy="4" r="2"/><circle cx="5" cy="20" r="2"/><circle cx="19" cy="20" r="2"/><path d="M12 6v6"/><path d="M5 18v-2a4 4 0 014-4h6a4 4 0 014 4v2"/>',
  chat:    '<path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>',
  paperclip: '<path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/>',
  chart:   '<line x1="6" y1="20" x2="6" y2="14"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="18" y1="20" x2="18" y2="10"/>',
  folder:  '<path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/>',
};

function _helpIcon(name, size = 16) {
  const inner = HELP_ICON_SVG[name] || HELP_ICON_SVG.search;
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
}

function _escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

async function loadHelpCatalogue(force = false) {
  if (_helpLoaded && !force && _helpCatalogue) return _helpCatalogue;
  try {
    const res = await fetch('/api/prompts', { headers: { 'Accept': 'application/json' } });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _helpCatalogue = await res.json();
    _helpLoaded = true;
    return _helpCatalogue;
  } catch (err) {
    _helpCatalogue = { _error: err.message || String(err) };
    _helpLoaded = true;
    return _helpCatalogue;
  }
}

function renderHelpCatalogue(catalogue) {
  const intro = document.getElementById('help-modal-intro');
  const body  = document.getElementById('help-modal-body');
  if (!body) return;

  if (catalogue && catalogue._error) {
    if (intro) intro.textContent = '';
    body.innerHTML = `<div class="help-error">Could not load the prompt catalogue: ${_escapeHtml(catalogue._error)}.</div>`;
    return;
  }
  if (!catalogue || !Array.isArray(catalogue.categories)) {
    body.innerHTML = `<div class="help-error">Catalogue malformed.</div>`;
    return;
  }

  if (intro && catalogue.intro) intro.textContent = catalogue.intro;

  const totalPrompts = catalogue.categories.reduce(
    (sum, c) => sum + (Array.isArray(c.prompts) ? c.prompts.length : 0), 0
  );

  const html = catalogue.categories.map((cat, idx) => {
    const prompts = Array.isArray(cat.prompts) ? cat.prompts : [];
    const cards = prompts.map(p => _renderPromptCard(p, cat)).join('');
    const openCls = idx === 0 ? ' is-open' : '';
    return `
      <section class="help-category${openCls}" data-category-id="${_escapeHtml(cat.id)}">
        <button type="button" class="help-category-head" onclick="toggleHelpCategory(this)" aria-expanded="${idx === 0 ? 'true' : 'false'}">
          <span class="help-category-icon">${_helpIcon(cat.icon || 'search', 18)}</span>
          <span class="help-category-meta">
            <span class="help-category-title">${_escapeHtml(cat.title)}</span>
            <span class="help-category-desc">${_escapeHtml(cat.description || '')}</span>
          </span>
          <span class="help-category-count">${prompts.length}</span>
          <svg class="help-chevron" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 6 15 12 9 18"/></svg>
        </button>
        <div class="help-category-body">${cards || '<div class="help-empty">No prompts in this category yet.</div>'}</div>
      </section>
    `;
  }).join('');

  body.innerHTML = `
    <div style="font-size:11.5px;color:var(--text-muted);padding:6px 4px 2px;">
      ${totalPrompts} prompts across ${catalogue.categories.length} categories. Click a header to fold.
    </div>
    ${html}
  `;
}

function _renderPromptCard(p, cat) {
  if (!p || !p.prompt) return '';
  const id    = _escapeHtml(p.id || '');
  const title = _escapeHtml(p.title || '(untitled)');
  const desc  = _escapeHtml(p.description || '');
  const diff  = (p.difficulty || '').toLowerCase();
  const risk  = (p.risk || '').toLowerCase();
  const tags  = Array.isArray(p.tags) ? p.tags : [];

  const badges = [];
  if (diff) badges.push(`<span class="help-badge help-badge--${_escapeHtml(diff)}" title="Difficulty">${_escapeHtml(diff)}</span>`);
  if (risk) badges.push(`<span class="help-badge help-badge--${_escapeHtml(risk)}" title="Risk">${_escapeHtml(risk)}</span>`);
  const tagsHtml = tags.length
    ? `<div class="help-tags">${tags.map(t => `<span class="help-tag">${_escapeHtml(t)}</span>`).join('')}</div>`
    : '';

  // Stash full prompt text on the dataset to avoid quoting hell in onclick.
  const safePrompt = _escapeHtml(p.prompt);
  const haystack = _escapeHtml(
    [title, desc, p.id, cat && cat.title, cat && cat.id, ...tags].filter(Boolean).join(' ').toLowerCase()
  );

  return `
    <div class="help-prompt" data-prompt-id="${id}" data-haystack="${haystack}">
      <div class="help-prompt-head">
        <h4 class="help-prompt-title">${title}</h4>
        <div class="help-prompt-badges">${badges.join('')}</div>
      </div>
      ${desc ? `<p class="help-prompt-desc">${desc}</p>` : ''}
      <pre class="help-prompt-pre" data-prompt-text>${safePrompt}</pre>
      ${tagsHtml}
      <div class="help-prompt-actions">
        <button type="button" class="help-action-btn" onclick="copyHelpPrompt(this)" title="Copy to clipboard">
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
          Copy
        </button>
        <button type="button" class="help-action-btn help-action-btn--primary" onclick="insertHelpPromptIntoChat(this)" title="Insert into chat input and close this dialog">
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14"/><path d="M12 5l7 7-7 7"/></svg>
          Insert into chat
        </button>
      </div>
    </div>
  `;
}

function toggleHelpCategory(btn) {
  const cat = btn && btn.closest('.help-category');
  if (!cat) return;
  const willOpen = !cat.classList.contains('is-open');
  cat.classList.toggle('is-open', willOpen);
  btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
}

function _getPromptText(actionBtn) {
  const card = actionBtn && actionBtn.closest('.help-prompt');
  if (!card) return '';
  const pre = card.querySelector('[data-prompt-text]');
  return pre ? pre.textContent : '';
}

async function copyHelpPrompt(btn) {
  const text = _getPromptText(btn);
  if (!text) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); } finally { document.body.removeChild(ta); }
    }
    const orig = btn.innerHTML;
    btn.classList.add('is-success');
    btn.innerHTML = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg> Copied';
    setTimeout(() => { btn.classList.remove('is-success'); btn.innerHTML = orig; }, 1500);
    if (typeof showToast === 'function') showToast('Prompt copied to clipboard', 1400);
  } catch (e) {
    if (typeof showToast === 'function') showToast('Could not copy: ' + (e.message || e), 2400);
  }
}

function insertHelpPromptIntoChat(btn) {
  const text = _getPromptText(btn);
  if (!text) return;
  const input = document.getElementById('user-input');
  if (!input) {
    // Fallback: copy to clipboard so the user can paste it elsewhere.
    copyHelpPrompt(btn);
    if (typeof showToast === 'function') showToast('Chat input not available — copied instead', 2400);
    return;
  }
  // Append (do not overwrite) to preserve any draft the user typed.
  const existing = (input.value || '').trim();
  input.value = existing ? `${existing}\n\n${text}` : text;
  closeHelpModal();
  // Trigger any auto-resize wired on the textarea.
  try { input.dispatchEvent(new Event('input', { bubbles: true })); } catch {}
  setTimeout(() => {
    input.focus();
    try {
      input.scrollIntoView({ behavior: 'smooth', block: 'center' });
      input.setSelectionRange(input.value.length, input.value.length);
    } catch {}
  }, 80);
  if (typeof showToast === 'function') showToast('Prompt inserted — review the placeholders before sending', 2200);
}

function filterHelpPrompts(query) {
  const q = (query || '').trim().toLowerCase();
  const body = document.getElementById('help-modal-body');
  const clearBtn = document.getElementById('help-search-clear');
  if (clearBtn) clearBtn.hidden = !q;
  if (!body) return;

  const cards = body.querySelectorAll('.help-prompt');
  const cats  = body.querySelectorAll('.help-category');

  if (!q) {
    cards.forEach(c => c.style.display = '');
    cats.forEach((c, i) => {
      c.style.display = '';
      // Restore the default fold state: only first category open.
      c.classList.toggle('is-open', i === 0);
      const head = c.querySelector('.help-category-head');
      if (head) head.setAttribute('aria-expanded', i === 0 ? 'true' : 'false');
    });
    return;
  }

  cats.forEach(cat => {
    let visibleCount = 0;
    cat.querySelectorAll('.help-prompt').forEach(card => {
      const hay = card.getAttribute('data-haystack') || '';
      const match = hay.includes(q);
      card.style.display = match ? '' : 'none';
      if (match) visibleCount++;
    });
    cat.style.display = visibleCount === 0 ? 'none' : '';
    // Force-open categories that have matches so the user sees results.
    cat.classList.toggle('is-open', visibleCount > 0);
    const head = cat.querySelector('.help-category-head');
    if (head) head.setAttribute('aria-expanded', visibleCount > 0 ? 'true' : 'false');
  });
}

function clearHelpSearch() {
  const input = document.getElementById('help-search');
  if (input) input.value = '';
  filterHelpPrompts('');
  if (input) input.focus();
}

async function openHelpModal() {
  const modal = document.getElementById('help-modal');
  if (!modal) return;
  modal.classList.remove('hidden');
  trapFocus(modal);
  // Reset search every open
  const input = document.getElementById('help-search');
  if (input) input.value = '';
  const clearBtn = document.getElementById('help-search-clear');
  if (clearBtn) clearBtn.hidden = true;

  if (!_helpLoaded || !_helpCatalogue) {
    const body = document.getElementById('help-modal-body');
    if (body) body.innerHTML = '<div class="help-loading">Loading prompt catalogue…</div>';
    const cat = await loadHelpCatalogue();
    renderHelpCatalogue(cat);
  } else {
    renderHelpCatalogue(_helpCatalogue);
  }
  setTimeout(() => { if (input) input.focus(); }, 80);
}

function closeHelpModal() {
  const modal = document.getElementById('help-modal');
  if (!modal) return;
  modal.classList.add('hidden');
  releaseFocus('help-modal');
}

// ═══════ Prompt Library Sidebar (right margin of chat) ═══════
//
// A persistent, collapsible sidebar that surfaces the same prompt
// catalogue as the Help modal but in a denser, always-at-hand
// layout. Click a prompt to inject it into the chat input;
// hover to reveal a copy button. Open/closed state and which
// categories are expanded are persisted in localStorage so the
// experience feels stable across reloads.

const PSB_LS_OPEN = 'ss_ctrl_psb_open';
const PSB_LS_CATS = 'ss_ctrl_psb_open_cats';
const PSB_DEFAULT_OPEN_CATS = ['exploration'];

function _psbReadOpen() {
  const v = localStorage.getItem(PSB_LS_OPEN);
  if (v == null) return true;
  return v === '1';
}
function _psbWriteOpen(open) {
  try { localStorage.setItem(PSB_LS_OPEN, open ? '1' : '0'); } catch {}
}
function _psbReadOpenCats() {
  try {
    const raw = localStorage.getItem(PSB_LS_CATS);
    if (raw == null) return new Set(PSB_DEFAULT_OPEN_CATS);
    return new Set(JSON.parse(raw));
  } catch { return new Set(PSB_DEFAULT_OPEN_CATS); }
}
function _psbWriteOpenCats(set) {
  try { localStorage.setItem(PSB_LS_CATS, JSON.stringify([...set])); } catch {}
}

function togglePromptSidebar(forceOpen) {
  const sb = document.getElementById('prompt-sidebar');
  const rail = document.getElementById('prompt-sidebar-rail');
  const btn = document.getElementById('btn-prompt-sidebar');
  if (!sb) return;
  const isCollapsed = sb.classList.contains('collapsed');
  const willOpen = (forceOpen != null) ? !!forceOpen : isCollapsed;
  sb.classList.toggle('collapsed', !willOpen);
  if (rail) rail.style.display = willOpen ? 'none' : 'block';
  if (btn) btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
  _psbWriteOpen(willOpen);
}

async function initPromptSidebar() {
  const sb = document.getElementById('prompt-sidebar');
  const rail = document.getElementById('prompt-sidebar-rail');
  if (!sb) return;

  // Apply persisted open/closed state without writing it back.
  const open = _psbReadOpen();
  sb.classList.toggle('collapsed', !open);
  if (rail) rail.style.display = open ? 'none' : 'block';
  const btn = document.getElementById('btn-prompt-sidebar');
  if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');

  // Load + render the catalogue (re-uses the help modal cache).
  const body = document.getElementById('psb-body');
  if (!body) return;
  const cat = await loadHelpCatalogue();
  renderPromptSidebar(cat);
}

function renderPromptSidebar(catalogue) {
  const body = document.getElementById('psb-body');
  if (!body) return;
  if (catalogue && catalogue._error) {
    body.innerHTML = `<div class="psb-empty">Could not load prompts: ${_escapeHtml(catalogue._error)}</div>`;
    return;
  }
  if (!catalogue || !Array.isArray(catalogue.categories)) {
    body.innerHTML = `<div class="psb-empty">No prompts available.</div>`;
    return;
  }

  const openCats = _psbReadOpenCats();
  const html = catalogue.categories.map(cat => {
    const prompts = Array.isArray(cat.prompts) ? cat.prompts : [];
    const isOpen = openCats.has(cat.id);
    const cards = prompts.map(p => _psbRenderPrompt(p, cat)).join('');
    return `
      <div class="psb-category${isOpen ? ' is-open' : ''}" data-cat-id="${_escapeHtml(cat.id)}">
        <button type="button" class="psb-cat-head" onclick="togglePromptSidebarCategory(this)" aria-expanded="${isOpen ? 'true' : 'false'}">
          <span class="psb-cat-icon">${_helpIcon(cat.icon || 'search', 14)}</span>
          <span class="psb-cat-title">${_escapeHtml(cat.title)}</span>
          <span class="psb-cat-count">${prompts.length}</span>
          <svg class="psb-chevron" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 6 15 12 9 18"/></svg>
        </button>
        <div class="psb-cat-body">${cards || '<div class="psb-empty">No prompts.</div>'}</div>
      </div>
    `;
  }).join('');
  body.innerHTML = html;
}

function _psbRenderPrompt(p, cat) {
  if (!p || !p.prompt) return '';
  const title = _escapeHtml(p.title || '(untitled)');
  const desc = _escapeHtml(p.description || '');
  const diff = (p.difficulty || '').toLowerCase();
  const risk = (p.risk || '').toLowerCase();
  const safePrompt = _escapeHtml(p.prompt);
  const tags = Array.isArray(p.tags) ? p.tags : [];
  const haystack = _escapeHtml(
    [p.title, p.description, p.id, cat && cat.title, cat && cat.id, ...tags]
      .filter(Boolean).join(' ').toLowerCase()
  );

  const meta = [];
  if (diff) meta.push(`<span class="psb-pill psb-pill--${_escapeHtml(diff)}">${_escapeHtml(diff)}</span>`);
  if (risk === 'destructive') meta.push(`<span class="psb-pill psb-pill--destructive">⚠ destructive</span>`);
  else if (risk === 'high') meta.push(`<span class="psb-pill psb-pill--high">${_escapeHtml(risk)}</span>`);

  const tooltip = desc ? `${p.title || ''}\n\n${p.description || ''}\n\nClick to insert into the chat input.` : `${p.title || ''}\n\nClick to insert into the chat input.`;
  return `
    <div class="psb-prompt" role="listitem" data-haystack="${haystack}" title="${_escapeHtml(tooltip)}" onclick="insertPromptSidebarPrompt(this)">
      <div class="psb-prompt-inner">
        <div class="psb-prompt-title">${title}</div>
        ${meta.length ? `<div class="psb-prompt-meta">${meta.join('')}</div>` : ''}
      </div>
      <button type="button" class="psb-prompt-copy" onclick="event.stopPropagation(); copyPromptSidebarPrompt(this)" title="Copy to clipboard" aria-label="Copy prompt">
        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
      </button>
      <pre data-prompt-text style="display:none">${safePrompt}</pre>
    </div>
  `;
}

function togglePromptSidebarCategory(headBtn) {
  const cat = headBtn && headBtn.closest('.psb-category');
  if (!cat) return;
  const willOpen = !cat.classList.contains('is-open');
  cat.classList.toggle('is-open', willOpen);
  headBtn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
  const id = cat.getAttribute('data-cat-id');
  const set = _psbReadOpenCats();
  if (willOpen) set.add(id); else set.delete(id);
  _psbWriteOpenCats(set);
}

function _psbGetPromptText(card) {
  if (!card) return '';
  const pre = card.querySelector('[data-prompt-text]');
  return pre ? pre.textContent : '';
}

function insertPromptSidebarPrompt(card) {
  const text = _psbGetPromptText(card);
  if (!text) return;
  const input = document.getElementById('user-input');
  if (!input) return;
  const existing = (input.value || '').trim();
  input.value = existing ? `${existing}\n\n${text}` : text;
  try { input.dispatchEvent(new Event('input', { bubbles: true })); } catch {}
  setTimeout(() => {
    input.focus();
    try { input.setSelectionRange(input.value.length, input.value.length); } catch {}
  }, 30);
  if (typeof showToast === 'function') showToast('Prompt inserted — review placeholders before sending', 1800);
}

async function copyPromptSidebarPrompt(btn) {
  const card = btn && btn.closest('.psb-prompt');
  const text = _psbGetPromptText(card);
  if (!text) return;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); } finally { document.body.removeChild(ta); }
    }
    if (typeof showToast === 'function') showToast('Prompt copied to clipboard', 1400);
  } catch (e) {
    if (typeof showToast === 'function') showToast('Could not copy: ' + (e.message || e), 2200);
  }
}

function filterPromptSidebar(query) {
  const q = (query || '').trim().toLowerCase();
  const body = document.getElementById('psb-body');
  if (!body) return;
  const cats = body.querySelectorAll('.psb-category');

  if (!q) {
    // Restore persisted open state for each category.
    const persisted = _psbReadOpenCats();
    cats.forEach(cat => {
      cat.style.display = '';
      cat.querySelectorAll('.psb-prompt').forEach(c => { c.style.display = ''; });
      const id = cat.getAttribute('data-cat-id');
      const open = persisted.has(id);
      cat.classList.toggle('is-open', open);
      const head = cat.querySelector('.psb-cat-head');
      if (head) head.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    // Remove any "no results" message.
    const nores = body.querySelector('.psb-no-results');
    if (nores) nores.remove();
    return;
  }

  let totalMatches = 0;
  cats.forEach(cat => {
    let any = false;
    cat.querySelectorAll('.psb-prompt').forEach(c => {
      const haystack = c.getAttribute('data-haystack') || '';
      const match = haystack.includes(q);
      c.style.display = match ? '' : 'none';
      if (match) { any = true; totalMatches++; }
    });
    cat.style.display = any ? '' : 'none';
    cat.classList.toggle('is-open', any);
    const head = cat.querySelector('.psb-cat-head');
    if (head) head.setAttribute('aria-expanded', any ? 'true' : 'false');
  });

  // Show or remove the "no results" message.
  let nores = body.querySelector('.psb-no-results');
  if (totalMatches === 0) {
    if (!nores) {
      nores = document.createElement('div');
      nores.className = 'psb-no-results';
      body.appendChild(nores);
    }
    nores.textContent = `No prompt matches “${query}”.`;
  } else if (nores) {
    nores.remove();
  }
}

// ═══════ Focus trap (a11y) ═══════
const _focusTrapState = {};
function trapFocus(modal) {
  if (!modal) return;
  const id = modal.id || ('modal-' + Math.random().toString(36).slice(2));
  modal.id = id;
  const previouslyFocused = document.activeElement;
  const focusables = modal.querySelectorAll(
    'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
  );
  const list = Array.from(focusables).filter(el => el.offsetParent !== null);
  if (list.length === 0) return;
  const first = list[0];
  const last = list[list.length - 1];

  const onKey = (e) => {
    if (e.key !== 'Tab') return;
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first.focus();
    }
  };
  modal.addEventListener('keydown', onKey);
  _focusTrapState[id] = { previouslyFocused, onKey, modal };
  setTimeout(() => first.focus(), 50);
}

function releaseFocus(modalId) {
  const state = _focusTrapState[modalId];
  if (!state) return;
  state.modal.removeEventListener('keydown', state.onKey);
  if (state.previouslyFocused && typeof state.previouslyFocused.focus === 'function') {
    try { state.previouslyFocused.focus(); } catch {}
  }
  delete _focusTrapState[modalId];
}

// ═══════ Pinned Messages ═══════
function getPinnedMessages() {
  try { return JSON.parse(localStorage.getItem('ss_ctrl_pins') || '[]'); }
  catch { return []; }
}

function savePinnedMessages(pins) {
  try { localStorage.setItem('ss_ctrl_pins', JSON.stringify(pins.slice(0, 50))); }
  catch {}
}

function togglePinMessage(content, timestamp) {
  const pins = getPinnedMessages();
  const idx = pins.findIndex(p => p.timestamp === timestamp);
  if (idx >= 0) {
    pins.splice(idx, 1);
  } else {
    pins.unshift({ content: content.substring(0, 500), timestamp, pinnedAt: Date.now() });
  }
  savePinnedMessages(pins);
  renderPinnedList();
}

function isMessagePinned(timestamp) {
  return getPinnedMessages().some(p => p.timestamp === timestamp);
}

function renderPinnedList() {
  const list = document.getElementById('pinned-list');
  if (!list) return;
  const pins = getPinnedMessages();
  if (pins.length === 0) { list.innerHTML = '<div style="font-size:11px;color:var(--text-muted)">No pinned messages</div>'; return; }

  list.innerHTML = '';
  pins.forEach(p => {
    const item = document.createElement('div');
    item.className = 'pinned-item';
    item.title = 'Click to jump to message - Right-click to unpin';
    item.innerHTML = '<div class="pinned-item-text">' + escapeHtml(p.content) + '</div>' +
      '<div class="pinned-item-time">' + timeAgo(p.pinnedAt) + '</div>';
    item.onclick = () => jumpToPinnedMessage(p);
    item.oncontextmenu = (e) => { e.preventDefault(); togglePinMessage(p.content, p.timestamp); showToast('Unpinned'); };
    list.appendChild(item);
  });
}

function jumpToPinnedMessage(pin) {
  // Try by timestamp first (exact match on data-ts)
  const container = document.getElementById('messages');
  if (!container) return;
  let target = container.querySelector('.msg-group[data-ts="' + pin.timestamp + '"]');

  // Fallback: search by content snippet match
  if (!target) {
    const groups = container.querySelectorAll('.msg-group.assistant');
    const needle = (pin.content || '').substring(0, 60).toLowerCase();
    for (const g of groups) {
      const txt = (g.textContent || '').toLowerCase();
      if (txt.includes(needle)) { target = g; break; }
    }
  }

  if (!target) {
    showToast('Original message not in current conversation');
    return;
  }

  target.scrollIntoView({ behavior: 'smooth', block: 'center' });
  target.classList.add('pin-flash');
  setTimeout(() => target.classList.remove('pin-flash'), 1600);
}

function createPinButton(content, timestamp) {
  const btn = document.createElement('button');
  const pinnedNow = isMessagePinned(timestamp);
  btn.className = 'msg-pin-btn' + (pinnedNow ? ' pinned' : '');
  btn.title = pinnedNow ? 'Unpin message' : 'Pin message';
  btn.setAttribute('aria-label', btn.title);
  btn.setAttribute('aria-pressed', pinnedNow ? 'true' : 'false');
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="' + (pinnedNow ? 'currentColor' : 'none') + '" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 17v5M9 3h6l-1 7h4l-7 8 1-6H8l1-9z"/></svg>';
  btn.onclick = (e) => {
    e.stopPropagation();
    togglePinMessage(content, timestamp);
    const pinned = isMessagePinned(timestamp);
    btn.className = 'msg-pin-btn' + (pinned ? ' pinned' : '');
    btn.title = pinned ? 'Unpin message' : 'Pin message';
    btn.setAttribute('aria-label', btn.title);
    btn.setAttribute('aria-pressed', pinned ? 'true' : 'false');
    btn.querySelector('svg').setAttribute('fill', pinned ? 'currentColor' : 'none');
  };
  return btn;
}

// ═══════ Watch Mode ═══════
let watchActive = false;
let watchIntervalSec = 60;

function getWatchKey() {
  return 'ss_ctrl_watch_' + (currentSheetId || 'default');
}

function loadWatchState() {
  try {
    const raw = localStorage.getItem(getWatchKey());
    return raw ? JSON.parse(raw) : { enabled: false, interval: 60 };
  } catch { return { enabled: false, interval: 60 }; }
}

function saveWatchState(state) {
  try { localStorage.setItem(getWatchKey(), JSON.stringify(state)); } catch {}
}

function toggleWatchPopover(e) {
  if (e) e.stopPropagation();
  const pop = document.getElementById('watch-popover');
  if (!pop) return;
  pop.classList.toggle('hidden');
  const btn = document.getElementById('btn-watch');
  if (btn) btn.setAttribute('aria-expanded', pop.classList.contains('hidden') ? 'false' : 'true');
  if (!pop.classList.contains('hidden')) {
    // Sync UI with current state
    const state = loadWatchState();
    const cb = document.getElementById('watch-enabled');
    const sel = document.getElementById('watch-interval');
    if (cb) cb.checked = !!state.enabled;
    if (sel) sel.value = String(state.interval || 60);
    // Close on outside click
    setTimeout(() => {
      document.addEventListener('click', closeWatchPopoverOnOutside, { once: true });
    }, 0);
  }
}

function closeWatchPopoverOnOutside(e) {
  const pop = document.getElementById('watch-popover');
  const btn = document.getElementById('btn-watch');
  if (!pop || pop.classList.contains('hidden')) return;
  if (pop.contains(e.target) || (btn && btn.contains(e.target))) {
    document.addEventListener('click', closeWatchPopoverOnOutside, { once: true });
    return;
  }
  pop.classList.add('hidden');
}

function onWatchToggle(enabled) {
  if (enabled) startWatch(watchIntervalSec);
  else stopWatch();
}

function onWatchIntervalChange(value) {
  watchIntervalSec = parseInt(value, 10) || 60;
  if (watchActive) {
    // Restart with new interval
    startWatch(watchIntervalSec);
  } else {
    saveWatchState({ enabled: false, interval: watchIntervalSec });
  }
}

function startWatch(intervalSec) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    showToast('Not connected — cannot start watch');
    const cb = document.getElementById('watch-enabled');
    if (cb) cb.checked = false;
    return;
  }
  watchIntervalSec = parseInt(intervalSec, 10) || 60;
  ws.send(JSON.stringify({ type: 'watch', enabled: true, interval: watchIntervalSec }));
  watchActive = true;
  const dot = document.getElementById('watch-dot');
  if (dot) dot.style.display = 'block';
  saveWatchState({ enabled: true, interval: watchIntervalSec });
  showToast('Watch mode enabled (every ' + watchIntervalSec + 's)');
}

function stopWatch() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'watch', enabled: false }));
  }
  watchActive = false;
  const dot = document.getElementById('watch-dot');
  if (dot) dot.style.display = 'none';
  saveWatchState({ enabled: false, interval: watchIntervalSec });
  showToast('Watch mode disabled');
}

function restoreWatchStateForSheet() {
  const state = loadWatchState();
  watchIntervalSec = state.interval || 60;
  watchActive = false;
  const dot = document.getElementById('watch-dot');
  if (dot) dot.style.display = 'none';
  if (state.enabled && ws && ws.readyState === WebSocket.OPEN) {
    setTimeout(() => startWatch(watchIntervalSec), 500);
  }
}

function showNotification(title, body) {
  const el = document.createElement('div');
  el.className = 'toast-notification';
  el.innerHTML = '<div class="toast-title">' + escapeHtml(title) + '</div>' +
    '<div class="toast-body">' + escapeHtml(body) + '</div>';
  el.onclick = () => el.remove();
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 8000);
}

// ═══════ Settings Modal ═══════
function toggleSettingsModal() {
  const modal = document.getElementById('settings-modal');
  if (!modal) return;
  const willOpen = modal.classList.contains('hidden');
  if (willOpen) {
    populateSettingsModal();
    modal.classList.remove('hidden');
    trapFocus(modal);
  } else {
    modal.classList.add('hidden');
    releaseFocus('settings-modal');
  }
}

function populateSettingsModal() {
  // Sheets
  const sheetSel = document.getElementById('settings-sheet-select');
  const currentBadge = document.getElementById('sheet-badge').textContent || '';
  sheetSel.innerHTML = '';
  allSheets.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.name;
    if (currentBadge.startsWith(s.name)) opt.selected = true;
    sheetSel.appendChild(opt);
  });

  // Providers / models
  const provSel = document.getElementById('settings-provider-select');
  provSel.innerHTML = '';
  Object.keys(availableProviders).forEach(p => {
    const opt = document.createElement('option');
    opt.value = p;
    opt.textContent = p.charAt(0).toUpperCase() + p.slice(1);
    provSel.appendChild(opt);
  });
  // Detect current from model-select
  const current = (document.getElementById('model-select') || {}).value || '';
  const [curProv, curModel] = current.split('::');
  if (curProv) provSel.value = curProv;
  renderSettingsModelList(curProv || provSel.value, curModel);

  // Pinned list
  renderSettingsPinnedList();
  const pinSel = document.getElementById('settings-pin-select');
  pinSel.innerHTML = '<option value="">+ Pin another sheet...</option>';
  allSheets.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.name;
    pinSel.appendChild(opt);
  });

  // Templates
  renderTemplatesList();

  // Usage stats
  refreshUsageStats();

  // User info
  const uinfo = document.getElementById('settings-user-info');
  if (currentUser) {
    uinfo.innerHTML =
      '<div><strong>' + escapeHtml((currentUser.firstName || '') + ' ' + (currentUser.lastName || '')).trim() + '</strong></div>' +
      '<div>' + escapeHtml(currentUser.email || '') + '</div>' +
      (currentUser.account ? '<div style="color:var(--text-muted)">' + escapeHtml(currentUser.account) + '</div>' : '');
  } else {
    uinfo.textContent = '(not available)';
  }
}

async function refreshUsageStats() {
  const el = document.getElementById('settings-usage-stats');
  if (!el || !sessionId) return;
  el.textContent = 'Loading…';
  try {
    const r = await fetch(`/api/usage?session_id=${encodeURIComponent(sessionId)}`);
    if (!r.ok) { el.textContent = 'Unavailable'; return; }
    const data = await r.json();
    const t = data.tokens || {};
    const c = data.cache || {};
    const fmt = (n) => (n || 0).toLocaleString('en-US');
    let html = '';
    html += `<div><strong>Provider</strong>: ${escapeHtml(data.provider || '?')} · <strong>Model</strong>: ${escapeHtml(data.current_model || '?')}</div>`;
    html += `<div><strong>Total tokens</strong>: ${fmt(t.input_tokens)} in · ${fmt(t.output_tokens)} out · over ${fmt(t.calls)} call(s)</div>`;
    if (t.last_call) {
      const last = t.last_call;
      html += `<div style="color:var(--text-muted);">Last: ${fmt(last.input)} in · ${fmt(last.output)} out (${escapeHtml(last.model || '')})</div>`;
    }
    if (Object.keys(t.by_model || {}).length > 1) {
      html += '<div style="margin-top:6px;"><strong>By model</strong>:</div>';
      for (const [m, u] of Object.entries(t.by_model)) {
        html += `<div style="padding-left:8px;color:var(--text-muted);">${escapeHtml(m)}: ${fmt(u.input)}/${fmt(u.output)} (${u.calls} calls)</div>`;
      }
    }
    if (c && (c.hits || c.misses)) {
      const total = (c.hits || 0) + (c.misses || 0);
      const pct = total ? Math.round((c.hits / total) * 100) : 0;
      html += `<div style="margin-top:6px;"><strong>Schema cache</strong>: ${pct}% hit rate (${fmt(c.hits)}/${fmt(total)})</div>`;
    }
    el.innerHTML = html;
  } catch (e) {
    el.textContent = 'Error: ' + e.message;
  }
}

// ═══════ Sprint 5: Server sync (conversations, audit, webhooks, export) ═══════
async function registerActiveConversation() {
  if (!sessionId || !currentConversationId) return;
  try {
    await fetch('/api/conversations/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: sessionId,
        conversation_id: currentConversationId,
        sheet_id: currentSheetId || null,
        title: null,
      }),
    });
  } catch (e) { /* offline-friendly */ }
}

let _webhookPollTimer = null;
let _webhookSince = 0;
function startWebhookPolling() {
  if (_webhookPollTimer) clearInterval(_webhookPollTimer);
  _webhookSince = Date.now() / 1000;
  _webhookPollTimer = setInterval(pollWebhookEvents, 15000);
}
async function pollWebhookEvents() {
  if (!sessionId) return;
  try {
    const r = await fetch(`/api/webhook-events?session_id=${encodeURIComponent(sessionId)}&since=${_webhookSince}`);
    if (!r.ok) return;
    const data = await r.json();
    const events = data.events || [];
    if (events.length) {
      _webhookSince = events[0].received_at;
      events.slice(0, 3).forEach(ev => {
        const msg = `Smartsheet event: ${ev.event_type || 'update'} on sheet ${ev.sheet_id || ''}`;
        if (typeof showToast === 'function') showToast(msg, 4000);
      });
    }
  } catch (e) { /* silent */ }
}

let _migrationDone = false;
async function maybeMigrateLocalHistory() {
  if (_migrationDone || !sessionId) return;
  _migrationDone = true;
  try {
    const flag = localStorage.getItem('ssctrl_migrated_v1');
    if (flag === '1') return;
    const history = loadHistory();
    if (!history || history.length === 0) {
      localStorage.setItem('ssctrl_migrated_v1', '1');
      return;
    }
    const payload = history.slice(0, 50).map(h => ({
      id: h.id,
      title: h.title || null,
      sheet_id: h.sheetId || null,
      messages: (h.messages || []).map(m => ({
        role: m.role,
        content: m.content || '',
        created_at: (h.updatedAt || Date.now()) / 1000,
      })),
    }));
    const r = await fetch('/api/conversations/migrate', {
      method: 'POST',
      headers: sessionAuthHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ session_id: sessionId, conversations: payload }),
    });
    if (r.ok) localStorage.setItem('ssctrl_migrated_v1', '1');
  } catch (e) { /* silent */ }
}

async function openAuditModal() {
  if (!sessionId) return;
  let modal = document.getElementById('audit-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'audit-modal';
    modal.className = 'modal-overlay';
    modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:9999;';
    modal.innerHTML = `
      <div style="background:var(--bg-secondary, #1a1a1a);color:var(--text-primary, #fff);width:min(800px,92vw);max-height:80vh;overflow:auto;border-radius:12px;padding:20px;border:1px solid var(--border, #333);">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
          <h3 style="margin:0;">Audit log</h3>
          <button onclick="document.getElementById('audit-modal').remove()" style="background:transparent;border:none;color:inherit;font-size:24px;cursor:pointer;">&times;</button>
        </div>
        <div id="audit-content" style="font-size:12px;line-height:1.5;">Loading…</div>
      </div>`;
    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => { if (e.target === modal) modal.remove(); });
  }
  try {
    const r = await fetch(`/api/audit?${sessionAuthQuery({ limit: '200' })}`);
    const data = await r.json();
    const entries = data.entries || [];
    const el = document.getElementById('audit-content');
    if (!entries.length) { el.innerHTML = '<div style="opacity:0.6;">No audit entries yet.</div>'; return; }
    let html = '<table style="width:100%;border-collapse:collapse;"><thead><tr style="text-align:left;border-bottom:1px solid var(--border,#333);"><th style="padding:6px;">When</th><th style="padding:6px;">Tool</th><th style="padding:6px;">Sheet</th><th style="padding:6px;">Status</th></tr></thead><tbody>';
    entries.forEach(e => {
      const when = new Date((e.created_at || 0) * 1000).toLocaleString();
      const color = e.status === 'approved' ? '#4ade80' : (e.status === 'rejected' ? '#f87171' : 'inherit');
      html += `<tr style="border-bottom:1px solid var(--border,#222);"><td style="padding:6px;white-space:nowrap;">${escapeHtml(when)}</td><td style="padding:6px;font-family:monospace;">${escapeHtml(e.tool_name||'')}</td><td style="padding:6px;font-family:monospace;font-size:11px;opacity:0.7;">${escapeHtml(e.sheet_id||'')}</td><td style="padding:6px;color:${color};">${escapeHtml(e.status||'')}</td></tr>`;
    });
    html += '</tbody></table>';
    el.innerHTML = html;
  } catch (e) {
    document.getElementById('audit-content').textContent = 'Error: ' + e.message;
  }
}

async function exportMyData() {
  if (!sessionId) return;
  try {
    const r = await fetch(`/api/export?${sessionAuthQuery()}`);
    if (!r.ok) { alert('Export failed'); return; }
    const data = await r.json();
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `smartsheet-controller-export-${Date.now()}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert('Export error: ' + e.message);
  }
}

function renderSettingsModelList(provider, selectedModel) {
  const modelSel = document.getElementById('settings-model-select');
  modelSel.innerHTML = '';
  const info = availableProviders[provider];
  if (!info) return;
  (info.models || []).forEach(m => {
    const opt = document.createElement('option');
    opt.value = provider + '::' + m;
    opt.textContent = m;
    if (m === selectedModel) opt.selected = true;
    modelSel.appendChild(opt);
  });
}

function onSettingsProviderChange() {
  const p = document.getElementById('settings-provider-select').value;
  renderSettingsModelList(p, null);
}

async function settingsSwitchSheet(sheetId) {
  if (!sheetId) return;
  await switchSheet(sheetId);
  // Sync main header sheet selector
  const mainSel = document.getElementById('sheet-select');
  if (mainSel) mainSel.value = sheetId;
}

async function settingsSwitchModel() {
  const val = document.getElementById('settings-model-select').value;
  if (!val) return;
  const [provider, model] = val.split('::');
  showToast('Switching to ' + model + '...');
  try {
    const res = await fetch('/api/switch-model', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, provider, model }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Switch failed');
    showToast('Model: ' + data.model);
    // Sync header selector
    const mainSel = document.getElementById('model-select');
    if (mainSel) mainSel.value = val;
  } catch (e) {
    showToast('Error: ' + e.message);
  }
}

function getPinnedSheets() {
  try { return JSON.parse(localStorage.getItem('ss_ctrl_pinned_sheets') || '[]'); } catch { return []; }
}
function savePinnedSheets(arr) {
  localStorage.setItem('ss_ctrl_pinned_sheets', JSON.stringify(arr.slice(0, 10)));
}

function renderSettingsPinnedList() {
  const el = document.getElementById('settings-pinned-list');
  if (!el) return;
  const pinned = getPinnedSheets();
  if (pinned.length === 0) { el.textContent = 'None'; return; }
  el.innerHTML = '';
  pinned.forEach(p => {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:4px 0;';
    row.innerHTML = '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:280px;">' + escapeHtml(p.name) + '</span>' +
      '<button style="background:none;border:0;color:var(--text-muted);cursor:pointer;font-size:12px;" onclick="settingsUnpinSheet(\'' + p.id + '\')">Remove</button>';
    el.appendChild(row);
  });
}

async function settingsPinSheet(sheetId) {
  if (!sheetId) return;
  const sheet = allSheets.find(s => String(s.id) === String(sheetId));
  if (!sheet) return;
  const pinned = getPinnedSheets();
  if (pinned.some(p => String(p.id) === String(sheetId))) {
    showToast('Already pinned');
    document.getElementById('settings-pin-select').value = '';
    return;
  }
  pinned.unshift({ id: sheet.id, name: sheet.name });
  savePinnedSheets(pinned);

  // Backend pin (best-effort)
  try {
    await fetch('/api/pin-sheet', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, sheet_id: sheetId }),
    });
  } catch {}

  renderSettingsPinnedList();
  document.getElementById('settings-pin-select').value = '';
  showToast('Pinned ' + sheet.name);
}

async function settingsUnpinSheet(sheetId) {
  const pinned = getPinnedSheets().filter(p => String(p.id) !== String(sheetId));
  savePinnedSheets(pinned);
  try {
    await fetch('/api/unpin-sheet', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, sheet_id: sheetId }),
    });
  } catch {}
  renderSettingsPinnedList();
  showToast('Unpinned');
}

async function disconnectSession() {
  if (!confirm('Disconnect this session and return to the landing page?')) return;
  try { if (window.speechSynthesis) window.speechSynthesis.cancel(); } catch {}
  _ttsCurrent = null;
  try {
    if (sessionId) {
      await fetch('/api/disconnect', {
        method: 'POST',
        headers: sessionAuthHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ session_id: sessionId }),
      });
    }
  } catch {}
  try { if (ws) ws.close(); } catch {}
  // Clear session state (but keep remembered token if user opted in)
  sessionId = null;
  wsToken = null;
  authCookie = null;
  currentUser = null;
  ws = null;
  conversationMessages = [];
  currentConversationId = null;
  // Hide chat, show setup
  const chatEl = document.getElementById('chat');
  const setupEl = document.getElementById('setup');
  if (chatEl) chatEl.style.display = 'none';
  if (setupEl) { setupEl.style.display = ''; setupEl.classList.remove('screen-exit'); }
  document.getElementById('settings-modal').classList.add('hidden');
  const ub = document.getElementById('user-badge'); if (ub) ub.style.display = 'none';
  const bs = document.getElementById('btn-settings'); if (bs) bs.style.display = 'none';
  const bw = document.getElementById('btn-watch'); if (bw) bw.style.display = 'none';
  const blo = document.getElementById('btn-logout'); if (blo) blo.style.display = 'none';
  const bps = document.getElementById('btn-prompt-sidebar'); if (bps) bps.style.display = 'none';
  const psbSb = document.getElementById('prompt-sidebar'); if (psbSb) psbSb.classList.add('collapsed');
  const psbRail = document.getElementById('prompt-sidebar-rail'); if (psbRail) psbRail.style.display = 'none';
  const bg = document.getElementById('btn-burger'); if (bg) bg.style.display = 'none';
  document.querySelector('header')?.classList.remove('mobile-menu-open');
  const wp = document.getElementById('watch-popover'); if (wp) wp.classList.add('hidden');
  if (watchActive) stopWatch();
  const hs = document.getElementById('header-status'); if (hs) hs.style.display = 'none';
  const sb = document.getElementById('sheet-badge'); if (sb) sb.style.display = 'none';
  document.getElementById('messages').innerHTML = '';
  showToast('Disconnected');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ═══════ Onboarding Tour ═══════
const TOUR_STEPS = [
  {
    target: '#quick-actions',
    title: 'Quick actions',
    body: 'One-click shortcuts for common tasks: show the sheet, search rows, summarize, generate formulas.',
  },
  {
    target: '#user-input',
    title: 'Ask anything',
    body: 'Type natural language. Press Enter to send, Shift+Enter for a new line. Use the mic for voice input.',
  },
  {
    target: '#model-selector',
    title: 'AI model picker',
    body: 'Switch between OpenAI, Anthropic, Gemini, Groq, Mistral, DeepSeek, and OpenRouter on the fly.',
  },
  {
    target: '#btn-watch',
    title: 'Watch mode',
    body: 'Get an in-app notification whenever rows in your sheet change. Toggle the eye icon any time.',
  },
  {
    target: '#btn-settings',
    title: 'Settings',
    body: 'Switch sheet, manage pinned messages, replay this tour, or disconnect your account here.',
  },
];
let _tourIdx = 0;

function startTour(opts) {
  // opts.force = true bypasses the "already done" check (used by Settings replay)
  if (!opts || !opts.force) {
    if (localStorage.getItem('ss_ctrl_tour_done') === '1') return;
  }
  _tourIdx = 0;
  showTourStep();
}

function skipTour() {
  endTour();
  showToast('Tour skipped. Replay anytime from Settings.');
}

function replayTour() {
  // Close settings modal first if open
  const sm = document.getElementById('settings-modal');
  if (sm && !sm.classList.contains('hidden')) toggleSettingsModal();
  localStorage.removeItem('ss_ctrl_tour_done');
  setTimeout(() => startTour({ force: true }), 250);
}

function showTourStep() {
  const step = TOUR_STEPS[_tourIdx];
  const overlay = document.getElementById('tour-overlay');
  const pop = document.getElementById('tour-popover');
  if (!step || !overlay || !pop) return;
  const target = document.querySelector(step.target);
  if (!target || target.offsetParent === null) {
    // Skip invisible step
    _tourIdx++;
    if (_tourIdx >= TOUR_STEPS.length) return endTour();
    return showTourStep();
  }
  overlay.style.display = 'block';
  pop.style.display = 'block';
  document.getElementById('tour-title').textContent = step.title;
  document.getElementById('tour-body').textContent = step.body;
  document.getElementById('tour-step-count').textContent = (_tourIdx + 1) + ' / ' + TOUR_STEPS.length;
  document.getElementById('tour-next-btn').textContent = (_tourIdx === TOUR_STEPS.length - 1) ? 'Done' : 'Next';
  const rect = target.getBoundingClientRect();
  const popW = 320;
  let left = rect.left + rect.width / 2 - popW / 2;
  let top = rect.bottom + 12;
  left = Math.max(12, Math.min(window.innerWidth - popW - 12, left));
  if (top + 160 > window.innerHeight) top = Math.max(12, rect.top - 170);
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';
  target.classList.add('tour-highlight');
}

function nextTourStep() {
  const prev = TOUR_STEPS[_tourIdx];
  if (prev) document.querySelector(prev.target)?.classList.remove('tour-highlight');
  _tourIdx++;
  if (_tourIdx >= TOUR_STEPS.length) return endTour();
  showTourStep();
}

function endTour() {
  const prev = TOUR_STEPS[_tourIdx];
  if (prev) document.querySelector(prev.target)?.classList.remove('tour-highlight');
  document.getElementById('tour-overlay').style.display = 'none';
  document.getElementById('tour-popover').style.display = 'none';
  localStorage.setItem('ss_ctrl_tour_done', '1');
}

// ═══════ Init ═══════
function isExtensionEmbedMode() {
  try {
    if (new URLSearchParams(location.search).get('ssc_ext') === '1') return true;
  } catch (_) {}
  try {
    return window.self !== window.top;
  } catch (_) {
    return true;
  }
}

function initExtensionEmbedMode() {
  if (!isExtensionEmbedMode()) return;
  document.documentElement.classList.add('ssc-embed');
  try { localStorage.setItem('ss_ctrl_tour_done', '1'); } catch (_) {}
}

function insertTextIntoChatInput(text) {
  const input = document.getElementById('user-input');
  if (!input || !text) return;
  const existing = (input.value || '').trim();
  input.value = existing ? `${existing}\n\n${text}` : text;
  try { input.dispatchEvent(new Event('input', { bubbles: true })); } catch (_) {}
  setTimeout(() => {
    input.focus();
    try { input.setSelectionRange(input.value.length, input.value.length); } catch (_) {}
  }, 30);
  if (typeof showToast === 'function') {
    showToast('Prompt inserted — review before sending', 1800);
  }
}

function applyExtensionToken(token) {
  const ti = document.getElementById('ss-token');
  if (!ti || !token) return;
  if ((ti.value || '').trim()) return;
  ti.value = String(token).trim();
  if (typeof onTokenInput === 'function') onTokenInput();
  if (typeof showToast === 'function') {
    showToast('Token filled from extension — click Validate to continue', 2600);
  }
}

function initExtensionBridge() {
  if (!isExtensionEmbedMode()) return;

  window.addEventListener('message', (event) => {
    if (event.source !== window.parent) return;
    if (!String(event.origin || '').startsWith('chrome-extension://')) return;
    const data = event.data;
    if (!data || data.source !== 'ssc-extension') return;

    if (data.type === 'apply_token' && data.payload?.token) {
      applyExtensionToken(data.payload.token);
    }
    if (data.type === 'insert_prompt' && data.payload?.text) {
      insertTextIntoChatInput(data.payload.text);
    }
  });

  window.parent.postMessage({ source: 'ssc-controller', type: 'ready' }, '*');
}

function applySheetIdQueryParam() {
  try {
    const u = new URL(location.href);
    const sid = (u.searchParams.get('sheet_id') || u.searchParams.get('sheet') || '').trim().replace(/[\s"'`]/g, '');
    if (!/^\d{6,}$/.test(sid)) return;
    const inp = document.getElementById('sheet-byid-input');
    if (inp && !(inp.value || '').trim()) {
      inp.value = sid;
      onByIdInput();
    }
  } catch (_) {}
}

// Expose handlers for inline HTML attributes (transitional).
window._detectLang = _detectLang;
window._escapeHtml = _escapeHtml;
window._getPromptText = _getPromptText;
window._helpIcon = _helpIcon;
window._isCsvFile = _isCsvFile;
window._parseCsv = _parseCsv;
window._parseNumeric = _parseNumeric;
window._pickVoice = _pickVoice;
window._psbGetPromptText = _psbGetPromptText;
window._psbReadOpen = _psbReadOpen;
window._psbReadOpenCats = _psbReadOpenCats;
window._psbRenderPrompt = _psbRenderPrompt;
window._psbWriteOpen = _psbWriteOpen;
window._psbWriteOpenCats = _psbWriteOpenCats;
window._renderDiffBody = _renderDiffBody;
window._renderPromptCard = _renderPromptCard;
window._renderSparkSvg = _renderSparkSvg;
window._safe = _safe;
window._slashMenuEl = _slashMenuEl;
window._stripMarkdownForTts = _stripMarkdownForTts;
window.addAgentHint = addAgentHint;
window.addChart = addChart;
window.addConfirmCard = addConfirmCard;
window.addCsvButtonsIn = addCsvButtonsIn;
window.addImageMessage = addImageMessage;
window.addMessage = addMessage;
window.addSparklinesIn = addSparklinesIn;
window.addSuggestions = addSuggestions;
window.addToFavorites = addToFavorites;
window.addToolMessage = addToolMessage;
window.addTryCards = addTryCards;
window.applyExtensionToken = applyExtensionToken;
window.applySheetIdQueryParam = applySheetIdQueryParam;
window.backToStep1 = backToStep1;
window.cancelFirstMessageHint = cancelFirstMessageHint;
window.cancelRequest = cancelRequest;
window.checkEnvReady = checkEnvReady;
window.clearHelpSearch = clearHelpSearch;
window.clearRememberedToken = clearRememberedToken;
window.clearSessionInfo = clearSessionInfo;
window.closeBugReportModal = closeBugReportModal;
window.closeCsvPreview = closeCsvPreview;
window.closeHelpModal = closeHelpModal;
window.closeTemplateEditor = closeTemplateEditor;
window.closeWatchPopoverOnOutside = closeWatchPopoverOnOutside;
window.collectBugContext = collectBugContext;
window.confirmCsvImport = confirmCsvImport;
window.connectWebSocket = connectWebSocket;
window.copyHelpPrompt = copyHelpPrompt;
window.copyPromptSidebarPrompt = copyPromptSidebarPrompt;
window.copyText = copyText;
window.createBlankSheet = createBlankSheet;
window.createPinButton = createPinButton;
window.createTtsButton = createTtsButton;
window.deleteTemplate = deleteTemplate;
window.disconnectSession = disconnectSession;
window.downloadTableAsCsv = downloadTableAsCsv;
window.endTour = endTour;
window.enhanceAssistantBubble = enhanceAssistantBubble;
window.escapeAttr = escapeAttr;
window.escapeHtml = escapeHtml;
window.exportConversation = exportConversation;
window.exportMyData = exportMyData;
window.exportPDF = exportPDF;
window.filterHelpPrompts = filterHelpPrompts;
window.filterHistory = filterHistory;
window.filterPromptSidebar = filterPromptSidebar;
window.filterSheets = filterSheets;
window.finalConnect = finalConnect;
window.friendlyTokenError = friendlyTokenError;
window.getFavorites = getFavorites;
window.getHistoryKey = getHistoryKey;
window.getPinnedMessages = getPinnedMessages;
window.getPinnedSheets = getPinnedSheets;
window.getRememberedToken = getRememberedToken;
window.getWatchKey = getWatchKey;
window.handleEvent = handleEvent;
window.handleKey = handleKey;
window.hideReconnectBanner = hideReconnectBanner;
window.highlightFormulasIn = highlightFormulasIn;
window.initCsvDragDrop = initCsvDragDrop;
window.initExtensionBridge = initExtensionBridge;
window.initExtensionEmbedMode = initExtensionEmbedMode;
window.initKeyboardShortcuts = initKeyboardShortcuts;
window.initPromptSidebar = initPromptSidebar;
window.initVoice = initVoice;
window.insertHelpPromptIntoChat = insertHelpPromptIntoChat;
window.insertPromptSidebarPrompt = insertPromptSidebarPrompt;
window.insertTextIntoChatInput = insertTextIntoChatInput;
window.isExtensionEmbedMode = isExtensionEmbedMode;
window.isInputFocused = isInputFocused;
window.isMessagePinned = isMessagePinned;
window.jumpToPinnedMessage = jumpToPinnedMessage;
window.loadConversation = loadConversation;
window.loadHelpCatalogue = loadHelpCatalogue;
window.loadHistory = loadHistory;
window.loadTemplates = loadTemplates;
window.loadWatchState = loadWatchState;
window.lookupSheetById = lookupSheetById;
window.maybeExpandSlash = maybeExpandSlash;
window.maybeGenerateTitle = maybeGenerateTitle;
window.maybeMigrateLocalHistory = maybeMigrateLocalHistory;
window.newConversation = newConversation;
window.nextTourStep = nextTourStep;
window.onByIdInput = onByIdInput;
window.onCreateInput = onCreateInput;
window.onProviderChange = onProviderChange;
window.onSettingsProviderChange = onSettingsProviderChange;
window.onTokenInput = onTokenInput;
window.onWatchIntervalChange = onWatchIntervalChange;
window.onWatchToggle = onWatchToggle;
window.openAuditModal = openAuditModal;
window.openBugReportModal = openBugReportModal;
window.openChat = openChat;
window.openCsvPreview = openCsvPreview;
window.openHelpModal = openHelpModal;
window.openTemplateEditor = openTemplateEditor;
window.pickSheet = pickSheet;
window.pickSlashCommand = pickSlashCommand;
window.pollWebhookEvents = pollWebhookEvents;
window.populateModelSelector = populateModelSelector;
window.populateSettingsModal = populateSettingsModal;
window.populateSheetSwitcher = populateSheetSwitcher;
window.populateStep2Providers = populateStep2Providers;
window.quickAction = quickAction;
window.quickConnect = quickConnect;
window.quickConnectSheet = quickConnectSheet;
window.refreshUsageStats = refreshUsageStats;
window.registerActiveConversation = registerActiveConversation;
window.releaseFocus = releaseFocus;
window.removeSuggestions = removeSuggestions;
window.removeTyping = removeTyping;
window.renderFavorites = renderFavorites;
window.renderHelpCatalogue = renderHelpCatalogue;
window.renderHistoryList = renderHistoryList;
window.renderMarkdown = renderMarkdown;
window.renderPinnedList = renderPinnedList;
window.renderPromptSidebar = renderPromptSidebar;
window.renderSettingsModelList = renderSettingsModelList;
window.renderSettingsPinnedList = renderSettingsPinnedList;
window.renderSheetDropdown = renderSheetDropdown;
window.renderSlashMenu = renderSlashMenu;
window.renderTemplatesList = renderTemplatesList;
window.renderTokenError = renderTokenError;
window.replayTour = replayTour;
window.respondConfirm = respondConfirm;
window.restoreWatchStateForSheet = restoreWatchStateForSheet;
window.saveCurrentConversation = saveCurrentConversation;
window.saveFavorites = saveFavorites;
window.saveHistory = saveHistory;
window.savePinnedMessages = savePinnedMessages;
window.savePinnedSheets = savePinnedSheets;
window.saveRememberedToken = saveRememberedToken;
window.saveSessionInfo = saveSessionInfo;
window.saveTemplate = saveTemplate;
window.saveTemplatesArr = saveTemplatesArr;
window.saveWatchState = saveWatchState;
window.send = send;
window.sessionAuthHeaders = sessionAuthHeaders;
window.sessionAuthQuery = sessionAuthQuery;
window.setAgentRunning = setAgentRunning;
window.settingsPinSheet = settingsPinSheet;
window.settingsSwitchModel = settingsSwitchModel;
window.settingsSwitchSheet = settingsSwitchSheet;
window.settingsUnpinSheet = settingsUnpinSheet;
window.showNotification = showNotification;
window.showReconnectBanner = showReconnectBanner;
window.showToast = showToast;
window.showTourStep = showTourStep;
window.showTyping = showTyping;
window.showValidateSkeleton = showValidateSkeleton;
window.skipTour = skipTour;
window.speakAssistantMessage = speakAssistantMessage;
window.startFirstMessageHint = startFirstMessageHint;
window.startTour = startTour;
window.startVoice = startVoice;
window.startWatch = startWatch;
window.startWebhookPolling = startWebhookPolling;
window.stopVoice = stopVoice;
window.stopWatch = stopWatch;
window.submitBugReport = submitBugReport;
window.switchModel = switchModel;
window.switchSheet = switchSheet;
window.switchSheetTab = switchSheetTab;
window.talkToSelectedSheet = talkToSelectedSheet;
window.timeAgo = timeAgo;
window.toggleApiKeyField = toggleApiKeyField;
window.toggleFavStar = toggleFavStar;
window.toggleHelpCategory = toggleHelpCategory;
window.toggleHistory = toggleHistory;
window.toggleMobileHeaderMenu = toggleMobileHeaderMenu;
window.togglePinMessage = togglePinMessage;
window.togglePromptSidebar = togglePromptSidebar;
window.togglePromptSidebarCategory = togglePromptSidebarCategory;
window.toggleSettingsModal = toggleSettingsModal;
window.toggleShortcutsModal = toggleShortcutsModal;
window.toggleVoice = toggleVoice;
window.toggleWatchPopover = toggleWatchPopover;
window.trapFocus = trapFocus;
window.updateApiKeyBadge = updateApiKeyBadge;
window.updateSlashMenu = updateSlashMenu;
window.useTemplate = useTemplate;
window.validateToken = validateToken;
