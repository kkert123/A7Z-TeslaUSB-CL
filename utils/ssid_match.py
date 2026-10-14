"""ssid_match.py — WiFi 标识匹配的唯一权威实现（M105，v0.3.1.65）

为什么单独抽一个模块：
配置里 `sentry.json` 的 `home_wifi_ssids` / `hotspot_ssids` 历史上**混存**两种形态——
有时是裸 SSID（`YL-MIFI-000500`），有时是 NetworkManager **连接名**
（`WiFi-YL-MIFI-000500`，`wifi_service.switch_wifi()` 建连接时的命名规范）。
而运行时能拿到的也是两种名字：
  - `wifi_service.get_current_wifi()` → 裸 **SSID**（取自 `nmcli -t -f ACTIVE,SSID`）
  - `wifi_service._get_active_connection_name()` → **连接名**

只要比较写成 `x in list`（裸字符串相等），两种表示不一致就是**静默失效**——
不报错、不告警，只是永远不命中。实测 `location_detector` 的「连热点=外出」
辅助判定就因此从上线起一直没生效过。

因此：匹配一律走 `matches()`，同时比对 SSID 与连接名，并容忍 `WiFi-` 前缀差异；
`traffic_monitor` 与 `location_detector` 共用本模块，避免两套口径继续漂移。
"""


def norm(s):
    """归一：去空白 + 小写（比较统一不敏感）"""
    return (s or "").strip().lower()


def strip_wifi_prefix(s):
    """去掉连接名的 `WiFi-` 前缀（返回归一后的字符串）"""
    s = norm(s)
    return s[5:] if s.startswith("wifi-") else s


def as_list(value):
    """把配置项规整成不可变列表。

    - None / 其他类型        → ()
    - str（历史/手改残留）    → 按逗号拆分（**不做 tuple(str) 那种退化成字符元组的蠢事**）
    - list / tuple          → 仅保留非空字符串
    """
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(x.strip() for x in value.split(",") if x.strip())
    if isinstance(value, (list, tuple)):
        return tuple(x for x in value if isinstance(x, str) and x.strip())
    return ()


def _pair_hit(a, b):
    """单个名字对单个配置项：原文相等，或「一侧带 WiFi- 前缀、另一侧不带」时等价。

    只剥离「确实带前缀」的那一侧，避免把 X 与 WiFi-X 无条件当作同一等价类
    （那会让配置 `Foo` 与真实网络 `WiFi-Foo` 混同）。
    """
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if a.startswith("wifi-") and a[5:] == b:
        return True
    if b.startswith("wifi-") and b[5:] == a:
        return True
    return False


def matches(current_names, configured):
    """current_names（SSID / 连接名，任一）是否命中 configured 列表。

    Args:
        current_names: 可迭代的字符串（可含 None/空串，会被忽略）
        configured:    配置里的 SSID / 连接名列表
    """
    cur = [x for x in (current_names or ()) if x]
    cfg = as_list(configured)
    if not cur or not cfg:
        return False
    for c in cur:
        for k in cfg:
            if _pair_hit(c, k):
                return True
    return False
