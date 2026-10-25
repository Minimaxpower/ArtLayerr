(function() {
  const html = document.documentElement;
  const toggle = document.getElementById('theme-toggle');
  const stored = localStorage.getItem('theme');
  const mq = window.matchMedia('(prefers-color-scheme: dark)');

  function apply(theme) {
    html.setAttribute('data-bs-theme', theme);
    // the button shows the mode you would switch to (texts come from the page, translated)
    toggle.textContent = theme === 'dark' ? toggle.dataset.labelLight : toggle.dataset.labelDark;
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
    el.textContent = d.toLocaleString(document.documentElement.lang || undefined, { dateStyle: 'short', timeStyle: 'short' });
    el.setAttribute('datetime', d.toISOString());
  });
})();

// Press "/" anywhere (outside a field) to jump to the global search box
(function() {
  document.addEventListener('keydown', function(e) {
    if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
    var tag = (document.activeElement && document.activeElement.tagName) || '';
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || (document.activeElement && document.activeElement.isContentEditable)) return;
    var box = document.getElementById('global-search');
    if (!box) return;
    e.preventDefault();
    box.focus();
    box.select();
  });
})();
