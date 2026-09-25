#ifndef UURB_DXVK_GPU_INTEROP_H
#define UURB_DXVK_GPU_INTEROP_H
/* Pinned DXVK 3.1 / Wine 11.0 internal GPU ownership helpers. */
typedef struct InteropDevice InteropDevice;
typedef struct InteropSurface InteropSurface;
typedef struct {
    HRESULT (STDMETHODCALLTYPE *QueryInterface)(InteropDevice *, REFIID, void **);
    ULONG (STDMETHODCALLTYPE *AddRef)(InteropDevice *);
    ULONG (STDMETHODCALLTYPE *Release)(InteropDevice *);
    void (STDMETHODCALLTYPE *GetVulkanHandles)(InteropDevice *, VkInstance *, VkPhysicalDevice *, VkDevice *);
    void (STDMETHODCALLTYPE *GetSubmissionQueue)(InteropDevice *, VkQueue *, uint32_t *);
    void (STDMETHODCALLTYPE *TransitionSurfaceLayout)(InteropDevice *, InteropSurface *,
        const VkImageSubresourceRange *, VkImageLayout, VkImageLayout);
    void (STDMETHODCALLTYPE *FlushRenderingCommands)(InteropDevice *);
    void (STDMETHODCALLTYPE *LockSubmissionQueue)(InteropDevice *);
    void (STDMETHODCALLTYPE *ReleaseSubmissionQueue)(InteropDevice *);
} InteropDeviceVtbl;
struct InteropDevice { const InteropDeviceVtbl *lpVtbl; };
typedef struct {
    HRESULT (STDMETHODCALLTYPE *QueryInterface)(InteropSurface *, REFIID, void **);
    ULONG (STDMETHODCALLTYPE *AddRef)(InteropSurface *);
    ULONG (STDMETHODCALLTYPE *Release)(InteropSurface *);
    HRESULT (STDMETHODCALLTYPE *GetDevice)(InteropSurface *, InteropDevice **);
    HRESULT (STDMETHODCALLTYPE *GetVulkanImageInfo)(InteropSurface *, VkImage *, VkImageLayout *, VkImageCreateInfo *);
} InteropSurfaceVtbl;
struct InteropSurface { const InteropSurfaceVtbl *lpVtbl; };
static const GUID interop_device_iid = {0xe2ef5fa5,0xdc21,0x4af7,{0x90,0xc4,0xf6,0x7e,0xf6,0xa0,0x93,0x23}};
static const GUID interop_surface_iid = {0x5546cf8c,0x77e7,0x4341,{0xb0,0x5d,0x8d,0x4d,0x50,0x00,0xe7,0x7d}};

static int shared_fd(HANDLE handle)
{
    /* Wine 11 uses D3DKMT server objects, not Proton's legacy SharedGpuResource
     * IOCTL. Use only this process's own shared handle. Source/protocol must be
     * pinned to Wine 11.0 (server protocol 930); this is not a stable Wine API. */
    HANDLE unix_handle = NULL;
    int fd = uurb_wine11_gpu_fd(handle, &unix_handle);
    if (unix_handle) NtClose(unix_handle);
    return fd;
}

struct transfer_state {
    VkCommandPool pool;
    VkCommandBuffer commands[2];
    VkFence fence;
};
static void transfer_destroy(struct transfer_state *s, VkDevice device)
{
    if (s->fence) vkDestroyFence(device, s->fence, NULL);
    if (s->pool) vkDestroyCommandPool(device, s->pool, NULL);
}
static int transfer_ownership(struct transfer_state *s, InteropDevice *interop, VkDevice device, VkImage image, int acquire)
{
    VkQueue queue;
    uint32_t family;
    interop->lpVtbl->GetSubmissionQueue(interop, &queue, &family);
    VkCommandPoolCreateInfo pool_info = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
        .flags = VK_COMMAND_POOL_CREATE_TRANSIENT_BIT, .queueFamilyIndex = family};
    int result = 1;
#define VK(call) do { VkResult error_ = (call); if (error_ != VK_SUCCESS) { \
    fprintf(stderr, "%s: Vulkan error %d\n", #call, error_); goto done; } } while (0)
    if (!s->pool) VK(vkCreateCommandPool(device, &pool_info, NULL, &s->pool));
    if (!s->fence) {
        VkFenceCreateInfo info = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
        VK(vkCreateFence(device, &info, NULL, &s->fence));
    }
    if (!s->commands[acquire]) {
        VkCommandBufferAllocateInfo allocate = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
            .commandPool = s->pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1};
        VK(vkAllocateCommandBuffers(device, &allocate, &s->commands[acquire]));
        VkCommandBuffer command = s->commands[acquire];
        VkCommandBufferBeginInfo begin = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
            .flags = 0};
        VK(vkBeginCommandBuffer(command, &begin));
        VkImageMemoryBarrier barrier = {.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
            .srcAccessMask = acquire ? 0 : VK_ACCESS_MEMORY_WRITE_BIT | VK_ACCESS_MEMORY_READ_BIT,
            .dstAccessMask = acquire ? VK_ACCESS_MEMORY_WRITE_BIT | VK_ACCESS_MEMORY_READ_BIT : 0,
            .oldLayout = VK_IMAGE_LAYOUT_GENERAL, .newLayout = VK_IMAGE_LAYOUT_GENERAL,
            .srcQueueFamilyIndex = acquire ? VK_QUEUE_FAMILY_EXTERNAL : family,
            .dstQueueFamilyIndex = acquire ? family : VK_QUEUE_FAMILY_EXTERNAL,
            .image = image, .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
        vkCmdPipelineBarrier(command,
            acquire ? VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT : VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,
            acquire ? VK_PIPELINE_STAGE_ALL_COMMANDS_BIT : VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,
            0, 0, NULL, 0, NULL, 1, &barrier);
        VK(vkEndCommandBuffer(command));
    }
    VK(vkResetFences(device, 1, &s->fence));
    VkSubmitInfo submit = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
        .commandBufferCount = 1, .pCommandBuffers = &s->commands[acquire]};
    interop->lpVtbl->FlushRenderingCommands(interop);
    interop->lpVtbl->LockSubmissionQueue(interop);
    VkResult status = vkQueueSubmit(queue, 1, &submit, s->fence);
    interop->lpVtbl->ReleaseSubmissionQueue(interop);
    VK(status);
    /* Bounded CPU control wait, not pixel readback. This diagnostic deliberately
     * serializes ownership; production needs GPU semaphore pipelining. */
    status = vkWaitForFences(device, 1, &s->fence, VK_TRUE, 5000000000ull);
    if (status != VK_SUCCESS) {
        fprintf(stderr, "GPU ownership fence failed (%d); terminating isolated probe\n", status);
        _Exit(4); /* Do not destroy a command pool with commands still pending. */
    }
    result = 0;
done:
    return result;
}

#undef VK
#endif
