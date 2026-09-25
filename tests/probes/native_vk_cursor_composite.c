/* Only synthetic, fixture-owned images are read back. Never opens a desktop,
 * PipeWire, Portal, UU, or an external image FD. Real NVIDIA Vulkan execution. */
#include "native_vk_cursor_composite.h"
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define W 32
#define H 24
#define REQUIRE(x) do { if (!(x)) { fprintf(stderr, "fixture failed at line %d\n", __LINE__); exit(1); } } while (0)
#define VK(x) REQUIRE((x) == VK_SUCCESS)
static unsigned validation_errors;
static VKAPI_ATTR VkBool32 VKAPI_CALL debug(VkDebugUtilsMessageSeverityFlagBitsEXT severity,
    VkDebugUtilsMessageTypeFlagsEXT type, const VkDebugUtilsMessengerCallbackDataEXT *data, void *opaque)
{
    (void)type; (void)opaque;
    if (severity & VK_DEBUG_UTILS_MESSAGE_SEVERITY_ERROR_BIT_EXT) {
        validation_errors++; fprintf(stderr, "fixture Vulkan validation: %s\n", data->pMessage);
    }
    return VK_FALSE;
}

static uint32_t type_index(VkPhysicalDevice physical, uint32_t bits, VkMemoryPropertyFlags flags)
{
    VkPhysicalDeviceMemoryProperties types; vkGetPhysicalDeviceMemoryProperties(physical, &types);
    for (uint32_t i = 0; i < types.memoryTypeCount; ++i)
        if ((bits & (1u << i)) && (types.memoryTypes[i].propertyFlags & flags) == flags) return i;
    REQUIRE(0); return 0;
}

static void image_create(VkPhysicalDevice physical, VkDevice device, VkFormat format,
    VkImageUsageFlags usage, VkImage *image, VkDeviceMemory *memory)
{
    VkImageCreateInfo create = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,
        .imageType = VK_IMAGE_TYPE_2D, .format = format, .extent = {W,H,1},
        .mipLevels = 1, .arrayLayers = 1, .samples = VK_SAMPLE_COUNT_1_BIT,
        .tiling = VK_IMAGE_TILING_OPTIMAL, .usage = usage, .sharingMode = VK_SHARING_MODE_EXCLUSIVE};
    VK(vkCreateImage(device, &create, NULL, image));
    VkMemoryRequirements requirements; vkGetImageMemoryRequirements(device, *image, &requirements);
    VkMemoryAllocateInfo allocate = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        .allocationSize = requirements.size,
        .memoryTypeIndex = type_index(physical, requirements.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)};
    VK(vkAllocateMemory(device, &allocate, NULL, memory)); VK(vkBindImageMemory(device, *image, *memory, 0));
}

static void transition(VkCommandBuffer command, VkImage image, VkImageLayout old, VkImageLayout next)
{
    VkImageMemoryBarrier barrier = {.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
        .srcAccessMask = old == VK_IMAGE_LAYOUT_UNDEFINED ? 0 : VK_ACCESS_MEMORY_READ_BIT | VK_ACCESS_MEMORY_WRITE_BIT,
        .dstAccessMask = VK_ACCESS_MEMORY_READ_BIT | VK_ACCESS_MEMORY_WRITE_BIT,
        .oldLayout = old, .newLayout = next, .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
        .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .image = image,
        .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1}};
    vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,
        0, 0, NULL, 0, NULL, 1, &barrier);
}

static void verify(const uint8_t *pixels, const struct uurb_cursor_snapshot *cursor, const uint8_t bg[4])
{
    const struct uurb_cursor_header *h = &cursor->header;
    for (int y = 0; y < H; ++y) for (int x = 0; x < W; ++x) {
        int64_t cx = x - ((int64_t)h->x - h->hotspot_x), cy = y - ((int64_t)h->y - h->hotspot_y);
        const uint8_t *sprite = h->active && h->visible && cx >= 0 && cy >= 0 && cx < h->width && cy < h->height ?
            &cursor->pixels[(cy * h->width + cx) * 4] : NULL;
        for (int c = 0; c < 4; ++c) {
            int expected = sprite ? sprite[c] + (bg[c] * (255 - sprite[3]) + 127) / 255 : bg[c];
            int got = pixels[(y * W + x) * 4 + c];
            REQUIRE(abs(expected - got) <= 1);
        }
    }
}

