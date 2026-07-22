# A7Z TeslaUSB

把一台小 ARM 板子塞车里，接上 M.2 SSD，它自己搞定特斯拉哨兵视频的管理。

拿 [TeslaUSB](https://github.com/mphacker/TeslaUSB) 的方案做底，重新写了 Web 管理后台、企业微信通知和云备份那几块。GPL-3.0 开源。

## 装

```bash
git clone <repo> /opt/radxa_data/teslausb && cd /opt/radxa_data/teslausb
chmod +x install.sh && sudo ./install.sh
```

跑完装好 venv、ffmpeg、rclone 和 systemd 服务。脚本会问你几个分区路径，回答完就行。

## 用

```bash
source venv/bin/activate && python app.py
```

浏览器打开 `http://设备IP:5000`：

- `/` 仪表盘：最近哨兵事件的缩略图预览
- `/videos`：所有事件列表，点进去可以看视频
- `/system`：**配置页**，在这填企业微信 webhook、TeslaMate 地址、NAS 路径
- `/media`：上传车牌、涂装、Boombox、灯光秀文件，车机下次启动就生效

配置全在 Web 上改，不用 SSH 上去翻 JSON。改完即时生效。

## 注意事项

- ffmpeg 缩略图是后台线程跑的，大文件多的时候可能需要点时间，不影响 Web 浏览
- 企业微信通知依赖 webhook 配置，在 `/system` 页填好群机器人的 key 就行
- 云备份走 rclone，14 种远端都支持，在网页上选类型填参数
- USB Gadget 需要内核有 ConfigFS，Radxa 官方镜像默认开了

## 参考与许可

- [mphacker/TeslaUSB](https://github.com/mphacker/TeslaUSB) — USB Gadget 和 auto_cleanup 核心
- [ejaramilla/teslausb-neo](https://github.com/ejaramilla/teslausb-neo) — boot_notify 和清理模块增强
- [DeaglePC/TDashcamStudio](https://github.com/DeaglePC/TDashcamStudio) — 前端视频播放器

本仓库 GPL-3.0，跟上游保持一致。`LICENSE` 文件里有全文。
