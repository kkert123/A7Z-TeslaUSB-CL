#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
完整修复 app.py:
1. 添加缺失的 require_auth 装饰器
2. 确保 switch_mode() 函数正确
"""

import sys
import os

APP_PY = "/opt/radxa_data/teslausb/app.py"
BACKUP = APP_PY + ".backup." + __import__('datetime').datetime.now().strftime("%Y%m%d_%H%M%S")

print("=" * 60)
print("完整修复 app.py")
print("=" * 60)
print()

# 1. 读取原文件
print("📖 读取 app.py...")
with open(APP_PY, 'r', encoding='utf-8') as f:
    content = f.read()
print(f"✅ 文件大小: {len(content)} 字节, {content.count(chr(10))} 行")
print()

# 2. 备份
print(f"📦 备份到: {BACKUP}")
with open(BACKUP, 'w', encoding='utf-8') as f:
    f.write(content)
print("✅ 备份成功!")
print()

# 3. 检查是否有 require_auth 定义
print("🔍 检查 require_auth 装饰器...")
if 'def require_auth(' in content or 'def require_auth (' in content:
    print("✅ require_auth 已定义")
else:
    print("❌ require_auth 未定义，正在添加...")
    
    # 在文件开头部分（import 之后，第一个路由之前）添加 require_auth 定义
    # 查找合适的位置（在第一个 @app.route 之前）
    
    require_auth_code = '''
def require_auth(f):
    """认证装饰器 - 如果启用了认证，则要求登录"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # 从配置文件读取是否需要认证
        config = load_config()
        if config.get('auth_enabled', False):
            # 检查 session
            if 'user' not in session:
                # API 请求返回 JSON 错误
                if request.path.startswith('/api/'):
                    return jsonify({'success': False, 'error': '需要登录'}), 401
                # 页面请求重定向到登录页
                return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

'''
    
    # 查找导入 wraps 的语句
    if 'from functools import wraps' not in content:
        # 在文件开头的导入部分添加
        import_section_end = content.find('\n\n', content.find('import'))
        if import_section_end == -1:
            import_section_end = content.find('\ndef ') + 1
        
        content = content[:import_section_end] + '\nfrom functools import wraps' + content[import_section_end:]
        print("  ✓ 添加了 wraps 导入")
    
    # 在第一个 @app.route 之前插入 require_auth 定义
    first_route = content.find('\n@app.route')
    if first_route == -1:
        print("  ❌ 找不到 @app.route，无法插入 require_auth")
    else:
        # 向前找到函数或类的结束位置
        insert_pos = first_route
        content = content[:insert_pos] + require_auth_code + '\n' + content[insert_pos:]
        print("  ✓ 已添加 require_auth 定义")
print()

# 4. 验证 switch_mode() 函数
print("🔍 验证 switch_mode() 函数...")
if 'subprocess.run' in content:
    print("✅ switch_mode() 包含 subprocess.run (新代码)")
else:
    print("❌ switch_mode() 不包含 subprocess.run (旧代码)")
    print("正在修复...")
    
    # 查找并替换 switch_mode 函数
    start_marker = 'def switch_mode():'
    start_idx = content.find(start_marker)
    
    if start_idx == -1:
        print("  ❌ 找不到 switch_mode() 函数!")
    else:
        # 查找函数结束位置
        rest = content[start_idx + len(start_marker):]
        end_patterns = ['\ndef ', '\n@app.']
        end_idx = len(content)
        for pattern in end_patterns:
            pos = rest.find(pattern)
            if pos != -1:
                candidate = start_idx + len(start_marker) + pos
                if candidate < end_idx:
                    end_idx = candidate
        
        # 新函数代码
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
        
        # 替换函数
        content = content[:start_idx] + new_func + content[end_idx:]
        print("  ✓ 已修复 switch_mode() 函数")
print()

# 5. 写入文件
print("💾 写入修复后的文件...")
with open(APP_PY, 'w', encoding='utf-8') as f:
    f.write(content)
print("✅ 写入成功!")
print()

# 6. 验证语法
print("🧪 验证 Python 语法...")
try:
    compile(content, APP_PY, 'exec')
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

print("=" * 60)
print("✅ 修复完成!")
print("=" * 60)
print()
print("📊 下一步:")
print("  1. 停止服务: sudo systemctl stop teslausb-web")
print("  2. 删除缓存: find /opt/radxa_data/ -name '*.pyc' -delete")
print("  3. 启动服务: sudo systemctl start teslausb-web")
print("  4. 测试 API: curl -X POST http://localhost:5000/api/mode/switch -H 'Content-Type: application/json' -d '{\"mode\":\"edit\"}'")
print()
