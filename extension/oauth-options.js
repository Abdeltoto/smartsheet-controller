/** Optional Smartsheet OAuth via chrome.identity + server /exchange */

const STORAGE_TOKEN = 'smartsheet_oauth_access_token';

async function getBaseUrl() {
  const { controllerOrigin } = await chrome.storage.sync.get({
    controllerOrigin: 'http://127.0.0.1:8100',
  });
  let base = String(controllerOrigin || 'http://127.0.0.1:8100').trim();
  if (!/^https?:\/\//i.test(base)) base = 'http://127.0.0.1:8100';
  return base.replace(/\/$/, '');
}

function $(id) {
  return document.getElementById(id);
}

async function renderOAuthSection() {
  const mount = $('oauth-mount');
  if (!mount) return;

  const base = await getBaseUrl();
  const redirectDefault = chrome.identity.getRedirectURL();

  try {
    const r = await fetch(`${base}/api/oauth/smartsheet/config`);
    const cfg = await r.json();
    if (!r.ok || !cfg) throw new Error('Config request failed');

    if (!cfg.enabled) {
      mount.innerHTML = `
        <p class="mini">OAuth is disabled until you set <code>SMARTSHEET_OAUTH_CLIENT_ID</code> and
        <code>SMARTSHEET_OAUTH_CLIENT_SECRET</code> on the server and restart.</p>
        <p class="mini">In Smartsheet Developer Tools, add this <strong>redirect URL</strong> for your app:</p>
        <p class="mini"><code id="oauth-redirect">${redirectDefault}</code></p>
      `;
      return;
    }

    mount.innerHTML = `
      <p class="mini">Uses your registered Smartsheet app. Redirect must include:</p>
      <p class="mini"><code id="oauth-redirect">${redirectDefault}</code></p>
      <button type="button" class="btn-secondary" id="oauth-signin">Sign in with Smartsheet</button>
      <div id="oauth-error" class="error-text" style="display:none;"></div>
      <div class="oauth-result" id="oauth-result" style="display:none;">
        <label for="oauth-token-out">Access token (paste into Controller)</label>
        <textarea id="oauth-token-out" readonly rows="3"></textarea>
        <button type="button" class="btn-secondary" id="oauth-copy">Copy token</button>
      </div>
    `;

    $('oauth-signin').addEventListener('click', () => runOAuthFlow(base, cfg));
    $('oauth-copy')?.addEventListener('click', async () => {
      const ta = $('oauth-token-out');
      if (ta?.value) {
        await navigator.clipboard.writeText(ta.value);
        ta.select();
      }
    });
  } catch {
    mount.innerHTML = `<p class="mini">Could not reach <code>${base}</code>. Save the Controller URL above, then reload this page.</p>`;
  }
}

async function runOAuthFlow(base, cfg) {
  const errEl = $('oauth-error');
  const resEl = $('oauth-result');
  const out = $('oauth-token-out');
  errEl.style.display = 'none';
  resEl.style.display = 'none';

  const redirectUri = chrome.identity.getRedirectURL();
  const state = crypto.randomUUID();
  const authUrl =
    `${cfg.authorize_url}?response_type=code` +
    `&client_id=${encodeURIComponent(cfg.client_id)}` +
    `&redirect_uri=${encodeURIComponent(redirectUri)}` +
    `&scope=${encodeURIComponent(cfg.scope || '')}` +
    `&state=${encodeURIComponent(state)}`;

  chrome.identity.launchWebAuthFlow({ url: authUrl, interactive: true }, async (responseUrl) => {
    if (chrome.runtime.lastError) {
      errEl.textContent = chrome.runtime.lastError.message || 'Auth cancelled.';
      errEl.style.display = 'block';
      return;
    }
    if (!responseUrl) return;

    let u;
    try {
      u = new URL(responseUrl);
    } catch {
      errEl.textContent = 'Unexpected redirect.';
      errEl.style.display = 'block';
      return;
    }

    if (u.searchParams.get('error')) {
      errEl.textContent = u.searchParams.get('error_description') || u.searchParams.get('error');
      errEl.style.display = 'block';
      return;
    }

    if (u.searchParams.get('state') !== state) {
      errEl.textContent = 'Invalid OAuth state.';
      errEl.style.display = 'block';
      return;
    }

    const code = u.searchParams.get('code');
    if (!code) {
      errEl.textContent = 'No authorization code returned.';
      errEl.style.display = 'block';
      return;
    }

    try {
      const r = await fetch(`${base}/api/oauth/smartsheet/exchange`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code, redirect_uri: redirectUri }),
      });
      const data = await r.json();
      if (!r.ok) {
        throw new Error(data.detail || data.error || r.statusText);
      }
      const tok = data.access_token;
      if (!tok) throw new Error('No access_token in response');
      await chrome.storage.local.set({ [STORAGE_TOKEN]: tok });
      out.value = tok;
      resEl.style.display = 'block';
    } catch (e) {
      errEl.textContent = e && e.message ? e.message : String(e);
      errEl.style.display = 'block';
    }
  });
}

function openPromptsBrowser() {
  chrome.tabs.create({ url: chrome.runtime.getURL('prompts-browser.html') });
}

document.addEventListener('DOMContentLoaded', () => {
  renderOAuthSection();
  $('open-prompts')?.addEventListener('click', (e) => {
    e.preventDefault();
    openPromptsBrowser();
  });
});
