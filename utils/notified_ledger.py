#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""已通知事件账本（NotifiedLedger）—— `sentry_notified_events.json` 的唯一读写入口。

## 背景（2026-11-26 事故）

旧实现在 `sentry_notify_queue._save_notified()` 里用

    ids = list(set(notified))[-NOTIFIED_MAX_KEEP:]

做"FIFO 裁剪"—— **set 是无序的**，剪掉的是随机一条，不是最老的一条。
账本饱和（旧上限 2000）后，每 mark 一个新事件就可能把一个仍在 3 天对账窗口内
的事件挤出账本；下一轮 `reconcile_missed_events()` 把它判成"孤儿"再补发，
补发成功又 mark 一次，又挤出另一条 —— 形成自我维持的循环。
实测：同一条事件 `2026-11-24_20-20-33` 在 2026-11-26 当天被补发 14 次，
一次 `2026-11-19_08-16-44` 被补发 29 次。

## 本模块的四个约束

1. **真 FIFO**：条目按「事件时间戳」（从 event_id 解析）升序，超限淘汰最老的。
2. **排他锁**：read-modify-write 全程持 `flock`，避免 `sentry_service` 与
   `teslausb-notify-retry` 两个写者互相覆盖（"丢更新"的另一条路径）。
3. **写后回读校验**：落盘后重读确认 ID 真在文件里；缺失记 ERROR 并**扩窗重试**。
4. **后端可替换**：JSON 细节只出现在 `_read_raw` / `_write_raw` 两处。
   后续换 SQLite 只需替换这两个方法（或整个类），调用方与文件兼容层都不动。

## 文件格式（v2，向下兼容 v1）

    v2: {"version": 2,
         "event_ids": ["2026-11-24_20-20-33", ...],   # 按事件时间升序；老版本读这个
         "timestamps": {"2026-11-24_20-20-33": 1763...}}   # 新版本据此做 FIFO
    v1: ["2026-11-24_20-20-33", ...]                  # 旧格式，读到时自动升级

