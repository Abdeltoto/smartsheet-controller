/**
 * Detects sheet IDs from Smartsheet app URLs and stores them for the side panel iframe.
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

chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
});

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (!tab?.url || info.status !== 'complete') return;
  const sheetId = extractSheetId(tab.url);
  if (!sheetId) return;
  chrome.storage.session.set({
    detectedSheetId: sheetId,
    detectedSheetUrl: tab.url,
    detectedAt: Date.now(),
  });
  chrome.action.setBadgeText({ text: '●', tabId });
  chrome.action.setBadgeBackgroundColor({ color: '#3B82F6', tabId });
});
