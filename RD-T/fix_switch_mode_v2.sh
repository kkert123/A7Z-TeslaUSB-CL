#!/bin/bash
# 修复 switch_mode() 函数 - 真正执行模式切换脚本
# 使用方法: bash fix_switch_mode_v2.sh

APP_PY="/opt/radxa_data/teslausb/app.py"
BACKUP="${APP_PY}.backup.$(date +%Y%m%d_%H%M%S)"
TMP_DIR="/opt/radxa_data"
TMP_FILE="${TMP_DIR}/switch_mode_new.py"

echo "=========================================="
echo "修复 switch_mode() 函数 (v2)"
echo "=========================================="
echo ""

# 检查 app.py 是否存在
if [ ! -f "$APP_PY" ]; then
    echo "❌ 错误: $APP_PY 不存在!"
    exit 1
fi

# 备份
echo "📦 备份 app.py..."
cp "$APP_PY" "$BACKUP"
if [ $? -eq 0 ]; then
    echo "✅ 备份成功: $BACKUP"
else
    echo "❌ 备份失败!"
    exit 1
fi
echo ""

# 查找当前的 switch_mode() 函数
echo "🔍 查找当前的 switch_mode() 函数..."
START_LINE=$(grep -n "def switch_mode():" "$APP_PY" | cut -d: -f1)

if [ -z "$START_LINE" ]; then
    echo "❌ 错误: 找不到 switch_mode() 函数!"
    exit 1
fi

echo "✅ 找到 switch_mode() 函数 (第 $START_LINE 行)"

# 查找函数结束位置（下一个 @app.route 或 def ）
END_LINE=$(tail -n +$((START_LINE + 1)) "$APP_PY" | grep -n -E "^@app\.route|^def " | head -1 | cut -d: -f1)
if [ -n "$END_LINE" ]; then
    END_LINE=$((START_LINE + END_LINE - 1))
else
    # 如果找不到，就到文件末尾
    END_LINE=$(wc -l < "$APP_PY")
fi

echo "📊 函数范围: 第 $START_LINE - $END_LINE 行"
echo ""

# 生成新的 switch_mode() 函数
echo "🔧 生成新的 switch_mode() 函数..."

cat > "$TMP_FILE" << 'FUNC_EOF'
@app.route('/api/mode/switch', methods=['POST'])
def switch_mode():
    """真正执行模式切换 - 调用底层脚本"""
    import subprocess
    import os
    
    data = request.get_json()
    mode = data.get('mode', '').lower()
    
    if mode not in ['present', 'edit']:
        return jsonify({'success': False, 'error': '无效的模式'}), 400
    
    try:
        # 根据模式选择脚本
        if mode == 'present':
            script_path = '/opt/radxa_data/present_usb.sh'
            mode_name = 'Present Mode (连接 Tesla)'
        else:  # edit mode
            script_path = '/opt/radxa_data/edit_usb.sh'
            mode_name = 'Edit Mode (网络访问)'
        
        # 检查脚本是否存在
        if not os.path.exists(script_path):
            return jsonify({
                'success': False,
                'error': f'切换脚本不存在: {script_path}'
            }), 500
        
        # 记录日志
        app.logger.info(f"🔄 开始切换到 {mode_name}...")
        app.logger.info(f"📝 执行脚本: {script_path}")
        
        # 执行切换脚本（等待完成）
        result = subprocess.run(
            ['bash', script_path],
            capture_output=True,
            text=True,
            timeout=60  # 60秒超时
        )
        
        # 检查执行结果
        if result.returncode == 0:
            app.logger.info(f"✅ 成功切换到 {mode_name}")
            app.logger.debug(f"脚本输出: {result.stdout}")
            
            return jsonify({
                'success': True,
                'mode': mode,
                'message': f'已切换到 {mode_name}',
                'script_output': result.stdout[-500:] if result.stdout else ''  # 只返回最后500字符
            })
        else:
            # 脚本执行失败
            error_msg = result.stderr or result.stdout or '未知错误'
            app.logger.error(f"❌ 切换失败: {error_msg}")
            
            return jsonify({
                'success': False,
                'mode': mode,
                'error': error_msg[-500:]  # 只返回最后500字符
            }), 500
            
    except subprocess.TimeoutExpired:
        app.logger.error("❌ 切换脚本执行超时（60秒）")
        return jsonify({
            'success': False,
            'error': '切换脚本执行超时，请检查系统状态'
        }), 500
        
    except Exception as e:
        app.logger.error(f"❌ 切换过程发生异常: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'切换失败: {str(e)}'
        }), 500

FUNC_EOF

if [ $? -eq 0 ]; then
    echo "✅ 新函数生成成功!"
else
    echo "❌ 新函数生成失败!"
    exit 1
fi
echo ""

# 替换函数
echo "🔧 替换 switch_mode() 函数..."
{
    head -n $((START_LINE - 1)) "$APP_PY"
    cat "$TMP_FILE"
    tail -n +$((END_LINE + 1)) "$APP_PY"
} > "${APP_PY}.tmp"

if [ $? -eq 0 ]; then
    mv "${APP_PY}.tmp" "$APP_PY"
    echo "✅ 函数替换成功!"
else
    echo "❌ 函数替换失败!"
    exit 1
fi
echo ""

# 清理临时文件
rm -f "$TMP_FILE"

# 验证语法
echo "🧪 验证 Python 语法..."
python3 -m py_compile "$APP_PY" 2>&1 | head -20

if [ $? -eq 0 ]; then
    echo "✅ Python 语法验证通过!"
    echo ""
    echo "🔄 重启 Web 服务..."
    sudo systemctl restart teslausb-web
    
    if [ $? -eq 0 ]; then
        echo "✅ 服务重启成功!"
        echo ""
        echo "=========================================="
        echo "✅ 修复完成!"
        echo "=========================================="
        echo ""
        echo "📊 下一步:"
        echo "  1. 清除浏览器缓存 (Ctrl + F5)"
        echo "  2. 访问 http://100.116.18.42"
        echo "  3. 测试模式切换，这次应该真正生效了！"
        echo ""
        echo "📝 查看日志:"
        echo "  sudo journalctl -u teslausb-web -f"
        echo ""
    else
        echo "⚠️  服务重启失败，请手动重启"
    fi
else
    echo "❌ Python 语法错误!"
    echo "📦 恢复备份..."
    cp "$BACKUP" "$APP_PY"
    exit 1
fi
