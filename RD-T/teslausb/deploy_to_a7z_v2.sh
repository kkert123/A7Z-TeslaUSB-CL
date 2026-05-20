#!/bin/bash
# deploy_to_a7z_v2.sh - 部署 teslausb 到 A7Z 的 /opt/radxa_data/teslausb/
# 修复: 不再运行 add_to_app.py，而是直接追加内容到 app.py
# 使用方法: sudo bash deploy_to_a7z_v2.sh

set -e

TARGET_DIR="/opt/radxa_data/teslausb"
BACKUP_DIR="/opt/radxa_data/teslausb_backup_$(date +%Y%m%d_%H%M%S)"
CURRENT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=========================================="
echo "TeslaUSB Web 界面部署脚本 v2"
echo "目标路径: $TARGET_DIR"
echo "当前目录: $CURRENT_DIR"
echo "=========================================="
echo ""

# 检查是否以 root 运行
if [ "$EUID" -ne 0 ]; then 
    echo "❌ 请使用 sudo 运行此脚本"
    echo "用法: sudo bash deploy_to_a7z_v2.sh"
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
echo "📁 创建/确认目标目录..."
mkdir -p "$TARGET_DIR"
mkdir -p "$TARGET_DIR/templates"
mkdir -p "$TARGET_DIR/static"
mkdir -p "$TARGET_DIR/config"
echo "✅ 目录创建完成"
echo ""

# 3. 复制 Python 文件 (不包括 add_to_app.py)
echo "📄 复制 Python 文件..."
for pyfile in "$CURRENT_DIR"/*.py; do
    filename=$(basename "$pyfile")
    if [ "$filename" != "add_to_app.py" ]; then
        cp "$pyfile" "$TARGET_DIR/"
        echo "   ✓ $filename"
    fi
done
echo "✅ Python 文件复制完成 (已排除 add_to_app.py)"
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

# 8. 应用模式切换功能到 app.py
if [ -f "$CURRENT_DIR/add_to_app.py" ]; then
    echo "🔧 应用模式切换功能到 app.py..."
    
    # 提取 add_to_app.py 中的实际代码 (跳过导入和 if __name__ 块)
    cat >> "$TARGET_DIR/app.py" << 'PYTHON_EOF'

# ===== 模式切换功能 (自动添加) =====
import subprocess
import json as pyjson

@app.route('/api/mode/status', methods=['GET'])
def get_mode_status():
    """获取当前模式状态"""
    try:
        # 检查 USB Gadget 当前状态
        current_mode = "unknown"
        present_active = False
        edit_active = False
        
        # 检查 /sys/class/udc/*/gadget/ 来判断当前模式
        gadgets = glob.glob('/sys/class/udc/*/gadget/')
        if gadgets:
            # 有 gadget 运行
            if os.path.exists('/opt/radxa_data/usb_gadget_present.sh'):
                # 检查是否运行 present 模式
                result = subprocess.run(['pgrep', '-f', 'present_usb'], capture_output=True)
                if result.returncode == 0:
                    current_mode = "present"
                    present_active = True
                else:
                    current_mode = "edit"
                    edit_active = True
        else:
            current_mode = "stopped"
            
        return jsonify({
            'success': True,
            'mode': current_mode,
            'present_active': present_active,
            'edit_active': edit_active
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/mode/switch', methods=['POST'])
def switch_mode():
    """切换 Present/Edit 模式"""
    try:
        data = request.get_json()
        target_mode = data.get('mode', 'present')
        
        if target_mode not in ['present', 'edit', 'stop']:
            return jsonify({'success': False, 'error': '无效的模式'}), 400
        
        # 停止当前模式
        subprocess.run(['/opt/radxa_data/usb_gadget_stop.sh'], check=False)
        time.sleep(2)
        
        if target_mode == 'stop':
            return jsonify({'success': True, 'message': '已停止所有模式'})
        
        # 启动目标模式
        if target_mode == 'present':
            script = '/opt/radxa_data/usb_gadget_present.sh'
        else:
            script = '/opt/radxa_data/usb_gadget_edit.sh'
            
        if os.path.exists(script):
            subprocess.Popen([script], start_new_session=True)
            time.sleep(3)
            return jsonify({'success': True, 'message': f'已切换到 {target_mode} 模式'})
        else:
            return jsonify({'success': False, 'error': f'脚本不存在: {script}'}), 500
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ===== 模式切换功能结束 =====
PYTHON_EOF
    
    echo "✅ 模式切换功能已添加到 app.py"
    echo ""
fi

# 9. 确保 app.py 有必要的导入
echo "🔧 检查并添加必要的导入..."
sed -i '1a import glob' "$TARGET_DIR/app.py"
sed -i '1a import subprocess' "$TARGET_DIR/app.py"
sed -i '1a import time' "$TARGET_DIR/app.py"
echo "✅ 导入检查完成"
echo ""

# 10. 设置权限
echo "🔐 设置文件权限..."
chmod +x "$TARGET_DIR"/*.py
chown -R radxa:radxa "$TARGET_DIR" 2>/dev/null || chown -R root:root "$TARGET_DIR"
echo "✅ 权限设置完成"
echo ""

# 11. 重载 systemd 并重启服务
echo "🔄 重载 systemd 并重启服务..."
systemctl daemon-reload
systemctl enable teslausb-web.service 2>/dev/null || true
systemctl restart teslausb-web.service 2>/dev/null || true
echo "✅ 服务操作完成"
echo ""

# 12. 显示服务状态
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
