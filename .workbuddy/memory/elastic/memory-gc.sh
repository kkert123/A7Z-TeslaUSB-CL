#!/bin/bash
# Memory Garbage Collection Script - 记忆垃圾回收脚本
# 功能：自动归档30天前的日记，清理临时记忆

MEMORY_DIR="D:/teslausb/a7z/.workbuddy/memory"
ARCHIVE_DIR="$MEMORY_DIR/archive"
TODAY=$(date +%Y-%m-%d)
THIRTY_DAYS_AGO=$(date -d "30 days ago" +%Y-%m-%d 2>/dev/null || date -v-30d +%Y-%m-%d 2>/dev/null || echo "2026-04-12")

echo "=========================================="
echo "🗑️  Memory GC - 记忆垃圾回收"
echo "=========================================="
echo ""
echo "📅 今天: $TODAY"
echo "📅 30天前: $THIRTY_DAYS_AGO"
echo ""

# 创建归档目录
if [ ! -d "$ARCHIVE_DIR" ]; then
    mkdir -p "$ARCHIVE_DIR"
    echo "✅ 创建归档目录: $ARCHIVE_DIR"
fi

# 1. 归档30天前的日记
echo "📦 开始归档30天前的日记..."
count=0
for file in "$MEMORY_DIR"/2026-*.md; do
    if [ -f "$file" ]; then
        filename=$(basename "$file")
        date_str=$(echo "$filename" | sed 's/\(2026-[0-9]*\)\.md/\1/')
        
        if [[ "$date_str" < "$THIRTY_DAYS_AGO" ]]; then
            echo "  📦 归档: $filename"
            mv "$file" "$ARCHIVE_DIR/"
            count=$((count + 1))
        fi
    fi
done

if [ $count -eq 0 ]; then
    echo "  ℹ️  没有需要归档的日记"
else
    echo "  ✅ 已归档 $count 个文件到 $ARCHIVE_DIR"
fi

echo ""

# 2. 清理 short-term/ 中的临时文件（7天前）
echo "🧹 清理短期记忆（7天前）..."
if [ -d "$MEMORY_DIR/short-term" ]; then
    find "$MEMORY_DIR/short-term" -type f -name "*.md" -mtime +7 -delete 2>/dev/null
    echo "  ✅ 已清理7天前的短期记忆"
else
    echo "  ⚠️  short-term/ 目录不存在"
fi

echo ""

# 3. 压缩归档文件
echo "🗜️  压缩归档文件..."
if [ -d "$ARCHIVE_DIR" ] && [ "$(ls -A $ARCHIVE_DIR 2>/dev/null)" ]; then
    cd "$ARCHIVE_DIR"
    tar -czf "archive-$THIRTY_DAYS_AGO.tar.gz" *.md 2>/dev/null
    if [ $? -eq 0 ]; then
        echo "  ✅ 已压缩归档文件: archive-$THIRTY_DAYS_AGO.tar.gz"
        # 删除已压缩的原始文件
        rm -f "$ARCHIVE_DIR"/*.md
    else
        echo "  ⚠️  压缩失败或无需压缩"
    fi
else
    echo "  ℹ️  归档目录为空，无需压缩"
fi

echo ""
echo "=========================================="
echo "✅ Memory GC 完成！"
echo "=========================================="
