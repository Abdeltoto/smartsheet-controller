import { MSG_SOURCE_APP, MSG_SOURCE_EXT, MSG_TYPES } from './constants.js';

/**
 * Bidirectional postMessage bridge between the side-panel shell and the iframe.
 */
export function createIframeBridge(frame, allowedOrigin) {
  let ready = false;
  const queue = [];
  let onReadyCallback = null;

  function post(type, payload = {}) {
    const msg = { source: MSG_SOURCE_EXT, type, payload };
    if (!ready || !frame.contentWindow) {
      queue.push(msg);
      return;
    }
    frame.contentWindow.postMessage(msg, allowedOrigin);
  }

  function flushQueue() {
    if (!ready || !frame.contentWindow) return;
    while (queue.length) {
      const msg = queue.shift();
      frame.contentWindow.postMessage(msg, allowedOrigin);
    }
  }

  function handleMessage(event) {
    if (event.source !== frame.contentWindow) return;
    if (event.origin !== allowedOrigin) return;
    const data = event.data;
    if (!data || data.source !== MSG_SOURCE_APP) return;
    if (data.type === MSG_TYPES.READY) {
      ready = true;
      flushQueue();
      if (onReadyCallback) onReadyCallback();
    }
  }

  window.addEventListener('message', handleMessage);

  frame.addEventListener('load', () => {
    ready = false;
  });

  return {
    post,
    set onReady(fn) {
      onReadyCallback = fn;
    },
    get onReady() {
      return onReadyCallback;
    },
    destroy() {
      window.removeEventListener('message', handleMessage);
    },
  };
}
