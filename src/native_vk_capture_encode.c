/* Real capture -> explicit DMA-BUF sync -> Vulkan GPU blit -> CUDA/NVENC.
 * Synchronous single-image reference, not the final UU or asynchronous backend.
 * Desktop pixels are never host mapped. Optional bounded cursor metadata is
 * uploaded for GPU composition over a retained, device-local clean background. */
#define _GNU_SOURCE
#include "native_vk_capture_encode.h"
#include "native_vk_dmabuf_import.h"
#include "native_cuda_encode_session.h"
#include "native_vk_cursor_composite.h"
#include <libdrm/drm_fourcc.h>
#include <linux/dma-buf.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <time.h>

struct input_cache {
    const void *key;
    struct uurb_vk_image image;
    uint32_t width, height, fourcc, offset, stride;
    uint64_t modifier;
    dev_t device;
    ino_t inode;
};

struct uurb_capture_encoder {
    struct uurb_vk_importer *importer;
    VkPhysicalDevice physical;
    VkDevice device;
    VkQueue queue;
    uint32_t family, width, height;
    VkImage output;
    VkDeviceMemory memory;
    VkDeviceSize bytes;
    VkCommandPool pool;
    VkCommandBuffer command;
    VkFence fence;
    struct uurb_encoder *encoder;
    unsigned frames;
    int failed, hevc, output_fd;
    unsigned char uuid[16];
    PFN_vkGetMemoryFdKHR get_fd;
    PFN_vkImportSemaphoreFdKHR import_semaphore;
    struct input_cache inputs[16];
    unsigned imports;
    uint64_t elapsed_ns, maximum_ns;
    size_t written_bytes;
    uurb_capture_gpu_sink sink;
    void *sink_opaque;
    int shared_fd;
    const struct uurb_cursor_snapshot *cursor;
    struct uurb_vk_cursor_composite *composite;
};

#define GPU(call) do { VkResult r_ = (call); if (r_ != VK_SUCCESS) { \
    fprintf(stderr, "%s: Vulkan %d\n", #call, r_); goto fail; } } while (0)

static struct uurb_capture_encoder *capture_open(VkPhysicalDevice physical, int hevc, int output_fd,
    uurb_capture_gpu_sink sink, void *opaque)
{
    if (!physical || (hevc != 0 && hevc != 1) || (!sink && output_fd < 0)) return NULL;
    struct uurb_capture_encoder *s = calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->physical = physical; s->hevc = hevc; s->output_fd = output_fd;
    s->sink = sink; s->sink_opaque = opaque; s->shared_fd = -1;
    VkPhysicalDeviceExternalSemaphoreInfo semaphore_info = {
        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_SEMAPHORE_INFO,
        .handleType = VK_EXTERNAL_SEMAPHORE_HANDLE_TYPE_SYNC_FD_BIT};
    VkExternalSemaphoreProperties semaphore_properties = {
        .sType = VK_STRUCTURE_TYPE_EXTERNAL_SEMAPHORE_PROPERTIES};
    vkGetPhysicalDeviceExternalSemaphoreProperties(physical, &semaphore_info, &semaphore_properties);
    if (!(semaphore_properties.externalSemaphoreFeatures & VK_EXTERNAL_SEMAPHORE_FEATURE_IMPORTABLE_BIT)) {
        fprintf(stderr, "GPU cannot import DMA-BUF sync-file semaphores\n"); goto fail;
    }
    s->importer = uurb_vk_importer_open_transfer(physical);
    if (!s->importer) goto fail;
    uurb_vk_importer_context(s->importer, &s->device, &s->queue, &s->family);
    s->get_fd = (void *)vkGetDeviceProcAddr(s->device, "vkGetMemoryFdKHR");
    s->import_semaphore = (void *)vkGetDeviceProcAddr(s->device, "vkImportSemaphoreFdKHR");
    if (!s->get_fd || !s->import_semaphore) goto fail;
    VkPhysicalDeviceIDProperties id = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ID_PROPERTIES};
    VkPhysicalDeviceProperties2 properties = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2, .pNext = &id};
    vkGetPhysicalDeviceProperties2(physical, &properties);
    memcpy(s->uuid, id.deviceUUID, sizeof(s->uuid));
    VkCommandPoolCreateInfo pool = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
        .queueFamilyIndex = s->family, .flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT};
    GPU(vkCreateCommandPool(s->device, &pool, NULL, &s->pool));
    VkCommandBufferAllocateInfo command = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
        .commandPool = s->pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1};
    GPU(vkAllocateCommandBuffers(s->device, &command, &s->command));
    VkFenceCreateInfo fence = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    GPU(vkCreateFence(s->device, &fence, NULL, &s->fence));
    return s;