int main(void)
{
    const char *layer = "VK_LAYER_KHRONOS_validation", *extension = VK_EXT_DEBUG_UTILS_EXTENSION_NAME;
    VkValidationFeatureEnableEXT enable = VK_VALIDATION_FEATURE_ENABLE_SYNCHRONIZATION_VALIDATION_EXT;
    VkValidationFeaturesEXT validation = {.sType = VK_STRUCTURE_TYPE_VALIDATION_FEATURES_EXT,
        .enabledValidationFeatureCount = 1, .pEnabledValidationFeatures = &enable};
    VkDebugUtilsMessengerCreateInfoEXT messenger = {.sType = VK_STRUCTURE_TYPE_DEBUG_UTILS_MESSENGER_CREATE_INFO_EXT,
        .pNext = &validation, .messageSeverity = VK_DEBUG_UTILS_MESSAGE_SEVERITY_ERROR_BIT_EXT,
        .messageType = VK_DEBUG_UTILS_MESSAGE_TYPE_VALIDATION_BIT_EXT | VK_DEBUG_UTILS_MESSAGE_TYPE_GENERAL_BIT_EXT,
        .pfnUserCallback = debug};
    VkApplicationInfo application = {.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .apiVersion = VK_API_VERSION_1_1};
    VkInstanceCreateInfo instance_info = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pNext = &messenger,
        .pApplicationInfo = &application, .enabledLayerCount = 1, .ppEnabledLayerNames = &layer,
        .enabledExtensionCount = 1, .ppEnabledExtensionNames = &extension};
    VkInstance instance; VK(vkCreateInstance(&instance_info, NULL, &instance));
    PFN_vkCreateDebugUtilsMessengerEXT create_debug = (void *)vkGetInstanceProcAddr(instance, "vkCreateDebugUtilsMessengerEXT");
    PFN_vkDestroyDebugUtilsMessengerEXT destroy_debug = (void *)vkGetInstanceProcAddr(instance, "vkDestroyDebugUtilsMessengerEXT");
    VkDebugUtilsMessengerEXT handle; messenger.pNext = NULL;
    REQUIRE(create_debug && destroy_debug); VK(create_debug(instance, &messenger, NULL, &handle));
    uint32_t count = 0; VK(vkEnumeratePhysicalDevices(instance, &count, NULL)); REQUIRE(count && count <= 16);
    VkPhysicalDevice devices[16], physical = VK_NULL_HANDLE; VK(vkEnumeratePhysicalDevices(instance, &count, devices));
    for (uint32_t i = 0; i < count; ++i) {
        VkPhysicalDeviceProperties properties; vkGetPhysicalDeviceProperties(devices[i], &properties);
        if (properties.vendorID == 0x10de && properties.deviceType == VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU) physical = devices[i];
    }
    REQUIRE(physical);
    vkGetPhysicalDeviceQueueFamilyProperties(physical, &count, NULL); REQUIRE(count && count <= 128);
    VkQueueFamilyProperties families[128]; vkGetPhysicalDeviceQueueFamilyProperties(physical, &count, families);
    uint32_t family = UINT32_MAX;
    for (uint32_t i = 0; i < count; ++i) if (families[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) { family = i; break; }
    REQUIRE(family != UINT32_MAX);
    float priority = 0.1f;
    VkDeviceQueueCreateInfo queue_info = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
        .queueFamilyIndex = family, .queueCount = 1, .pQueuePriorities = &priority};
    VkDeviceCreateInfo device_info = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
        .queueCreateInfoCount = 1, .pQueueCreateInfos = &queue_info};
    VkDevice device; VK(vkCreateDevice(physical, &device_info, NULL, &device));
    VkQueue queue; vkGetDeviceQueue(device, family, 0, &queue);
    VkImage source, output; VkDeviceMemory source_memory, output_memory;
    image_create(physical, device, VK_FORMAT_R8G8B8A8_UNORM,
        VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT, &source, &source_memory);
    image_create(physical, device, VK_FORMAT_B8G8R8A8_UNORM,
        VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT,
        &output, &output_memory);
    VkBuffer readback; VkDeviceMemory readback_memory;
    VkBufferCreateInfo buffer = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = W * H * 4,
        .usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT, .sharingMode = VK_SHARING_MODE_EXCLUSIVE};
    VK(vkCreateBuffer(device, &buffer, NULL, &readback));
    VkMemoryRequirements requirements; vkGetBufferMemoryRequirements(device, readback, &requirements);
    VkMemoryAllocateInfo allocate = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        .allocationSize = requirements.size, .memoryTypeIndex = type_index(physical, requirements.memoryTypeBits,
            VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)};
    VK(vkAllocateMemory(device, &allocate, NULL, &readback_memory)); VK(vkBindBufferMemory(device, readback, readback_memory, 0));
    void *pixels; VK(vkMapMemory(device, readback_memory, 0, W * H * 4, 0, &pixels));
    VkCommandPool pool;
    VkCommandPoolCreateInfo pool_info = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
        .queueFamilyIndex = family, .flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT};
    VK(vkCreateCommandPool(device, &pool_info, NULL, &pool));
    VkCommandBuffer command;
    VkCommandBufferAllocateInfo command_info = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
        .commandPool = pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1};
    VK(vkAllocateCommandBuffers(device, &command_info, &command));
    VkFence fence; VkFenceCreateInfo fence_info = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    VK(vkCreateFence(device, &fence_info, NULL, &fence));
    struct uurb_vk_cursor_composite *composite = uurb_vk_cursor_composite_open(physical, device, output, W, H);
    REQUIRE(composite);
    struct uurb_cursor_snapshot *cursor = calloc(1, sizeof(*cursor)); REQUIRE(cursor);
    cursor->header = (struct uurb_cursor_header){.magic = UURB_CURSOR_MAGIC, .version = 1,
        .generation = 1, .active = 1, .visible = 1, .shape_serial = 1, .width = 3, .height = 2,
        .hotspot_x = 1, .hotspot_y = 1, .x = 8, .y = 7};
    const uint8_t sprite[] = {0,0,255,255, 0,128,0,128, 0,0,0,0,
                             255,0,0,255, 64,0,64,64, 50,70,90,255};
    memcpy(cursor->pixels, sprite, sizeof(sprite));
    uint8_t bg[] = {31, 63, 95, 255};
    unsigned completed = 0;
    for (unsigned step = 0; step < 10; ++step) {
        if (step == 1) cursor->header.x = 18; // Move only; no new desktop buffer.
        if (step == 2) cursor->header.visible = 0; // Erase old cursor from clean background.
        if (step == 3) { cursor->header.visible = 1; cursor->header.x = 0; cursor->header.y = 0; }
        if (step == 4) { cursor->header.x = INT_MIN; cursor->header.y = INT_MAX; }
        if (step == 5) { cursor->header.x = 15; cursor->header.y = 10; cursor->header.generation++;
            cursor->pixels[0] = 255; cursor->pixels[2] = 0; }
        if (step == 6) { cursor->header.shape_serial++; cursor->pixels[4] = 128; cursor->pixels[5] = 0; }
        if (step == 7) { bg[0] = 81; bg[1] = 23; bg[2] = 51; }
        if (step == 8) { cursor->header.visible = 0; cursor->header.active = 0; }
        if (step == 9) { cursor->header.visible = 1; cursor->header.active = 1;
            cursor->header.x = W; cursor->header.y = H; }
        VK(vkResetCommandBuffer(command, 0));
        VkCommandBufferBeginInfo begin = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
        VK(vkBeginCommandBuffer(command, &begin));
        if (step == 0) REQUIRE(uurb_vk_cursor_composite_record(composite, command, VK_NULL_HANDLE, cursor) != 0);
        // Rejected metadata must not poison clean-background state.
        cursor->header.sequence++;
        REQUIRE(uurb_vk_cursor_composite_record(composite, command, source, cursor) != 0);
        cursor->header.sequence--;
        if (step == 0 || step == 7) {
            transition(command, source, step ? VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL : VK_IMAGE_LAYOUT_UNDEFINED,
                VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL);
            VkClearColorValue color = {.float32 = {bg[2] / 255.0f, bg[1] / 255.0f, bg[0] / 255.0f, 1}};
            VkImageSubresourceRange range = {VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1};
            vkCmdClearColorImage(command, source, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &color, 1, &range);
            transition(command, source, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
        }
        transition(command, output, step ? VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL : VK_IMAGE_LAYOUT_UNDEFINED,
            VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL);
        REQUIRE(!uurb_vk_cursor_composite_record(composite, command, step == 0 || step == 7 ? source : VK_NULL_HANDLE, cursor));
        transition(command, output, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
        VkBufferImageCopy copy = {.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT,0,0,1}, .imageExtent = {W,H,1}};
        vkCmdCopyImageToBuffer(command, output, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, readback, 1, &copy);
        VkMemoryBarrier host = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
            .srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT, .dstAccessMask = VK_ACCESS_HOST_READ_BIT};
        vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT,
            0, 1, &host, 0, NULL, 0, NULL);
        VK(vkEndCommandBuffer(command)); VK(vkResetFences(device, 1, &fence));
        VkSubmitInfo submit = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1, .pCommandBuffers = &command};
        VK(vkQueueSubmit(queue, 1, &submit, fence)); VK(vkWaitForFences(device, 1, &fence, VK_TRUE, 2000000000ull));
        verify(pixels, cursor, bg); completed++;
        REQUIRE(uurb_vk_cursor_composite_uploads(composite) == (step < 5 ? 1 : step < 6 ? 2 : 3));
    }
    free(cursor); uurb_vk_cursor_composite_close(composite);
    vkDestroyFence(device, fence, NULL); vkDestroyCommandPool(device, pool, NULL);
    vkUnmapMemory(device, readback_memory); vkDestroyBuffer(device, readback, NULL); vkFreeMemory(device, readback_memory, NULL);
    vkDestroyImage(device, source, NULL); vkFreeMemory(device, source_memory, NULL);
    vkDestroyImage(device, output, NULL); vkFreeMemory(device, output_memory, NULL);
    vkDestroyDevice(device, NULL); destroy_debug(instance, handle, NULL); vkDestroyInstance(instance, NULL);
    REQUIRE(!validation_errors);
    printf("{\"nvidia_gpu\":true,\"synthetic_frames_verified\":%u,\"cursor_uploads\":3,\"validation_errors\":%u,\"desktop_opened\":false,\"uu_phone_tested\":false}\n", completed, validation_errors);
    return 0;
}
