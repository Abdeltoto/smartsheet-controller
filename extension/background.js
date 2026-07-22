import { DEFAULT_ORIGIN, ORIGIN_KEY, isValidSheetId } from './constants.js';
import { ensureOriginPermission } from './permissions.js';

/**
 * Detects sheet IDs from Smartsheet app URLs.
 * Path pattern: .../sheets/<numericId>
 */
function extractSheetId(url) {
  try {
    const u = new URL(url);
    if (!/\.smartsheet\.com$/i.test(u.hostname)) return null;
    const m = u.pathname.match(/\/sheets\/(\d{6,})\/?/);
    return m ? m[1] : null;
  } catch {
    return null;
  }
}

async function updateSheetContextForTab(tab) {
  if (!tab?.id) return;

  const sheetId = tab.url ? extractSheetId(tab.url) : null;

  if (sheetId && isValidSheetId(sheetId)) {
    await chrome.storage.session.set({
      detectedSheetId: sheetId,
      detectedSheetUrl: tab.url,
      detectedAt: Date.now(),
    });
    await chrome.action.setBadgeText({ text: '●', tabId: tab.id });
    await chrome.action.setBadgeBackgroundColor({ color: '#3B82F6', tabId: tab.id });
    return;
  }

  await chrome.action.setBadgeText({ text: '', tabId: tab.id });

  try {
    const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (active?.id === tab.id) {
      await chrome.storage.session.remove([
        'detectedSheetId',
        'detectedSheetUrl',
        'detectedAt',
      ]);
    }
  } catch {
    /* tab may have closed */
  }
}

async function syncControllerPermissions(origin) {
  await ensureOriginPermission(origin || DEFAULT_ORIGIN);
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
  chrome.storage.sync
    .get({ [ORIGIN_KEY]: DEFAULT_ORIGIN })
    .then((data) => syncControllerPermissions(data[ORIGIN_KEY]));
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === 'origin_updated') {
    syncControllerPermissions(msg.origin).then((ok) => sendResponse({ ok }));
    return true;
  }
  return false;
});

chrome.tabs.onUpdated.addListener((_tabId, info, tab) => {
  if (info.status !== 'complete') return;
  updateSheetContextForTab(tab);
});

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  try {
    const tab = await chrome.tabs.get(tabId);
    await updateSheetContextForTab(tab);
  } catch {
    /* tab closed while switching */
  }
});
