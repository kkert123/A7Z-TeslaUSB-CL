#!/usr/bin/env python3
"""
知识库自动更新机制
功能：从任务完成记录中自动提取知识，更新到知识库
"""

import os
import re
from datetime import datetime
from pathlib import Path

# 配置
MEMORY_DIR = Path("D:/teslausb/a7z/.workbuddy/memory")
KNOWLEDGE_FILE = MEMORY_DIR / "semantic/knowledge-base.md"
REFLECTIONS_DIR = MEMORY_DIR / "reflections"
TASKS_DIR = MEMORY_DIR / "tasks"

def extract_knowledge_from_reflection(reflection_file):
    """
    从反思文件中提取知识
    
    Args:
        reflection_file: 反思文件路径
        
    Returns:
        dict: 提取的知识
    """
    if not reflection_file.exists():
        return None
    
    with open(reflection_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    knowledge = {
        'task': '',
        'problem': '',
        'root_cause': '',
        'solution': '',
        'prevention': '',
        'new_knowledge': ''
    }
    
    # 简单解析（实际应该用更复杂的方法）
    patterns = {
        'task': r'\*\*任务名称\*\*:?\s*(.+?)[\n\r]',
        'problem': r'## 问题分析.*?\*\*根本原因\*\*:?\s*(.+?)[\n\r]',
        'root_cause': r'\*\*根本原因\*\*:?\s*(.+?)[\n\r]',
        'solution': r'## 经验总结.*?\*\*做得好的\*\*:?\s*(.+?)[\n\r]',
        'prevention': r'\*\*预防措施\*\*:?\s*(.+?)[\n\r]',
        'new_knowledge': r'\*\*新知识\*\*:?\s*(.+?)[\n\r]'
    }
    
    for key, pattern in patterns.items():
        match = re.search(pattern, content, re.DOTALL)
        if match:
            knowledge[key] = match.group(1).strip()
    
    return knowledge

def update_knowledge_base(new_knowledge):
    """
    更新知识库
    
    Args:
        new_knowledge: 新知识字典
    """
    if not KNOWLEDGE_FILE.exists():
        print(f"⚠️  知识库文件不存在: {KNOWLEDGE_FILE}")
        return False
    
    # 读取现有知识库
    with open(KNOWLEDGE_FILE, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 检查是否已存在相似知识
    # （简化版：只检查任务名称）
    if new_knowledge.get('task') and new_knowledge['task'] in content:
        print(f"⚠️  知识已存在: {new_knowledge['task']}")
        return False
    
    # 添加新知识到"经验教训"部分
    new_entry = f"""
### {new_knowledge.get('task', '未知任务')}

**问题**: {new_knowledge.get('problem', '（待补充）')}
**根本原因**: {new_knowledge.get('root_cause', '（待分析）')}
**解决方案**: {new_knowledge.get('solution', '（待补充）')}
**预防措施**: {new_knowledge.get('prevention', '（待补充）')}
**新知识**: {new_knowledge.get('new_knowledge', '（待补充）')}

---"""
    
    # 插入到"经验教训"部分之前
    if '## 经验教训' in content:
        content = content.replace('## 经验教训', new_entry + '\n## 经验教训')
    else:
        content += '\n' + new_entry
    
    # 写回文件
    with open(KNOWLEDGE_FILE, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print(f"✅ 已更新知识库: {KNOWLEDGE_FILE.name}")
    return True

def auto_update_from_new_reflections():
    """
    自动从新的反思文件更新知识库
    """
    print("=" * 60)
    print("📗 知识库自动更新")
    print("=" * 60)
    print()
    
    if not REFLECTIONS_DIR.exists():
        print("⚠️  反思目录不存在")
        return
    
    # 获取所有反思文件
    reflection_files = list(REFLECTIONS_DIR.glob("reflection-*.md"))
    
    if not reflection_files:
        print("ℹ️  没有找到反思文件")
        return
    
    print(f"📗 找到 {len(reflection_files)} 个反思文件")
    print()
    
    updated_count = 0
    
    for rf in reflection_files:
        print(f"📖 处理: {rf.name}")
        
        # 提取知识
        knowledge = extract_knowledge_from_reflection(rf)
        
        if knowledge and knowledge.get('task'):
            # 更新知识库
            if update_knowledge_base(knowledge):
                updated_count += 1
                # 移动已处理的反思文件到 archive/
                archive_dir = REFLECTIONS_DIR / "archive"
                if not archive_dir.exists():
                    archive_dir.mkdir(parents=True)
                rf.rename(archive_dir / rf.name)
                print(f"  ✅ 已处理并归档: {rf.name}")
            else:
                print(f"  ⚠️  跳过（已存在或失败）")
        else:
            print(f"  ⚠️  无法提取知识")
        
        print()
    
    print("=" * 60)
    print(f"✅ 知识库自动更新完成！共处理 {updated_count} 个反思文件")
    print("=" * 60)

if __name__ == "__main__":
    auto_update_from_new_reflections()
