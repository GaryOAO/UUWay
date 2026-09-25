/* Import only a caller-held, single-plane DMA-BUF with explicit DRM metadata.
 * No mmap, staging, queue submission, pixels, or changes to external ownership.
 * The encoding consumer retains imports for each live PipeWire buffer. */
#define _GNU_SOURCE
#include "native_vk_dmabuf_import.h"
#include <libdrm/drm_fourcc.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

struct uurb_vk_importer {
    VkPhysicalDevice physical;
    VkDevice device;
    PFN_vkGetMemoryFdPropertiesKHR fd_properties;
    uint32_t family;
    VkQueue queue;
};

static struct uurb_vk_importer *open_importer(VkPhysicalDevice physical, int transfer)
{
    struct uurb_vk_importer *s = calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->physical = physical;
    uint32_t count = 0, family = UINT32_MAX;
    vkGetPhysicalDeviceQueueFamilyProperties(physical, &count, NULL);
    if (!count || count > 128) goto fail;
    VkQueueFamilyProperties queues[128];
    vkGetPhysicalDeviceQueueFamilyProperties(physical, &count, queues);
    for (unsigned i = 0; i < count; i++) {
        if (queues[i].queueCount && queues[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) { family = i; break; }
    }
    if (family == UINT32_MAX) goto fail;
    float priority = 0.5f;
    VkDeviceQueueCreateInfo queue = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
        .queueFamilyIndex = family, .queueCount = 1, .pQueuePriorities = &priority};
    const char *extensions[] = {VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME,
        VK_EXT_EXTERNAL_MEMORY_DMA_BUF_EXTENSION_NAME, VK_EXT_IMAGE_DRM_FORMAT_MODIFIER_EXTENSION_NAME,
        VK_KHR_IMAGE_FORMAT_LIST_EXTENSION_NAME,
        VK_EXT_QUEUE_FAMILY_FOREIGN_EXTENSION_NAME, VK_KHR_EXTERNAL_SEMAPHORE_FD_EXTENSION_NAME};
    VkDeviceCreateInfo create = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
        .queueCreateInfoCount = 1, .pQueueCreateInfos = &queue,
        .enabledExtensionCount = transfer ? 6 : 4, .ppEnabledExtensionNames = extensions};
    VkResult status = vkCreateDevice(physical, &create, NULL, &s->device);
    if (status != VK_SUCCESS) { fprintf(stderr, "Vulkan DMA-BUF device: %d\n", status); goto fail; }
    s->fd_properties = (void *)vkGetDeviceProcAddr(s->device, "vkGetMemoryFdPropertiesKHR");
    if (!s->fd_properties) goto fail;
    s->family = family;
    vkGetDeviceQueue(s->device, family, 0, &s->queue);
    return s;
fail:
    uurb_vk_importer_close(s);
    return NULL;
}

struct uurb_vk_importer *uurb_vk_importer_open(VkPhysicalDevice physical)
{ return open_importer(physical, 0); }
struct uurb_vk_importer *uurb_vk_importer_open_transfer(VkPhysicalDevice physical)
{ return open_importer(physical, 1); }
void uurb_vk_importer_context(struct uurb_vk_importer *s, VkDevice *device, VkQueue *queue, uint32_t *family)
{ *device = s->device; *queue = s->queue; *family = s->family; }

