# TeslaMate 自定义服务接口与车辆状态字段

> 事实来源：对 `http://100.111.252.121:7777/`（**容器内运行**）的实际探测 + 前端 `app.js` 逆向（原文归档于 `.workbuddy/artifacts/refs/app_7777.js`）。
> 记录日期：2026-09-12 ｜ 关联：`location_detector.py`（现有消费方，仅用 `/msg` + `/api/dashboard`）

## 1. 认证

| 项 | 值 |
|----|----|
| 登录 | `POST /login`，body `{"password": "<密码>", "publicIp": "127.0.0.1"}` |
| 返回 | `{"token": "<JWT>"}` |
| 使用 | `Authorization: Bearer <token>` |
| 过期 | 约 24h（24h 内用）。仓库侧缓存 23h：`/opt/teslausb-web/data/.teslamate_token.json` |
| 401 处理 | 清缓存重登一次（`location_detector.py` 已实现） |
| 密码存放 | `/opt/radxa_data/teslausb/config/sentry.json` → `teslamate_password`（**明文**）；`config_manager.py` 侧为 Fernet 加密字段 |

> 注意：仓库 `config/sentry.json` 是**空模板**（`teslamate_url` / `teslamate_password` 均为空串），真实值只在设备上。

## 2. 常用端点

| 端点 | 说明 | 仓库是否在用 |
|------|------|------------|
| `GET /states` | **车辆实时状态**（196 字段，见 §3） | ❌ 未用（**低垂果实**） |
| `GET /msg` | `raw_data`：`ui_current_address`、`location`(lat/lng)、`latitude`、`longitude` | ✅ `get_location()` |
| `GET /api/dashboard` | `drives_list[0].desc`（"起点 → 终点"） | ✅ `get_last_trip_end()` |
| `GET /logs?limit=N` / `/logs/download` | 运行日志 | ❌ |
| `POST /wake-vehicle` | 唤醒车辆 | ❌ |
| `/path` `/timeline` `/event_detail` `/api/events` `/api/export-records` | 轨迹与记录 | ❌ |
| `/fance` `/geofence-price-*` `/parking-timer-*` | 围栏与计费 | ❌ |
| `/config` `/system` `/containers` `/images` `/image-prune-schedule` `/update` | 服务自身管理 | ❌ |
| `/push_sub/set` `/wework_ip/*` | 服务端推送订阅（企业微信相关） | ❌ |
| `/client-list` `/client-kick` `/check-login-status` `/changepass` | 会话管理 | ❌ |

完整端点清单见 `.workbuddy/artifacts/自检守护-可行性与设计评估.md` 附录 B。

## 3. `GET /states` 关键字段

### 3.1 车辆状态 / 判断类（自检守护最关心）

| 字段 | 类型 | 说明 |
|------|------|------|
| `locked` | **三态** | `true` → 已锁；`false` → 未锁；其他 → `--`（前端 `getTriState()`） |
| `sentry_mode` | **三态** | 哨兵模式开关 |
| `state` | string | `online` / `asleep` / `offline` / `driving`（前端 `String(o.state \|\| "offline").toLowerCase()`） |
| `speed` | number | 车速 |
| `is_user_present` | bool | 车内是否有人 |
| `ui_park_start_str` | string | 泊车起始时间（服务端已算好） |
| `ui_park_time_str` | string | 已泊车时长 |
| `ui_park_power` / `ui_park_loss` | number | 泊车期间耗电 / 损失 |
| `ui_last_comm_time` | string | 最后一次通信时间 |
| `sentry_trigger_count` | number | 哨兵触发次数 |
| `sentry_trigger_times` / `sentry_events` | - | 哨兵触发时刻 / 事件 |

### 3.2 开合件

`driver_front_door_open`、`passenger_front_door_open`、`driver_rear_door_open`、`passenger_rear_door_open`、
`driver_front_window_open`、`passenger_front_window_open`、`driver_rear_window_open`、`passenger_rear_window_open`、
`frunk_open`、`trunk_open`、`charge_port_door_open`

### 3.3 能源 / 充电

`battery_level`、`usable_battery_level`、`charging_state`、`charger_power`、`charger_voltage`、`charger_actual_current`、
`charge_energy_added`、`charge_limit_soc`、`time_to_full_charge`、`charge_cable_type`、`charge_curve`、`current_soc`、`current_power`、`battery_heater`

### 3.4 位置 / 环境 / 里程

`ui_current_address`、`lat`、`lng`、`inside_temp`、`outside_temp`、`driver_temp_setting`、`passenger_temp_setting`、
`is_climate_on`、`is_preconditioning`、`odometer`、`est_battery_range_km`、`rated_battery_range_km`、
`car_type`、`exterior_color`、`display_name`、`version`、`capabilities`

### 3.5 胎压

`tpms_pressure_fl` / `_fr` / `_rl` / `_rr`、`tpms_soft_warning_fl` / `_fr` / `_rl` / `_rr`、`tpms_last_seen_pressure_time_*`

### 3.6 版本升级

`update_available`、`update_version`

## 4. 解析注意事项

1. **三态字段**（`locked` / `sentry_mode`）取值为 `true` / `false` / `null` / 字符串 `"nil"` 都可能出现——服务端有把 `nil` 序列化成字符串 `"nil"` 的习惯（M62）。**不要写 `if o.locked:`**，需归一化后再判断。
2. **`/states` 不包含位置信息**——位置走 `/msg` 或 `/api/dashboard`（`location_detector.py` 的注释即指此，勿误读为"states 无用"）。
3. **仍属网络依赖**：该服务在容器里，设备断网时读不到，不能作为唯一的自检触发器。
4. **配额**：`/states` 由服务端向特斯拉侧取数，轮询频率需克制（参照 M63 的"设计余量"原则），避免与版本检测等既有轮询叠加触发限流。
5. 字段名大小写与下划线形式以本文为准，别猜；新增字段以 `app.js` 实际引用为准。
