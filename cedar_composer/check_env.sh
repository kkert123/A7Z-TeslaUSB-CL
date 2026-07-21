#!/bin/bash
# check_env.sh — A7Z 编译环境检查
# 在 A7Z 板端运行此脚本, 确认编译依赖是否就绪

echo "════════════════════════════════════════════"
echo "  A7Z Cedar Composer — 环境检查"
echo "════════════════════════════════════════════"
echo ""

errors=0

check() {
    local what="$1"
    local cmd="$2"
    printf "  %-30s " "$what"
    if eval "$cmd" >/dev/null 2>&1; then
        echo "✅"
    else
        echo "❌ 缺少"
        errors=$((errors + 1))
    fi
}

check_file() {
    local desc="$1"
    local path="$2"
    printf "  %-30s " "$desc"
    if [ -e "$path" ]; then
        echo "✅ $path"
    else
        echo "❌ 不存在"
        errors=$((errors + 1))
    fi
}

echo "── 内核模块 ──"
check "g2d_sunxi" "lsmod | grep -q g2d_sunxi"
echo ""

echo "── 设备节点 ──"
check_file "/dev/g2d"           /dev/g2d
check_file "/dev/cedar_dev"     /dev/cedar_dev
check_file "/dev/dma_heap/system" /dev/dma_heap/system
echo ""

echo "── 头文件 ──"
check_file "sunxi-g2d.h"        /usr/include/bsp/linux/sunxi-g2d.h
check_file "dma-heap.h"         /usr/include/linux/dma-heap.h
echo ""

echo "── OMX IL 头文件 (可能位置) ──"
found_omx=0
for d in /usr/include/OMX /usr/include/omxil /usr/include/bellagio; do
    if [ -d "$d" ]; then
        echo "  ✅ $d"
        found_omx=1
    fi
done
if [ $found_omx -eq 0 ]; then
    echo "  ❌ 未找到 OMX IL 头文件"
    echo "    安装: sudo apt install libomxil-bellagio-dev"
    errors=$((errors + 1))
fi
echo ""

echo "── Cedar 库 ──"
for lib in libawh264.so libawh265.so libawmjpeg.so; do
    found=$(find /usr/lib -name "$lib" 2>/dev/null | head -1)
    if [ -n "$found" ]; then
        echo "  ✅ $found"
    else
        echo "  ⚠ $lib 未找到 (非致命)"
    fi
done
echo ""

echo "── CMA 内存池 ──"
cma=$(grep CmaTotal /proc/meminfo 2>/dev/null | awk '{print $2}')
if [ -n "$cma" ]; then
    cma_mb=$((cma / 1024))
    if [ $cma_mb -ge 64 ]; then
        echo "  ✅ CMA: ${cma_mb} MB (足够)"
    else
        echo "  ⚠ CMA: ${cma_mb} MB (偏小, 建议 >= 64MB)"
    fi
else
    echo "  ⚠ 无法读取 CMA"
fi
echo ""

echo "── GCC ──"
check "gcc" "gcc --version"
echo ""

echo "════════════════════════════════════════════"
if [ $errors -eq 0 ]; then
    echo "  ✅ 所有检查通过! 可以编译."
else
    echo "  ⚠ 发现 $errors 个问题, 请按提示修复."
fi
echo "════════════════════════════════════════════"
