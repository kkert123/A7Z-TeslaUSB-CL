/*
 * cedar_composer.h — A7Z 硬件加速视频合成器
 * ==========================================
 * 使用 Allwinner Cedar VPU 解码/编码 + G2D 2D 引擎做 2×2 画面合成
 *
 * 硬件要求: Radxa Cubie A7Z (Allwinner A733)
 *   - /dev/cedar_dev    — Cedar VPU (OMX IL 封装)
 *   - /dev/g2d          — G2D 2D 加速器
 *   - /dev/dma_heap/system — ION DMA 缓冲区
 *
 * 管线:
 *   MP4 → demux(H.264) → OMX解码 → DMA buffer
 *   → G2D缩放+定位(4路→2×2) → DMA buffer
 *   → OMX编码 → mux → MP4
 */

#ifndef CEDAR_COMPOSER_H
#define CEDAR_COMPOSER_H

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <linux/dma-heap.h>
#include <pthread.h>

/* ── G2D 头文件 (来自 Allwinner BSP) ── */
#include <bsp/linux/sunxi-g2d.h>

/* ── OMX IL 头文件 (标准 Khronos OpenMAX IL 1.1.2) ── */
/* 如果系统没有，可从 gst-omx 或 Allwinner BSP 中获取 */
#include <OMX_Core.h>
#include <OMX_Component.h>
#include <OMX_Video.h>
#include <OMX_IVCommon.h>

/* ═══════════════════════════════════════════════════
 *  配置
 * ═══════════════════════════════════════════════════ */

/* 特斯拉哨兵视频参数 */
#define SRC_WIDTH   1280
#define SRC_HEIGHT  960
#define CELL_WIDTH  640     /* 2×2 网格每格宽度 */
#define CELL_HEIGHT 480     /* 2×2 网格每格高度 */
#define OUT_WIDTH   1280    /* 合成输出宽度 = 2*640 */
#define OUT_HEIGHT  960     /* 合成输出高度 = 2*480 */

/* 视频片段时长（秒） */
#define SEGMENT_DURATION 20

/* 目标帧率 */
#define TARGET_FPS  30

/* DMA buffer 个数 (双缓冲) */
#define DMA_BUF_COUNT 4

/* OMX buffer 个数 */
#define OMX_IN_BUF_COUNT  2
#define OMX_OUT_BUF_COUNT 4

/* Cedar VPU OMX 组件名 */
#define CEDAR_DECODER "OMX.allwinner.video.decoder.avc"
#define CEDAR_ENCODER "OMX.allwinner.video.encoder.avc"

/* ═══════════════════════════════════════════════════
 *  DMA Buffer 管理
 * ═══════════════════════════════════════════════════ */

typedef struct {
    int    fd;          /* dma-buf fd */
    void  *vaddr;       /* mmap 虚拟地址 */
    size_t size;        /* 缓冲区大小 */
    int    dma_fd;      /* heap fd 引用 */
} dma_buf_t;

/** 从 /dev/dma_heap/system 分配物理连续 DMA buffer */
dma_buf_t *dma_alloc(size_t size);
void       dma_free(dma_buf_t *buf);

/* ═══════════════════════════════════════════════════
 *  G2D 合成引擎
 * ═══════════════════════════════════════════════════ */

typedef struct {
    int           g2d_fd;       /* /dev/g2d 文件描述符 */
    dma_buf_t    *src_bufs[4];  /* 4 路源 DMA 缓冲区 (NV12) */
    dma_buf_t    *dst_buf;      /* 目标 DMA 缓冲区 (NV12, 1280×960) */
} g2d_compositor_t;

/** 初始化 G2D 合成器 */
g2d_compositor_t *g2d_compositor_create(void);
void              g2d_compositor_destroy(g2d_compositor_t *c);

/**
 * 将 4 路 NV12 帧合成 2×2 网格到目标 buffer
 *
 * 布局:
 *   ┌─────────┬─────────┐
 *   │  src[0] │  src[1] │   (front) (back)
 *   │ 640×480 │ 640×480 │
 *   ├─────────┼─────────┤
 *   │  src[2] │  src[3] │   (left)  (right)
 *   │ 640×480 │ 640×480 │
 *   └─────────┴─────────┘
 *         1280×960
 *
 * 每路先缩放 1280×960 → 640×480，再 G2D_BITBLT 定位拷贝。
 * 注意: G2D 的 blit 本身可以做缩放，一次 ioctl 搞定缩放+定位。
 *
 * @return 0 成功, -1 失败
 */
int g2d_composite_frame(g2d_compositor_t *c,
                        dma_buf_t *src[4],
                        dma_buf_t *dst);

/** 仅缩放+定位单路 (内部使用) */
int g2d_blit_scaled(int g2d_fd,
                    dma_buf_t *src, int src_w, int src_h,
                    dma_buf_t *dst, int dst_x, int dst_y,
                    int dst_w, int dst_h,
                    int dst_full_w, int dst_full_h);

/* ═══════════════════════════════════════════════════
 *  Cedar VPU OMX IL 封装
 * ═══════════════════════════════════════════════════ */

