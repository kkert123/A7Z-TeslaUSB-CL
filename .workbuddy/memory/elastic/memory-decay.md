# 记忆衰减机制 - Memory Decay

## 温度模型

### Hot（热记忆）
- **定义**: 近7天内的记忆
- **访问频率**: 高
- **存储位置**: short-term/ + 今日 YYYY-MM-DD.md
- **保留策略**: 完全保留，优先展示

### Warm（温记忆）
- **定义**: 7-30天内的记忆
- **访问频率**: 中
- **存储位置**: YYYY-MM-DD.md
- **保留策略**: 保留关键信息，压缩细节

### Cold（冷记忆）
- **定义**: 30天以上的记忆
- **访问频率**: 低
- **存储位置**: 归档到 MEMORY.md 或删除
- **保留策略**: 只保留经验教训和重要决策

---

## 衰减规则

### 自动归档
```bash
# 将30天前的日记归档到 MEMORY.md
./memory-gc.sh
```

### 归档逻辑
1. **保留内容**:
   - 重要决策和原因
   - 故障排除经验
   - 用户偏好和习惯
   - 项目关键信息

2. **删除内容**:
   - 临时调试记录
   - 中间状态描述
   - 已修复的错误详情
   - 重复性操作记录

---

## 记忆优先级

### P0 - 核心记忆（永久保留）
- 项目架构和关键信息
- 用户偏好和沟通风格
- 重要故障和解决方案

### P1 - 重要记忆（保留6个月）
- 任务执行记录
- 技术方案选择
- 代码模式和实践

### P2 - 临时记忆（保留30天）
- 调试过程
- 临时配置
- 实验性方案

---

## 自动清理脚本

### memory-gc.sh
```bash
#!/bin/bash
# 记忆垃圾回收脚本

MEMORY_DIR="D:\teslausb\a7z\.workbuddy\memory"
THIRTY_DAYS_AGO=$(date -d "30 days ago" +%Y-%m-%d)

echo "开始记忆清理..."

# 1. 归档30天前的日记
for file in "$MEMORY_DIR"/2026-*.md; do
    filename=$(basename "$file")
    date_str=$(echo "$filename" | sed 's/\(2026-[0-9-]*\)\.md/\1/')
    
    if [[ "$date_str" < "$THIRTY_DAYS_AGO" ]]; then
        echo "归档: $filename"
        # 提取关键信息到 MEMORY.md
        # 然后删除或移动到 archive/
    fi
done

echo "清理完成！"
```

---

## 使用示例

### 手动触发衰减
```bash
cd D:\teslausb\a7z\.workbuddy\memory
node memory-decay.js
```

### 查看记忆统计
```bash
ls -lh *.md
find . -type f -name "*.md" -mtime +30
```

---

_本文件由 Memory System Optimizer 自动维护_
