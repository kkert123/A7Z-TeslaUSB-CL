#!/bin/bash
# deploy_task20.sh - Task 2.0 分段上传部署脚本
# 运行方式: 在能 SSH 到 A7Z 的终端中执行
#   bash deploy_task20.sh

set -e
A7Z_HOST="100.116.18.42"
A7Z_USER="radxa"

echo "================================================"
echo "  Task 2.0: 分段上传（Staging Area）部署"
echo "================================================"

# Step 1: 创建 staging 目录
echo ""
echo "[Step 1/4] 创建 staging 目录..."
ssh ${A7Z_USER}@${A7Z_HOST} "mkdir -p /opt/radxa_data/staging/{music,lightshow,boombox} && echo '  目录已创建: /opt/radxa_data/staging/'"

# Step 2: 上传修改后的 app.py
echo ""
echo "[Step 2/4] 上传 app.py..."
scp teslausb/app.py ${A7Z_USER}@${A7Z_HOST}:/home/radxa/teslausb/app.py.new

# Step 3: 上传修改后的 edit_usb.sh
echo ""
echo "[Step 3/4] 上传 edit_usb.sh..."
scp edit_usb.sh ${A7Z_USER}@${A7Z_HOST}:/opt/radxa_data/edit_usb.sh.new

# Step 4: 在 A7Z 上替换文件并重启服务
echo ""
echo "[Step 4/4] 替换文件并重启服务..."
ssh ${A7Z_USER}@${A7Z_HOST} << 'DEPLOY_EOF'
set -e

# 备份原文件
TS=$(date +%Y%m%d_%H%M%S)
cp /home/radxa/teslausb/app.py /home/radxa/teslausb/app.py.bak.${TS}
cp /opt/radxa_data/edit_usb.sh /opt/radxa_data/edit_usb.sh.bak.${TS}

# 替换文件
mv /home/radxa/teslausb/app.py.new /home/radxa/teslausb/app.py
mv /opt/radxa_data/edit_usb.sh.new /opt/radxa_data/edit_usb.sh
chmod +x /opt/radxa_data/edit_usb.sh

# 重启 Web 服务
systemctl restart teslausb-web
sleep 2

# 验证
echo ""
echo "=== 部署验证 ==="
echo "Web 服务状态: $(systemctl is-active teslausb-web)"
echo "Staging 目录:"
ls -la /opt/radxa_data/staging/
echo ""
echo "模式: $(cat /tmp/teslausb_mode 2>/dev/null || echo 'unknown')"
echo ""
echo "✅ Task 2.0 部署完成！"
DEPLOY_EOF

echo ""
echo "================================================"
echo "  ✅ 部署完成！"
echo "  访问 http://${A7Z_HOST} 验证"
echo "================================================"
