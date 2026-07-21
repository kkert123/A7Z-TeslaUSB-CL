#!/usr/bin/env bash
# ============================================================================
# A7Z TeslaUSB 安装/初始化脚本（仅复制配置模板，绝不写入真实密钥）
# 适用：Linux（Raspberry Pi / Radxa 等）。Windows 用户请参考 README 手动操作。
#
# 约定部署路径：本仓库应位于 /opt/radxa_data/teslausb（systemd 单元里的
# WorkingDirectory / 配置路径均据此硬编码，安装脚本会在部署时自动替换为实际路径）。
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"

# 权限提示：Tailscale 安装、systemd 服务、/var/log、挂载点均需 root
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
  echo "提示：检测到非 root 运行。依赖安装 / systemd 部署 / Tailscale 需要 root 权限，"
  echo "      相关步骤会自动加 sudo；若当前用户无 sudo 权限，请在设备上以 root 重新运行。"
fi

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

echo "==> 检查系统依赖（缩略图 / 云上传需要）"
check_dep() {
  # $1=命令名  $2=缺失说明  $3=可选自动安装命令
  local _name="$1" _hint="$2" _install="$3" _ans
  if command -v "$_name" >/dev/null 2>&1; then
    echo "    $_name 已安装"
    return 0
  fi
  echo "    未检测到 $_name：$_hint"
  if [ -n "$_install" ]; then
    read -r -p "    是否尝试自动安装 $_name? [y/N]: " _ans
    if [[ "$_ans" =~ ^[Yy]$ ]]; then
      eval "$_install" || echo "    自动安装失败，请手动安装：$_hint"
    fi
  fi
}
# ffmpeg：缩略图 / 视频裁剪 / gif 生成所依赖的系统二进制
check_dep ffmpeg "缩略图与视频预览功能所需" "$SUDO apt-get update -y && $SUDO apt-get install -y ffmpeg"
# rclone：NAS / 云存储上传所需（可选功能）
check_dep rclone "NAS / 云上传功能所需 (https://rclone.org/install.sh)" "curl https://rclone.org/install.sh | $SUDO bash"

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
      curl -fsSL https://tailscale.com/install.sh | $SUDO sh || echo "    Tailscale 安装失败，请参考 https://tailscale.com/download 手动安装"
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

echo "==> 可选：部署并启用 systemd 服务"
if command -v systemctl >/dev/null 2>&1 && [ -d /etc/systemd/system ]; then
  read -r -p "    是否将 systemd 服务部署到本机? [y/N]: " _svc
  if [[ "$_svc" =~ ^[Yy]$ ]]; then
    DEPLOY_DIR="$(pwd)"
    VENV_PY="$DEPLOY_DIR/venv/bin/python"
    $SUDO cp -f services/*.service services/*.timer /etc/systemd/system/ 2>/dev/null || {
      echo "    复制服务单元失败（权限不足？），跳过部署";
      _svc=""
    }
    if [ -n "$_svc" ]; then
      # 把单元里的硬编码路径替换为实际部署路径与 venv 解释器
      if [ -x "$VENV_PY" ]; then
        $SUDO sed -i "s#/usr/bin/python3#$VENV_PY#g" /etc/systemd/system/teslausb-*.service
      fi
      $SUDO sed -i "s#/opt/radxa_data/teslausb#$DEPLOY_DIR#g" /etc/systemd/system/teslausb-*.service /etc/systemd/system/teslausb-*.timer
      $SUDO systemctl daemon-reload
      # 启用并启动核心服务
      for u in teslausb-web teslausb-sentry teslausb-sync teslausb-gadget teslausb-watchdog; do
        if [ -f "/etc/systemd/system/$u.service" ]; then
          $SUDO systemctl enable --now "$u.service" 2>/dev/null \
            && echo "    已启用并启动: $u" \
            || echo "    警告：$u 启动失败（见 journalctl -u $u）"
        fi
      done
      # 启用定时器
      for t in services/*.timer; do
        [ -e "$t" ] || continue
        bn="$(basename "$t")"
        $SUDO systemctl enable --now "$bn" 2>/dev/null \
          && echo "    已启用定时器: $bn" \
          || echo "    警告：$bn 启用失败"
      done
    fi
  else
    echo "    跳过 systemd 服务部署（可随时手动部署 services/ 下的单元）"
  fi
else
  echo "    未检测到 systemd，跳过（请参考 README 手动配置服务）"
fi

echo "==> 安装后冒烟测试"
if [ -x venv/bin/python ]; then
  venv/bin/python -c "import config; print('  OK: config 导入正常，挂载点 =', config.PARTITIONS)" \
    || echo "  警告：config 导入失败，请检查 config.py / config/paths.json"
  venv/bin/python -m py_compile app.py && echo "  OK: app.py 语法正常" \
    || echo "  警告：app.py 语法错误，请检查"
else
  echo "  跳过冒烟测试（venv 未创建）"
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
