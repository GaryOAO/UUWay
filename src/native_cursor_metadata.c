#define _GNU_SOURCE
#include "native_cursor_metadata.h"
#include <spa/buffer/meta.h>
#include <spa/buffer/buffer.h>
#include <spa/param/video/raw.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <string.h>

struct uurb_cursor_snapshot *uurb_cursor_map(int fd)
{
    struct stat info;
    if (fd < 3 || fstat(fd, &info) || !S_ISREG(info.st_mode) ||
        info.st_uid != geteuid() || (info.st_mode & 077) || info.st_nlink != 1 ||
        info.st_size != sizeof(struct uurb_cursor_snapshot) ||
        (fcntl(fd, F_GETFL) & O_ACCMODE) != O_RDWR) return NULL;
    void *data = mmap(NULL, sizeof(struct uurb_cursor_snapshot), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (data == MAP_FAILED) return NULL;
    struct uurb_cursor_snapshot *state = data;
    if (state->header.magic != UURB_CURSOR_MAGIC || state->header.version != UURB_CURSOR_VERSION ||
        !state->header.generation || state->header.active || (state->header.sequence & 1)) {
        munmap(data, sizeof(*state));
        return NULL;
    }
    return state;
}

void uurb_cursor_unmap(struct uurb_cursor_snapshot *state)
{
    if (state) munmap(state, sizeof(*state));
}

int uurb_cursor_only_buffer(struct spa_buffer *buffer)
{
    if (!buffer || !buffer->n_datas || buffer->n_datas > 4 ||
        !spa_buffer_find_meta_data(buffer, SPA_META_Cursor, sizeof(struct spa_meta_cursor))) return 0;
    struct spa_meta_header *header = spa_buffer_find_meta_data(buffer, SPA_META_Header, sizeof(*header));
    if (!header || (header->flags & SPA_META_HEADER_FLAG_CORRUPTED)) return 0;
    for (uint32_t i = 0; i < buffer->n_datas; ++i)
        if (!buffer->datas[i].chunk || buffer->datas[i].chunk->size) return 0;
    /* Mutter 46 deliberately marks zero-size cursor-only VIDEO chunks as
     * CORRUPTED. That flag must not discard valid independent cursor metadata.
     * No video FD/stride is consumed on this path, and no frame is encoded. */
    return 1;
}

int uurb_cursor_metadata(struct uurb_cursor_snapshot *state, const void *data, size_t size, int mutter)
{
    if (!state || !data) return 0;
    struct spa_meta_cursor cursor;
    if (size < sizeof(cursor)) return -1;
    memcpy(&cursor, data, sizeof(cursor));
    if (!cursor.id) {
        /* Generic SPA: id=0 means no update. Mutter monitor streams instead
         * write id=0 when the pointer is invisible or outside the stream.
         * This backend quirk is explicit, never inferred for other sources. */
        if (!mutter || (state->header.active && !state->header.visible)) return 0;
        __atomic_add_fetch(&state->header.sequence, 1, __ATOMIC_SEQ_CST);
        state->header.active = 1; state->header.visible = 0;
        __atomic_add_fetch(&state->header.sequence, 1, __ATOMIC_SEQ_CST);
        return 1;
    }
    struct spa_meta_bitmap bitmap = {0};
    const uint8_t *pixels = NULL;
    int changed = 0, visible = state->header.visible;
    uint32_t width = state->header.width, height = state->header.height;
    int32_t hx = state->header.hotspot_x, hy = state->header.hotspot_y;
    if (mutter && !cursor.bitmap_offset) {
        visible = 0;
        for (size_t i = 3; i < (size_t)width * height * 4; i += 4)
            if (state->pixels[i]) { visible = 1; break; }
    }
    if (cursor.bitmap_offset) {
        if (cursor.bitmap_offset < sizeof(cursor) || cursor.bitmap_offset > size ||
            sizeof(bitmap) > size - cursor.bitmap_offset) return -1;
        memcpy(&bitmap, (const uint8_t *)data + cursor.bitmap_offset, sizeof(bitmap));
        /* Mutter 46 sends a zeroed bitmap to hide its sprite. The general SPA
         * no-new-format marker with a nonzero data offset preserves the shape. */
        if (!bitmap.offset) {
            visible = 0;
            changed = width || height;
            width = height = 0; hx = hy = 0;
        } else if (bitmap.format) {
            size_t available = size - cursor.bitmap_offset;
            if ((bitmap.format != SPA_VIDEO_FORMAT_RGBA && bitmap.format != SPA_VIDEO_FORMAT_BGRA) ||
                !bitmap.size.width || bitmap.size.width > UURB_CURSOR_MAX_SIDE ||
                !bitmap.size.height || bitmap.size.height > UURB_CURSOR_MAX_SIDE ||
                bitmap.stride < (int32_t)(bitmap.size.width * 4) ||
                bitmap.offset < sizeof(bitmap) || bitmap.offset > available ||
                (uint64_t)(bitmap.size.height - 1) * (uint32_t)bitmap.stride + bitmap.size.width * 4 > available - bitmap.offset ||
                cursor.hotspot.x < 0 || cursor.hotspot.y < 0 ||
                cursor.hotspot.x >= (int32_t)bitmap.size.width || cursor.hotspot.y >= (int32_t)bitmap.size.height) return -1;
            pixels = (const uint8_t *)data + cursor.bitmap_offset + bitmap.offset;
            width = bitmap.size.width; height = bitmap.size.height;
            hx = cursor.hotspot.x; hy = cursor.hotspot.y;
            /* Toolkits may hide with a fully transparent sprite instead of
             * the SPA empty-bitmap marker. Both mean no visible cursor. */
            visible = 0;
            for (uint32_t y = 0; !visible && y < height; ++y)
                for (uint32_t x = 0; x < width; ++x)
                    if (pixels[(size_t)y * bitmap.stride + x * 4 + 3]) { visible = 1; break; }
            changed = width != state->header.width || height != state->header.height ||
                hx != state->header.hotspot_x || hy != state->header.hotspot_y;
            for (uint32_t y = 0; !changed && y < height; ++y)
                for (uint32_t x = 0; !changed && x < width; ++x) {
                    const uint8_t *source = pixels + (size_t)y * bitmap.stride + x * 4;
                    const uint8_t *old = state->pixels + ((size_t)y * width + x) * 4;
                    int swap = bitmap.format == SPA_VIDEO_FORMAT_RGBA;
                    changed = source[swap ? 2 : 0] != old[0] || source[1] != old[1] ||
                        source[swap ? 0 : 2] != old[2] || source[3] != old[3];
                }
        }
    }
    if (state->header.active && !changed && visible == (int)state->header.visible &&
        cursor.position.x == state->header.x && cursor.position.y == state->header.y) return 0;
    __atomic_add_fetch(&state->header.sequence, 1, __ATOMIC_SEQ_CST);
    if (changed) {
        for (uint32_t y = 0; y < height; ++y)
            for (uint32_t x = 0; x < width; ++x) {
                const uint8_t *source = pixels + (size_t)y * bitmap.stride + x * 4;
                uint8_t *target = state->pixels + ((size_t)y * width + x) * 4;
                int swap = bitmap.format == SPA_VIDEO_FORMAT_RGBA;
                target[0] = source[swap ? 2 : 0]; target[1] = source[1];
                target[2] = source[swap ? 0 : 2]; target[3] = source[3];
            }
        if (!++state->header.shape_serial) ++state->header.shape_serial;
    }
    state->header.active = 1;
    state->header.visible = visible;
    state->header.width = width; state->header.height = height;
    state->header.hotspot_x = hx; state->header.hotspot_y = hy;
    state->header.x = cursor.position.x; state->header.y = cursor.position.y;
    __atomic_add_fetch(&state->header.sequence, 1, __ATOMIC_SEQ_CST);
    return 1;
}
