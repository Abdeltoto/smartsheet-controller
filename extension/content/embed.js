/**
 * Injects minimal layout when the Controller is embedded (side panel iframe)
 * or explicitly (?ssc_ext=1). Does not modify the main repo.
 */
(function () {
  function shouldEmbed() {
    try {
      if (new URLSearchParams(window.location.search).get('ssc_ext') === '1') {
        return true;
      }
    } catch (e) {
      /* ignore */
    }
    try {
      return window.self !== window.top;
    } catch (e) {
      return false;
    }
  }

  if (!shouldEmbed()) return;

  document.documentElement.classList.add('ssc-ext-embed');

  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = chrome.runtime.getURL('content/embed.css');
  const root = document.head || document.documentElement;
  root.insertBefore(link, root.firstChild);
})();
