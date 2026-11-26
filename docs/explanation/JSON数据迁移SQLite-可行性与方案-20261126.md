# 全量 JSON → SQLite 迁移：可行性与方案建议

> 调研日期：2026-11-26　对象：A7Z `teslausb` v0.3.1.93（设备实机取证 + 工作树代码比对）
> 结论：**技术上可行，且升级链已原生支持；但不应"全量"迁移 —— 23 个 JSON 里只有 6 个值得迁，其余 17 个迁了是负收益。**

---

## 0. 结论摘要（TL;DR）

| 项 | 结论 |
|---|---|
| 可行性 | **可行**。`upgrade_service._save_user_data` 已内置 `wal_checkpoint(FULL)` + `quick_check` + 失败 `VACUUM INTO` 重建，`data/` 下已有 2 个 `.db` 在跑，新增库是"同类项"而非新风险 |
| 迁移范围 | 23 个 JSON 中 **6 个建议迁**、3 类明确不迁 |
| 优先级 | P0：`sentry_notified_events`（当前 bug 根因）+ `cleanup_history`（2.05MB 全量重写）→ P1：3 个队列 → P2：状态快照不动 |
| 关键设计 | **分 2 库**（`history.db` / `queue.db`），不是 1 个大库；统一 `utils/db.py`；写操作强制走事务 |
| 回退兼容 | 迁移期**双写 JSON 快照**（只写不读），老版本回退零改造 |
| 最大风险 | 多进程写同一库的 `database is locked`；必须 `busy_timeout` + 分库 + 写重试 |

---

## 1. 现状盘点（设备实机）

`data/` 下 JSON 合计 **2.2 MB**，其中 `cleanup_history.json` 一个文件占 **2.05 MB（93%）**。

### 1.1 A 类 — 累积历史（只增不减，体积大）

| 文件 | 大小 | 条数 | 写者 | 现有保护 | 风险 |
|---|---|---|---|---|---|
| `cleanup_history.json` | 2.05 MB | **50 条**（单条均值 41 KB） | `auto_cleanup.append_cleanup_history` + `routes/cleanup_routes._save_cleanup_history`（已合并为同一入口） | 原子写 + 锁（M122 修） | **每次清理全量读 2MB + 全量重写**；已实际损坏过一次（`cleanup_history.json.corrupt-20261107-112930` 2.1 MB 仍留在现场） |
| `sentry_notified_events.json` | 50 KB | **2000 条（= 上限，已饱和）** | `sentry_service._mark_notified` + `sentry_notify_queue.mark_notified` | 原子写，**无锁** | **当前"反复补发"的根因**：`list(set())[-2000:]` 裁剪非 FIFO，集合饱和后每写必丢随机一条 |
| `cloud_sync_history.json` | 15 KB | — | `cloud_archive_service` | 无 | 写频率低，风险低但同样无锁 |
| `cloud_sync_log.json` | 5.4 KB | — | 同上 | 无 | 低 |
| `version_history.json` | 14 KB | — | `upgrade_service` | 原子 | 低，可留 |

### 1.2 B 类 — 队列（进出频繁，要求强原子性）

| 文件 | 大小 | 写者数 | 风险 |
|---|---|---|---|
| `sentry_notify_queue.json` | 2 B（空） | 2（web / notify-retry） | 中 |
| `preview_queue.json` | 18 KB | 4+（`guard_client`、web、bgpreview） | 中 |
| `guard_queue.json` | 2 B（空） | 2 | 低 |
| `preview_blocked.json` | 37 KB | 2 | 低 |

### 1.3 C 类 — 状态快照（最新值覆盖，单写者，高频）

`disk_cache.json`(830B)、`health_status.json`(1KB)、`guard_status.json`(1.2KB)、`usb_guard_status.json`(450B)、`location_state.json`(103B)、`preview_scan.json`(53B)、`guard_push_health.json`(153B)、`push_health.json`(1.8KB)、`auto_present.json`(45B)、`monthly_traffic.json`(126B)、`auth_runtime.json`(935B)、`web_auth.json`(726B)、`fan_curve.json`(115B)、`services_refreshed.json`(116B)

### 1.4 D 类 — 配置（人可编辑）

`config.json`(根, 1KB)、`config/sentry.json`、`config/guard.json`、`config/sync.json`、`config/cleanup_policies.json`、`config/cloud.json`、`config/ap_config.json`、`*.example.json`

---

## 2. 为什么值得迁（4 条硬证据，非推测）

