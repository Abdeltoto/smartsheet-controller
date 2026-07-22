/**
 * Early embed class at document_start (before paint). Layout rules live in
 * frontend/index.html under html.ssc-embed — this file only toggles the flag.
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

  document.documentElement.classList.add('ssc-embed');
})();
