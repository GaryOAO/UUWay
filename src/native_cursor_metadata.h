#ifndef UURB_NATIVE_CURSOR_METADATA_H
#define UURB_NATIVE_CURSOR_METADATA_H
#include "native_cursor_protocol.h"
struct uurb_cursor_snapshot *uurb_cursor_map(int fd);
void uurb_cursor_unmap(struct uurb_cursor_snapshot *state);
/* 1 committed; 0 absent/no change; -1 malformed. No mutation on malformed. */
int uurb_cursor_metadata(struct uurb_cursor_snapshot *state, const void *data, size_t size, int mutter);
struct spa_buffer;
int uurb_cursor_only_buffer(struct spa_buffer *buffer);
#endif
