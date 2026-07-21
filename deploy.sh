#!/bin/bash
# ============================================================
# deploy.sh - A7Z TeslaUSB 标准化部署脚本 v1.0
# ============================================================
# 用法:
#   ./deploy.sh              # 交互式部署
#   ./deploy.sh --dry-run    # 仅预览，不执行
#   ./deploy.sh --quick       # 跳过确认
# ============================================================

set -euo pipefail

# --- 配置 ---
A7Z_HOST="${A7Z_HOST:-100.116.18.42}"
A7Z_USER="${A7Z_USER:-radxa}"
REMOTE_BASE="/opt/radxa_data/teslausb"
LOCAL_BASE="$(cd "$(dirname "$0")" && pwd)"
DRY_RUN=false
QUICK_MODE=false

# --- 参数解析 ---
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --quick)   QUICK_MODE=true ;;
        -h|--help)
            echo "用法: $0 [--dry-run] [--quick]"
            echo "  --dry-run  仅预览将要部署的文件"
            echo "  --quick    跳过交互确认"
            exit 0
            ;;
    esac
done

# --- 颜色 ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "============================================"
echo " A7Z TeslaUSB 部署工具 v1.0"
echo "============================================"
echo "  目标: ${A7Z_USER}@${A7Z_HOST}:${REMOTE_BASE}"
echo "  模式: $([ "$DRY_RUN" = true ] && echo 'DRY-RUN' || echo 'LIVE')"
echo ""

# --- 部署文件清单 ---
DEPLOY_FILES=(
    # Python 核心模块
    "app.py"
    "boot_notify.py"
    "config.py"
    "config_manager.py"
    "media_service.py"
    "wifi_service.py"
    "video_preview.py"
    "sync_service.py"
    "sentry_watchdog.py"
    "sentry_service.py"
    "system_monitor.py"
    "auto_cleanup.py"
    "location_detector.py"
    "hardware_watchdog.py"
    "fsck_check.py"
    "upload_scheduler.py"
    "preview_generator.py"
    "weixin_notifier.py"
    "player_routes.py"
    "disk_image_manager.py"
    "sei_service.py"
    "teslausb-gadgetd.py"
    "dashcam_pb2.py"
    "add_to_app.py"
    "_player_loader.py"
)

DEPLOY_TEMPLATES=(
    "analytics.html" "base.html" "boombox.html" "dashboard.html"
    "lightshow.html" "lockchime.html" "login.html" "logs.html"
    "media.html" "player.html" "sentry.html" "system.html"
    "upload_progress.html" "videos.html" "wifi.html" "wraps.html"
)

DEPLOY_STATIC=(
    "app.js" "style.css" "placeholder.svg"
)

DEPLOY_CONFIG=(
    "dashcam.proto"
)

DEPLOY_SERVICES=(
    "teslausb-web.service" "teslausb-sentry.service"
    "teslausb-gadget.service" "teslausb-boot-notify.service"
    "teslausb-mode.service" "teslausb-fsck.service"
    "teslausb-fsck.timer" "teslausb-io-tune.service"
)

# --- 函数 ---
deploy_file() {
    local local_path="$1"
    local remote_path="$2"
    
    if [ ! -f "$local_path" ]; then
        echo -e "  ${YELLOW}SKIP${NC} $local_path (not found locally)"
        return 0
    fi
    
    if [ "$DRY_RUN" = true ]; then
        echo -e "  ${GREEN}WOULD${NC} upload: $local_path -> $remote_path"
        return 0
    fi
    
    scp -q "$local_path" "${A7Z_USER}@${A7Z_HOST}:${remote_path}" && \
        echo -e "  ${GREEN}OK${NC}   $local_path" || \
        echo -e "  ${RED}FAIL${NC} $local_path"
}

health_check() {
    echo ""
    echo "--- 健康检查 ---"
    if [ "$DRY_RUN" = true ]; then
        echo "  (dry-run: 跳过健康检查)"
        return
    fi
    
    # 重启服务
    echo "  重启 teslausb-web..."
    ssh "${A7Z_USER}@${A7Z_HOST}" "sudo systemctl restart teslausb-web" 2>/dev/null || true
    
    sleep 3
    
    # HTTP 检查
    if curl -s -o /dev/null -w "%{http_code}" "http://${A7Z_HOST}:5000/" 2>/dev/null | grep -q 200; then
        echo -e "  ${GREEN}HTTP 200${NC} - Web 服务正常"
    else
        echo -e "  ${RED}HTTP 检查失败${NC} - 请检查服务状态"
    fi
    
    # 服务状态
    ssh "${A7Z_USER}@${A7Z_HOST}" "systemctl is-active teslausb-web teslausb-sentry" 2>/dev/null || true
}

# --- 主流程 ---
echo "--- 部署 Python 模块 ---"
for f in "${DEPLOY_FILES[@]}"; do
    deploy_file "$LOCAL_BASE/$f" "$REMOTE_BASE/$f"
done

echo ""
echo "--- 部署模板 ---"
for f in "${DEPLOY_TEMPLATES[@]}"; do
    deploy_file "$LOCAL_BASE/templates/$f" "$REMOTE_BASE/templates/$f"
done

echo ""
echo "--- 部署静态资源 ---"
for f in "${DEPLOY_STATIC[@]}"; do
    deploy_file "$LOCAL_BASE/static/$f" "$REMOTE_BASE/static/$f"
done

echo ""
echo "--- 部署配置 ---"
for f in "${DEPLOY_CONFIG[@]}"; do
    deploy_file "$LOCAL_BASE/config/$f" "$REMOTE_BASE/$f"
done

echo ""
echo "--- 部署 systemd 服务 ---"
for f in "${DEPLOY_SERVICES[@]}"; do
    deploy_file "$LOCAL_BASE/services/$f" "/etc/systemd/system/$f"
done

# 健康检查
health_check

echo ""
echo "============================================"
echo " 部署完成"
echo "============================================"
