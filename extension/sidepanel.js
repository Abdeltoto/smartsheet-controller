function $(id) {
  return document.getElementById(id);
}

function shortenSheetId(id) {
  const s = String(id);
  if (s.length <= 14) return s;
  return `${s.slice(0, 6)}…${s.slice(-6)}`;
}

async function main() {
  const hintEl = $('hint');
  const dot = $('status-dot');
  const btnOpts = $('btn-opts');
  const frame = $('app');

  const { controllerOrigin } = await chrome.storage.sync.get({
    controllerOrigin: 'http://127.0.0.1:8100',
  });

  const { detectedSheetId } = await chrome.storage.session.get(['detectedSheetId']);

  let base = String(controllerOrigin || 'http://127.0.0.1:8100').trim();
  if (!/^https?:\/\//i.test(base)) base = 'http://127.0.0.1:8100';
  base = base.replace(/\/$/, '');

  const params = new URLSearchParams();
  params.set('ssc_ext', '1');
  if (detectedSheetId && /^\d{6,}$/.test(String(detectedSheetId))) {
    params.set('sheet_id', String(detectedSheetId));
  }

  const url = `${base}/?${params.toString()}`;
  const hasSheet = params.has('sheet_id');

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

  frame.src = url;

  btnOpts.addEventListener('click', () => chrome.runtime.openOptionsPage());
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
