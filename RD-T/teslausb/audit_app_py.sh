#!/bin/bash
# 完整审核和修复 app.py

set -e

APP_PY="/opt/radxa_data/teslausb/app.py"
BACKUP="${APP_PY}.backup.$(date +%Y%m%d_%H%M%S)"

echo "============================================================"
echo "🔍 完整审核 app.py"
echo "============================================================"

# 0. 备份
echo ""
echo "📦 备份文件..."
cp "$APP_PY" "$BACKUP"
echo "✅ 已备份: $BACKUP"

# 1. 检查 Python 语法
echo ""
echo "============================================================"
echo "🧪 1. 检查 Python 语法"
echo "============================================================"
python3 -m py_compile "$APP_PY" 2>&1 && echo "✅ 语法检查通过" || {
    echo "❌ 语法错误！"
    python3 -m py_compile "$APP_PY" 2>&1 | head -20
}

# 2. 修复缺少逗号的 return 语句
echo ""
echo "============================================================"
echo "🔧 2. 修复缺少逗号的 return 语句"
echo "============================================================"
count=$(grep -c "return jsonify.*[0-9])" "$APP_PY" || true)
if [ "$count" -gt 0 ]; then
    echo "  发现 $count 处可能缺少逗号..."
    sed -i 's/return jsonify($.*$)\s\+$[0-9]\+$/return jsonify(\1), \2/' "$APP_PY"
    echo "✅ 已修复"
else
    echo "  ℹ️  没有发现问题"
fi

# 3. 修复 with open 缺少逗号
echo ""
echo "============================================================"
echo "🔧 3. 修复 with open 缺少逗号"
echo "============================================================"
count=$(grep -c "with open.*as" "$APP_PY" | grep -v "," || true)
if [ "$count" -gt 0 ]; then
    echo "  发现 $count 处可能缺少逗号..."
    sed -i 's/with open($.*$) as/with open(\1), as/' "$APP_PY"
    echo "✅ 已修复"
else
    echo "  ℹ️  没有发现问题"
fi

# 4. 删除重复的函数定义
echo ""
echo "============================================================"
echo "🔧 4. 检查重复的函数定义"
echo "============================================================"
echo "  检查 /api/mode/status 路由..."
mode_status_count=$(grep -c "@app.route.*mode/status" "$APP_PY" || true)
if [ "$mode_status_count" -gt 1 ]; then
    echo "  ⚠️  发现 $mode_status_count 个 /api/mode/status 路由定义"
    echo "  保留 get_mode_status()，删除其他..."
    # 删除第 479-485 行的旧函数（如果存在）
    sed -i '479,485d' "$APP_PY" 2>/dev/null || true
    echo "  ℹ️  请手动验证"
    grep -n "@app.route.*mode/status" "$APP_PY"
else
    echo "  ✅ 没有重复定义"
fi

# 5. 验证 get_mode_status() 实现
echo ""
echo "============================================================"
echo "🔍 5. 验证 get_mode_status() 实现"
echo "============================================================"
if grep -q "def get_mode_status" "$APP_PY"; then
    echo "  ✅ 找到 get_mode_status() 函数"
    
    # 提取函数内容
    start_line=$(grep -n "def get_mode_status" "$APP_PY" | cut -d: -f1)
    end_line=$((start_line + 20))
    func_content=$(sed -n "${start_line},${end_line}p" "$APP_PY")
    
    if echo "$func_content" | grep -q "UDC\|udc_path"; then
        echo "  ✅ 函数检查 UDC 文件（正确）"
    else
        echo "  ⚠️  函数可能未检查 UDC 文件"
    fi
else
    echo "  ❌ 未找到 get_mode_status() 函数"
fi

# 6. 最终语法检查
echo ""
echo "============================================================"
echo "🧪 6. 最终语法检查"
echo "============================================================"
python3 -m py_compile "$APP_PY" 2>&1 && {
    echo "✅ 语法检查通过！"
    echo ""
    echo "============================================================"
    echo "🚀 重启服务"
    echo "============================================================"
    systemctl restart teslausb-web
    sleep 3
    echo "✅ 服务已重启"
    echo ""
    echo "🧪 测试 API..."
    curl -s http://localhost:5000/api/mode/status | python3 -m json.tool
} || {
    echo "❌ 还有语法错误！"
    python3 -m py_compile "$APP_PY" 2>&1 | head -10
    echo ""
    echo "🔙 恢复备份..."
    cp "$BACKUP" "$APP_PY"
    echo "✅ 已恢复备份"
}

echo ""
echo "============================================================"
echo "📋 审核完成"
echo "============================================================"
