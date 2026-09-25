#ifndef UURB_VK_CURSOR_COMPOSITE_H
#define UURB_VK_CURSOR_COMPOSITE_H
#include <vulkan/vulkan.h>
#include "native_cursor_protocol.h"

struct uurb_vk_cursor_composite;
/* Output is borrowed B8G8R8A8_UNORM with TRANSFER_DST|COLOR_ATTACHMENT usage.
 * Device/queue must support graphics. All calls are single-threaded. */
struct uurb_vk_cursor_composite *uurb_vk_cursor_composite_open(
    VkPhysicalDevice physical, VkDevice device, VkImage output, uint32_t width, uint32_t height);
/* Record GPU-only desktop copy plus a bounded, premultiplied cursor overlay.
 * source is TRANSFER_SRC_OPTIMAL and output is TRANSFER_DST_OPTIMAL, both owned
 * by the command queue. NULL source reuses the clean GPU background; it is an
 * error before the first successful source submission. Layouts are preserved.
 * Prior recorded work and external output reads MUST be complete before this
 * call (cursor upload memory is reused). Caller handles submit/fence/ownership;
 * on failed or abandoned submission, destroy this instance after GPU quiesces.
 * Snapshot must be stable (call from its producer, never race a shared writer).
 * CPU memory contains only cursor pixels, never desktop pixels. */
int uurb_vk_cursor_composite_record(struct uurb_vk_cursor_composite *, VkCommandBuffer,
    VkImage source, const struct uurb_cursor_snapshot *);
unsigned uurb_vk_cursor_composite_uploads(const struct uurb_vk_cursor_composite *);
/* Caller must finish GPU work before closing; does not own output or device. */
void uurb_vk_cursor_composite_close(struct uurb_vk_cursor_composite *);
#endif
