#!/bin/bash
# 完整调试和修复 switch_mode() 函数

APP_PY="/opt/radxa_data/teslausb/app.py"

echo "=========================================="
echo "调试 switch_mode() 函数"
echo "=========================================="
echo ""

# 1. 显示完整的 switch_mode() 函数
echo "📊 完整的 switch_mode() 函数:"
echo "=========================================="
grep -A 50 "def switch_mode():" "$APP_PY" | head -60
echo "=========================================="
echo ""

# 2. 检查 Python 语法
echo "🧪 验证 Python 语法..."
python3 -m py_compile "$APP_PY" 2>&1
if [ $? -eq 0 ]; then
    echo "✅ 语法检查通过!"
else
    echo "❌ 语法错误！"
    echo "正在修复..."
    
    # 备份
    cp "$APP_PY" "${APP_PY}.broken.$(date +%Y%m%d_%H%M%S)"
    
    # 重新部署修复
    bash /opt/radxa_data/fix_switch_mode_v2.sh
    exit $?
fi
echo ""

# 3. 测试 API 端点
echo "🧪 测试 API 端点..."
response=$(curl -s -X POST http://localhost:5000/api/mode/switch \
  -H 'Content-Type: application/json' \
  -d '{"mode":"edit"}')
echo "响应: $response"
echo ""

# 4. 检查脚本是否存在
echo "📊 检查切换脚本..."
echo "  - present_usb.sh: $(test -x /opt/radxa_data/present_usb.sh && echo '✅ 存在且可执行' || echo '❌ 不存在或不可执行')"
echo "  - edit_usb.sh: $(test -x /opt/radxa_data/edit_usb.sh && echo '✅ 存在且可执行' || echo '❌ 不存在或不可执行')"
echo ""

# 5. 手动测试脚本
echo "🧪 手动测试 edit_usb.sh..."
if [ -x /opt/radxa_data/edit_usb.sh ]; then
    bash -n /opt/radxa_data/edit_usb.sh 2>&1 && echo "✅ 脚本语法正确" || echo "❌ 脚本语法错误"
else
    echo "⚠️  脚本不可执行，跳过测试"
fi
echo ""

# 6. 查看最近的日志
echo "📝 最近的 Web 服务日志 (switch_mode 相关):"
sudo journalctl -u teslausb-web --no-pager -n 50 | grep -E "switch_mode|切换|ERROR" | tail -20
echo ""

echo "=========================================="
echo "调试完成"
echo "=========================================="
