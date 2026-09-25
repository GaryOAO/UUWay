#ifndef UURB_NATIVE_EIS_INPUT_H
#define UURB_NATIVE_EIS_INPUT_H
#include "uu_native_input_translate.h"
struct uurb_eis_input;
/* Caller retains fd and its SourceLease; close both on any fatal return.
 * The exclusively leased socket is switched to nonblocking mode (shared OFD).
 * No Portal requests, uinput device, text submission or payload logging here.
 * Single-threaded. dispatch(): -1 fatal, 0 negotiating, 1 mapped input ready. */
struct uurb_eis_input *uurb_eis_open(int fd, const char *mapping_id);
int uurb_eis_fd(struct uurb_eis_input *input);
int uurb_eis_dispatch(struct uurb_eis_input *input);
/* Accept translator output BEFORE legacy wheel expansion. Apply existing
 * speed settings first. Successful send means queued, not target acceptance. */
int uurb_eis_send(struct uurb_eis_input *input, const struct uurb_native_events *events);
/* Release tracked keys or buttons on a live transport; no replay after failure.
 * Caller still closes the joint-session lease if release cannot be queued. */
int uurb_eis_release(struct uurb_eis_input *input, int keyboard);
/* Fixed diagnostic label only; never returns peer names, mapping or payload. */
const char *uurb_eis_failure(struct uurb_eis_input *input);
int uurb_eis_dimensions(struct uurb_eis_input *input, uint32_t *width, uint32_t *height);
void uurb_eis_close(struct uurb_eis_input *input);
#endif
