#!/usr/bin/env python3
"""修复 app.py 中的语法错误"""
import re

APP = '/opt/radxa_data/teslausb/app.py'
BACKUP = APP + '.backup.' + __import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S')

# 读取
with open(APP, 'r', encoding='utf-8') as f:
    content = f.read()

# 备份
with open(BACKUP, 'w', encoding='utf-8') as f:
    f.write(content)

print(f"✓ 备份: {BACKUP}")

# 修复1: f-string 引号顺序错误  f"... {e}")"  ->  f"... {e}")"
# 匹配模式:  f".... {var}")"  或  f'.... {var}')"
old = content
content = re.sub(r'f(["\'])(.*?)\{(.*?)\}(\1\]?)("|\')(\))', r'f\1\2{\3}\5\6', content)

# 修复2: open() 函数调用中逗号被替换成空格
# with open(path  'r') as f:  ->  with open(path, 'r') as f:
content = re.sub(r'open$\s*(.*?)\s+(\'|")(r|w|a)(\'|")\s*$', r'open(\1, \2\3\4)', content)

# 修复3: 字典缺少逗号  False, 'ssid'  ->  False, 'ssid'  (这个需要看实际情况)
# 更安全的做法：逐行检查语法
print(f"  原始长度: {len(old)}")
print(f"  修复后长度: {len(content)}")

# 写回
with open(APP, 'w', encoding='utf-8') as f:
    f.write(content)

print(f"✓ 修复完成，正在验证语法...")
try:
    compile(content, APP, 'exec')
    print(f"✓ 语法正确！")
except SyntaxError as e:
    print(f"✗ 仍有语法错误: 行{e.lineno}: {e.msg}")
    print(f"  内容: {e.text}")
    # 回滚
    with open(APP, 'w', encoding='utf-8') as f:
        f.write(old)
    print(f"  已回滚到原始版本")
