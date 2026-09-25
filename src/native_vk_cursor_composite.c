/* Desktop pixels stay in device-local images. Only bounded cursor metadata is
 * host mapped. One clean background is retained for cursor-only source events. */
#include "native_vk_cursor_composite.h"
#include <stdlib.h>
#include <string.h>
#include "native_cursor_vert.h"
#include "native_cursor_frag.h"

struct uurb_vk_cursor_composite {
    VkDevice device;
    VkImage output, background;
    VkDeviceMemory background_memory, upload_memory;
    VkBuffer upload;
    void *mapped;
    VkImageView view;
    VkRenderPass render;
    VkFramebuffer framebuffer;
    VkDescriptorSetLayout descriptor_layout;
    VkDescriptorPool descriptor_pool;
    VkDescriptorSet descriptor;
    VkPipelineLayout layout;
    VkPipeline pipeline;
    uint32_t width, height, generation, serial, sprite_width, sprite_height;
    unsigned uploads;
    int background_valid;
};

#define CHECK(call) do { if ((call) != VK_SUCCESS) goto fail; } while (0)
static uint32_t memory_type(VkPhysicalDevice physical, uint32_t bits, VkMemoryPropertyFlags flags)
{
    VkPhysicalDeviceMemoryProperties types;
    vkGetPhysicalDeviceMemoryProperties(physical, &types);
    for (uint32_t i = 0; i < types.memoryTypeCount; ++i)
        if ((bits & (1u << i)) && (types.memoryTypes[i].propertyFlags & flags) == flags) return i;
    return UINT32_MAX;
}

