# A7Z TeslaUSB 开发手册

> 最后更新: 2026-07-14 (v160)

---

## 开发流程

```bash
# 1. 本地修改代码
# 2. 代码审查
# 3. 部署:
python deploy_manager.py deploy -f file1.py,file2.py -m "描述" -y
# 4. 验证:
curl -s http://100.116.18.42:5000/api/system/stats-stream | head -2

# 回滚:
python deploy_manager.py rollback v{N}
```

## 文件管理

- 新增文件需加入 `deploy_manager.py` 的 `Config.MANAGED_FILES` 白名单
- 部署自动: 备份远程 → 上传 → SHA256 校验 → 版本记录 → 重启服务
- 重启的服务: `teslausb-web`, `teslausb-bgpreview`

## 关键模块速查

| 模块 | 职责 |
|------|------|
| `utils/thumbnail_decision.py` | 缩略图缓存有效性判断（唯一决策中心） |
| `utils/mvhd_timestamp.py` | MP4 mvhd atom 时间戳提取（修正 Tesla 时钟偏差） |
| `utils/log_rotator.py` | 日志轮转（systemd timer 每日触发） |
| `utils/app_helpers.py` | SSE 广播器、仪表盘状态采集、预览队列管理 |
| `routes/system_routes.py` | SSE 端点、系统 API |
| `routes/misc_routes.py` | 日志 API、缩略图服务、历史日志查询 |
| `routes/video_routes.py` | 视频列表/事件管理（含 mvhd 时钟修正） |
| `bg_preview_generator.py` | 后台缩略图生成器（CPU 自适应） |
| `gif_service.py` | GIF 时光机合成 |
| `video_service.py` | 视频扫描、MP4 验证、缩略图路径 |

## 常见陷阱

1. **SSE 广播**: 必须用 `put_nowait()` 或 `put(timeout=N)`，禁止 `put(block=True)`
2. **except: pass**: 后台循环至少需要一条错误日志
3. **缩略图修改**: 3 个入口点都要改（serve_thumbnail / _scan_missing / bg_preview）
4. **异步 JS**: `finally` 在 `setTimeout` 回调完成前就会执行
5. **Format 后 fstab**: 必须更新 UUID，否则启动进入 emergency mode
6. **新增 Python 文件**: 要加入 `deploy_manager.py` 白名单

## A7Z 连接信息

- Tailscale: `100.116.18.42`
- SSH: `radxa@100.116.18.42`，密码 `radxa`，sudo 需手动输入
- TeslaCam: `/mnt/teslacam/TeslaCam/{SentryClips,SavedClips,RecentClips}`
- 缩略图: `/opt/radxa_data/teslausb/static/thumbnails/`
- 日志: `/var/log/teslausb-*.log`
