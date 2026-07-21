/*
 * g2d_composer.c — A7Z G2D 硬件加速 2×2 视频合成器
 * ==================================================
 * 纯 G2D 实现，不依赖 OMX IL。
 * 在 Radxa Cubie A7Z (Allwinner A733) 上编译运行。
 *
 * 编译: gcc -O2 -Wall -o g2d_composer g2d_composer.c \
 *           -I/usr/include/bsp -lpthread -lm
 *
 * 用法: ./g2d_composer
 *       (先用测试图案验证 G2D → 再对接真实视频帧)
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/time.h>
#include <linux/dma-heap.h>
#include <bsp/linux/sunxi-g2d.h>

/* ═══════════════════════════════════════════════
 *  配置
 * ═══════════════════════════════════════════════ */

#define SRC_W   1280
#define SRC_H   960
#define CELL_W  640
#define CELL_H  480
#define OUT_W   1280
#define OUT_H   960

/*
 * G2D 1.0 接口: g2d_blt + g2d_image (用物理地址)
 * G2D 2.0 接口: g2d_blt_h + g2d_image_enh (用 dma-buf fd)
 *
 * 这里用 2.0 接口 (dma-buf zero-copy)。
 * NV12 格式: G2D_FORMAT_YUV420UVC_V1U1V0U0 (0x28)
 */

/* ═══════════════════════════════════════════════
 *  DMA Buffer
 * ═══════════════════════════════════════════════ */

typedef struct {
    int   fd;
    void *vaddr;
    int   size;
    int   heap_fd;
} dmabuf_t;

static dmabuf_t *dmabuf_alloc(int size)
{
    dmabuf_t *b = calloc(1, sizeof(*b));
    if (!b) return NULL;
    
    size = (size + 4095) & ~4095;
    b->size = size;
    
    b->heap_fd = open("/dev/dma_heap/system", O_RDONLY);
    if (b->heap_fd < 0) { perror("heap open"); free(b); return NULL; }
    
    struct dma_heap_allocation_data alloc = {
        .len = size, .fd_flags = O_RDWR | O_CLOEXEC, .heap_flags = 0
    };
    if (ioctl(b->heap_fd, DMA_HEAP_IOCTL_ALLOC, &alloc) < 0) {
        perror("heap alloc"); close(b->heap_fd); free(b); return NULL;
    }
    b->fd = alloc.fd;
    
    b->vaddr = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, b->fd, 0);
    if (b->vaddr == MAP_FAILED) { perror("mmap"); close(b->fd); close(b->heap_fd); free(b); return NULL; }
    
    return b;
}

static void dmabuf_free(dmabuf_t *b)
{
    if (!b) return;
    if (b->vaddr != MAP_FAILED) munmap(b->vaddr, b->size);
    if (b->fd >= 0) close(b->fd);
    if (b->heap_fd >= 0) close(b->heap_fd);
    free(b);
}

static const char *fmt_bytes(int n)
{
    static char s[32];
    if (n >= 1024*1024) snprintf(s,32,"%.1f MB", n/(1024.0*1024.0));
    else if (n >= 1024) snprintf(s,32,"%.1f KB", n/1024.0);
    else snprintf(s,32,"%d B", n);
    return s;
}

/* ═══════════════════════════════════════════════
 *  G2D 操作
 * ═══════════════════════════════════════════════ */

/*
 * G2D BITBLT_H (2.0 接口): 缩放 + 定位到目标区域
 *
 * 输入:
 *   g2d_fd   - /dev/g2d 文件描述符
 *   src      - 源 DMA buffer (NV12, SRC_W×SRC_H)
 *   dst      - 目标 DMA buffer (NV12, OUT_W×OUT_H)
 *   dst_x, dst_y - 在目标中的左上角坐标
 *   dst_w, dst_h - 在目标中的尺寸 (CELL_W×CELL_H)
 *   flag     - G2D_BLT_NONE_H 无旋转, 或 G2D_ROT_90 等
 */
static int g2d_blit_frame(int g2d_fd, dmabuf_t *src, dmabuf_t *dst,
                          int dst_x, int dst_y, int dst_w, int dst_h, int flag)
{
    struct g2d_blt_h blit;
    memset(&blit, 0, sizeof(blit));
    
    /* ── 源图像 ── */
    blit.src_image_h.fd     = src->fd;
    blit.src_image_h.format = G2D_FORMAT_YUV420UVC_V1U1V0U0; /* NV12 */
    blit.src_image_h.width  = SRC_W;
    blit.src_image_h.height = SRC_H;
    /* clip_rect: 全图 */
    blit.src_image_h.clip_rect.x = 0;
    blit.src_image_h.clip_rect.y = 0;
    blit.src_image_h.clip_rect.w = SRC_W;
    blit.src_image_h.clip_rect.h = SRC_H;
    /* resize: 缩放到目标尺寸 */
    blit.src_image_h.resize.w = dst_w;
    blit.src_image_h.resize.h = dst_h;
    /* coor: 放置在目标中的位置 */
    blit.src_image_h.coor.x = dst_x;
    blit.src_image_h.coor.y = dst_y;
    
    /* ── 目标图像 ── */
    blit.dst_image_h.fd     = dst->fd;
    blit.dst_image_h.format = G2D_FORMAT_YUV420UVC_V1U1V0U0;
    blit.dst_image_h.width  = OUT_W;
    blit.dst_image_h.height = OUT_H;
    
    /* ── 操作标志 ── */
    blit.flag_h = flag;
    
    if (ioctl(g2d_fd, G2D_CMD_BITBLT_H, (unsigned long)&blit) < 0) {
        fprintf(stderr, "[G2D] blit @(%d,%d) %dx%d 失败: %s\n",
                dst_x, dst_y, dst_w, dst_h, strerror(errno));
        return -1;
    }
    return 0;
}