1. **`cleanup_history` 的全量重写代价已失真**：50 条记录占 2.05 MB（每条带完整 `errors`/`actions` 明细），每次清理都要 `load 2MB → insert(0) → 原子写 2MB`。SQLite 只需 `INSERT` 一行 + 按需 `DELETE` 超龄行。
2. **`sentry_notified_events` 已因"用 JSON 当集合"出事**：2000 条饱和后用 `list(set())[-2000:]` 裁剪 → 丢的是随机条目 → 被丢的若在 3 天窗口内，下一轮对账就把它当"孤儿"再补发。今天 06:28–20:50 共 17 次补发，其中 14 次是同一条 `2026-11-24_20-20-33`。唯一索引 + `notified_at` 列能一次性消灭这个缺陷。
3. **同文件多写者只能靠"约定"约束**：`cleanup_history` 曾因两个写者各自 `open(w)+json.dump` 被截断在 8 KB 边界（M122）；`sentry_notified_events` 现在仍有两个写者且**无任何锁**。SQLite 的写锁是强制的、由内核保证的。
4. **断电安全**：JSON 全量重写期间断电 = 文件半截（已发生，靠 `ops/salvage_cleanup_history.py` 抢救）。WAL 模式的事务有原子提交语义。

---

## 3. 明确不建议迁移的（3 类）

| 类别 | 理由 |
|---|---|
| **D 类 config** | 用户/我都要能 `cat` 看、能手改、能被发布包脱敏（M148）、能被升级链单独搬运。进 SQLite 后排障成本陡增，收益为零（几 KB、低频写、单写者） |
| **C 类状态快照** | 全部 < 2 KB，单写者，几秒~几分钟覆盖一次。迁 SQLite 只是把"一个小文件覆盖写"换成"打开连接 + 事务 + WAL 追加"，**写路径更重**，且每个模块都要改读法 |
| **`*.example.json` / `services_refreshed.json`** | 只读模板 / 幂等标记，本身无并发问题 |

> 一句话：**JSON 的敌人不是"格式"，是"体积 + 多写者 + 需要裁剪"**。三条都命中才迁。

---

## 4. 目标架构：两个库，不是一个大库

```
data/
├── history.db     ← A 类（累积历史，低频写、大行）
├── queue.db       ← B 类（队列，高频进出、小行）
├── file_index.db  ← 已有，不动
└── *.json         ← C/D 类保持原样 + 迁移期双写快照
```

**为什么分 2 库而不是 1 个**：SQLite 同一时刻只允许一个写者。`history.db` 的写者是清理/上传/升级（低频、可能持锁 100ms 级），`queue.db` 的写者是 web/guard/notify-retry（高频、必须低延迟）。合库会让高频队列写被低频大事务阻塞，正是要避免的耦合。

### 4.1 关键表结构

```sql
-- history.db
CREATE TABLE IF NOT EXISTS notified_event (
    event_id     TEXT PRIMARY KEY,          -- '2026-11-24_20-20-33'
    notified_at  TEXT NOT NULL,             -- ISO 本地时间（列存，不再从文件名解析）
    source       TEXT                       -- 'push' | 'retry' | 'reconcile'
);
CREATE INDEX IF NOT EXISTS idx_notified_at ON notified_event(notified_at);

CREATE TABLE IF NOT EXISTS cleanup_run (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    trigger      TEXT,                      -- 'auto' | 'manual'
    payload      TEXT NOT NULL,             -- 整条 JSON（保留原结构，前端零改造）
    deleted      INTEGER DEFAULT 0,
    freed_bytes  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cleanup_started ON cleanup_run(started_at DESC);

-- queue.db
CREATE TABLE IF NOT EXISTS notify_queue (
    event_id     TEXT PRIMARY KEY,
    payload      TEXT NOT NULL,             -- 原 entry JSON
    status       TEXT NOT NULL DEFAULT 'pending',
    retry_count  INTEGER NOT NULL DEFAULT 0,
    added_at     TEXT NOT NULL,
    last_retry   TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON notify_queue(status, added_at);
```

> 设计取舍：**业务负载仍存 JSON 字符串**（`payload` 列），只把"主键、状态、时间、计数"这些**需要查询/裁剪/去重**的字段提成列。这样前端和大部分业务代码零改造，迁移面最小，同时拿到索引、事务和 FIFO 裁剪能力。

---

## 5. 统一 db 层（`utils/db.py`）六条硬规矩

1. **连接管理**：进程内复用单连接（`check_same_thread=False` + `threading.Lock`），沿用 `file_index.py` 已验证的写法。
2. **PRAGMA 必设四条**：
   ```python
   conn.execute("PRAGMA journal_mode=WAL")
   conn.execute("PRAGMA synchronous=NORMAL")
   conn.execute("PRAGMA busy_timeout=5000")     # file_index.py 缺这条，是潜在 locked 风险
   conn.execute("PRAGMA wal_autocheckpoint=400")
   ```
3. **写操作唯一入口**：`with db.txn(conn) as c:` —— 禁止模块自己 `conn.execute` + `commit`。
4. **写重试**：捕获 `sqlite3.OperationalError` 且信息含 `locked` → 指数退避重试 3 次（100/300/900ms）。
5. **迁移器**：`migrate_once(json_path, table, converter)` —— 幂等（`INSERT OR IGNORE`）、迁移后回读条数校验、成功才把 JSON 改名 `.json.bak-<ts>`、失败整体回滚并继续用 JSON。
6. **双写快照**：每次写库后同步落一份 JSON（仅最近 N 条，**只写不读**，文件头写 `"_snapshot_only": true`），保证回退老版本可用；文档与代码注释都必须写明"快照不是数据源"。

