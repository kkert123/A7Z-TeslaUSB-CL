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

// ── 5. 预置按钮判定（M146：码过期后按钮永久灰 → 死锁）──────────
// 现场：B 停在 verified（包已暂存等确认），确认码 600 秒过期后，
// 「预置到僚机」被 st !== 'verified' 永久禁用，而红字又提示"请重新点预置" ——
// 提示指向的正是自己禁掉的按钮，用户无路可走。
function fnSrc(name) {
  const m = src.match(new RegExp('function ' + name + '\\([\\s\\S]*?\\n\\}'));
  if (!m) { console.log('FAIL 模板里找不到 ' + name); process.exit(1); }
  return m[0];
}
const prepDecision = new Function(
  [fnSrc('peerVerTuple'), fnSrc('peerVerCmp'), fnSrc('peerPrepDecision')].join('\n')
  + '\nreturn peerPrepDecision;')();

const dIdle = prepDecision('idle', 0, true, '1.1.7', '1.1.5');
ok('5.1 空闲 + 有更新包 → 可预置', dIdle.can === true, JSON.stringify(dIdle));
eq('5.2 标签写目标版本', dIdle.label, '预置 v1.1.7 到僚机');

const dWaiting = prepDecision('verified', 300, true, '1.1.7', '1.1.5');
ok('5.3 待确认且码仍有效 → 禁（别覆盖正在等确认的 job）', dWaiting.can === false,
  JSON.stringify(dWaiting));

// ★ 核心回归：码过期必须放开，否则永远出不去
const dExpired = prepDecision('verified', 0, true, '1.1.7', '1.1.5');
ok('5.4 待确认但码已过期 → 可重新预置（核心回归）', dExpired.can === true,
  JSON.stringify(dExpired));
ok('5.5 过期时标签要说明会换新码', dExpired.label.indexOf('换新码') >= 0, dExpired.label);
ok('5.6 expires_in 是字符串 "0" 也算过期', prepDecision('verified', '0', true, '1.1.7', '1.1.5').can === true);
ok('5.7 负数（时钟漂移）也算过期', prepDecision('verified', -5, true, '1.1.7', '1.1.5').can === true);

ok('5.8 committed（正在切换）即使过期也禁',
  prepDecision('committed', 0, true, '1.1.7', '1.1.5').can === false);
ok('5.9 僚机未开远程升级 → 禁',
  prepDecision('idle', 0, false, '1.1.7', '1.1.5').can === false);
ok('5.10 没有更新的包 → 禁且提示已是最新',
  prepDecision('idle', 0, true, '1.1.5', '1.1.5').can === false
  && prepDecision('idle', 0, true, '1.1.5', '1.1.5').label === '已是最新');
ok('5.11 包库为空 → 已是最新',
  prepDecision('idle', 0, true, '', '1.1.5').label === '已是最新');

// 接线静态断言
ok('5.12 存在「换新码」按钮且复用 peerUpgrade',
  /id="btn-peer-recoded"[^>]*onclick="peerUpgrade\(\)"/.test(src.replace(/\s+/g, ' ')));
ok('5.13 loadPeer 不再硬编码 st !== \'verified\' 禁按钮',
  codeLines.indexOf("st !== 'verified'") < 0);
ok('5.14 canUp 改用 peerPrepDecision', src.indexOf('peerPrepDecision(st, p.confirm_expires_in') >= 0);
ok('5.15 倒计时归零会放开按钮（调 peerOnCodeExpired）',
  /left <= 0[\s\S]{0,300}peerOnCodeExpired\(\)/.test(src));
ok('5.16 过期文案不再指向被禁用的「预置到僚机」',
  src.indexOf('请重新点「预置到僚机」生成新码') < 0);

console.log('\n' + pass + '/' + (pass + fail) + ' passed');
if (failed.length) console.log('FAILED: ' + failed.join(', '));
process.exit(fail === 0 ? 0 : 1);
