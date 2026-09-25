#include "native_cursor_metadata.h"
#include <spa/buffer/meta.h>
#include <spa/buffer/buffer.h>
#include <spa/param/video/raw.h>
#include <assert.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
static int parse(struct uurb_cursor_snapshot *state, const void *data, size_t size)
{
    return uurb_cursor_metadata(state, data, size, 0);
}
#define uurb_cursor_metadata(state, data, size) parse(state, data, size)
int main(void)
{
    struct uurb_cursor_snapshot *state = calloc(1, sizeof(*state));
    struct uurb_cursor_snapshot *before = calloc(1, sizeof(*before));
    assert(state && before);
    state->header.magic = UURB_CURSOR_MAGIC; state->header.version = 1; state->header.generation = 1;
    struct {
        struct spa_meta_cursor cursor;
        struct spa_meta_bitmap bitmap;
        uint8_t pixels[24];
    } meta = {.cursor = {.id = 1, .position = {100, 200}, .hotspot = {1, 1},
                        .bitmap_offset = sizeof(struct spa_meta_cursor)},
              .bitmap = {.format = SPA_VIDEO_FORMAT_RGBA, .size = {2, 2}, .stride = 12,
                         .offset = sizeof(struct spa_meta_bitmap)},
              .pixels = {1, 2, 3, 255, 4, 5, 6, 128, 0, 0, 0, 0, 7, 8, 9, 255, 10, 11, 12, 255}};
    assert(uurb_cursor_metadata(state, &meta, sizeof(meta)) == 1);
    assert(state->header.visible && state->header.active && state->header.shape_serial == 1);
    assert(state->header.hotspot_x == 1 && state->pixels[0] == 3 && state->pixels[8] == 9);
    assert(uurb_cursor_metadata(state, &meta, sizeof(meta)) == 0);
    uint8_t original_pixels[24]; memcpy(original_pixels, meta.pixels, sizeof(original_pixels));
    memset(meta.pixels, 0, sizeof(meta.pixels));
    assert(uurb_cursor_metadata(state, &meta, sizeof(meta)) == 1 && !state->header.visible);
    memcpy(meta.pixels, original_pixels, sizeof(meta.pixels));
    assert(uurb_cursor_metadata(state, &meta, sizeof(meta)) == 1 && state->header.visible);
    meta.cursor.bitmap_offset = 0; meta.cursor.hotspot.x = 0; meta.cursor.position.x = 101;
    assert(uurb_cursor_metadata(state, &meta, sizeof(meta)) == 1);
    assert(state->header.hotspot_x == 1 && state->header.shape_serial == 3 && state->header.x == 101);
    meta.cursor.id = 0;
    assert(uurb_cursor_metadata(state, &meta, sizeof(meta)) == 0 && state->header.visible);
    assert((uurb_cursor_metadata)(state, &meta, sizeof(meta), 1) == 1 && !state->header.visible);
    meta.cursor.id = 1;
    assert((uurb_cursor_metadata)(state, &meta, sizeof(meta), 1) == 1 && state->header.visible);
    meta.cursor.id = 1; meta.cursor.bitmap_offset = sizeof(struct spa_meta_cursor);
    *before = *state;
    for (size_t size = 0; size < sizeof(meta) - 4; ++size) {
        assert(uurb_cursor_metadata(state, &meta, size) == -1);
        assert(!memcmp(state, before, sizeof(*state)));
    }
    meta.bitmap.stride = -8;
    assert(uurb_cursor_metadata(state, &meta, sizeof(meta)) == -1);
    meta.bitmap.stride = 12; meta.bitmap.size.width = 385;
    assert(uurb_cursor_metadata(state, &meta, sizeof(meta)) == -1);
    meta.bitmap.size.width = 2; meta.cursor.hotspot.x = 2;
    assert(uurb_cursor_metadata(state, &meta, sizeof(meta)) == -1);
    meta.cursor.hotspot.x = 1; meta.cursor.bitmap_offset = UINT32_MAX;
    assert(uurb_cursor_metadata(state, &meta, sizeof(meta)) == -1);
    meta.cursor.bitmap_offset = sizeof(struct spa_meta_cursor); meta.bitmap.offset = UINT32_MAX;
    assert(uurb_cursor_metadata(state, &meta, sizeof(meta)) == -1);
    meta.bitmap = (struct spa_meta_bitmap){0};
    assert(uurb_cursor_metadata(state, &meta, sizeof(meta)) == 1 && !state->header.visible);
    assert(!(state->header.sequence & 1));
    struct spa_meta_header header = {0};
    struct spa_meta metas[] = {{.type = SPA_META_Cursor, .size = sizeof(meta), .data = &meta},
                              {.type = SPA_META_Header, .size = sizeof(header), .data = &header}};
    struct spa_chunk chunk = {.size = 0, .flags = SPA_CHUNK_FLAG_CORRUPTED};
    struct spa_data plane = {.chunk = &chunk};
    struct spa_buffer buffer = {.n_metas = 2, .metas = metas, .n_datas = 1, .datas = &plane};
    assert(uurb_cursor_only_buffer(&buffer));
    chunk.size = 1;
    assert(!uurb_cursor_only_buffer(&buffer));
    chunk.size = 0; header.flags = SPA_META_HEADER_FLAG_CORRUPTED;
    assert(!uurb_cursor_only_buffer(&buffer));
    free(before); free(state);
    puts("NATIVE_CURSOR_METADATA_PASS shape position hotspot padding invalid bounds hide");
    return 0;
}
