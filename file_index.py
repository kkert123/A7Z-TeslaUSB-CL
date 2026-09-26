#!/usr/bin/env python3
"""
TeslaUSB A7Z — 文件索引模块（SQLite 增量扫描）
================================================
替代每次全量 os.listdir 扫描，通过 SQLite 记录文件时间戳，
实现增量扫描 + 5 分钟被动补全。

主动：扫描时对比 DB 中的 mtime，只处理新/变化文件
被动：页面打开时查询 DB，对最近 5 分钟内未处理的文件立即补全

DB 文件: /opt/radxa_data/teslausb/data/file_index.db
"""

import os
import sqlite3
import threading
import time
from contextlib import contextmanager

DB_PATH = "/opt/radxa_data/teslausb/data/file_index.db"
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS file_events (
    folder_type TEXT NOT NULL,
    event_id TEXT NOT NULL,
    newest_mtime REAL NOT NULL,
    file_count INTEGER DEFAULT 0,
    total_size INTEGER DEFAULT 0,
    has_thumbnail INTEGER DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (folder_type, event_id)
);

CREATE INDEX IF NOT EXISTS idx_folder_mtime ON file_events(folder_type, newest_mtime DESC);
CREATE INDEX IF NOT EXISTS idx_thumbnail ON file_events(folder_type, has_thumbnail, newest_mtime);
"""

_lock = threading.Lock()

_db = None


def _get_db():
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("PRAGMA synchronous=NORMAL")
        _db.executescript(SCHEMA_SQL)
        _db.commit()
    return _db


def init_db():
    _get_db()


@contextmanager
def _txn():
    with _lock:
        conn = _get_db()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def get_newest_mtime(folder_type: str) -> float:
    """获取指定文件夹下已记录的最新文件 mtime，用于增量扫描"""
    conn = _get_db()
    row = conn.execute(
        "SELECT MAX(newest_mtime) FROM file_events WHERE folder_type = ?",
        (folder_type,)
    ).fetchone()
    return row[0] if row and row[0] else 0.0


def upsert_event(folder_type: str, event_id: str, newest_mtime: float,
                 file_count: int = 0, total_size: int = 0, has_thumbnail: bool = False):
    """插入或更新事件记录"""
    with _txn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO file_events 
               (folder_type, event_id, newest_mtime, file_count, total_size, has_thumbnail, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (folder_type, event_id, newest_mtime, file_count, total_size,
             1 if has_thumbnail else 0, time.strftime("%Y-%m-%d %H:%M:%S"))
        )


def delete_event(folder_type: str, event_id: str):
    """删除已不存在的事件记录"""
    with _txn() as conn:
        conn.execute(
            "DELETE FROM file_events WHERE folder_type = ? AND event_id = ?",
            (folder_type, event_id)
        )


def get_events_needing_thumbnail(folder_type: str, since_minutes: int = 5) -> list:
    """获取指定文件夹下最近 N 分钟内缺少缩略图的事件列表"""
    cutoff = time.time() - (since_minutes * 60)
    conn = _get_db()
    rows = conn.execute(
        """SELECT event_id, newest_mtime, file_count, total_size
           FROM file_events
           WHERE folder_type = ? AND has_thumbnail = 0 AND newest_mtime >= ?
           ORDER BY newest_mtime DESC""",
        (folder_type, cutoff)
    ).fetchall()
    return [
        {"event_id": r[0], "newest_mtime": r[1], "file_count": r[2], "total_size": r[3]}
        for r in rows
    ]


def mark_thumbnail_done(folder_type: str, event_id: str):
    """标记事件缩略图已生成"""
    with _txn() as conn:
        conn.execute(
            "UPDATE file_events SET has_thumbnail = 1 WHERE folder_type = ? AND event_id = ?",
            (folder_type, event_id)
        )


def get_event_count(folder_type: str) -> int:
    """获取指定文件夹下的事件总数"""
    conn = _get_db()
    row = conn.execute(
        "SELECT COUNT(*) FROM file_events WHERE folder_type = ?",
        (folder_type,)
    ).fetchone()
    return row[0] if row else 0


def get_all_event_ids(folder_type: str) -> list:
    """获取指定文件夹下所有事件 ID 列表"""
    conn = _get_db()
    rows = conn.execute(
        "SELECT event_id, newest_mtime, file_count, total_size, has_thumbnail "
        "FROM file_events WHERE folder_type = ? ORDER BY newest_mtime DESC",
        (folder_type,)
    ).fetchall()
    return [
        {"event_id": r[0], "newest_mtime": r[1], "file_count": r[2],
         "total_size": r[3], "has_thumbnail": bool(r[4])}
        for r in rows
    ]


def cleanup_stale(folder_type: str, valid_event_ids: set):
    """删除 DB 中已不存在于磁盘的事件"""
    conn = _get_db()
    all_ids = set(r[0] for r in conn.execute(
        "SELECT event_id FROM file_events WHERE folder_type = ?", (folder_type,)
    ).fetchall())
    stale = all_ids - valid_event_ids
    if stale:
        with _txn() as conn:
            conn.executemany(
                "DELETE FROM file_events WHERE folder_type = ? AND event_id = ?",
                [(folder_type, eid) for eid in stale]
            )
    return len(stale)


# ── 增量扫描 ────────────────────────────────────────────

# TeslaCam 文件夹映射
VIDEO_FOLDERS_TO_SCAN = {
    'SentryClips': '/mnt/teslacam/TeslaCam/SentryClips',
    'SavedClips': '/mnt/teslacam/TeslaCam/SavedClips',
    'RecentClips': '/mnt/teslacam/TeslaCam/RecentClips',
}

# 支持的视频扩展名
VIDEO_EXTS = ('.mp4',)


def incremental_scan() -> dict:
    """增量扫描所有视频文件夹，仅处理 mtime 变化的文件/目录。
    
    首次调用时全量扫描（DB 为空），后续仅处理增量。
    
    Returns:
        {folder_type: {'new': N, 'updated': N, 'deleted': N}}
    """
    results = {}
    for ft, folder_path in VIDEO_FOLDERS_TO_SCAN.items():
        if not os.path.isdir(folder_path):
            continue
        
        last_mtime = get_newest_mtime(ft)
        new_count = 0
        updated_count = 0
        
        if ft == 'RecentClips':
            # 平铺结构：按文件名前缀分组
            sessions = {}
            for fname in os.listdir(folder_path):
                if not fname.lower().endswith(VIDEO_EXTS):
                    continue
                fpath = os.path.join(folder_path, fname)
                try:
                    fsize = os.path.getsize(fpath)
                    fmtime = os.path.getmtime(fpath)
                except OSError:
                    continue
                
                # 只处理新文件（mtime > 已记录的最大 mtime）
                if fmtime <= last_mtime and last_mtime > 0:
                    # 文件未变化，但检查事件是否已记录
                    continue
                
                # 提取事件 ID
                parts = fname.split('-front')[0].split('-back')[0]
                for cam in ('left_repeater', 'right_repeater', 'left_pillar', 'right_pillar'):
                    if f'-{cam}' in parts:
                        parts = parts.replace(f'-{cam}', '')
                        break
                ts_parts = parts.split('_')
                session_id = f"{ts_parts[0]}_{ts_parts[1][:8]}" if len(ts_parts) >= 2 else parts[:19]
                
                if session_id not in sessions:
                    sessions[session_id] = {'size': 0, 'count': 0, 'mtime': 0, 'files': []}
                sessions[session_id]['size'] += fsize
                sessions[session_id]['count'] += 1
                sessions[session_id]['mtime'] = max(sessions[session_id]['mtime'], fmtime)
                sessions[session_id]['files'].append(fpath)
            
            valid_ids = set()
            for eid, info in sessions.items():
                valid_ids.add(eid)
                # 检查缩略图是否存在
                tn_file = f"/opt/radxa_data/teslausb/static/thumbnails/REC_{eid}_grid.jpg"
                has_tn = os.path.exists(tn_file)
                upsert_event(ft, eid, info['mtime'], info['count'], info['size'], has_tn)
                new_count += 1
            
            deleted = cleanup_stale(ft, valid_ids)
            updated_count = 0
        else:
            # 事件文件夹结构
            valid_ids = set()
            for entry in sorted(os.listdir(folder_path)):
                event_path = os.path.join(folder_path, entry)
                if not os.path.isdir(event_path):
                    continue
                
                valid_ids.add(entry)
                event_mtime = 0
                file_count = 0
                total_size = 0
                try:
                    for vf in os.listdir(event_path):
                        vpath = os.path.join(event_path, vf)
                        if os.path.isfile(vpath) and vf.lower().endswith(VIDEO_EXTS):
                            try:
                                fmtime = os.path.getmtime(vpath)
                                event_mtime = max(event_mtime, fmtime)
                                total_size += os.path.getsize(vpath)
                                file_count += 1
                            except OSError:
                                pass
                except OSError:
                    pass
                
                if file_count == 0:
                    continue
                
                # 仅处理变化的事件
                if event_mtime <= last_mtime and last_mtime > 0:
                    continue
                
                prefix = {'SentryClips': 'SEN_', 'SavedClips': 'SAV_'}.get(ft, 'UNK_')
                tn_file = f"/opt/radxa_data/teslausb/static/thumbnails/{prefix}{entry}_grid.jpg"
                has_tn = os.path.exists(tn_file)
                upsert_event(ft, entry, event_mtime, file_count, total_size, has_tn)
                updated_count += 1
            
            deleted = cleanup_stale(ft, valid_ids)
        
        results[ft] = {'new': new_count, 'updated': updated_count, 'deleted': deleted}
    
    return results
