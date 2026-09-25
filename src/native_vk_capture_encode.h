#ifndef UURB_VK_CAPTURE_ENCODE_H
#define UURB_VK_CAPTURE_ENCODE_H
#include <vulkan/vulkan.h>
#include <stdint.h>
#include "native_cursor_protocol.h"
struct uurb_capture_encoder;
/* GPU-frame sink. The FD is borrowed and describes dedicated opaque Vulkan
 * BGRA memory on uuid. Ownership has been released to EXTERNAL and the source
 * fence completed. The sink must finish ALL GPU reads before returning; it may
 * retain an imported allocation, but must never read it between callbacks.
 * Nonzero return poisons this capture session; never recycle its output. */
typedef int (*uurb_capture_gpu_sink)(void *opaque, int fd, uint64_t allocation_bytes,
    const unsigned char uuid[16], uint32_t width, uint32_t height, uint64_t timestamp);
struct uurb_capture_encoder *uurb_capture_sink_open(VkPhysicalDevice, uurb_capture_gpu_sink, void *opaque);
struct uurb_capture_encoder *uurb_capture_encoder_open(VkPhysicalDevice, int hevc, int output_fd);
/* Explicit opt-in before the first frame. Borrowed stable producer-owned
 * metadata; no concurrent writers. Desktop source must exclude its cursor. */
int uurb_capture_encoder_composite_cursor(struct uurb_capture_encoder *, const struct uurb_cursor_snapshot *);
/* Recompose the retained clean GPU background on an actual cursor-only source
 * event. Caller supplies its genuine PipeWire PTS, never an invented clock.
 * Requires at least one delivered desktop frame in this encoder generation. */
int uurb_capture_encoder_cursor_frame(struct uurb_capture_encoder *, uint64_t timestamp);
/* Caller must hold the PipeWire buffer until this synchronous call returns.
 * GPU timeout terminates the isolated worker; never requeue in-flight data. */
int uurb_capture_encoder_frame(struct uurb_capture_encoder *, const void *buffer_key, int fd, uint32_t width, uint32_t height,
    uint32_t fourcc, uint64_t modifier, uint32_t offset, uint32_t stride, uint64_t timestamp);
void uurb_capture_encoder_forget(struct uurb_capture_encoder *, const void *buffer_key);
struct uurb_capture_stats { unsigned imports; double average_us, maximum_us; };
struct uurb_capture_stats uurb_capture_encoder_stats(struct uurb_capture_encoder *);
int uurb_capture_encoder_close(struct uurb_capture_encoder *);
#endif
