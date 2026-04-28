async function getBaseUrl() {
  const { controllerOrigin } = await chrome.storage.sync.get({
    controllerOrigin: 'http://127.0.0.1:8100',
  });
  let base = String(controllerOrigin || 'http://127.0.0.1:8100').trim();
  if (!/^https?:\/\//i.test(base)) base = 'http://127.0.0.1:8100';
  return base.replace(/\/$/, '');
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function norm(s) {
  return (s || '').toLowerCase();
}

async function main() {
  const base = await getBaseUrl();
  document.getElementById('base-label').textContent = base;

  const err = document.getElementById('err');
  const root = document.getElementById('root');

  let data;
  try {
    const r = await fetch(`${base}/api/prompts`, { headers: { Accept: 'application/json' } });
    if (!r.ok) {
      const t = await r.text();
      throw new Error(t || r.statusText);
    }
    data = await r.json();
  } catch (e) {
    err.style.display = 'block';
    err.textContent =
      e && e.message
        ? `Could not load prompts: ${e.message}`
        : 'Could not load prompts. Is the server running?';
    return;
  }

  const categories = data.categories || [];

  function render(filter) {
    const q = norm(filter);
    root.innerHTML = '';
    for (const cat of categories) {
      const prompts = (cat.prompts || []).filter((p) => {
        if (!q) return true;
        const blob = norm(p.title) + norm(p.prompt) + (p.tags || []).join(' ');
        return blob.includes(q);
      });
      if (!prompts.length) continue;

      const sec = document.createElement('section');
      sec.className = 'cat';
      sec.innerHTML = `<div class="cat-title">${esc(cat.title || cat.id || 'Category')}</div>`;
      for (const p of prompts) {
        const card = document.createElement('div');
        card.className = 'card';
        card.innerHTML = `
          <h3>${esc(p.title || 'Untitled')}</h3>
          <pre>${esc(p.prompt || '')}</pre>
          <div class="actions"><button type="button">Copy</button></div>
        `;
        card.querySelector('button').addEventListener('click', async () => {
          await navigator.clipboard.writeText(p.prompt || '');
        });
        sec.appendChild(card);
      }
      root.appendChild(sec);
    }
    if (!root.children.length) {
      root.innerHTML = '<p class="sub">No prompts match your search.</p>';
    }
  }

  render('');
  document.getElementById('q').addEventListener('input', (e) => {
    render(e.target.value || '');
  });
}

main();
