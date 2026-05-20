#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
直接修复 app.py 中的 switch_mode() 函数
避免 bash 脚本的转义和权限问题
"""

import sys
import os
import re

APP_PY = "/opt/radxa_data/teslausb/app.py"
BACKUP = APP_PY + ".backup." + __import__('datetime').datetime.now().strftime("%Y%m%d_%H%M%S")

print("=" * 50)
print("直接修复 switch_mode() 函数")
print("=" * 50)
print()

# 1. 备份
print(f"📦 备份到: {BACKUP}")
with open(APP_PY, 'r', encoding='utf-8') as f:
    original_content = f.read()
with open(BACKUP, 'w', encoding='utf-8') as f:
    f.write(original_content)
print("✅ 备份成功!")
print()

# 2. 查找函数位置
start_marker = 'def switch_mode():'
start_idx = original_content.find(start_marker)
if start_idx == -1:
    print("❌ 错误: 找不到 switch_mode() 函数!")
    sys.exit(1)

print(f"✅ 找到函数定义 (位置: {start_idx})")

# 3. 查找函数结束位置
rest = original_content[start_idx + len(start_marker):]
# 查找下一个 @app.route 或 def 
end_patterns = ['\n@app.route', '\ndef ']
end_idx = len(original_content)
for pattern in end_patterns:
    pos = rest.find(pattern)
    if pos != -1:
        candidate = start_idx + len(start_marker) + pos
        if candidate < end_idx:
            end_idx = candidate

print(f"✅ 函数范围: {start_idx} - {end_idx}")
print()

# 4. 新函数代码（使用 Python 字符串，避免转义问题）
new_func = '''def switch_mode():
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
        else:
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
        
        # 执行切换脚本
        result = subprocess.run(
            ['bash', script_path],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode == 0:
            app.logger.info(f"✅ 成功切换到 {mode_name}")
            return jsonify({
                'success': True,
                'mode': mode,
                'message': f'已切换到 {mode_name}'
            })
        else:
            error_msg = result.stderr or result.stdout or '未知错误'
            app.logger.error(f"❌ 切换失败: {error_msg}")
            return jsonify({
                'success': False,
                'error': error_msg[-500:]
            }), 500
            
    except Exception as e:
        app.logger.error(f"❌ 切换异常: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

'''

# 5. 替换函数
new_content = original_content[:start_idx] + new_func + original_content[end_idx:]

# 6. 写入文件
print("🔧 写入修复后的代码...")
with open(APP_PY, 'w', encoding='utf-8') as f:
    f.write(new_content)
print("✅ 写入成功!")
print()

# 7. 验证语法
print("🧪 验证 Python 语法...")
try:
    compile(new_content, APP_PY, 'exec')
    print("✅ 语法验证通过!")
except SyntaxError as e:
    print(f"❌ 语法错误: {e}")
    print("📦 恢复备份...")
    with open(BACKUP, 'r', encoding='utf-8') as f:
        original = f.read()
    with open(APP_PY, 'w', encoding='utf-8') as f:
        f.write(original)
    print("❌ 已恢复备份，退出")
    sys.exit(1)

print()
print("=" * 50)
print("✅ 修复完成!")
print("=" * 50)
print()
print("📊 下一步:")
print("  1. 重启 Web 服务: sudo systemctl restart teslausb-web")
print("  2. 清除浏览器缓存 (Ctrl + F5)")
print("  3. 测试模式切换")
print()
