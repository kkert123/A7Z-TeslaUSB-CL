#!/bin/bash
# deploy_to_a7z.sh - 部署 teslausb 到 A7Z 的 /opt/radxa_data/teslausb/
# 使用方法: sudo bash deploy_to_a7z.sh

set -e

TARGET_DIR="/opt/radxa_data/teslausb"
BACKUP_DIR="/opt/radxa_data/teslausb_backup_$(date +%Y%m%d_%H%M%S)"
CURRENT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=========================================="
echo "TeslaUSB Web 界面部署脚本"
echo "目标路径: $TARGET_DIR"
echo "当前目录: $CURRENT_DIR"
echo "=========================================="
echo ""

# 检查是否以 root 运行
if [ "$EUID" -ne 0 ]; then 
    echo "❌ 请使用 sudo 运行此脚本"
    echo "用法: sudo bash deploy_to_a7z.sh"
    exit 1
fi

# 1. 备份现有安装
if [ -d "$TARGET_DIR" ]; then
    echo "📦 备份现有安装到: $BACKUP_DIR"
    cp -r "$TARGET_DIR" "$BACKUP_DIR"
    echo "✅ 备份完成"
    echo ""
fi

# 2. 创建目标目录
echo "📁 创建目标目录..."
mkdir -p "$TARGET_DIR"
mkdir -p "$TARGET_DIR/templates"
mkdir -p "$TARGET_DIR/static"
mkdir -p "$TARGET_DIR/config"
echo "✅ 目录创建完成"
echo ""

# 3. 复制 Python 文件
echo "📄 复制 Python 文件..."
cp "$CURRENT_DIR"/*.py "$TARGET_DIR/" 2>/dev/null || true
echo "✅ Python 文件复制完成"
echo ""

# 4. 复制模板文件
echo "📄 复制模板文件..."
cp "$CURRENT_DIR/templates/"*.html "$TARGET_DIR/templates/" 2>/dev/null || true
echo "✅ 模板文件复制完成"
echo ""

# 5. 复制静态文件
echo "📄 复制静态文件..."
cp "$CURRENT_DIR/static/"* "$TARGET_DIR/static/" 2>/dev/null || true
echo "✅ 静态文件复制完成"
echo ""

# 6. 复制配置文件
echo "📄 复制配置文件..."
cp "$CURRENT_DIR/config/"* "$TARGET_DIR/config/" 2>/dev/null || true
echo "✅ 配置文件复制完成"
echo ""

# 7. 复制服务文件
echo "📄 复制 systemd 服务文件..."
if [ -f "$CURRENT_DIR/teslausb-web.service" ]; then
    cp "$CURRENT_DIR/teslausb-web.service" "/etc/systemd/system/"
    echo "✅ 服务文件复制到 /etc/systemd/system/"
fi
echo ""

# 8. 设置权限
echo "🔐 设置文件权限..."
chmod +x "$TARGET_DIR"/*.py
chown -R radxa:radxa "$TARGET_DIR" 2>/dev/null || chown -R root:root "$TARGET_DIR"
echo "✅ 权限设置完成"
echo ""

# 9. 应用 app.py 的补丁（如果 add_to_app.py 存在）
if [ -f "$CURRENT_DIR/add_to_app.py" ]; then
    echo "🔧 应用 app.py 补丁..."
    cd "$TARGET_DIR"
    python3 "$CURRENT_DIR/add_to_app.py"
    echo "✅ 补丁应用完成"
    echo ""
fi

# 10. 重载 systemd 并重启服务
echo "🔄 重载 systemd 并重启服务..."
systemctl daemon-reload
systemctl enable teslausb-web.service 2>/dev/null || true
systemctl restart teslausb-web.service 2>/dev/null || true
echo "✅ 服务已重启"
echo ""

# 11. 显示服务状态
echo "📊 服务状态:"
systemctl status teslausb-web.service --no-pager || true
echo ""

echo "=========================================="
echo "✅ 部署完成！"
echo "=========================================="
echo ""
echo "📍 安装位置: $TARGET_DIR"
echo "📍 备份位置: $BACKUP_DIR"
echo "🌐 Web 界面: http://$(hostname -I | awk '{print $1}'):5000"
echo ""
echo "🔍 查看日志: sudo journalctl -u teslausb-web.service -f"
echo "🔧 重启服务: sudo systemctl restart teslausb-web.service"
echo ""