int uurb_vk_import_image(struct uurb_vk_importer *s, int fd, uint32_t width, uint32_t height,
                        uint32_t fourcc, uint64_t modifier, uint32_t offset, uint32_t stride,
                        VkImageUsageFlags usage, struct uurb_vk_image *output)
{
    if (!output) return 1;
    *output = (struct uurb_vk_image){0};
    if (!s || fd < 0 || !width || !height || width > 8192 || height > 8192 || stride < width * 4)
        return 1;
    VkFormat format;
    switch (fourcc) {
    case DRM_FORMAT_ARGB8888: case DRM_FORMAT_XRGB8888: format = VK_FORMAT_B8G8R8A8_UNORM; break;
    case DRM_FORMAT_ABGR8888: case DRM_FORMAT_XBGR8888: format = VK_FORMAT_R8G8B8A8_UNORM; break;
    default: return 1;
    }
    if (usage & VK_IMAGE_USAGE_TRANSFER_SRC_BIT) {
        VkDrmFormatModifierPropertiesListEXT modifiers = {
            .sType = VK_STRUCTURE_TYPE_DRM_FORMAT_MODIFIER_PROPERTIES_LIST_EXT};
        VkFormatProperties2 features = {.sType = VK_STRUCTURE_TYPE_FORMAT_PROPERTIES_2, .pNext = &modifiers};
        vkGetPhysicalDeviceFormatProperties2(s->physical, format, &features);
        if (!modifiers.drmFormatModifierCount || modifiers.drmFormatModifierCount > 128) return 1;
        VkDrmFormatModifierPropertiesEXT entries[128];
        modifiers.pDrmFormatModifierProperties = entries;
        vkGetPhysicalDeviceFormatProperties2(s->physical, format, &features);
        int supported = 0;
        for (unsigned i = 0; i < modifiers.drmFormatModifierCount; i++)
            if (entries[i].drmFormatModifier == modifier && entries[i].drmFormatModifierPlaneCount == 1 &&
                (entries[i].drmFormatModifierTilingFeatures & VK_FORMAT_FEATURE_BLIT_SRC_BIT)) supported = 1;
        if (!supported) { fprintf(stderr, "DMA-BUF modifier cannot be used as a single-plane blit source\n"); return 1; }
    }
    VkPhysicalDeviceImageDrmFormatModifierInfoEXT drm = {
        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_DRM_FORMAT_MODIFIER_INFO_EXT,
        .drmFormatModifier = modifier, .sharingMode = VK_SHARING_MODE_EXCLUSIVE};
    VkPhysicalDeviceExternalImageFormatInfo external = {
        .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_IMAGE_FORMAT_INFO,
        .pNext = &drm, .handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT};
    VkPhysicalDeviceImageFormatInfo2 query = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_FORMAT_INFO_2,
        .pNext = &external, .format = format, .type = VK_IMAGE_TYPE_2D,
        .tiling = VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT, .usage = usage};
    VkExternalImageFormatProperties capabilities = {.sType = VK_STRUCTURE_TYPE_EXTERNAL_IMAGE_FORMAT_PROPERTIES};
    VkImageFormatProperties2 properties = {.sType = VK_STRUCTURE_TYPE_IMAGE_FORMAT_PROPERTIES_2, .pNext = &capabilities};
    if (vkGetPhysicalDeviceImageFormatProperties2(s->physical, &query, &properties) != VK_SUCCESS ||
        !(capabilities.externalMemoryProperties.externalMemoryFeatures & VK_EXTERNAL_MEMORY_FEATURE_IMPORTABLE_BIT))
        return 1;
    int result = 1, imported_fd = -1;
    VkImage image = VK_NULL_HANDLE;
    VkDeviceMemory memory = VK_NULL_HANDLE;
    VkSubresourceLayout plane = {.offset = offset, .rowPitch = stride};
    VkImageDrmFormatModifierExplicitCreateInfoEXT layout = {
        .sType = VK_STRUCTURE_TYPE_IMAGE_DRM_FORMAT_MODIFIER_EXPLICIT_CREATE_INFO_EXT,
        .drmFormatModifier = modifier, .drmFormatModifierPlaneCount = 1, .pPlaneLayouts = &plane};
    VkExternalMemoryImageCreateInfo external_image = {.sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO,
        .pNext = &layout, .handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT};
    VkImageCreateInfo create = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, .pNext = &external_image,
        .imageType = VK_IMAGE_TYPE_2D, .format = format, .extent = {width, height, 1},
        .mipLevels = 1, .arrayLayers = 1, .samples = VK_SAMPLE_COUNT_1_BIT,
        .tiling = VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT, .usage = usage,
        .sharingMode = VK_SHARING_MODE_EXCLUSIVE, .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED};
#define IMPORT_OK(call) do { VkResult status = (call); if (status != VK_SUCCESS) { \
    fprintf(stderr, "%s: %d\n", #call, status); goto done; } } while (0)
    IMPORT_OK(vkCreateImage(s->device, &create, NULL, &image));
    VkMemoryRequirements requirements;
    vkGetImageMemoryRequirements(s->device, image, &requirements);
    VkMemoryFdPropertiesKHR fd_properties = {.sType = VK_STRUCTURE_TYPE_MEMORY_FD_PROPERTIES_KHR};
    IMPORT_OK(s->fd_properties(s->device, VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT, fd, &fd_properties));
    uint32_t compatible = requirements.memoryTypeBits & fd_properties.memoryTypeBits;
    if (!compatible) goto done;
    uint32_t memory_type = 0;
    while (!(compatible & (1u << memory_type))) memory_type++;
    imported_fd = fcntl(fd, F_DUPFD_CLOEXEC, 3);
    if (imported_fd < 0) goto done;
    VkMemoryDedicatedAllocateInfo dedicated = {.sType = VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO, .image = image};
    VkImportMemoryFdInfoKHR import = {.sType = VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR,
        .pNext = &dedicated, .handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_DMA_BUF_BIT_EXT, .fd = imported_fd};
    VkMemoryAllocateInfo allocate = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        .pNext = &import, .allocationSize = requirements.size, .memoryTypeIndex = memory_type};
    IMPORT_OK(vkAllocateMemory(s->device, &allocate, NULL, &memory));
    imported_fd = -1;  /* Vulkan owns the duplicate; caller's original stays valid. */
    IMPORT_OK(vkBindImageMemory(s->device, image, memory, 0));
    *output = (struct uurb_vk_image){image, memory};
    image = VK_NULL_HANDLE; memory = VK_NULL_HANDLE;
    result = 0;
done:
    if (image) vkDestroyImage(s->device, image, NULL);
    if (memory) vkFreeMemory(s->device, memory, NULL);
    if (imported_fd >= 0) close(imported_fd);
    return result;
}

void uurb_vk_import_image_close(struct uurb_vk_importer *s, struct uurb_vk_image *image)
{
    if (image->image) vkDestroyImage(s->device, image->image, NULL);
    if (image->memory) vkFreeMemory(s->device, image->memory, NULL);
    *image = (struct uurb_vk_image){0};
}

int uurb_vk_importer_check(struct uurb_vk_importer *s, int fd, uint32_t width, uint32_t height,
                           uint32_t fourcc, uint64_t modifier, uint32_t offset, uint32_t stride)
{
    struct uurb_vk_image image;
    int status = uurb_vk_import_image(s, fd, width, height, fourcc, modifier, offset, stride,
                                     VK_IMAGE_USAGE_SAMPLED_BIT, &image);
    if (!status) uurb_vk_import_image_close(s, &image);
    return status;
}

void uurb_vk_importer_close(struct uurb_vk_importer *s)
{
    if (!s) return;
    if (s->device) vkDestroyDevice(s->device, NULL);
    free(s);
}
