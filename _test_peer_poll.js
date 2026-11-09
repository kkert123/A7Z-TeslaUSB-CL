#!/usr/bin/env node
/**
 * 僚机卡片「切换中」状态机回归测试（M138）
 * =====================================
 * 现场：B 端升级成功后，A 页面红框一直显示「正在切换 / 切换中…（idle）」，
 * 必须整页刷新才恢复；卡片里的「刷新」按钮点了也没用。
 *
 * 两个缺陷都在前端：
 *   ① startPeerPoll 只把 healthy / rolled_back / failed 当终态，
 *      idle 落进 else → 一直打印「切换中…（idle）」，且**再也不调 loadPeer**
 *      → 卡片停在旧版本（B 切换成功后 journal 被重置/读不到，只会回 idle）。
 *   ② 「刷新」按钮跑 loadPeer 后，3 秒一次的轮询又把提示改回「切换中…」，
 *      看起来像刷新无效。
 *
 * 这里把「终态判定」抽成了纯函数 peerPollKind(st)，用 node 直接钉死；
 * 另外用静态断言钉住三条接线（刷新先停轮询、终态必停轮询、idle 不再兜底成灰字）。
 *
 * 跑法：node _test_peer_poll.js
 */
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, 'templates', 'system.html');
const src = fs.readFileSync(SRC, 'utf-8');

let pass = 0, fail = 0;
const failed = [];
function ok(name, cond, detail) {
  if (cond) { pass++; console.log('PASS ' + name); }
  else { fail++; failed.push(name); console.log('FAIL ' + name + (detail ? '  ' + detail : '')); }
}
function eq(name, got, want) {
  ok(name, got === want, 'got=' + JSON.stringify(got) + ' want=' + JSON.stringify(want));
}

// ── 1. 取出纯函数并求值 ────────────────────────────────────
const m = src.match(/function peerPollKind\(st\)\s*\{[\s\S]*?\n\}/);
if (!m) {
  console.log('FAIL 模板里找不到 peerPollKind');
  process.exit(1);
}
const peerPollKind = new Function('return (' + m[0] + ')')();

eq('healthy  → done', peerPollKind('healthy'), 'done');
eq('rolled_back → failed', peerPollKind('rolled_back'), 'failed');
eq('failed   → failed', peerPollKind('failed'), 'failed');
// ★ 核心回归：idle 必须是终态，否则卡片永远停在「切换中」
eq('idle     → gone（核心回归）', peerPollKind('idle'), 'gone');
eq('verified → gone', peerPollKind('verified'), 'gone');
eq('空串     → gone', peerPollKind(''), 'gone');
eq('null     → gone', peerPollKind(null), 'gone');
eq('undefined→ gone', peerPollKind(undefined), 'gone');
eq('committed → running', peerPollKind('committed'), 'running');
eq('未知状态  → running（不误判成终态）', peerPollKind('whatever'), 'running');

// ── 2. 接线静态断言 ───────────────────────────────────────
const refreshBtn = src.match(/onclick="([^"]*)"[^>]*>刷新<\/button>/);
ok('2.1 刷新按钮调用 peerRefreshNow()',
  !!refreshBtn && refreshBtn[1].trim() === 'peerRefreshNow()',
  refreshBtn ? refreshBtn[1] : 'not found');

const fnRefresh = src.match(/function peerRefreshNow\(\)\s*\{[\s\S]*?\n\}/);
ok('2.2 peerRefreshNow 存在', !!fnRefresh);
if (fnRefresh) {
  const body = fnRefresh[0];
  const iStop = body.indexOf('stopPeerPoll()');
  const iLoad = body.indexOf('loadPeer(');
  ok('2.3 刷新先停轮询再 loadPeer（否则提示被轮询改回去）',
    iStop >= 0 && iLoad > iStop, body.replace(/\s+/g, ' '));
  ok('2.4 刷新也停 watch', body.indexOf('peerStopWatch()') >= 0, body.replace(/\s+/g, ' '));
}

const fnPoll = src.match(/function startPeerPoll\(\)\s*\{[\s\S]*?\n  \}, 3000\);\n\}/);
ok('2.5 startPeerPoll 找到', !!fnPoll);
if (fnPoll) {
  const b = fnPoll[0];
  ok('2.6 轮询用 peerPollKind 判定', b.indexOf('peerPollKind(') >= 0);
  ok('2.7 gone 分支会停轮询', /kind === 'gone'[\s\S]{0,400}stopPeerPoll\(\)/.test(b));
  ok('2.8 done/failed 分支都会停轮询',
    (b.match(/stopPeerPoll\(\)/g) || []).length >= 4,
    'stopPeerPoll 次数=' + (b.match(/stopPeerPoll\(\)/g) || []).length);
  ok('2.9 终态刷新保留收尾文案（loadPeer(true, true)）',
    b.indexOf('loadPeer(true, true)') >= 0);
  ok('2.10 超时不是静默返回', /轮询超时/.test(b));
}

// ── 3. 「· idle」灰字回归 ─────────────────────────────────
// 只看真正的代码行：注释里出现这个反例是有意义的（它就是踩坑说明）
const codeLines = src.split('\n').filter(function (l) {
  return l.trim().indexOf('//') !== 0;
}).join('\n');
ok('3.1 不再用 stText[st] || … 兜底（空串会被兜成「· idle」）',
  codeLines.indexOf("stText[st] || ('· ' + st)") < 0);
ok('3.2 改用 hasOwnProperty 判定',
  src.indexOf('hasOwnProperty.call(stText, st)') >= 0);

// ── 4. 旧实现的反面断言（防止改回去）──────────────────────
ok('4.1 不再有「else 一律切换中」的写法',
  !/\}\s*else\s*\{\s*peerMsg\('切换中…（'/.test(src));
ok('4.2 peerMsg 支持 warn 三态', /tone === 'warn'/.test(src));

console.log('\n' + pass + '/' + (pass + fail) + ' passed');
if (failed.length) console.log('FAILED: ' + failed.join(', '));
process.exit(fail === 0 ? 0 : 1);