保留 `event_ids` 冗余字段是**回退兼容的代价**：老版本 `_load_notified()` 只认
`data.get('event_ids')`，缺了它会把账本读成空集，然后重新补发全部 3 天事件。
文件因此约两份数据，20000 条约 1.2 MB，可接受。
"""

import contextlib
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

try:                       # Linux 设备上生效；Windows 开发机退化为无锁（仅跑测试）
    import fcntl
except ImportError:        # pragma: no cover
    fcntl = None

logger = logging.getLogger("NotifiedLedger")

# event_id 形如 2026-11-24_20-20-33（日期与时间之间用下划线，时间内部用连字符）
_EVENT_ID_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})$')

DEFAULT_MAX_KEEP = 20000
# 占用率超过该比例时告警：说明账本快要再次走到"饱和 → 频繁裁剪"
_KEEP_WARN_RATIO = 0.9


def event_time(event_id) -> Optional[float]:
    """从 event_id 解析事件发生时间（epoch 秒）。解析不了返回 None。

    用事件时间而非"标记时刻"排序：账本要按事件先后淘汰，与"何时被标记"无关。
    """
    m = _EVENT_ID_RE.match(str(event_id or '').strip())
    if not m:
        return None
    try:
        return datetime(*[int(x) for x in m.groups()]).timestamp()
    except ValueError:
        return None


class NotifiedLedger:
    """已通知事件账本。线程/进程安全（flock），真 FIFO，写后校验。"""

    def __init__(self, path, max_keep: int = DEFAULT_MAX_KEEP, lock_path=None):
        self.path = path
        self.max_keep = int(max_keep)
        # 锁文件与数据文件同目录：flock 只在同一文件系统的同一 inode 上互斥
        self.lock_path = lock_path or (str(path) + '.lock')

    # ══════════════════════════════════════════════════════════
    # 后端实现（换 SQLite 时只改下面这两个方法）
    # ══════════════════════════════════════════════════════════

    def _read_raw(self) -> Tuple[Dict[str, float], str]:
        """读取账本 → ({event_id: ts}, 格式标记)。

        格式标记：`v2` / `v1` / `missing` / `corrupt:...`。
        损坏或缺失都返回空字典（调用方按"无记录"处理，不能抛异常阻断补发链）。
        """
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}, 'missing'
        except Exception as exc:                       # noqa: BLE001 — 损坏不能让补发链中断
            return {}, 'corrupt:%s' % exc

        if isinstance(data, dict):
            ts_map = data.get('timestamps')
            ts_map = ts_map if isinstance(ts_map, dict) else {}
            out: Dict[str, float] = {}

            def _ts(eid, raw):
                if isinstance(raw, (int, float)) and raw > 0:
                    return float(raw)
                parsed = event_time(eid)
                return parsed if parsed else 0.0

            # 以 event_ids 为准（它才是老版本兼容字段），timestamps 提供精确时间
            for eid in (data.get('event_ids') or []):
                eid = str(eid)
                out[eid] = _ts(eid, ts_map.get(eid))
            # timestamps 里多出来的（异常/人工编辑）也收进来，避免静默丢条目
            for eid, raw in ts_map.items():
                out.setdefault(str(eid), _ts(eid, raw))
            return out, 'v2'

        if isinstance(data, list):                    # v1 旧格式
            return {str(i): (event_time(i) or 0.0) for i in data}, 'v1'

        return {}, 'unknown'

    def _write_raw(self, entries: Dict[str, float], keep: int) -> Tuple[int, List[str]]:
        """按事件时间升序写盘，超出 keep 淘汰最老的。返回 (保留条数, 被淘汰的 ID 列表)。"""
        items = sorted(entries.items(), key=lambda kv: (kv[1] or 0.0, kv[0]))
        dropped: List[str] = []
        if keep > 0 and len(items) > keep:
            cut = len(items) - keep
            dropped = [k for k, _ in items[:cut]]
            items = items[cut:]

        payload = {
            'version': 2,
            'event_ids': [k for k, _ in items],
            'timestamps': {k: round(v, 3) for k, v in items},
        }

        self.path = str(self.path)
        parent = os.path.dirname(self.path) or '.'
        os.makedirs(parent, exist_ok=True)
        # tmp 名必须唯一：固定名在多写者下会互相截断（M114/M122 两次栽在这）
        fd, tmp = tempfile.mkstemp(dir=parent, prefix='.notified-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return len(items), dropped

    # ══════════════════════════════════════════════════════════
    # 锁
    # ══════════════════════════════════════════════════════════

    @contextlib.contextmanager
    def _locked(self):
        """排他锁覆盖整段 read-modify-write。

        没有它，两个写者会各自 `读 2000 条 → 加自己那条 → 全量覆盖`，
        后写的把先写的抹掉 —— 回读校验只能发现、救不回来。

        注意：`except OSError` **只能包住取锁阶段**。若把它包住 `yield`，
        业务代码在临界区里抛 OSError 时 contextmanager 会二次 yield →
        `RuntimeError: generator didn't stop after throw()`，原始异常被掩盖。
        """
        f = None
        try:
            parent = os.path.dirname(str(self.lock_path)) or '.'
            os.makedirs(parent, exist_ok=True)
            f = open(self.lock_path, 'a+')
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except OSError as exc:                        # 锁文件不可用时不阻断业务，但留痕
            logger.warning("账本锁不可用，退化为无锁写入: %s", exc)
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
                f = None

        try:
            yield
        finally:
            if f is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                f.close()

    # ══════════════════════════════════════════════════════════
    # 对外接口
    # ══════════════════════════════════════════════════════════

    def ids(self) -> Set[str]:
        """已通知事件 ID 集合（供 reconcile 去重）。"""
        entries, _fmt = self._read_raw()
        return set(entries.keys())

    def load(self) -> Dict[str, float]:
        entries, _fmt = self._read_raw()
        return entries

    def contains(self, event_id: str) -> bool:
        return str(event_id) in self._read_raw()[0]

    def size(self) -> int:
        return len(self._read_raw()[0])

    def save_ids(self, event_ids, reason: str = 'save') -> bool:
        """整集合写入（基线初始化用）。同样走真 FIFO 与上限裁剪。"""
        entries: Dict[str, float] = {}
        for eid in event_ids or []:
            eid = str(eid)
            entries[eid] = event_time(eid) or time.time()
        with self._locked():
            kept, dropped = self._write_raw(entries, self.max_keep)
            self._log_dropped(dropped, reason)
        self._warn_if_near_limit(kept)
        return True

    def compact(self, reason: str = 'compact') -> Tuple[int, str]:
        """把账本按事件时间重排并写回（v1→v2 格式升级 / 修复历史无序）。

        不增不删条目，只重排 + 补 timestamps。用于升级后一次性收拾旧账本：
        旧实现是 `list(set())` 写出去的，文件里的顺序完全无序，健康检查的
        "严格升序"判据会一直 FAIL —— 跑一次 compact 即可转成真 FIFO 形态。

        返回 (保留条数, 原格式标记)。
        """
        with self._locked():
            entries, fmt = self._read_raw()
            if not entries:
                return 0, fmt
            kept, dropped = self._write_raw(entries, self.max_keep)
            self._log_dropped(dropped, reason)
        logger.info("[账本] 重排完成: %s → v2，%d 条（淘汰 %d）", fmt, kept, len(dropped))
        return kept, fmt

    def add(self, event_id: str, source: str = 'mark') -> bool:
        """标记单个事件已通知。返回是否**确认落盘**（回读校验通过才算成功）。"""
        if not event_id:
            return False
        eid = str(event_id)

        with self._locked():
            entries, fmt = self._read_raw()
            if eid in entries:
                return True                            # 已在账本，无需重复写盘
            entries[eid] = event_time(eid) or time.time()
            kept, dropped = self._write_raw(entries, self.max_keep)

            back, _ = self._read_raw()
            if eid in back:
                self._log_dropped(dropped, source)
                self._log_written(eid, source, len(back), kept, upgraded=(fmt == 'v1'))
                self._warn_if_near_limit(kept)
                return True

            # 落盘后回读缺失 —— 旧版这里完全静默，是"补发循环"能持续多日的直接原因
            logger.error(
                "[账本] 写入后回读缺失: %s (格式=%s 本次保留=%d/%d 丢掉=%d) → 扩窗重试",
                eid, fmt, kept, self.max_keep, len(dropped)
            )
            widen = max(self.max_keep * 2, len(entries))
            kept2, dropped2 = self._write_raw(entries, widen)
            back2, _ = self._read_raw()
            if eid in back2:
                logger.warning(
                    "[账本] 扩窗重试成功: %s (keep=%d 保留=%d 丢弃=%d)",
                    eid, widen, kept2, len(dropped2)
                )
                self._log_written(eid, source, len(back2), kept2, upgraded=(fmt == 'v1'))
                return True

            logger.error(
                "[账本] 扩窗重试后仍缺失: %s (keep=%d 保留=%d 文件条数=%d) —— 标记未生效，"
                "该事件可能被重复补发",
                eid, widen, kept2, len(back2)
            )
            return False

    # ── 诊断 ──────────────────────────────────────────────────

    def describe(self) -> str:
        """一行摘要，供 reconcile / 排障日志使用。"""
        entries, fmt = self._read_raw()
        if not entries:
            return 'format=%s n=0/%d' % (fmt, self.max_keep)
        ordered = sorted(entries.items(), key=lambda kv: (kv[1] or 0.0, kv[0]))
        return 'format=%s n=%d/%d oldest=%s newest=%s' % (
            fmt, len(entries), self.max_keep, ordered[0][0], ordered[-1][0]
        )

    def _log_written(self, eid, source, total, kept, upgraded=False):
        logger.info("[账本] +%s (%s) n=%d/%d%s", eid, source, total, self.max_keep,
                    ' [v1→v2 升级]' if upgraded else '')

    def _log_dropped(self, dropped, source):
        if not dropped:
            return
        logger.info(
            "[账本] FIFO 淘汰 %d 条 (%s): 最老=%s ... 最新被淘汰=%s",
            len(dropped), source, dropped[0], dropped[-1]
        )

    def _warn_if_near_limit(self, size: int):
        if self.max_keep and size >= self.max_keep * _KEEP_WARN_RATIO:
            logger.warning(
                "[账本] 已用 %d/%d（≥%.0f%%），继续增长将开始淘汰最老记录",
                size, self.max_keep, _KEEP_WARN_RATIO * 100
            )
