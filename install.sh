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

echo "==> 配置视频/媒体存储路径"
echo "    下列路径是各类型文件的存储位置（默认指向 M.2 挂载点）。"
echo "    若你的磁盘挂载点不同，请输入实际绝对路径；直接回车沿用默认。"
ask_mount() {
  # $1=显示标签  $2=默认值  -> 输出用户输入（去尾部斜杠）
  local _label="$1" _def="$2" _val
  printf '%s [%s]: ' "$_label" "$_def" >&2
  read -r _val
  _val="${_val:-$_def}"
  printf '%s' "${_val%/}"
}
CAM=$(ask_mount "TeslaCam 视频存储路径 (cam)" "/mnt/teslacam")
MUSIC=$(ask_mount "music 存储路径" "/mnt/music")
BOOMBOX=$(ask_mount "boombox 存储路径" "/mnt/boombox")
LIGHTSHOW=$(ask_mount "lightshow 存储路径" "/mnt/lightshow")

mkdir -p config
cat > config/paths.json <<EOF
{
  "cam": "$CAM",
  "music": "$MUSIC",
  "boombox": "$BOOMBOX",
  "lightshow": "$LIGHTSHOW"
}
EOF
echo "    已写入 config/paths.json（该文件已被 .gitignore 忽略，不会提交）"

echo "==> 可选：安装 Node/Playwright 用于缩略图预览生成"
if command -v npm >/dev/null 2>&1; then
  npm install
  npx playwright install chromium || echo "    警告：Playwright 浏览器下载失败，预览功能将不可用（不影响主流程）"
else
  echo "    未检测到 npm，跳过 Playwright（预览生成为可选功能）"
fi

echo "==> 可选：安装 Tailscale（远程访问 / 内网穿透工具）"
if command -v tailscale >/dev/null 2>&1; then
  echo "    Tailscale 已安装，跳过"
else
  read -r -p "    是否安装 Tailscale? [y/N]: " _ts
  if [[ "$_ts" =~ ^[Yy]$ ]]; then
    if command -v curl >/dev/null 2>&1; then
      echo "    正在通过官方脚本安装 Tailscale..."
      curl -fsSL https://tailscale.com/install.sh | sh || echo "    Tailscale 安装失败，请参考 https://tailscale.com/download 手动安装"
      if command -v tailscale >/dev/null 2>&1; then
        echo "    安装完成。请在本机（需 root）执行以下命令完成登录："
        echo "      sudo tailscale up"
        echo "    或带 authkey 无交互登录："
        echo "      sudo tailscale up --authkey <YOUR_AUTHKEY>"
      fi
    else
      echo "    未检测到 curl，无法自动安装。请参考 https://tailscale.com/download 手动安装。"
    fi
  else
    echo "    跳过 Tailscale 安装"
  fi
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
