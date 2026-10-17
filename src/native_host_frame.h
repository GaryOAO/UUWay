#ifndef UURB_HOST_FRAME_H
#define UURB_HOST_FRAME_H
#include <vulkan/vulkan.h>
struct uurb_host_frame;
struct uurb_host_frame *uurb_host_frame_open(VkPhysicalDevice, VkDevice, unsigned, unsigned);
void uurb_host_frame_record(struct uurb_host_frame *, VkCommandBuffer, VkImage);
/* Called only after the submission fence signals. Borrowed FD, size sealed. */
int uurb_host_frame_publish(struct uurb_host_frame *);
void uurb_host_frame_close(struct uurb_host_frame *);
#endif
