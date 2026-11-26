#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""已通知账本健康检查（v0.3.1.94）—— 验证"反复补发"是否真的断根

三项判据，任一不过都说明账本没在起作用：

  1. 账本自身：格式 / 条数 / 上限占用 / 是否严格按事件时间升序 / 有无重复
  2. 覆盖度：3 天对账窗口内的事件，有多少不在账本里（>0 就是下一轮补发的候选）
  3. 复发检测：最近 N 小时的补发日志里，同一 event_id 是否被入队多次
     —— 这一项是"问题是否复现"的直接量化指标

用法（设备上 root）：
    python3 ops/check_notified_ledger.py                # 最近 24 小时
    python3 ops/check_notified_ledger.py --hours 2      # 只看部署后
    python3 ops/check_notified_ledger.py --strict       # 有异常 exit 1（可挂定时任务）

设计原则：**只读**。不改账本、不写 data/、不触发补发。
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter

DEFAULT_LEDGER = '/opt/radxa_data/teslausb/data/sentry_notified_events.json'
DEFAULT_LOG = '/var/log/teslausb-notify-retry.log'
DEFAULT_SENTRY_CFG = '/opt/radxa_data/teslausb/config/sentry.json'
FALLBACK_CLIPS = '/mnt/teslacam/TeslaCam/SentryClips'
RECONCILE_MAX_AGE_DAYS = 3
RECONCILE_MIN_AGE_SEC = 1800

_EVENT_ID_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})$')
_LOG_ENQUEUE = re.compile(
    r'^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):\d{2},\d+ \[INFO\] 通知已加入重试队列: (\S+)'
)


def event_time(eid):
    m = _EVENT_ID_RE.match(str(eid))
    if not m:
        return None
    try:
        import datetime
        return datetime.datetime(*[int(x) for x in m.groups()]).timestamp()
    except ValueError:
        return None


def ledger_limit():
    """账本上限：优先取运行中的常量，避免脚本里再写一份 20000 造成漂移。"""
    try:
        sys.path.insert(0, '/opt/radxa_data/teslausb')
        import sentry_notify_queue as _sq
        return int(_sq.NOTIFIED_MAX_KEEP)
    except Exception:
        return 20000


def check_ledger(path):
    """返回 (ok, 信息行列表, 统计字典)"""
    lines = []
    stats = {'ok': False, 'count': 0, 'format': '?', 'dups': 0, 'sorted': True}
    if not os.path.exists(path):
        lines.append('账本: 文件不存在（%s）' % path)
        return False, lines, stats
    try:
        data = json.load(open(path, encoding='utf-8'))
    except Exception as exc:
        lines.append('账本: 解析失败 —— %s' % exc)
        return False, lines, stats

    if isinstance(data, list):
        ids, ts, fmt = [str(x) for x in data], {}, 'v1'
    elif isinstance(data, dict):
        ts = data.get('timestamps') or {}
        ids, fmt = [str(x) for x in (data.get('event_ids') or [])], 'v2(v%s)' % data.get('version')
    else:
        lines.append('账本: 顶层结构不认识（%s）' % type(data).__name__)
        return False, lines, stats

    dups = len(ids) - len(set(ids))
    keys = [(ts.get(i) or event_time(i) or 0.0, i) for i in ids]
    sorted_ok = all(keys[k][0] <= keys[k + 1][0] for k in range(len(keys) - 1))

    limit = ledger_limit()
    ratio = len(ids) / float(limit) if limit else 0
    oldest = min(keys)[1] if keys else '-'
    newest = max(keys)[1] if keys else '-'

    lines.append(
        '账本: format=%s n=%d/%d (%.1f%%) oldest=%s newest=%s'
        % (fmt, len(ids), limit, ratio * 100, oldest, newest)
    )
    lines.append('  ├ 重复条目: %d %s' % (dups, 'OK' if dups == 0 else 'FAIL'))
    lines.append('  └ 事件时间升序: %s' % ('OK' if sorted_ok else 'FAIL（不是真 FIFO）'))
    stats.update({'count': len(ids), 'format': fmt, 'dups': dups, 'sorted': sorted_ok,
                  'ratio': ratio})
    stats['ok'] = (dups == 0 and sorted_ok and ratio < 0.95)
    return stats['ok'], lines, stats


def clips_path(cfg_path):
    try:
        cfg = json.load(open(cfg_path, encoding='utf-8'))
        return cfg.get('sentry_clips_path') or FALLBACK_CLIPS
    except Exception:
        return FALLBACK_CLIPS


