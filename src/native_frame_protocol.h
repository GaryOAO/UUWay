#ifndef UURB_NATIVE_FRAME_PROTOCOL_H
#define UURB_NATIVE_FRAME_PROTOCOL_H
#include <stdint.h>
/* Cross-process little-endian ABI. Atomic words are 32-bit and aligned.
 * Each slot uses an odd/even sequence; consumers discard a changed sequence.
 * Only the latest slot is published, so slow consumers cannot build a queue. */
#define UURB_FRAME_MAGIC 0x46525555u
#define UURB_FRAME_VERSION 2u
#define UURB_FRAME_HEADER_BYTES 4096u
#define UURB_CURSOR_BYTES 65536u
#define UURB_FRAME_DATA_OFFSET (UURB_FRAME_HEADER_BYTES + UURB_CURSOR_BYTES)
#define UURB_FRAME_SLOT_BYTES (64u * 1024u * 1024u)
#define UURB_FRAME_FILE_BYTES (UURB_FRAME_DATA_OFFSET + 2u * UURB_FRAME_SLOT_BYTES)
#define UURB_FRAME_ACTIVE 4u
#define UURB_FRAME_META(slot) (5u + (slot) * 7u)
/* Per-slot: width, height, stride, sequence, frame number, origin x, origin y. */
#define UURB_FRAME_HEARTBEAT 19u
/* Advances on every capture tick, including unchanged frames. */
#define UURB_CURSOR_META 32u
/* Cursor: sequence, x, y, width, height, hotspot x, hotspot y, serial. */
static inline int uurb_frame_geometry_valid(uint32_t width, uint32_t height, uint32_t stride)
{
    return width && height && width <= 16384 && height <= 16384 &&
        stride == width * 4 && (uint64_t)stride * height <= UURB_FRAME_SLOT_BYTES;
}
#endif
