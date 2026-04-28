const ORIGIN_KEY = 'controllerOrigin';
const DEFAULT_ORIGIN = 'http://127.0.0.1:8100';

async function load() {
  const { [ORIGIN_KEY]: v } = await chrome.storage.sync.get({ [ORIGIN_KEY]: DEFAULT_ORIGIN });
  document.getElementById('origin').value = v || DEFAULT_ORIGIN;
}

document.getElementById('form').addEventListener('submit', async (e) => {
  e.preventDefault();
  let v = document.getElementById('origin').value.trim() || DEFAULT_ORIGIN;
  v = v.replace(/\/$/, '');
  await chrome.storage.sync.set({ [ORIGIN_KEY]: v });
  const st = document.getElementById('status');
  st.textContent = 'Saved. Reload the side panel to use the new URL.';
  setTimeout(() => { st.textContent = ''; }, 4000);
});

load();
