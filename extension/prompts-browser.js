import {
  getStoredOrigin,
  PENDING_PROMPT_KEY,
} from './constants.js';

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function norm(s) {
  return (s || '').toLowerCase();
}

function getPromptText(li) {
  const pre = li.querySelector('.body');
  return pre ? pre.textContent : '';
}

async function sendToSidePanel(text) {
  const trimmed = String(text || '').trim();
  if (!trimmed) return;

  await chrome.storage.session.set({ [PENDING_PROMPT_KEY]: trimmed });

  try {
    const win = await chrome.windows.getCurrent();
    if (win?.id != null) {
      await chrome.sidePanel.open({ windowId: win.id });
    }
  } catch {
    /* sidePanel.open may fail on older builds */
  }

  try {
    await chrome.runtime.sendMessage({ type: 'send_prompt_to_panel', text: trimmed });
  } catch {
    /* side panel closed — pendingPrompt will apply on next open */
  }
}

async function main() {
  const base = await getStoredOrigin();
  document.getElementById('base-label').textContent = base;

  const err = document.getElementById('err');
  const root = document.getElementById('root');

  let data;
  try {
    const r = await fetch(`${base}/api/prompts`, { headers: { Accept: 'application/json' } });
    if (!r.ok) {
      throw new Error(`HTTP ${r.status}`);
    }
    data = await r.json();
  } catch (e) {
    err.style.display = 'block';
    err.textContent = `Could not load prompts from ${base}. Is the server running? ${
      e && e.message ? e.message : ''
    }`;
    return;
  }

  const categories = data.categories || [];
  root.innerHTML = categories
    .map(
      (cat) => `
    <section class="cat">
      <h2>${esc(cat.title || cat.id || 'Category')}</h2>
      <ul>
        ${(cat.prompts || [])
          .map(
            (p) => `
          <li>
            <div class="title">${esc(p.title || 'Prompt')}</div>
            <pre class="body">${esc(p.prompt || p.text || '')}</pre>
            <div class="actions">
              <button type="button" class="copy">Copy</button>
              <button type="button" class="send">Send to panel</button>
            </div>
          </li>`
          )
          .join('')}
      </ul>
    </section>`
    )
    .join('');

  root.querySelectorAll('li').forEach((li) => {
    const copyBtn = li.querySelector('.copy');
    const sendBtn = li.querySelector('.send');

    copyBtn?.addEventListener('click', async () => {
      const text = getPromptText(li);
      await navigator.clipboard.writeText(text);
      copyBtn.textContent = 'Copied';
      setTimeout(() => {
        copyBtn.textContent = 'Copy';
      }, 1500);
    });

    sendBtn?.addEventListener('click', async () => {
      const text = getPromptText(li);
      sendBtn.disabled = true;
      sendBtn.textContent = 'Sending…';
      try {
        await sendToSidePanel(text);
        sendBtn.textContent = 'Sent';
      } catch {
        sendBtn.textContent = 'Failed';
      }
      setTimeout(() => {
        sendBtn.disabled = false;
        sendBtn.textContent = 'Send to panel';
      }, 1800);
    });
  });

  const q = document.getElementById('q');
  q.addEventListener('input', () => {
    const needle = norm(q.value);
    root.querySelectorAll('.cat li').forEach((li) => {
      const hay = norm(li.textContent);
      li.style.display = !needle || hay.includes(needle) ? '' : 'none';
    });
    root.querySelectorAll('.cat').forEach((sec) => {
      const visible = [...sec.querySelectorAll('li')].some((li) => li.style.display !== 'none');
      sec.style.display = visible ? '' : 'none';
    });
  });
}

main();
