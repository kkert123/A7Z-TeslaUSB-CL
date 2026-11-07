#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guardian-b · 微信告警投递（精简、自包含）
==========================================
设计 §7 原话是「B 上跑一份 weixin_notifier（复用 A 侧模块）」。但 A 侧
`weixin_notifier.py` 携带大量 A 专属逻辑（Tailscale exit-node 探测 `subprocess`
调用、哨兵事件模板、写死 `/opt/teslausb-web/data` 的推送健康文件），**在 B 上
要么无意义、要么会因路径不存在而报错**。

因此 B 端自带一个**接口同形的精简推送器**：只保留 `send_text` / `send_markdown`，
零 A 侧耦合。日后若要换回完整 `weixin_notifier`，只需保证这两个方法签名一致。

对齐：docs/explanation/自检守护-B端VPS设计.md §7 / §8。
"""

import logging
import time

import requests

logger = logging.getLogger("guard-b.alerts")


class Alerter:
    def __init__(self, key: str = "", url: str = "", dry_run: bool = False) -> None:
        self.key = key or ""
        self.url = url or ""
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _endpoint(self) -> str:
        if self.url:
            return self.url
        if self.key:
            return "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + self.key
        return ""

    def _post(self, payload: dict, tries: int = 2) -> bool:
        ep = self._endpoint()
        if not ep:
            logger.warning("告警未投递：未配置 webhook key/url")
            return False
        if self.dry_run:
            logger.info("[DRY-RUN] 告警内容: %s", payload)
            return True
        last_err = ""
        for i in range(tries):
            try:
                r = self.session.post(ep, json=payload, timeout=(5, 10))
                if r.status_code != 200:
                    last_err = "HTTP %s" % r.status_code
                else:
                    body = r.json()
                    if body.get("errcode") == 0:
                        return True
                    last_err = "WeCom %s: %s" % (body.get("errcode"), body.get("errmsg"))
            except Exception as exc:  # noqa: BLE001 - 网络抖动一律重试一次
                last_err = repr(exc)
            if i + 1 < tries:
                time.sleep(1.5)
        logger.error("告警投递失败（重试耗尽）: %s", last_err)
        return False

    # ── 公开接口（与 A 侧 weixin_notifier 同形）───────────────
    def send_text(self, text: str) -> bool:
        return self._post({"msgtype": "text", "text": {"content": text}})

    def send_markdown(self, content: str) -> bool:
        return self._post({"msgtype": "markdown", "markdown": {"content": content}})
