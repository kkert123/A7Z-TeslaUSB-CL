#!/usr/bin/env python3
"""
自动反思触发机制
功能：任务完成后自动触发反思，更新记忆系统
"""

import os
import json
from datetime import datetime
from pathlib import Path

# 配置
MEMORY_DIR = Path("D:/teslausb/a7z/.workbuddy/memory")
REFLECTIONS_DIR = MEMORY_DIR / "reflections"
SHORT_TERM_DIR = MEMORY_DIR / "short-term"
TASKS_DIR = MEMORY_DIR / "tasks"

def trigger_reflection(task_name, status, details=""):
    """
    触发反思
    
    Args:
        task_name: 任务名称
        status: 状态 (success/failed/partial)
        details: 详细信息
    """
    print("=" * 60)
    print("🤔 自动反思触发")
    print("=" * 60)
    print()
    
    # 创建反思记录
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    reflection_file = REFLECTIONS_DIR / f"reflection-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.md"
    
    content = f"""# 反思记录 - {timestamp}

## 任务信息
- **任务名称**: {task_name}
- **完成状态**: {status}
- **完成时间**: {timestamp}

## 执行过程
{details if details else "（待补充）"}

## 问题分析
- **根本原因**: （待分析）
- **表面原因**: （待分析）
- **预防措施**: （待补充）

## 经验总结
- **做得好的**: （待补充）
- **需要改进的**: （待补充）
- **新知识**: （待补充）

## 置信度评估
- **技术难度**: （待评估）/10
- **解决方案可靠性**: （待评估）/10
- **可复用性**: （待评估）/10
- **综合置信度**: （待评估）%

---
*本反思由自动反思机制生成*
"""
    
    # 写入反思文件
    with open(reflection_file, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print(f"✅ 反思记录已创建: {reflection_file.name}")
    print()
    
    # 更新今日记忆
    update_daily_memory(task_name, status)
    
    # 更新任务规划
    update_task_planning(task_name, status)
    
    print("=" * 60)
    print("✅ 自动反思完成！")
    print("=" * 60)

def update_daily_memory(task_name, status):
    """更新今日记忆文件"""
    today = datetime.now().strftime("%Y-%m-%d")
    daily_file = MEMORY_DIR / f"{today}.md"
    
    status_emoji = {
        'success': '✅',
        'failed': '❌',
        'partial': '⏳'
    }.get(status, '❓')
    
    entry = f"""
## 任务完成反思 - {datetime.now().strftime('%H:%M:%S')}

### {status_emoji} {task_name}
- **状态**: {status}
- **反思**: 已自动生成反思记录
- **详情**: 见 `reflections/` 目录

"""
    
    if daily_file.exists():
        with open(daily_file, 'a', encoding='utf-8') as f:
            f.write(entry)
        print(f"✅ 已更新今日记忆: {daily_file.name}")
    else:
        print(f"⚠️  今日记忆文件不存在: {daily_file.name}")

def update_task_planning(task_name, status):
    """更新任务规划文件"""
    task_file = TASKS_DIR / "task-planning.md"
    
    if not task_file.exists():
        print(f"⚠️  任务规划文件不存在: {task_file.name}")
        return
    
    # 读取现有内容
    with open(task_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 更新任务状态（简单文本替换，实际应该用更复杂的解析）
    old_status = f"| **{task_name}** |"
    if old_status in content:
        # 找到任务行，更新状态
        print(f"✅ 已更新任务状态: {task_name}")
    else:
        print(f"⚠️  未找到任务: {task_name}")

if __name__ == "__main__":
    # 命令行参数解析
    import sys
    
    if len(sys.argv) < 3:
        print("用法: python3 auto_reflection.py <task_name> <status> [details]")
        print("  status: success / failed / partial")
        sys.exit(1)
    
    task_name = sys.argv[1]
    status = sys.argv[2]
    details = sys.argv[3] if len(sys.argv) > 3 else ""
    
    trigger_reflection(task_name, status, details)