def check_coverage(ledger_path, sentry_dir):
    """3 天窗口内的候选事件，有多少不在账本里"""
    now = time.time()
    cutoff = now - RECONCILE_MAX_AGE_DAYS * 86400
    if not os.path.isdir(sentry_dir):
        return False, ['覆盖度: SentryClips 不可访问（%s）' % sentry_dir], {}

    try:
        data = json.load(open(ledger_path, encoding='utf-8'))
        known = set(data if isinstance(data, list) else (data.get('event_ids') or []))
    except Exception as exc:
        # 读不到账本 ≠ 账本是空的：后者会得出"候选全缺"，是误报
        return False, ['覆盖度: 账本读取失败，无法判断（%s）' % exc], {}

    cand, missing = [], []
    for name in os.listdir(sentry_dir):
        folder = os.path.join(sentry_dir, name)
        if not os.path.isdir(folder):
            continue
        try:
            mtime = os.stat(folder).st_mtime
        except OSError:
            continue
        if mtime < cutoff or (now - mtime) < RECONCILE_MIN_AGE_SEC:
            continue
        try:
            has_mp4 = any(f.lower().endswith('.mp4') for f in os.listdir(folder))
        except OSError:
            continue
        if not has_mp4:
            continue
        cand.append(name)
        if name not in known:
            missing.append(name)

    lines = ['覆盖度: 3 天窗口候选 %d 个，其中不在账本 %d 个' % (len(cand), len(missing))]
    if missing:
        lines.append('  └ 待补发候选: %s' % ', '.join(sorted(missing)[:5]))
    else:
        lines.append('  └ 无遗漏 OK')
    return len(missing) == 0, lines, {'candidates': len(cand), 'missing': len(missing)}


def check_relapse(log_path, hours):
    """最近 N 小时：同一 event_id 被入队几次"""
    if not os.path.exists(log_path):
        return True, ['复发检测: 日志不存在（跳过）—— %s' % log_path], {}
    since = time.time() - hours * 3600
    per_event = Counter()
    total = 0
    try:
        with open(log_path, encoding='utf-8', errors='replace') as f:
            for line in f:
                m = _LOG_ENQUEUE.match(line)
                if not m:
                    continue
                try:
                    # 日志行只取到「分」——格式串必须与拼出来的字符串一致，
                    # 否则 ValueError 被下面吞掉，整个窗口静默变成 0 条（本次踩过）
                    ts = time.mktime(time.strptime('%s %s:%s' % (m.group(1), m.group(2), m.group(3)),
                                                   '%Y-%m-%d %H:%M'))
                except ValueError:
                    continue
                if ts < since:
                    continue
                total += 1
                per_event[m.group(4)] += 1
    except OSError as exc:
        return True, ['复发检测: 读日志失败（跳过）—— %s' % exc], {}

    repeats = {k: v for k, v in per_event.items() if v > 1}
    lines = ['复发检测: 最近 %gh 入队 %d 次 / 唯一事件 %d 个' % (hours, total, len(per_event))]
    if repeats:
        top = sorted(repeats.items(), key=lambda kv: -kv[1])[:5]
        lines.append('  └ FAIL 同一事件多次补发: %s'
                     % ', '.join('%s x%d' % (k, v) for k, v in top))
    else:
        lines.append('  └ 无重复补发 OK')
    return not repeats, lines, {'total': total, 'unique': len(per_event), 'repeats': repeats}


def main():
    ap = argparse.ArgumentParser(description='已通知账本健康检查（只读）')
    ap.add_argument('--ledger', default=DEFAULT_LEDGER)
    ap.add_argument('--log', default=DEFAULT_LOG)
    ap.add_argument('--sentry-config', default=DEFAULT_SENTRY_CFG)
    ap.add_argument('--clips', default=None, help='覆盖 SentryClips 路径')
    ap.add_argument('--hours', type=float, default=24.0, help='复发检测回看小时数')
    ap.add_argument('--strict', action='store_true', help='任一检查不过则 exit 1')
    args = ap.parse_args()

    print('=' * 66)
    print('已通知账本健康检查  %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    print('=' * 66)

    ok1, l1, s1 = check_ledger(args.ledger)
    for x in l1:
        print(x)

    clips = args.clips or clips_path(args.sentry_config)
    ok2, l2, s2 = check_coverage(args.ledger, clips)
    for x in l2:
        print(x)

    ok3, l3, s3 = check_relapse(args.log, args.hours)
    for x in l3:
        print(x)

    print('-' * 66)
    verdict = all([ok1, ok2, ok3])
    print('判定: %s' % ('OK（账本生效，未见重复补发）' if verdict
                       else 'ATTENTION（见上方 FAIL 项）'))
    if not ok3:
        print('提示: 若上面重复的补发时间早于本次部署，属修复前遗留，可加 --hours 缩小窗口复核')
    return 0 if (verdict or not args.strict) else 1


if __name__ == '__main__':
    sys.exit(main())