fail:
    uurb_capture_encoder_close(s);
    return NULL;
}

struct uurb_capture_encoder *uurb_capture_encoder_open(VkPhysicalDevice physical, int hevc, int output_fd)
{ return capture_open(physical, hevc, output_fd, NULL, NULL); }
struct uurb_capture_encoder *uurb_capture_sink_open(VkPhysicalDevice physical, uurb_capture_gpu_sink sink, void *opaque)
{ return sink ? capture_open(physical, 0, -1, sink, opaque) : NULL; }

int uurb_capture_encoder_composite_cursor(struct uurb_capture_encoder *s, const struct uurb_cursor_snapshot *cursor)
{
    if (!s || !cursor || s->failed || s->output || s->frames || s->cursor) return 1;
    s->cursor = cursor;
    return 0;
}

static int allocate_output(struct uurb_capture_encoder *s, uint32_t width, uint32_t height)
{
    if (width < 2 || height < 2 || width > 4096 || height > 4096 || (width | height) & 1u)
        return 1;
    VkFormatProperties format_properties;
    vkGetPhysicalDeviceFormatProperties(s->physical, VK_FORMAT_B8G8R8A8_UNORM, &format_properties);
    if (!(format_properties.optimalTilingFeatures & VK_FORMAT_FEATURE_BLIT_DST_BIT)) {
        fprintf(stderr, "GPU output format does not support blit destination\n"); return 1;
    }
    VkPhysicalDeviceExternalImageFormatInfo export_query = {
        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO,
        .handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT};
    VkPhysicalDeviceImageFormatInfo2 image_query = {
        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_FORMAT_INFO_2, .pNext = &export_query,
        .format = VK_FORMAT_B8G8R8A8_UNORM, .type = VK_IMAGE_TYPE_2D,
        .tiling = VK_IMAGE_TILING_OPTIMAL,
        .usage = VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT};
    VkExternalImageFormatProperties export_properties = {
        .sType = VK_STRUCTURE_TYPE_EXTERNAL_IMAGE_FORMAT_PROPERTIES};
    VkImageFormatProperties2 image_properties = {
        .sType = VK_STRUCTURE_TYPE_IMAGE_FORMAT_PROPERTIES_2, .pNext = &export_properties};
    if (vkGetPhysicalDeviceImageFormatProperties2(s->physical, &image_query, &image_properties) != VK_SUCCESS ||
        !(export_properties.externalMemoryProperties.externalMemoryFeatures & VK_EXTERNAL_MEMORY_FEATURE_EXPORTABLE_BIT) ||
        width > image_properties.imageFormatProperties.maxExtent.width ||
        height > image_properties.imageFormatProperties.maxExtent.height) {
        fprintf(stderr, "GPU output image cannot be exported with the requested geometry\n"); return 1;
    }
    VkExternalMemoryImageCreateInfo external = {.sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,
        .handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT};
    VkImageCreateInfo image = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, .pNext = &external,
        .imageType = VK_IMAGE_TYPE_2D, .format = VK_FORMAT_B8G8R8A8_UNORM, .extent = {width, height, 1},
        .mipLevels = 1, .arrayLayers = 1, .samples = VK_SAMPLE_COUNT_1_BIT,
        .tiling = VK_IMAGE_TILING_OPTIMAL, .usage = VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT,
        .sharingMode = VK_SHARING_MODE_EXCLUSIVE, .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED};
    GPU(vkCreateImage(s->device, &image, NULL, &s->output));
    VkMemoryRequirements requirements;
    vkGetImageMemoryRequirements(s->device, s->output, &requirements);
    VkPhysicalDeviceMemoryProperties types;
    vkGetPhysicalDeviceMemoryProperties(s->physical, &types);
    uint32_t type = UINT32_MAX;
    for (unsigned i = 0; i < types.memoryTypeCount; i++) {
        if ((requirements.memoryTypeBits & (1u << i)) &&
            (types.memoryTypes[i].propertyFlags & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)) { type = i; break; }
    }
    if (type == UINT32_MAX) goto fail;
    VkMemoryDedicatedAllocateInfo dedicated = {.sType = VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO, .image = s->output};
    VkExportMemoryAllocateInfo export = {.sType = VK_STRUCTURE_TYPE_EXPORT_MEMORY_ALLOCATE_INFO,
        .pNext = &dedicated, .handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT};
    VkMemoryAllocateInfo allocate = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        .pNext = &export, .allocationSize = requirements.size, .memoryTypeIndex = type};
    GPU(vkAllocateMemory(s->device, &allocate, NULL, &s->memory));
    GPU(vkBindImageMemory(s->device, s->output, s->memory, 0));
    s->bytes = requirements.size; s->width = width; s->height = height;
    if (s->cursor && !(s->composite = uurb_vk_cursor_composite_open(s->physical, s->device, s->output, width, height)))
        goto fail;
    return 0;