struct uurb_vk_cursor_composite *uurb_vk_cursor_composite_open(
    VkPhysicalDevice physical, VkDevice device, VkImage output, uint32_t width, uint32_t height)
{
    if (!physical || !device || !output || !width || !height || width > 8192 || height > 8192) return NULL;
    VkFormatProperties format;
    vkGetPhysicalDeviceFormatProperties(physical, VK_FORMAT_B8G8R8A8_UNORM, &format);
    VkFormatFeatureFlags required = VK_FORMAT_FEATURE_BLIT_DST_BIT |
        VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BIT | VK_FORMAT_FEATURE_COLOR_ATTACHMENT_BLEND_BIT;
    VkPhysicalDeviceProperties properties;
    vkGetPhysicalDeviceProperties(physical, &properties);
    if ((format.optimalTilingFeatures & required) != required ||
        properties.limits.maxStorageBufferRange < UURB_CURSOR_PIXEL_BYTES ||
        properties.limits.maxPushConstantsSize < 24) return NULL;
    struct uurb_vk_cursor_composite *s = calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->device = device; s->output = output; s->width = width; s->height = height;
    VkShaderModule vertex = VK_NULL_HANDLE, fragment = VK_NULL_HANDLE;
    VkImageCreateInfo image = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO,
        .imageType = VK_IMAGE_TYPE_2D, .format = VK_FORMAT_B8G8R8A8_UNORM,
        .extent = {width, height, 1}, .mipLevels = 1, .arrayLayers = 1,
        .samples = VK_SAMPLE_COUNT_1_BIT, .tiling = VK_IMAGE_TILING_OPTIMAL,
        .usage = VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT,
        .sharingMode = VK_SHARING_MODE_EXCLUSIVE, .initialLayout = VK_IMAGE_LAYOUT_UNDEFINED};
    CHECK(vkCreateImage(device, &image, NULL, &s->background));
    VkMemoryRequirements needs;
    vkGetImageMemoryRequirements(device, s->background, &needs);
    uint32_t type = memory_type(physical, needs.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    if (type == UINT32_MAX) goto fail;
    VkMemoryAllocateInfo allocate = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
        .allocationSize = needs.size, .memoryTypeIndex = type};
    CHECK(vkAllocateMemory(device, &allocate, NULL, &s->background_memory));
    CHECK(vkBindImageMemory(device, s->background, s->background_memory, 0));
    VkBufferCreateInfo buffer = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
        .size = UURB_CURSOR_PIXEL_BYTES, .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,
        .sharingMode = VK_SHARING_MODE_EXCLUSIVE};
    CHECK(vkCreateBuffer(device, &buffer, NULL, &s->upload));
    vkGetBufferMemoryRequirements(device, s->upload, &needs);
    type = memory_type(physical, needs.memoryTypeBits,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    if (type == UINT32_MAX) goto fail;
    allocate.allocationSize = needs.size; allocate.memoryTypeIndex = type;
    CHECK(vkAllocateMemory(device, &allocate, NULL, &s->upload_memory));
    CHECK(vkBindBufferMemory(device, s->upload, s->upload_memory, 0));
    CHECK(vkMapMemory(device, s->upload_memory, 0, UURB_CURSOR_PIXEL_BYTES, 0, &s->mapped));
    VkImageViewCreateInfo view = {.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO,
        .image = output, .viewType = VK_IMAGE_VIEW_TYPE_2D, .format = VK_FORMAT_B8G8R8A8_UNORM,
        .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    CHECK(vkCreateImageView(device, &view, NULL, &s->view));
    VkAttachmentDescription attachment = {.format = VK_FORMAT_B8G8R8A8_UNORM,
        .samples = VK_SAMPLE_COUNT_1_BIT, .loadOp = VK_ATTACHMENT_LOAD_OP_LOAD,
        .storeOp = VK_ATTACHMENT_STORE_OP_STORE, .stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE,
        .stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE,
        .initialLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
        .finalLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};
    VkAttachmentReference reference = {0, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL};
    VkSubpassDescription subpass = {.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS,
        .colorAttachmentCount = 1, .pColorAttachments = &reference};
    VkRenderPassCreateInfo render = {.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO,
        .attachmentCount = 1, .pAttachments = &attachment, .subpassCount = 1, .pSubpasses = &subpass};
    CHECK(vkCreateRenderPass(device, &render, NULL, &s->render));
    VkFramebufferCreateInfo framebuffer = {.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO,
        .renderPass = s->render, .attachmentCount = 1, .pAttachments = &s->view,
        .width = width, .height = height, .layers = 1};
    CHECK(vkCreateFramebuffer(device, &framebuffer, NULL, &s->framebuffer));
    VkDescriptorSetLayoutBinding binding = {.binding = 0, .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
        .descriptorCount = 1, .stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT};
    VkDescriptorSetLayoutCreateInfo descriptor = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
        .bindingCount = 1, .pBindings = &binding};
    CHECK(vkCreateDescriptorSetLayout(device, &descriptor, NULL, &s->descriptor_layout));
    VkDescriptorPoolSize pool_size = {VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1};
    VkDescriptorPoolCreateInfo pool = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,
        .maxSets = 1, .poolSizeCount = 1, .pPoolSizes = &pool_size};
    CHECK(vkCreateDescriptorPool(device, &pool, NULL, &s->descriptor_pool));
    VkDescriptorSetAllocateInfo set = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,
        .descriptorPool = s->descriptor_pool, .descriptorSetCount = 1, .pSetLayouts = &s->descriptor_layout};
    CHECK(vkAllocateDescriptorSets(device, &set, &s->descriptor));
    VkDescriptorBufferInfo buffer_info = {s->upload, 0, UURB_CURSOR_PIXEL_BYTES};
    VkWriteDescriptorSet write = {.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,
        .dstSet = s->descriptor, .dstBinding = 0, .descriptorCount = 1,
        .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, .pBufferInfo = &buffer_info};
    vkUpdateDescriptorSets(device, 1, &write, 0, NULL);
    VkPushConstantRange push = {VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT, 0, 24};
    VkPipelineLayoutCreateInfo layout = {.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
        .setLayoutCount = 1, .pSetLayouts = &s->descriptor_layout,
        .pushConstantRangeCount = 1, .pPushConstantRanges = &push};
    CHECK(vkCreatePipelineLayout(device, &layout, NULL, &s->layout));
    VkShaderModuleCreateInfo shader = {.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
        .codeSize = sizeof(uurb_cursor_vert_spv), .pCode = uurb_cursor_vert_spv};
    CHECK(vkCreateShaderModule(device, &shader, NULL, &vertex));
    shader.codeSize = sizeof(uurb_cursor_frag_spv); shader.pCode = uurb_cursor_frag_spv;
    CHECK(vkCreateShaderModule(device, &shader, NULL, &fragment));
    VkPipelineShaderStageCreateInfo stages[2] = {
        {.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
         .stage = VK_SHADER_STAGE_VERTEX_BIT, .module = vertex, .pName = "main"},
        {.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
         .stage = VK_SHADER_STAGE_FRAGMENT_BIT, .module = fragment, .pName = "main"}};
    VkPipelineVertexInputStateCreateInfo vertex_input = {.sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO};
    VkPipelineInputAssemblyStateCreateInfo assembly = {.sType = VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO,
        .topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST};
    VkViewport viewport = {0, 0, width, height, 0, 1};
    VkRect2D scissor = {{0, 0}, {width, height}};
    VkPipelineViewportStateCreateInfo viewport_state = {.sType = VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO,
        .viewportCount = 1, .pViewports = &viewport, .scissorCount = 1, .pScissors = &scissor};
    VkPipelineRasterizationStateCreateInfo raster = {.sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO,
        .polygonMode = VK_POLYGON_MODE_FILL, .cullMode = VK_CULL_MODE_NONE, .lineWidth = 1};
    VkPipelineMultisampleStateCreateInfo multisample = {.sType = VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO,
        .rasterizationSamples = VK_SAMPLE_COUNT_1_BIT};
    VkPipelineColorBlendAttachmentState blend = {.blendEnable = VK_TRUE,
        .srcColorBlendFactor = VK_BLEND_FACTOR_ONE, .dstColorBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA,
        .colorBlendOp = VK_BLEND_OP_ADD, .srcAlphaBlendFactor = VK_BLEND_FACTOR_ONE,
        .dstAlphaBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA, .alphaBlendOp = VK_BLEND_OP_ADD,
        .colorWriteMask = VK_COLOR_COMPONENT_R_BIT | VK_COLOR_COMPONENT_G_BIT |
            VK_COLOR_COMPONENT_B_BIT | VK_COLOR_COMPONENT_A_BIT};
    VkPipelineColorBlendStateCreateInfo blend_state = {.sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO,
        .attachmentCount = 1, .pAttachments = &blend};
    VkGraphicsPipelineCreateInfo pipeline = {.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO,
        .stageCount = 2, .pStages = stages, .pVertexInputState = &vertex_input,
        .pInputAssemblyState = &assembly, .pViewportState = &viewport_state,
        .pRasterizationState = &raster, .pMultisampleState = &multisample,
        .pColorBlendState = &blend_state, .layout = s->layout, .renderPass = s->render};
    CHECK(vkCreateGraphicsPipelines(device, VK_NULL_HANDLE, 1, &pipeline, NULL, &s->pipeline));
    vkDestroyShaderModule(device, vertex, NULL); vkDestroyShaderModule(device, fragment, NULL);
    return s;