---

## 6. 迁移顺序与验收判据

| 批次 | 内容 | 上机判据 | 回滚 |
|---|---|---|---|
| **P0-a** | `sentry_notified_events` → `history.db.notified_event`（含"迁移补洞"：把当前 3 天窗口内全部候选 `INSERT OR IGNORE`） | 连续 48h 无重复补发；`SELECT COUNT(*)` 与 JSON 导入数一致；日志无 `database is locked` | 停 timer，恢复 `.json.bak` |
| **P0-b** | `cleanup_history` → `history.db.cleanup_run`（保留 50 条 FIFO） | 一次清理后：库内 +1 行、接口返回结构不变、2MB 写盘消失（`iostat` 对比） | 同上 |
| **P1** | `sentry_notify_queue` / `preview_queue` / `guard_queue` → `queue.db` | 队列进出正确、断电重启不丢、并发两个写者无 locked 报错 | 同上 |
| **P2** | 状态快照 / config | **不迁移**，无此项 | — |

---

## 7. 风险登记

| 风险 | 触发条件 | 对策 |
|---|---|---|
| `database is locked` | 两个进程同时写同一库（尤其 P1 队列） | `busy_timeout=5000` + 写重试 + **分库**（高频队列独立） |
| WAL 文件膨胀 | 长事务 / 读连接长期不释放 | `wal_autocheckpoint=400` + 每次写后不持有长事务 + 定期 `PRAGMA wal_checkpoint(TRUNCATE)` |
| 升级搬运丢表 | `_checkpoint_sqlite` 失败 + WAL 未落盘 | 已有：先 `wal_checkpoint(FULL)` → 复制 `-wal/-shm` → `quick_check` 失败 `VACUUM INTO` 重建（M139 事故后就有的机制） |
| SD 卡写放大 | 高频小事务 | 净收益：`cleanup_history` 从每次 2 MB 全量写 → 单行写；`notified_event` 单行 INSERT。**总写量下降** |
| 时间戳/时区 | 沿用 `datetime.now()` 本地时间 | 统一存 ISO 本地时间（与现有日志口径一致），不引入 UTC 转换，避免 M-A7Z 无 RTC 场景下的混乱 |
| 回退老版本读不到库 | 回退到 < 本次版本 | 双写 JSON 快照（§5.6）；快照文件命名/结构与旧版完全一致 |
| 迁移瞬间误补发 | 历史裁剪留下的"洞"仍在 3 天窗口内 | 迁移器必做"补洞"：窗口内全部现存事件先 `INSERT OR IGNORE` |

---

## 8. 工作量评估

| 批次 | 代码改动 | 新增测试 | 风险 |
|---|---|---|---|
| P0 | `utils/db.py`(~150 行) + `sentry_notify_queue` 改写(~120 行) + `auto_cleanup` 改写(~80 行) | 迁移幂等、裁剪 FIFO、断电压测模拟 | 中 |
| P1 | 3 个队列模块(~250 行) | 双写者并发、重启不丢 | 中 |
| 合计 | **约 600 行** | 5~6 个测试 | — |

---

## 9. 明确建议

1. **做 P0，马上做**：`sentry_notified_events` 这一项既修当前"反复补发"缺陷，又是整条链路的试点；`cleanup_history` 紧随其后（它是最贵的那个文件）。
2. **P1 等 P0 稳定一周再做**：队列改动面更广（4 个写者），且当前队列是空的，没有紧迫性。
3. **config 与状态快照永不迁**：它们的"文件即接口"价值（可读、可改、可打包脱敏、可排障）远高于数据库带来的收益。
4. **不引入 ORM**：项目已有 `file_index.py` 的裸 sqlite 先例，保持一致；设备内存 959 MB，不上 SQLAlchemy。
5. **先修 bug 再迁移**：`sentry_notified_events` 的修复（真 FIFO + 写后回读校验）可以先以最小改动落到 JSON 上止血，再在同一批里切库，避免"迁移没完成、补发还在响"的中间态。

---

## 附：本次取证命令与结论来源

- 设备：`ls -laS data/*.json`、`du -ch`、`df -hT`（ext4，21G 可用）
- 代码：`md5sum` 三文件与工作树一致（`sentry_notify_queue.py` = `b66a05f7`）
- 行为：`/var/log/teslausb-notify-retry.log` 全量解析（4937 行，209 次入队 / 106 个唯一事件）
- 实验：跨进程裁剪丢谁（12/12 随机）、饱和状态反复 mark（60/60 保留）、systemd 环境 hash 随机性（5/5 随机）、`/tmp` 副本全流程复现（正常）