fail:
    return 1;
}

static VkImageMemoryBarrier barrier(VkImage image, VkImageLayout old, VkImageLayout next,
                                    uint32_t from, uint32_t to, VkAccessFlags src, VkAccessFlags dst)
{
    return (VkImageMemoryBarrier){.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
        .srcAccessMask = src, .dstAccessMask = dst, .oldLayout = old, .newLayout = next,
        .srcQueueFamilyIndex = from, .dstQueueFamilyIndex = to, .image = image,
        .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
}

static int write_packet(int fd, const void *data, size_t bytes)
{
    const unsigned char *position = data;
    while (bytes) {
        ssize_t written = write(fd, position, bytes);
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) return 1;
        position += written; bytes -= written;
    }
    return 0;
}

static struct input_cache *get_input(struct uurb_capture_encoder *s, const void *key, int fd,
    uint32_t width, uint32_t height, uint32_t fourcc, uint64_t modifier, uint32_t offset, uint32_t stride)
{
    struct stat identity;
    if (!key || fstat(fd, &identity) < 0) return NULL;
    struct input_cache *empty = NULL;
    for (unsigned i = 0; i < 16; i++) {
        struct input_cache *item = &s->inputs[i];
        if (!item->key && !empty) empty = item;
        if (item->key != key) continue;
        if (item->width != width || item->height != height || item->fourcc != fourcc || item->modifier != modifier ||
            item->offset != offset || item->stride != stride || item->device != identity.st_dev || item->inode != identity.st_ino) {
            fprintf(stderr, "PipeWire buffer changed identity/layout without removal\n"); return NULL;
        }
        return item;
    }
    if (!empty) { fprintf(stderr, "GPU input cache exceeds 16 live buffers\n"); return NULL; }
    if (uurb_vk_import_image(s->importer, fd, width, height, fourcc, modifier, offset, stride,
                             VK_IMAGE_USAGE_TRANSFER_SRC_BIT, &empty->image)) return NULL;
    empty->key = key; empty->width = width; empty->height = height; empty->fourcc = fourcc;
    empty->modifier = modifier; empty->offset = offset; empty->stride = stride;
    empty->device = identity.st_dev; empty->inode = identity.st_ino;
    s->imports++;
    return empty;
}

void uurb_capture_encoder_forget(struct uurb_capture_encoder *s, const void *key)
{
    if (!s || !key) return;
    for (unsigned i = 0; i < 16; i++) {
        if (s->inputs[i].key != key) continue;
        uurb_vk_import_image_close(s->importer, &s->inputs[i].image);
        memset(&s->inputs[i], 0, sizeof(s->inputs[i]));
        break;
    }
}

struct uurb_capture_stats uurb_capture_encoder_stats(struct uurb_capture_encoder *s)
{
    if (!s) return (struct uurb_capture_stats){0};
    return (struct uurb_capture_stats){s->imports, s->frames ? (double)s->elapsed_ns / s->frames / 1000 : 0,
                                      (double)s->maximum_ns / 1000};
}