fail:
    if (vertex) vkDestroyShaderModule(device, vertex, NULL);
    if (fragment) vkDestroyShaderModule(device, fragment, NULL);
    uurb_vk_cursor_composite_close(s);
    return NULL;
}

static void transition(VkCommandBuffer command, VkImage image, VkImageLayout old, VkImageLayout next,
    VkPipelineStageFlags src_stage, VkPipelineStageFlags dst_stage, VkAccessFlags src, VkAccessFlags dst)
{
    VkImageMemoryBarrier barrier = {.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
        .srcAccessMask = src, .dstAccessMask = dst, .oldLayout = old, .newLayout = next,
        .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
        .image = image, .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
    vkCmdPipelineBarrier(command, src_stage, dst_stage, 0, 0, NULL, 0, NULL, 1, &barrier);
}

int uurb_vk_cursor_composite_record(struct uurb_vk_cursor_composite *s, VkCommandBuffer command,
    VkImage source, const struct uurb_cursor_snapshot *cursor)
{
    if (!s || !command || !cursor || (!source && !s->background_valid) || source == s->output) return 1;
    const struct uurb_cursor_header *h = &cursor->header;
    if (h->magic != UURB_CURSOR_MAGIC || h->version != UURB_CURSOR_VERSION ||
        (h->sequence & 1) || !h->generation || h->active > 1 || h->visible > 1 ||
        (h->visible && !h->active) || h->width > UURB_CURSOR_MAX_SIDE || h->height > UURB_CURSOR_MAX_SIDE) return 1;
    int draw = h->active && h->visible;
    if (draw && (!h->width || !h->height || !h->shape_serial || h->hotspot_x < 0 || h->hotspot_y < 0 ||
        h->hotspot_x >= (int32_t)h->width || h->hotspot_y >= (int32_t)h->height)) return 1;
    int64_t x = (int64_t)h->x - h->hotspot_x, y = (int64_t)h->y - h->hotspot_y;
    if (x >= s->width || y >= s->height || x + h->width <= 0 || y + h->height <= 0) draw = 0;
    if (draw && (s->generation != h->generation || s->serial != h->shape_serial ||
        s->sprite_width != h->width || s->sprite_height != h->height)) {
        memcpy(s->mapped, cursor->pixels, (size_t)h->width * h->height * 4);
        s->generation = h->generation; s->serial = h->shape_serial;
        s->sprite_width = h->width; s->sprite_height = h->height; s->uploads++;
    }
    if (source) {
        transition(command, s->background,
            s->background_valid ? VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL : VK_IMAGE_LAYOUT_UNDEFINED,
            VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
            s->background_valid ? VK_PIPELINE_STAGE_TRANSFER_BIT : VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
            VK_PIPELINE_STAGE_TRANSFER_BIT, s->background_valid ? VK_ACCESS_TRANSFER_READ_BIT : 0,
            VK_ACCESS_TRANSFER_WRITE_BIT);
        VkImageBlit blit = {.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
            .dstSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
            .srcOffsets = {{0,0,0}, {s->width,s->height,1}},
            .dstOffsets = {{0,0,0}, {s->width,s->height,1}}};
        vkCmdBlitImage(command, source, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
            s->background, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &blit, VK_FILTER_NEAREST);
        transition(command, s->background, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
            VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
            VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT);
        s->background_valid = 1;
    }
    VkImageCopy copy = {.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
        .dstSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1}, .extent = {s->width, s->height, 1}};
    vkCmdCopyImage(command, s->background, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
        s->output, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &copy);
    if (!draw) return 0;
    transition(command, s->output, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL,
        VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
        VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_COLOR_ATTACHMENT_READ_BIT | VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT);
    VkRenderPassBeginInfo begin = {.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO,
        .renderPass = s->render, .framebuffer = s->framebuffer,
        .renderArea = {{0, 0}, {s->width, s->height}}};
    vkCmdBeginRenderPass(command, &begin, VK_SUBPASS_CONTENTS_INLINE);
    vkCmdBindPipeline(command, VK_PIPELINE_BIND_POINT_GRAPHICS, s->pipeline);
    vkCmdBindDescriptorSets(command, VK_PIPELINE_BIND_POINT_GRAPHICS, s->layout, 0, 1, &s->descriptor, 0, NULL);
    float geometry[6] = {(float)x, (float)y, h->width, h->height, s->width, s->height};
    vkCmdPushConstants(command, s->layout, VK_SHADER_STAGE_VERTEX_BIT | VK_SHADER_STAGE_FRAGMENT_BIT,
        0, sizeof(geometry), geometry);
    vkCmdDraw(command, 6, 1, 0, 0);
    vkCmdEndRenderPass(command);
    transition(command, s->output, VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
        VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
        VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT, VK_ACCESS_TRANSFER_WRITE_BIT);
    return 0;
}

