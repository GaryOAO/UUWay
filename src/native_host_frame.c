#define _GNU_SOURCE
#include "native_host_frame.h"
#include <sys/mman.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdlib.h>
#include <string.h>
struct uurb_host_frame {
    VkDevice device;
    VkBuffer buffer;
    VkDeviceMemory memory;
    void *gpu, *shared;
    size_t bytes;
    unsigned width, height;
    int fd;
};
struct uurb_host_frame *uurb_host_frame_open(VkPhysicalDevice physical, VkDevice device, unsigned width, unsigned height)
{
    struct uurb_host_frame *s = calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->device = device; s->width = width; s->height = height; s->bytes = (size_t)width * height * 4; s->fd = -1;
    VkBufferCreateInfo buffer = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
        .size = s->bytes, .usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT};
    if (vkCreateBuffer(device, &buffer, NULL, &s->buffer) != VK_SUCCESS) goto fail;
    VkMemoryRequirements req; VkPhysicalDeviceMemoryProperties mem;
    vkGetBufferMemoryRequirements(device, s->buffer, &req);
    vkGetPhysicalDeviceMemoryProperties(physical, &mem);
    unsigned type;
    for (type = 0; type < mem.memoryTypeCount; ++type)
        if ((req.memoryTypeBits & (1u << type)) &&
            (mem.memoryTypes[type].propertyFlags & (VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)) ==
            (VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)) break;
    if (type == mem.memoryTypeCount) goto fail;
    VkMemoryAllocateInfo alloc = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = req.size, .memoryTypeIndex = type};
    if (vkAllocateMemory(device, &alloc, NULL, &s->memory) != VK_SUCCESS ||
        vkBindBufferMemory(device, s->buffer, s->memory, 0) != VK_SUCCESS ||
        vkMapMemory(device, s->memory, 0, VK_WHOLE_SIZE, 0, &s->gpu) != VK_SUCCESS) goto fail;
    s->fd = memfd_create("uuway-amd-bgra", MFD_CLOEXEC | MFD_ALLOW_SEALING);
    if (s->fd < 0 || ftruncate(s->fd, s->bytes) || fcntl(s->fd, F_ADD_SEALS, F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_SEAL)) goto fail;
    s->shared = mmap(NULL, s->bytes, PROT_READ | PROT_WRITE, MAP_SHARED, s->fd, 0);
    if (s->shared == MAP_FAILED) { s->shared = NULL; goto fail; }
    return s;
fail:
    uurb_host_frame_close(s); return NULL;
}
void uurb_host_frame_record(struct uurb_host_frame *s, VkCommandBuffer cmd, VkImage image)
{
    VkImageMemoryBarrier barrier = {.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
        .srcAccessMask = VK_ACCESS_MEMORY_WRITE_BIT, .dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT,
        .oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, .newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
        .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
        .image = image, .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, NULL, 0, NULL, 1, &barrier);
    VkBufferImageCopy region = {.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1}, .imageExtent = {s->width, s->height, 1}};
    vkCmdCopyImageToBuffer(cmd, image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, s->buffer, 1, &region);
    VkBufferMemoryBarrier host = {.sType = VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER,
        .srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT, .dstAccessMask = VK_ACCESS_HOST_READ_BIT,
        .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
        .buffer = s->buffer, .size = VK_WHOLE_SIZE};
    barrier.srcAccessMask = VK_ACCESS_TRANSFER_READ_BIT; barrier.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL; barrier.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT | VK_PIPELINE_STAGE_TRANSFER_BIT,
                         0, 0, NULL, 1, &host, 1, &barrier);
}
int uurb_host_frame_publish(struct uurb_host_frame *s)
{ if (!s) return -1; memcpy(s->shared, s->gpu, s->bytes); return s->fd; }
void uurb_host_frame_close(struct uurb_host_frame *s)
{
    if (!s) return;
    if (s->shared) munmap(s->shared, s->bytes);
    if (s->fd >= 0) close(s->fd);
    if (s->gpu) vkUnmapMemory(s->device, s->memory);
    if (s->buffer) vkDestroyBuffer(s->device, s->buffer, NULL);
    if (s->memory) vkFreeMemory(s->device, s->memory, NULL);
    free(s);
}