static int capture_frame(struct uurb_capture_encoder *s, const void *buffer_key, int fd, uint32_t width, uint32_t height,
    uint32_t fourcc, uint64_t modifier, uint32_t offset, uint32_t stride, uint64_t timestamp)
{
    if (!s || s->failed || (!buffer_key && (!s->composite || !s->frames))) return 1;
    struct timespec begin_time, end_time;
    if (clock_gettime(CLOCK_MONOTONIC, &begin_time) < 0) return 1;
    VkSemaphore ready = VK_NULL_HANDLE;
    int sync_fd = -1, result = 1;
    if (!s->output && allocate_output(s, width, height)) goto fail;
    if (width != s->width || height != s->height) {
        fprintf(stderr, "Capture resize requires a new encoder session\n"); goto fail;
    }
    VkImage source = VK_NULL_HANDLE;
    if (buffer_key) {
        struct input_cache *input = get_input(s, buffer_key, fd, width, height, fourcc, modifier, offset, stride);
        if (!input) goto fail;
        source = input->image.image;
        struct dma_buf_export_sync_file sync = {.flags = DMA_BUF_SYNC_READ, .fd = -1};
        int sync_status;
        do { sync_status = ioctl(fd, DMA_BUF_IOCTL_EXPORT_SYNC_FILE, &sync); } while (sync_status < 0 && errno == EINTR);
        if (sync_status < 0) { perror("DMA-BUF acquire fence export"); goto fail; }
        sync_fd = sync.fd;
        VkSemaphoreCreateInfo semaphore = {.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO};
        GPU(vkCreateSemaphore(s->device, &semaphore, NULL, &ready));
        VkImportSemaphoreFdInfoKHR import = {.sType = VK_STRUCTURE_TYPE_IMPORT_SEMAPHORE_FD_INFO_KHR,
            .semaphore = ready, .flags = VK_SEMAPHORE_IMPORT_TEMPORARY_BIT,
            .handleType = VK_EXTERNAL_SEMAPHORE_HANDLE_TYPE_SYNC_FD_BIT, .fd = sync_fd};
        GPU(s->import_semaphore(s->device, &import));
        sync_fd = -1;  /* Imported sync payload FD consumed by Vulkan. */
    }
    GPU(vkResetCommandBuffer(s->command, 0));
    VkCommandBufferBeginInfo begin = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
        .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT};
    GPU(vkBeginCommandBuffer(s->command, &begin));
    VkImageMemoryBarrier acquire[2] = {
        barrier(s->output, s->frames ? VK_IMAGE_LAYOUT_GENERAL : VK_IMAGE_LAYOUT_UNDEFINED,
                VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, s->frames ? VK_QUEUE_FAMILY_EXTERNAL : VK_QUEUE_FAMILY_IGNORED,
                s->frames ? s->family : VK_QUEUE_FAMILY_IGNORED, 0, VK_ACCESS_TRANSFER_WRITE_BIT),
        barrier(source, VK_IMAGE_LAYOUT_GENERAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                VK_QUEUE_FAMILY_FOREIGN_EXT, s->family, 0, VK_ACCESS_TRANSFER_READ_BIT)};
    vkCmdPipelineBarrier(s->command, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                         0, 0, NULL, 0, NULL, source ? 2 : 1, acquire);
    if (s->composite) {
        if (uurb_vk_cursor_composite_record(s->composite, s->command, source, s->cursor)) goto fail;
    } else {
        VkImageBlit blit = {.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
            .dstSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
            .srcOffsets = {{0,0,0}, {width,height,1}}, .dstOffsets = {{0,0,0}, {width,height,1}}};
        vkCmdBlitImage(s->command, source, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                       s->output, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &blit, VK_FILTER_NEAREST);
    }
    VkImageMemoryBarrier release[2] = {
        barrier(s->output, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL,
                s->family, VK_QUEUE_FAMILY_EXTERNAL, s->composite ? VK_ACCESS_MEMORY_WRITE_BIT : VK_ACCESS_TRANSFER_WRITE_BIT, 0),
        barrier(source, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_LAYOUT_GENERAL,
                s->family, VK_QUEUE_FAMILY_FOREIGN_EXT, VK_ACCESS_TRANSFER_READ_BIT, 0)};
    vkCmdPipelineBarrier(s->command, s->composite ? VK_PIPELINE_STAGE_ALL_COMMANDS_BIT : VK_PIPELINE_STAGE_TRANSFER_BIT,
                         VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, 0, 0, NULL, 0, NULL, source ? 2 : 1, release);
    GPU(vkEndCommandBuffer(s->command));
    GPU(vkResetFences(s->device, 1, &s->fence));
    VkPipelineStageFlags wait_stage = VK_PIPELINE_STAGE_TRANSFER_BIT;
    VkSubmitInfo submit = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .waitSemaphoreCount = ready ? 1 : 0,
        .pWaitSemaphores = &ready, .pWaitDstStageMask = &wait_stage, .commandBufferCount = 1,
        .pCommandBuffers = &s->command};
    GPU(vkQueueSubmit(s->queue, 1, &submit, s->fence));
    VkResult completed = vkWaitForFences(s->device, 1, &s->fence, VK_TRUE, 2000000000ull);
    if (completed != VK_SUCCESS) {
        fprintf(stderr, "Capture GPU fence failed (%d); exiting isolated worker\n", completed);
        _Exit(4); /* Never destroy/requeue memory still referenced by GPU work. */
    }
    /* Source read is complete before returning to PipeWire. No release sync FD
     * is needed for this synchronous path; the buffer has not been requeued. */
    if (s->sink) {
        if (s->shared_fd < 0) {
            VkMemoryGetFdInfoKHR export = {.sType = VK_STRUCTURE_TYPE_MEMORY_GET_FD_INFO_KHR,
                .memory = s->memory, .handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT};
            GPU(s->get_fd(s->device, &export, &s->shared_fd));
        }
        if (s->sink(s->sink_opaque, s->shared_fd, s->bytes, s->uuid, width, height, timestamp)) {
            fprintf(stderr, "GPU sink failed delivery/acknowledgment; refusing allocation reuse\n");
            goto fail;
        }
    } else {
        if (!s->encoder) {
            VkMemoryGetFdInfoKHR export = {.sType = VK_STRUCTURE_TYPE_MEMORY_GET_FD_INFO_KHR,
                .memory = s->memory, .handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT};
            int encoded_fd = -1;
            GPU(s->get_fd(s->device, &export, &encoded_fd));
            s->encoder = uurb_encoder_open(encoded_fd, s->bytes, s->uuid, width, height, s->hevc, 20000000);
            if (!s->encoder) goto fail;
        }
        struct uurb_packet packet;
        if (uurb_encoder_encode(s->encoder, timestamp, s->frames == 0, &packet)) goto fail;
        /* Bound the diagnostic worker itself, not just its later decoder. */
        int written = packet.size > 64u * 1024u * 1024u - s->written_bytes;
        if (written) fprintf(stderr, "Encoded diagnostic exceeds 64 MiB limit\n");
        else written = write_packet(s->output_fd, packet.data, packet.size);
        if (!written) s->written_bytes += packet.size;
        if (uurb_encoder_release_packet(s->encoder) || written) goto fail;
    }
    s->frames++;
    if (!clock_gettime(CLOCK_MONOTONIC, &end_time)) {
        uint64_t elapsed = ((uint64_t)end_time.tv_sec * 1000000000ull + end_time.tv_nsec) -
                           ((uint64_t)begin_time.tv_sec * 1000000000ull + begin_time.tv_nsec);
        s->elapsed_ns += elapsed;
        if (elapsed > s->maximum_ns) s->maximum_ns = elapsed;
    }
    result = 0;
