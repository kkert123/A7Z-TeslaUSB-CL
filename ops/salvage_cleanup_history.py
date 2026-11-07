#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从被并发写坏的 cleanup_history.json 里抢救完整条目（M122）

用法（设备上 root）：
    sudo python3 ops/salvage_cleanup_history.py                 # 用默认路径
    sudo python3 ops/salvage_cleanup_history.py --path <file>    # 指定文件
    sudo python3 ops/salvage_cleanup_history.py --dry-run        # 只报告不写

为什么需要它：
    v0.3.1.75 之前的 `_save_cleanup_history()` 是 `open(w) + json.dump`，两个写者
    并发时会把文件截断在 8KB 缓冲边界（char 8192/8200）。整份 json.load 必然失败，
    但文件里仍有大量「完整对象」可以捞出来。

为什么不整份解析：
    坏点通常落在第 1~2 个对象内部，`json.loads(整个文件)` 或「从尾部回退 + loads」
    都会失败（后者还是 O(n²)）。这里用花括号配对扫描（正确处理字符串与转义），
    把每个看起来完整的对象切片单独 loads，成功且带 timestamp 就收下，坏块跳过。
"""
import argparse
import glob
import json
import os
import sys
import tempfile

DEFAULT = "/opt/radxa_data/teslausb/data/cleanup_history.json"


def scan_objects(text, max_attempts=50000):
    """返回 (完整对象列表, 尝试次数)。O(n) 量级。"""
    out, seen = [], set()
    n, i, attempts = len(text), 0, 0
    while i < n and attempts < max_attempts:
        if text[i] != "{":
            i += 1
            continue
        attempts += 1
        depth, j, in_str, esc = 0, i, False, False
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        if j >= n:
            break
        try:
            obj = json.loads(text[i:j + 1])
        except Exception:
            i += 1
            continue
        if isinstance(obj, dict) and obj.get("timestamp"):
            ts = str(obj["timestamp"])
            if ts not in seen:
                seen.add(ts)
                out.append(obj)
            i = j + 1
        else:
            i += 1
    return out, attempts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=DEFAULT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    live = args.path
    data_dir = os.path.dirname(live) or "."

    if os.path.exists(live):
        try:
            ok = json.load(open(live, "r", encoding="utf-8"))
            print("线上历史可用（%d 条），无需抢救。" % len(ok))
            return 0
        except Exception as exc:
            print("线上文件损坏: %s" % exc)

    cands = sorted(glob.glob(live + ".corrupt-*"),
                   key=os.path.getmtime, reverse=True)
    if not cands:
        print("没有隔离副本（.corrupt-*）可抢救；历史会随下次清理重建。")
        return 0

    src = cands[0]
    print("来源: %s (%d bytes)" % (src, os.path.getsize(src)))
    text = open(src, "r", encoding="utf-8", errors="replace").read()

    items, attempts = scan_objects(text)
    print("扫描 %d 次，抢救出 %d 条完整记录" % (attempts, len(items)))

    if not items:
        print("未抢救到完整条目；隔离副本已保留。")
        return 0
    if args.dry_run:
        print("[dry-run] 不写盘。")
        return 0

    items.sort(key=lambda o: str(o.get("timestamp")), reverse=True)
    fd, tmp = tempfile.mkstemp(prefix=".atomic-cleanup_history.", dir=data_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, live)

    back = json.load(open(live, "r", encoding="utf-8"))
    print("落盘校验: 条目=%d | 最新 %s | 最旧 %s"
          % (len(back), back[0]["timestamp"], back[-1]["timestamp"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