typedef struct {
    OMX_HANDLETYPE  handle;
    OMX_STRING      name;
    OMX_BOOL        is_encoder;
    
    /* 端口索引 */
    OMX_U32         in_port;
    OMX_U32         out_port;
    
    /* 端口参数 (运行时确定) */
    OMX_U32         in_width;
    OMX_U32         in_height;
    OMX_U32         out_width;
    OMX_U32         out_height;
    
    /* buffer 管理 */
    OMX_BUFFERHEADERTYPE **in_bufs;
    OMX_BUFFERHEADERTYPE **out_bufs;
    OMX_U32         in_buf_count;
    OMX_U32         out_buf_count;
    
    /* 同步 */
    pthread_mutex_t lock;
    pthread_cond_t  cond;
    OMX_BOOL        eos_reached;
    OMX_BOOL        error_occurred;
    
    /* 元数据 */
    OMX_U32         src_width;
    OMX_U32         src_height;
    OMX_FRAMERATETYPE framerate;
} cedar_omx_t;

/** 创建 Cedar OMX 组件 */
cedar_omx_t *cedar_omx_create(const char *component_name, OMX_BOOL is_encoder);
void         cedar_omx_destroy(cedar_omx_t *c);

/** 配置解码/编码端口参数 */
int cedar_omx_config_decoder(cedar_omx_t *c, OMX_U32 width, OMX_U32 height);
int cedar_omx_config_encoder(cedar_omx_t *c, OMX_U32 width, OMX_U32 height,
                              OMX_U32 framerate, OMX_U32 bitrate);

/** 分配端口 buffer 并转换到 Idle 状态 */
int cedar_omx_allocate_buffers(cedar_omx_t *c);

/** 转换到 Executing 状态，开始处理 */
int cedar_omx_start(cedar_omx_t *c);

/** 喂入一个输入 buffer (解码器: H.264 NAL; 编码器: 原始帧) */
int cedar_omx_feed_input(cedar_omx_t *c, OMX_BUFFERHEADERTYPE *buf);

/** 获取一个输出 buffer (阻塞等待, timeout_ms 超时) */
OMX_BUFFERHEADERTYPE *cedar_omx_get_output(cedar_omx_t *c, int timeout_ms);

/** 归还输出 buffer 给组件 */
int cedar_omx_return_output(cedar_omx_t *c, OMX_BUFFERHEADERTYPE *buf);

/** 发送 EOS */
int cedar_omx_send_eos(cedar_omx_t *c);

/** 停止并清理 */
int cedar_omx_stop(cedar_omx_t *c);

/* OMX 回调 (内部) */
OMX_ERRORTYPE cedar_omx_event_handler(
    OMX_HANDLETYPE hComponent, OMX_PTR pAppData, OMX_EVENTTYPE eEvent,
    OMX_U32 nData1, OMX_U32 nData2, OMX_PTR pEventData);

OMX_ERRORTYPE cedar_omx_empty_buffer_done(
    OMX_HANDLETYPE hComponent, OMX_PTR pAppData, OMX_BUFFERHEADERTYPE *pBuffer);

OMX_ERRORTYPE cedar_omx_fill_buffer_done(
    OMX_HANDLETYPE hComponent, OMX_PTR pAppData, OMX_BUFFERHEADERTYPE *pBuffer);

/* ═══════════════════════════════════════════════════
 *  主合成管线
 * ═══════════════════════════════════════════════════ */

typedef struct {
    /* 输入文件 */
    const char *input_files[4];  /* front, back, left, right MP4 路径 */
    
    /* Cedar 组件 */
    cedar_omx_t *decoders[4];    /* 4 个并行解码器 */
    cedar_omx_t *encoder;        /* 1 个编码器 */
    
    /* G2D 合成器 */
    g2d_compositor_t *compositor;
    
    /* 中间缓冲区 */
    dma_buf_t    *decoded_frames[4];  /* 解码后的 NV12 帧 (1280×960) */
    dma_buf_t    *composite_frame;    /* 合成后的 NV12 帧 (1280×960) */
    
    /* 状态 */
    int           running;
    int           frame_count;
    double        start_time;
} composer_pipeline_t;

/** 初始化完整管线 */
composer_pipeline_t *pipeline_create(const char *files[4]);

/** 运行合成管线 (处理所有帧) */
int pipeline_run(composer_pipeline_t *p);

/** 清理 */
void pipeline_destroy(composer_pipeline_t *p);

/* ═══════════════════════════════════════════════════
 *  工具函数
 * ═══════════════════════════════════════════════════ */

/** 格式化字节大小 */
static inline const char *fmt_size(size_t bytes) {
    static char buf[32];
    const char *units[] = {"B","KB","MB","GB"};
    int i = 0;
    double s = bytes;
    while (s >= 1024 && i < 3) { s /= 1024; i++; }
    snprintf(buf, sizeof(buf), "%.1f %s", s, units[i]);
    return buf;
}

/** 获取时间戳 (毫秒) */
static inline double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1000000.0;
}

#endif /* CEDAR_COMPOSER_H */
