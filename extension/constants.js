/** Shared defaults for the Chrome extension (importable from MV3 modules). */

export const ORIGIN_KEY = 'controllerOrigin';
export const DEFAULT_ORIGIN = 'http://127.0.0.1:8100';
export const SSC_EXT_PARAM = 'ssc_ext';
export const OAUTH_TOKEN_KEY = 'smartsheet_oauth_access_token';
export const PENDING_PROMPT_KEY = 'pendingPrompt';

/** postMessage protocol between extension shell and Controller iframe */
export const MSG_SOURCE_EXT = 'ssc-extension';
export const MSG_SOURCE_APP = 'ssc-controller';

export const MSG_TYPES = {
  READY: 'ready',
  APPLY_TOKEN: 'apply_token',
  INSERT_PROMPT: 'insert_prompt',
};

export function normalizeOrigin(value) {
  let base = String(value || DEFAULT_ORIGIN).trim();
  if (!/^https?:\/\//i.test(base)) base = DEFAULT_ORIGIN;
  return base.replace(/\/$/, '');
}

export function isValidSheetId(id) {
  return /^\d{6,}$/.test(String(id || ''));
}

/** Build the Controller URL loaded in the side-panel iframe. */
export function buildControllerAppUrl(origin, sheetId) {
  const base = normalizeOrigin(origin);
  const params = new URLSearchParams();
  params.set(SSC_EXT_PARAM, '1');
  if (sheetId && isValidSheetId(sheetId)) {
    params.set('sheet_id', String(sheetId));
  }
  return `${base}/?${params.toString()}`;
}

export async function getStoredOrigin() {
  const { [ORIGIN_KEY]: v } = await chrome.storage.sync.get({
    [ORIGIN_KEY]: DEFAULT_ORIGIN,
  });
  return normalizeOrigin(v);
}