unsigned uurb_vk_cursor_composite_uploads(const struct uurb_vk_cursor_composite *s)
{ return s ? s->uploads : 0; }

void uurb_vk_cursor_composite_close(struct uurb_vk_cursor_composite *s)
{
    if (!s) return;
    VkDevice d = s->device;
    if (s->pipeline) vkDestroyPipeline(d, s->pipeline, NULL);
    if (s->layout) vkDestroyPipelineLayout(d, s->layout, NULL);
    if (s->descriptor_pool) vkDestroyDescriptorPool(d, s->descriptor_pool, NULL);
    if (s->descriptor_layout) vkDestroyDescriptorSetLayout(d, s->descriptor_layout, NULL);
    if (s->framebuffer) vkDestroyFramebuffer(d, s->framebuffer, NULL);
    if (s->render) vkDestroyRenderPass(d, s->render, NULL);
    if (s->view) vkDestroyImageView(d, s->view, NULL);
    if (s->mapped) vkUnmapMemory(d, s->upload_memory);
    if (s->upload) vkDestroyBuffer(d, s->upload, NULL);
    if (s->upload_memory) vkFreeMemory(d, s->upload_memory, NULL);
    if (s->background) vkDestroyImage(d, s->background, NULL);
    if (s->background_memory) vkFreeMemory(d, s->background_memory, NULL);
    free(s);
}
