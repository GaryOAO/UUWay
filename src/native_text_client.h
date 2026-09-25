#ifndef UURB_NATIVE_TEXT_CLIENT_H
#define UURB_NATIVE_TEXT_CLIENT_H
#include "x11_input_protocol.h"
#include <stddef.h>
#define UURB_TEXT_MAGIC UINT32_C(0x54525555)
#define UURB_TEXT_VERSION UINT32_C(1)
#define UURB_TEXT_MAX_BYTES 8192u
struct uurb_text_header { uint32_t magic, version, sequence, size_or_error; };
/* Strict complete UTF-16 commits; no output mutation on invalid input. */
int uurb_native_text_encode(const uurb_x11_input_event *, size_t count, char *, size_t *bytes);
int uurb_native_text_plan(const uurb_x11_input_event *, size_t count, char *, size_t *bytes, uint32_t *delete_before);
/* One bounded same-user transaction. No replay after any ambiguous response. */
int uurb_native_text_send(const char *path, const char *utf8, size_t bytes);
int uurb_native_text_send_revision(const char *path, const char *utf8, size_t bytes, uint32_t delete_before);
#endif
