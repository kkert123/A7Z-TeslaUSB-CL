#!/usr/bin/env bash
# ============================================================================
# A7Z TeslaUSB 安装/初始化脚本（仅复制配置模板，绝不写入真实密钥）
# 适用：Linux（Raspberry Pi / Radxa 等）。Windows 用户请参考 README 手动操作。
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"

echo "==> 检查 Python3"
if ! command -v python3 >/dev/null 2>&1; then
  echo "错误：未找到 python3，请先安装 Python 3.8+"
  exit 1
fi

echo "==> 创建虚拟环境 venv 并安装 Python 依赖"
python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "==> 准备配置文件（仅复制模板，请随后填入你自己的密钥）"
# 哨兵配置位于 config/ 目录
if [ ! -f "config/sentry.json" ]; then
  cp "config/sentry.example.json" "config/sentry.json"
  echo "    已生成 config/sentry.json（模板），请编辑填入真实值"
else
  echo "    config/sentry.json 已存在，跳过"
fi
# 微信配置位于仓库根目录
if [ ! -f "weixin_config.json" ]; then
  cp "weixin_config.example.json" "weixin_config.json"
  echo "    已生成 weixin_config.json（模板），请编辑填入真实值"
else
  echo "    weixin_config.json 已存在，跳过"
fi

echo "==> 可选：安装 Node/Playwright 用于缩略图预览生成"
if command -v npm >/dev/null 2>&1; then
  npm install
  npx playwright install chromium || echo "    警告：Playwright 浏览器下载失败，预览功能将不可用（不影响主流程）"
else
  echo "    未检测到 npm，跳过 Playwright（预览生成为可选功能）"
fi

echo ""
echo "=============================================================="
echo " 安装完成。"
echo " 下一步："
echo "   1) 编辑 config/sentry.json  -> 填入企业微信 webhook key"
echo "   2) 编辑 weixin_config.json   -> 填入企业微信 webhook_url 与 secret"
echo "   3) 运行： source venv/bin/activate && python app.py"
echo " 注意：config/sentry.json 与 weixin_config.json 已被 .gitignore 忽略，"
echo "       切勿提交真实密钥！"
echo "=============================================================="
