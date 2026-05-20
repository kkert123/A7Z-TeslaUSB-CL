/**
 * TeslaUSB Neo Web — Common JS
 */
const App = {
  post: async (url, data) => (await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) })).json(),
  get: async (url) => (await fetch(url)).json(),
  formatBytes(b) { if (!b) return '0 B'; const k = 1024, s = ['B','KB','MB','GB','TB'], i = Math.floor(Math.log(b) / Math.log(k)); return parseFloat((b / Math.pow(k, i)).toFixed(2)) + ' ' + s[i]; },
};

document.addEventListener('DOMContentLoaded', () => {
  const path = window.location.pathname;
  document.querySelectorAll('.nav-link').forEach(link => {
    const href = link.getAttribute('href');
    if (href === path || (href !== '/' && path.startsWith(href))) link.classList.add('active');
  });
});
