#!/bin/bash
# 完整审核和修复 app.py (可靠版本)
# 用法：bash audit_app_py_v2.sh

set -e

APP_PY="/opt/radxa_data/teslausb/app.py"
BACKUP="${APP_PY}.backup.$(date +%Y%m%d_%H%M%S)"

echo "============================================================"
echo "🔍 完整审核 app.py (可靠版本)"
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
if python3 -m py_compile "$APP_PY" 2>/dev/null; then
    echo "✅ 语法检查通过"
else
    echo "❌ 语法错误！"
    python3 -m py_compile "$APP_PY" 2>&1 | head -20
    echo ""
    echo "🔧 开始自动修复..."
fi

# 2. 修复缺少逗号的 return 语句
echo ""
echo "============================================================"
echo "🔧 2. 修复 return 语句缺少逗号"
echo "============================================================"
# 查找：return jsonify({'success': ...}) 123)
# 修复为：return jsonify({'success': ...}), 123)
if grep -q "return jsonify.*)[[:space:]]*[0-9])" "$APP_PY"; then
    echo "  发现缺少逗号的 return 语句，正在修复..."
    # 使用 Python 脚本修复（比 sed 更可靠）
    python3 << 'PYTHON'
import re
with open("/opt/radxa_data/teslausb/app.py", 'r') as f:
    content = f.read()

# 修复：return jsonify(...) 200) -> return jsonify(...), 200)
pattern = r'(return jsonify\([^)]+\))\s+([0-9]+\))'
replacement = r'\1, \2'
new_content = re.sub(pattern, replacement, content)

with open("/opt/radxa_data/teslausb/app.py", 'w') as f:
    f.write(new_content)
print("  ✅ 已修复 return 语句")
PYTHON
else
    echo "  ℹ️  没有发现问题"
fi

# 3. 修复 with open 缺少逗号
echo ""
echo "============================================================"
echo "🔧 3. 修复 with open 缺少逗号"
echo "============================================================"
if grep -q "with open.*'r'\|'w'\) as" "$APP_PY"; then
    echo "  发现缺少逗号的 with open，正在修复..."
    # 使用 Python 脚本修复
    python3 << 'PYTHON'
import re
with open("/opt/radxa_data/teslausb/app.py", 'r') as f:
    content = f.read()

# 修复：with open(file, 'r') as -> with open(file, 'r'), as
pattern = r"with open\(([^)]+)\) as"
replacement = r'with open(\1), as'
new_content = re.sub(pattern, replacement, content)

with open("/opt/radxa_data/teslausb/app.py", 'w') as f:
    f.write(new_content)
print("  ✅ 已修复 with open 语句")
PYTHON
else
    echo "  ℹ️  没有发现问题"
fi

# 4. 删除重复的函数定义 (api_mode_status)
echo ""
echo "============================================================"
echo "🔧 4. 检查重复的函数定义"
echo "============================================================"
mode_status_count=$(grep -c "@app.route.*mode/status" "$APP_PY" || true)
if [ "$mode_status_count" -gt 1 ]; then
    echo "  ⚠️  发现 $mode_status_count 个 /api/mode/status 路由定义"
    echo "  删除第 479-485 行的旧函数..."
    sed -i '479,485d' "$APP_PY" 2>/dev/null || true
    echo "  ✅ 已删除旧函数"
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
    
    # 提取函数内容并检查
    start_line=$(grep -n "def get_mode_status" "$APP_PY" | head -1 | cut -d: -f1)
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
if python3 -m py_compile "$APP_PY" 2>/dev/null; then
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
else
    echo "❌ 还有语法错误！"
    python3 -m py_compile "$APP_PY" 2>&1 | head -10
    echo ""
    echo "🔙 恢复备份..."
    cp "$BACKUP" "$APP_PY"
    echo "✅ 已恢复备份"
    exit 1
fi

echo ""
echo "============================================================"
echo "📋 审核完成"
echo "============================================================"