fail:
    if (result) s->failed = 1;
    if (sync_fd >= 0) close(sync_fd);
    if (ready) vkDestroySemaphore(s->device, ready, NULL);
    return result;
}

int uurb_capture_encoder_frame(struct uurb_capture_encoder *s, const void *buffer_key, int fd, uint32_t width, uint32_t height,
    uint32_t fourcc, uint64_t modifier, uint32_t offset, uint32_t stride, uint64_t timestamp)
{
    if (!buffer_key) return 1;
    return capture_frame(s, buffer_key, fd, width, height, fourcc, modifier, offset, stride, timestamp);
}

int uurb_capture_encoder_cursor_frame(struct uurb_capture_encoder *s, uint64_t timestamp)
{
    if (!s) return 1;
    return capture_frame(s, NULL, -1, s->width, s->height, 0, 0, 0, 0, timestamp);
}

int uurb_capture_encoder_close(struct uurb_capture_encoder *s)
{
    if (!s) return 0;
    if (uurb_encoder_close(s->encoder)) {
        fprintf(stderr, "CUDA/NVENC cleanup failed; exiting isolated capture worker\n"); _Exit(4);
    }
    uurb_vk_cursor_composite_close(s->composite);
    if (s->output) vkDestroyImage(s->device, s->output, NULL);
    if (s->memory) vkFreeMemory(s->device, s->memory, NULL);
    if (s->shared_fd >= 0) close(s->shared_fd);
    if (s->fence) vkDestroyFence(s->device, s->fence, NULL);
    if (s->pool) vkDestroyCommandPool(s->device, s->pool, NULL);
    for (unsigned i = 0; i < 16; i++)
        if (s->inputs[i].key) uurb_vk_import_image_close(s->importer, &s->inputs[i].image);
    uurb_vk_importer_close(s->importer);
    free(s);
    return 0;
}
