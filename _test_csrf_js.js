// 验证 app.js 的 fetch 拦截器：同源写请求补头 / 外部域不加 / GET 不加 / 已有不覆盖
const fs = require('fs');
const vm = require('vm');

const src = fs.readFileSync('static/app.js', 'utf8');
const iife = src.slice(0, src.indexOf('const App'));
if (!iife.includes('window.fetch = function')) { console.error('FAIL 未找到拦截器'); process.exit(1); }

function run(csrf, origin, calls) {
  const win = {
    location: { href: origin + '/videos', origin },
    __CSRF__: csrf,
    fetch: (input, init) => { calls.push({ input, init }); return Promise.resolve('ok'); },
  };
  const sandbox = { window: win, Headers, URL, Object, String, console };
  vm.createContext(sandbox);
  vm.runInContext(iife, sandbox);
  return win.fetch;
}

let pass = 0, fail = 0;
function ck(name, cond, extra) {
  if (cond) { pass++; console.log('PASS', name); }
  else { fail++; console.log('FAIL', name, extra === undefined ? '' : JSON.stringify(extra)); }
}

const O = 'http://dev:5000';

// J1 同源 POST 自动加头
let calls = []; let f = run('tok123', O, calls);
f('/api/mode/switch', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
let h = calls[0].init.headers;
ck('J1 同源 POST 自动加 X-CSRF-Token', h.get('X-CSRF-Token') === 'tok123');
ck('J2 原有 Content-Type 保留', h.get('Content-Type') === 'application/json');

// J3 外部域不加（防 token 泄露）
calls = []; f = run('tok123', O, calls);
f('https://evil.com/collect', { method: 'POST' });
ck('J3 外部域 POST 不加 token', !calls[0].init || !calls[0].init.headers);

// J4 协议相对也判外部
calls = []; f = run('tok123', O, calls);
f('//evil.com/collect', { method: 'POST' });
ck('J4 协议相对 //evil.com 不加 token', !calls[0].init || !calls[0].init.headers);

// J5 GET 不加
calls = []; f = run('tok123', O, calls);
f('/api/system/info');
ck('J5 GET 不加 token', !calls[0].init || !calls[0].init.headers);

// J6 显式已有的不覆盖
calls = []; f = run('tok123', O, calls);
f('/api/x', { method: 'POST', headers: { 'X-CSRF-Token': 'explicit' } });
ck('J6 已有 token 不覆盖', calls[0].init.headers.get('X-CSRF-Token') === 'explicit');

// J7 无 token 时不加（未启用状态）
calls = []; f = run('', O, calls);
f('/api/x', { method: 'POST' });
ck('J7 无 __CSRF__ 时不加', !calls[0].init || !calls[0].init.headers);

// J8 PUT/PATCH/DELETE 也覆盖
['PUT','PATCH','DELETE'].forEach(m => {
  calls = []; f = run('tok123', O, calls);
  f('/api/x', { method: m });
  ck('J8 ' + m + ' 加 token', calls[0].init.headers.get('X-CSRF-Token') === 'tok123');
});

// J9 无 init 的 POST（方法在 Request 形态）不应崩
calls = []; f = run('tok123', O, calls);
try { f('/api/x'); ck('J9 无 init 不抛异常', true); } catch (e) { ck('J9 无 init 不抛异常', false, String(e)); }

console.log('\n结果: ' + pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
