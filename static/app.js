/**
 * TeslaUSB Neo Web — Common JS
 */

/* ─────────────────────────────────────────────────────────────
 * 全局 CSRF 注入（M116 / F2）
 *
 * 守卫在「已启用登录保护」后要求所有写请求带 X-CSRF-Token，
 * 而前端 60+ 处 fetch 分散在各模板里、无法逐个改。这里统一包装
 * window.fetch，对**同源**写请求自动补头。
 *
 * 三条约束（缺一不可）：
 *   1 只加给同源请求 —— 否则 token 会随第三方请求泄露到外部域；
 *   2 只加给写方法   —— GET 不需要，避免无谓的头部膨胀；
 *   3 已存在则不覆盖 —— 允许个别调用点（如重登后）显式指定新 token。
 *
 * 必须在任何业务代码之前执行，因此放在 app.js 顶部。
 * ───────────────────────────────────────────────────────────── */
(function () {
  'use strict';
  if (typeof window === 'undefined' || typeof window.fetch !== 'function') return;

  var orig = window.fetch;
  var MUTATING = { POST: 1, PUT: 1, PATCH: 1, DELETE: 1 };

  function sameOrigin(url) {
    try {
      return new URL(url, window.location.href).origin === window.location.origin;
    } catch (e) {
      return false;
    }
  }

  window.fetch = function (input, init) {
    try {
      var url = (typeof input === 'string') ? input : (input && input.url) || '';
      var method = String(
        (init && init.method) || (input && input.method) || 'GET'
      ).toUpperCase();
      var token = window.__CSRF__;

      if (MUTATING[method] && token && sameOrigin(url)) {
        // 合并原 headers（init 优先，其次 Request 自带），不丢 Content-Type 等
        var merged = new Headers(
          (init && init.headers) || (input && input.headers) || {}
        );
        if (!merged.has('X-CSRF-Token')) merged.set('X-CSRF-Token', token);
        init = init ? Object.assign({}, init) : {};
        init.headers = merged;
      }
    } catch (e) {
      /* 头部处理失败不应阻断请求本身 */
    }
    return orig.call(this, input, init);
  };
})();

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
