/**
 * Vite entry — loads vendor libs then the legacy app bundle.
 * Inline HTML handlers remain on window via legacy/app.js exports.
 */
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import Chart from 'chart.js/auto';

import './legacy/app.js';

window.marked = marked;
window.DOMPurify = DOMPurify;
window.Chart = Chart;

marked.setOptions({ breaks: true, gfm: true, headerIds: false, mangle: false });

document.addEventListener('DOMContentLoaded', () => {
  if (typeof window.initVoice === 'function') window.initVoice();
  if (typeof window.initKeyboardShortcuts === 'function') window.initKeyboardShortcuts();
  if (typeof window.initCsvDragDrop === 'function') window.initCsvDragDrop();
  if (typeof window.renderFavorites === 'function') window.renderFavorites();

  const remembered = typeof window.getRememberedToken === 'function'
    ? window.getRememberedToken()
    : '';
  if (remembered) {
    const ti = document.getElementById('ss-token');
    const cb = document.getElementById('remember-token');
    if (ti) ti.value = remembered;
    if (cb) cb.checked = true;
  }

  if (typeof window.initExtensionEmbedMode === 'function') window.initExtensionEmbedMode();
  if (typeof window.initExtensionBridge === 'function') window.initExtensionBridge();
  if (typeof window.applySheetIdQueryParam === 'function') window.applySheetIdQueryParam();
  if (typeof window.checkEnvReady === 'function') window.checkEnvReady();
});
