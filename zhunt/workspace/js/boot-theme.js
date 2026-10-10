// Classic (non-module) script, runs before first paint so the page never flashes the wrong theme.
(function () {
  try {
    var t = localStorage.getItem('zhunt.theme');
    if (!t || !/^[a-z0-9][a-z0-9_-]{0,39}$/.test(t)) {
      t = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    }
    document.documentElement.setAttribute('data-theme', t);
  } catch (e) { /* storage blocked: stay on the default */ }
})();
