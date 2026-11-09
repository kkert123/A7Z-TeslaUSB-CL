#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guardian-b · TeslaMate 车态板读取（免 token）
==============================================
问题：A7Z 离家后没网（车载 MIFI/热点掉线）时，B 端拿不到任何车态；
而 B 端要「代发停车自检」，就必须知道**车锁了没有**。

为什么不走 TeslaMate 的 `/api/*`：全都要 token，而 token 缓存在 A7Z 上；
为什么不让 B 也 `/login`：会与 TeslaMate 自己的会话抢 token（M115 实证过
40 分钟 77 次登录的场面）。

可行通道：TeslaMate 的**首页 HTML 是服务端渲染（LiveView SSR）**，且不需要鉴权。
实测（http://100.111.252.121:4000/，9.4 KB）里直接就有：

    data-tooltip="Sentry Mode"  →  <span class="mdi mdi-shield-check">   # 哨兵开
    data-tooltip="Locked"       →  <span class="mdi mdi-lock">           # 已锁
    <td>Status</td><td> online for 13 h, 26 min </td>                    # 车辆状态

所以「锁没锁」这件事是**可以**从这儿读到的，而且只读、零副作用、不需要凭据。

设计约束（都是踩过的坑）：
  1. **解析失败一律返回 None，绝不猜**。猜错会直接变成一条假告警。
  2. 只认**图标 class**，不认 `data-tooltip` 的文案——文案会随语言变，
     而 `mdi-lock` / `mdi-lock-open-variant` 是模板里的固定值。
  3. 每 60s 抓一次（9.4 KB），抓不到就记 `err`，不重试风暴。
"""

import logging
import re

logger = logging.getLogger("guard-b.board")

# 图标 → 三态。`has-text-danger` 之类的着色不参与判断。
_RE_SENTRY = re.compile(
    r'data-tooltip="Sentry Mode"[^>]*>\s*<span class="mdi ([^"]+)"', re.S)
_RE_LOCK = re.compile(
    r'data-tooltip="(?:Locked|Unlocked)"[^>]*>\s*<span class="mdi ([^"]+)"', re.S)
_RE_LOCK_WORD = re.compile(r'data-tooltip="(Locked|Unlocked)"')
_RE_STATUS = re.compile(r"Status\s+([A-Za-z]+)\s+for\b")
_RE_STATUS_ANY = re.compile(r"Status\s+(\S(?:\S| ){0,20}?)\s+for\b")


def parse_board(html: str) -> dict:
    """把首页 HTML 解析成 `{locked, sentry, state, err}`。解析不出的项为 None。"""
    out = {"locked": None, "sentry": None, "state": None, "err": ""}
    if not html:
        out["err"] = "empty"
        return out

    m = _RE_SENTRY.search(html)
    if m:
        cls = m.group(1).strip().lower()
        if "check" in cls:
            out["sentry"] = True                    # mdi-shield-check
        elif "off" in cls:
            out["sentry"] = False                   # mdi-shield-off
        elif cls.endswith("mdi-shield"):
            out["sentry"] = False                   # 旧模板没有后缀

    m = _RE_LOCK.search(html)
    if m:
        cls = m.group(1).strip().lower()
        # 锁：`mdi-lock` 已锁；`mdi-lock-open-variant` / `mdi-lock-open` 未锁
        out["locked"] = False if "open" in cls else (True if "lock" in cls else None)
    else:
        w = _RE_LOCK_WORD.search(html)     # 兜底：文案层面（图标被换掉时）
        if w:
            out["locked"] = (w.group(1) == "Locked")

    # 状态行：整页可见文本里找「Status <state> for ...」
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    m = _RE_STATUS.search(text) or _RE_STATUS_ANY.search(text)
    if m:
        out["state"] = m.group(1).strip().lower()

    if out["locked"] is None and out["sentry"] is None and out["state"] is None:
        out["err"] = "unparsable"          # 模板变了 / 换了语言 / 不是那个页面
    return out


def fetch_board(url: str, timeout=(3, 6)) -> dict:
    """抓一次首页并解析。**任何异常都不抛**，坏样本用 `err` 表达。"""
    if not url:
        return {"locked": None, "sentry": None, "state": None, "err": "no_url"}
    try:
        import requests
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "guard-b/board"})
    except Exception as exc:                       # noqa: BLE001
        return {"locked": None, "sentry": None, "state": None,
                "err": "%s" % type(exc).__name__}
    if r.status_code != 200:
        return {"locked": None, "sentry": None, "state": None,
                "err": "http_%s" % r.status_code}
    try:
        html = r.text
    except Exception as exc:                       # noqa: BLE001
        return {"locked": None, "sentry": None, "state": None,
                "err": "decode_%s" % type(exc).__name__}
    return parse_board(html)
