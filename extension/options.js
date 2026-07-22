import { DEFAULT_ORIGIN, ORIGIN_KEY } from './constants.js';
import { initOAuthOptions } from './oauth-options.js';

async function load() {
  const { [ORIGIN_KEY]: v } = await chrome.storage.sync.get({ [ORIGIN_KEY]: DEFAULT_ORIGIN });
  document.getElementById('origin').value = v || DEFAULT_ORIGIN;
}

async function reloadSidePanel() {
  try {
    await chrome.runtime.sendMessage({ type: 'reload_panel' });
    const st = document.getElementById('status');
    st.textContent = 'Side panel reload requested.';
    setTimeout(() => {
      if (st.textContent === 'Side panel reload requested.') st.textContent = '';
    }, 3000);
  } catch {
    const st = document.getElementById('status');
    st.textContent = 'Open the side panel first, then click Reload panel again.';
    setTimeout(() => {
      st.textContent = '';
    }, 4000);
  }
}

document.getElementById('form').addEventListener('submit', async (e) => {
  e.preventDefault();
  let v = document.getElementById('origin').value.trim() || DEFAULT_ORIGIN;
  v = v.replace(/\/$/, '');
  await chrome.storage.sync.set({ [ORIGIN_KEY]: v });
  try {
    await chrome.runtime.sendMessage({ type: 'origin_updated', origin: v });
  } catch {
    /* background may be asleep; permission prompt on next panel open */
  }
  const st = document.getElementById('status');
  st.textContent = 'Saved. Reload the side panel to use the new URL.';
  setTimeout(() => {
    st.textContent = '';
  }, 4000);
});

document.getElementById('reload-panel')?.addEventListener('click', (e) => {
  e.preventDefault();
  reloadSidePanel();
});

load();
initOAuthOptions();
