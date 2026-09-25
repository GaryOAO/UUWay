#ifndef UURB_NATIVE_CURSOR_PROTOCOL_H
#define UURB_NATIVE_CURSOR_PROTOCOL_H
#include <stdint.h>
#include <stddef.h>

/* Private, local, little-endian snapshot. NOT the video/GPU wire protocol.
 * One producer writes while its generation is alive; the broker invalidates
 * only after that producer and its children are reaped. Sequence is a seqlock:
 * odd during writes, even when committed. Pixels are premultiplied BGRA. */
#define UURB_CURSOR_MAGIC 0x43525555u
#define UURB_CURSOR_VERSION 1u
#define UURB_CURSOR_MAX_SIDE 384u
#define UURB_CURSOR_PIXEL_BYTES (UURB_CURSOR_MAX_SIDE * UURB_CURSOR_MAX_SIDE * 4u)
/* reserved[0] opt-in: compositor embeds the real cursor; USER32 must not
 * supply a second sprite. Never applied by older release contracts. */
#define UURB_CURSOR_VIDEO_EMBEDDED 1u
/* Explicit new policy: metadata capture, GPU-composited single sprite, and
 * native position reporting. Older USER32 readers must reject this policy. */
#define UURB_CURSOR_VIDEO_COMPOSITED 2u
struct uurb_cursor_header {
    uint32_t magic, version, sequence, generation;
    uint32_t active, visible, shape_serial, width;
    uint32_t height;
    int32_t hotspot_x, hotspot_y, x, y;
    uint32_t reserved[3];
};
struct uurb_cursor_snapshot {
    struct uurb_cursor_header header;
    uint8_t pixels[UURB_CURSOR_PIXEL_BYTES];
};
_Static_assert(sizeof(struct uurb_cursor_header) == 64, "cursor header ABI");
_Static_assert(offsetof(struct uurb_cursor_snapshot, pixels) == 64, "cursor pixel ABI");
#endif
