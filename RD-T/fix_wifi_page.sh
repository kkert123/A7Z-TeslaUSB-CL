#!/bin/bash
# 修复 wifi.html 中的类型错误
# 问题: current.signal 是字符串，不能用 >= 比较

WIFI_HTML="/opt/radxa_data/teslausb/templates/wifi.html"
BACKUP="${WIFI_HTML}.backup.$(date +%Y%m%d_%H%M%S)"

echo "=========================================="
echo "修复 wifi.html 类型错误"
echo "=========================================="
echo ""

# 检查文件是否存在
if [ ! -f "$WIFI_HTML" ]; then
    echo "❌ 错误: $WIFI_HTML 不存在!"
    exit 1
fi

# 备份
echo "📦 备份原文件..."
cp "$WIFI_HTML" "$BACKUP"
if [ $? -eq 0 ]; then
    echo "✅ 备份成功: $BACKUP"
else
    echo "❌ 备份失败!"
    exit 1
fi
echo ""

# 修复第 52 行：将 current.signal 转换为整数
echo "🔧 修复类型错误..."
sed -i 's/{% if current\.signal >= 60 %}/{% if current.signal|int >= 60 %}/g' "$WIFI_HTML"
sed -i 's/{% elif current\.signal >= 30 %}/{% elif current.signal|int >= 30 %}/g' "$WIFI_HTML"

if [ $? -eq 0 ]; then
    echo "✅ 修复成功!"
    echo ""
    echo "📊 验证修复:"
    grep -A 2 -B 2 "current.signal|int" "$WIFI_HTML" | head -10
else
    echo "❌ 修复失败!"
    exit 1
fi
echo ""

# 重启服务
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
    echo "  2. 访问 http://100.116.18.42/wifi"
    echo "  3. WiFi 页面应该能正常显示了"
    echo ""
else
    echo "⚠️  服务重启失败，请手动重启"
fi
