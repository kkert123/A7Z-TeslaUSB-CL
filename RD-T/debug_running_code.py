#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
终极调试：直接在运行中加载 app 模块，查看 switch_mode 的真实源码
"""

import sys
import os

# 切换到 app.py 所在目录
os.chdir('/opt/radxa_data/teslausb')

# 添加到 sys.path
sys.path.insert(0, '/opt/radxa_data/teslausb')

print("=" * 60)
print("终极调试：查看运行中的 switch_mode() 源码")
print("=" * 60)
print()

try:
    # 导入 app 模块
    import app
    
    print("✅ app 模块加载成功!")
    print(f"📊 模块位置: {app.__file__}")
    print()
    
    # 获取 switch_mode 函数
    if hasattr(app, 'switch_mode'):
        func = app.switch_mode
        print(f"✅ 找到 switch_mode 函数!")
        print(f"  函数名称: {func.__name__}")
        print(f"  函数模块: {func.__module__}")
        print()
        
        # 获取源码
        import inspect
        try:
            source = inspect.getsource(func)
            print("📝 函数源码:")
            print("=" * 60)
            print(source)
            print("=" * 60)
            print()
            
            # 检查是否包含新代码的特征
            if 'subprocess.run' in source:
                print("✅ 新代码已加载! (包含 subprocess.run)")
            else:
                print("❌ 旧代码仍在运行! (不包含 subprocess.run)")
                
            if 'logger.warning' in source:
                print("✅ 调试日志代码已加载!")
                
        except Exception as e:
            print(f"❌ 无法获取源码: {e}")
            
    else:
        print("❌ app 模块中没有 switch_mode 函数!")
        
    print()
    
    # 检查 app.routes 中是否有 switch_mode
    print("📊 检查 Flask 路由:")
    for rule in app.app.url_map.iter_rules():
        if 'switch' in rule.rule:
            print(f"  路由: {rule.rule}")
            print(f"  端点: {rule.endpoint}")
            # 获取端点对应的函数
            view_func = app.app.view_functions.get(rule.endpoint)
            if view_func:
                print(f"  函数: {view_func.__name__}")
                try:
                    src = inspect.getsource(view_func)
                    if 'subprocess.run' in src:
                        print("  ✅ 新代码!")
                    else:
                        print("  ❌ 旧代码!")
                except:
                    print("  无法获取源码")
            print()
            
except Exception as e:
    print(f"❌ 加载 app 模块失败: {e}")
    import traceback
    traceback.print_exc()
    
print("=" * 60)
print("调试完成")
print("=" * 60)
