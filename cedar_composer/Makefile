# A7Z Cedar Composer Makefile
# ==============================
# 在 Radxa Cubie A7Z (Allwinner A733) 上编译

CC       = gcc
CFLAGS   = -O2 -Wall -Wextra -D_GNU_SOURCE
INCLUDES = -I/usr/include/bsp
LDFLAGS  = -lOMX_Core -lOMX_Component -lpthread -lm

# OMX IL 头文件路径 (Allwinner BSP 可能不同位置)
# 如果找不到, 检查:
#   /usr/include/OMX/
#   /usr/include/omxil/
#   或从 gst-omx 包安装

TARGET   = cedar_composer
SRCS     = cedar_composer.c

.PHONY: all clean deploy test

all: $(TARGET)

$(TARGET): $(SRCS)
	$(CC) $(CFLAGS) $(INCLUDES) -o $@ $^ $(LDFLAGS)
	@echo "✅ 编译完成: ./$(TARGET)"
	@file $(TARGET)

# 免 OMX 版本 — 仅 G2D 合成测试 (不需要 OMX IL 库)
g2d_test: cedar_composer.c
	$(CC) $(CFLAGS) -DG2D_ONLY $(INCLUDES) -o $@ $^ -lpthread -lm
	@echo "✅ G2D 单测编译完成: ./g2d_test"

clean:
	rm -f $(TARGET) g2d_test

# 部署到 A7Z
deploy:
	@echo "部署到 A7Z..."
	scp $(TARGET) radxa@100.116.18.42:/tmp/
	ssh radxa@100.116.18.42 "chmod +x /tmp/$(TARGET)"

# 在 A7Z 上运行
test:
	ssh radxa@100.116.18.42 "/tmp/$(TARGET)"
