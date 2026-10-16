(function() {
  const html = document.documentElement;
  const toggle = document.getElementById('theme-toggle');
  const stored = localStorage.getItem('theme');
  const mq = window.matchMedia('(prefers-color-scheme: dark)');

  function apply(theme) {
    html.setAttribute('data-bs-theme', theme);
    toggle.textContent = theme === 'dark' ? '☀️' : '🌙';
  }

  apply(stored || (mq.matches ? 'dark' : 'light'));

  toggle.addEventListener('click', () => {
    const next = html.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark';
    localStorage.setItem('theme', next);
    apply(next);
  });
})();

// Show "last checked" times in the viewer's own timezone and locale
(function() {
  document.querySelectorAll('time.local-time[data-ts]').forEach(function(el) {
    var ts = parseFloat(el.dataset.ts);
    if (!ts) return;
    var d = new Date(ts * 1000);
    el.textContent = d.toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' });
    el.setAttribute('datetime', d.toISOString());
  });
})();