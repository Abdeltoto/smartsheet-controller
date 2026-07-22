import { normalizeOrigin } from './constants.js';

/** Turn a Controller base URL into a Chrome host permission pattern. */
export function originToPattern(origin) {
  const base = normalizeOrigin(origin);
  return `${base}/*`;
}

/** Request optional host permission for the user's Controller origin (no-op if already granted). */
export async function ensureOriginPermission(origin) {
  const pattern = originToPattern(origin);
  try {
    if (await chrome.permissions.contains({ origins: [pattern] })) {
      return true;
    }
    return await chrome.permissions.request({ origins: [pattern] });
  } catch {
    return false;
  }
}