/*
 * 4 路合成: 4× SRC_W×SRC_H → 2×2 网格 OUT_W×OUT_H
 */
static int g2d_composite(int g2d_fd, dmabuf_t *src[4], dmabuf_t *dst)
{
    struct { int x, y, w, h; } pos[4] = {
        {0,       0,   CELL_W, CELL_H},   /* front 左上 */
        {CELL_W,  0,   CELL_W, CELL_H},   /* back  右上 */
        {0,       CELL_H, CELL_W, CELL_H},/* left  左下 */
        {CELL_W,  CELL_H, CELL_W, CELL_H},/* right 右下 */
    };
    
    for (int i = 0; i < 4; i++) {
        if (g2d_blit_frame(g2d_fd, src[i], dst,
                           pos[i].x, pos[i].y, pos[i].w, pos[i].h,
                           G2D_BLT_NONE_H) < 0)
            return -1;
    }
    return 0;
}

/* ═══════════════════════════════════════════════
 *  main
 * ═══════════════════════════════════════════════ */

static double now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

int main(int argc, char *argv[])
{
    (void)argc; (void)argv;
    
    printf("\n╔════════════════════════════════════════╗\n");
    printf(  "║  A7Z G2D 2×2 硬件合成测试             ║\n");
    printf(  "║  Allwinner A733 / Radxa Cubie A7Z      ║\n");
    printf(  "╚════════════════════════════════════════╝\n\n");
    
    /* ── 打开 G2D ── */
    int g2d_fd = open("/dev/g2d", O_RDWR);
    if (g2d_fd < 0) {
        perror("/dev/g2d");
        return 1;
    }
    printf("[G2D] 设备已打开 fd=%d\n", g2d_fd);
    
    /* ── 分配 DMA buffer (NV12 = W*H*1.5) ── */
    int src_size = SRC_W * SRC_H * 3 / 2;
    int dst_size = OUT_W * OUT_H * 3 / 2;
    
    dmabuf_t *src[4] = {0};
    for (int i = 0; i < 4; i++) {
        src[i] = dmabuf_alloc(src_size);
        if (!src[i]) { fprintf(stderr, "DMA alloc failed src[%d]\n", i); return 1; }
    }
    dmabuf_t *dst = dmabuf_alloc(dst_size);
    if (!dst) { fprintf(stderr, "DMA alloc failed dst\n"); return 1; }
    
    printf("[DMA] 5 buffers: 4×%s (src) + 1×%s (dst)\n",
           fmt_bytes(src_size), fmt_bytes(dst_size));
    
    /* ── 填充测试图案 (4 种不同灰度 + 彩条标线) ── */
    uint8_t gray[4] = {64, 128, 192, 255};
    const char *label[4] = {"FRONT","BACK","LEFT","RIGHT"};
    
    for (int ch = 0; ch < 4; ch++) {
        uint8_t *Y  = (uint8_t *)src[ch]->vaddr;
        uint8_t *UV = Y + SRC_W * SRC_H;
        memset(Y,  gray[ch], SRC_W * SRC_H);
        memset(UV, 128,        SRC_W * SRC_H / 2);
        
        /* 画对角线标记 */;
        for (int y = 0; y < SRC_H && y < SRC_W; y++)
            Y[y * SRC_W + y] = (gray[ch] > 128) ? 0 : 255;
        
        printf("[SRC:%s] Y=%d 对角线已标记\n", label[ch], gray[ch]);
    }
    
    /* ── G2D 合成 ── */
    printf("\n[G2D] 开始合成...\n");
    double t0 = now_ms();
    int ret = g2d_composite(g2d_fd, src, dst);
    double t1 = now_ms();
    
    if (ret != 0) {
        fprintf(stderr, "[G2D] 合成失败\n");
        return 1;
    }
    
    printf("[G2D] ✅ 合成完成! 耗时 %.1f ms\n", t1 - t0);
    
    /* ── 验证: 检查 4 个角的像素值 ── */
    int errors = 0;
    uint8_t *Y = (uint8_t *)dst->vaddr;
    int stride = OUT_W;
    
    int check[4][2] = {
        {10,           10},           /* 左上 → FRONT gray=64 */
        {OUT_W-10,     10},           /* 右上 → BACK  gray=128 */
        {10,           OUT_H-10},     /* 左下 → LEFT  gray=192 */
        {OUT_W-10,     OUT_H-10},     /* 右下 → RIGHT gray=255 */
    };
    
    for (int i = 0; i < 4; i++) {
        int px = check[i][0], py = check[i][1];
        uint8_t val = Y[py * stride + px];
        if (val != gray[i]) {
            printf("  ❌ %s@(%d,%d): 期望 %d, 实际 %d\n",
                   label[i], px, py, gray[i], val);
            errors++;
        } else {
            printf("  ✅ %s@(%d,%d): %d OK\n", label[i], px, py, val);
        }
    }
    
    /* ── 清理 ── */
    for (int i = 0; i < 4; i++) dmabuf_free(src[i]);
    dmabuf_free(dst);
    close(g2d_fd);
    
    printf("\n════════════════════════════════════════\n");
    if (errors == 0) {
        printf("  ✅ G2D 2×2 合成验证通过!\n");
    } else {
        printf("  ❌ %d 个像素验证失败\n", errors);
    }
    printf("════════════════════════════════════════\n");
    
    return errors ? 1 : 0;
}
