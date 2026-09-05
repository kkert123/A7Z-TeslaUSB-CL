# A7Z TeslaUSB 文档

文档按 [Divio 四象限](https://documentation.divio.com/) 组织：**教程**（学习）、**操作手册**（做事）、**参考**（查表）、**说明**（理解）。四类不混写 —— 想学东西看教程，想干活看手册，想查东西看参考，想搞懂为什么看说明。

## 按你的角色选入口

| 你是 | 从这里开始 |
|------|-----------|
| 刚接手这个项目 | [15 分钟理解 A7Z](tutorials/01-十五分钟理解A7Z.md) |
| 拿到一台新设备要装系统 | [接管一台新设备](tutorials/02-接管一台新设备.md) |
| 日常改代码要部署 | [日常部署与回滚](how-to/日常部署与回滚.md) |
| 要发一个新版本 | [发布新版本](how-to/发布新版本.md) |
| 要修一个 Bug | [Bug 修复流程](how-to/Bug修复流程.md) |
| 设备出问题了 | [排障手册](how-to/排障手册.md) |
| 要查服务 / 路径 / 配置项 | [参考](#参考) |

---

## 教程（Tutorials）

跟着做一遍，做完就懂了。

- [01 · 15 分钟理解 A7Z](tutorials/01-十五分钟理解A7Z.md) —— 这个系统到底在干什么，数据怎么流动
- [02 · 接管一台新设备](tutorials/02-接管一台新设备.md) —— 从裸机到 Web 界面可用

## 操作手册（How-to）

有明确目标、照着做就能完成。

- [日常部署与回滚](how-to/日常部署与回滚.md) —— 改完代码怎么上设备，出问题怎么退回去
- [Bug 修复流程](how-to/Bug修复流程.md) —— 从 Bug 结构化到发布验证的 12 步标准流程
- [发布新版本](how-to/发布新版本.md) —— 打升级包、推 GitHub、建 Release 的完整流程
- [写发布说明](reference/发布说明规范.md) —— GitHub Release 的写作规范与模板
- [排障手册](how-to/排障手册.md) —— 按症状索引：车机报错、连不上网、缩略图异常、服务起不来

## 参考（Reference）

查表用的事实，不讲故事。

- [服务清单](reference/服务清单.md) —— 所有 systemd service / timer 及用途
- [路径与配置项](reference/路径与配置项.md) —— 目录布局、配置文件、环境变量
- [版本历史](reference/版本历史.md) —— v0.3.1.20 起的版本变更索引
- [教训索引](reference/教训索引.md) —— M15~M56 踩坑速查，改代码前值得扫一眼
- [发布说明规范](reference/发布说明规范.md) —— 写作风格与检查清单

## 说明（Explanation）

讲清楚为什么这么设计。

- [架构总览](explanation/架构总览.md) —— 模块划分与关键设计决策
- [缓存一致性](explanation/缓存一致性.md) —— Present 模式只读挂载下「货不对板」的成因与解法演进
- [WiFi 与 AP 状态机](explanation/WiFi与AP状态机.md) —— 双模切换、自愈探测、退避策略
- [USB Gadget 与车机交互](explanation/USB-Gadget与车机交互.md) —— Gadget 生命周期、udev 规则、端点故障
- [硬件编解码现状](explanation/硬件编解码现状.md) —— A733 硬转不可用，软转参数与验证结论

## 归档

以下文档是历史产物，内容仍有参考价值但不再维护，新内容请写到上面的分类里：

- `docs/system_design.md` —— 2026-06 深度重构时的设计与任务分解（含类图、时序图）
- `docs/watchdog-design.md` —— 哨兵 watchdog 设计
- `docs/deploy_manager_guide.md` —— deploy_manager 用法（已并入[日常部署与回滚](how-to/日常部署与回滚.md)）
- `docs/DEVELOPMENT.md` —— 旧开发手册（关键模块速查仍有效）
- `docs/code-review-standards.md` —— 代码审查标准
- `docs/conversation-summary-2026-05-28.md` —— 历史会话记录
- `docs/class-diagram.mermaid` / `docs/sequence-diagram.mermaid` —— 图源文件

---

## 文档维护约定

1. **改代码就要改文档** —— 文档滞后视为 bug，和代码一起提交
2. **一个文件只讲一类事** —— 教程里不写参考表，参考里不写背景故事
3. **写操作手册时自己跑一遍** —— 跑不通的步骤等于没写
4. **新踩的坑进[教训索引](reference/教训索引.md)** —— 编号递增，一行一条，附版本号
5. **发布说明按[规范](reference/发布说明规范.md)写** —— 发布前过一遍检查清单
