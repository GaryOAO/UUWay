#define _GNU_SOURCE
#define COBJMACROS
#include <windows.h>
#include <winternl.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <vulkan/vulkan.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "uu_d3d11_capture_texture.h"
#include "native_cuda_frame_copy.h"
int uurb_wine11_gpu_fd(void *, void **);
#include "uu_dxvk_gpu_interop.h"
struct uurb_capture_texture {
    ID3D11Texture2D *texture;
    InteropDevice *interop;
    InteropSurface *surface;
    HANDLE shared;
    VkDevice device;
    VkImage image;
    struct transfer_state transfer;
    struct uurb_gpu_copy *copy;
};
HRESULT uurb_capture_texture_open(ID3D11Device *device, int source_fd,
    const struct uurb_gpu_message *frame, struct uurb_capture_texture **out)
{
    struct uurb_capture_texture *s = NULL;
    IDXGIResource1 *resource = NULL;
    HMODULE vulkan = NULL;
    HRESULT result = E_INVALIDARG;
    int destination_fd = -1;
    if (!device || !out || source_fd < 0 || !uurb_gpu_message_valid(frame, 1) || frame->kind != UURB_GPU_FRAME) goto done;
    const char *(CDECL *wine_version)(void) = (void *)GetProcAddress(GetModuleHandleA("ntdll.dll"), "wine_get_version");
    result = E_NOTIMPL;
    if (!wine_version || strcmp(wine_version(), "11.0")) goto done;
    s = calloc(1, sizeof(*s));
    result = E_OUTOFMEMORY;
    if (!s) goto done;
#define HR(call) do { result = (call); if (FAILED(result)) goto done; } while (0)
    HR(ID3D11Device_QueryInterface(device, &interop_device_iid, (void **)&s->interop));
    D3D11_TEXTURE2D_DESC desc = {.Width = frame->width, .Height = frame->height, .MipLevels = 1, .ArraySize = 1,
        .Format = DXGI_FORMAT_B8G8R8A8_UNORM, .SampleDesc = {1, 0}, .Usage = D3D11_USAGE_DEFAULT,
        .BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE,
        .MiscFlags = D3D11_RESOURCE_MISC_SHARED | D3D11_RESOURCE_MISC_SHARED_NTHANDLE};
    HR(ID3D11Device_CreateTexture2D(device, &desc, NULL, &s->texture));
    s->interop->lpVtbl->FlushRenderingCommands(s->interop);
    HR(ID3D11Texture2D_QueryInterface(s->texture, &interop_surface_iid, (void **)&s->surface));
    HR(ID3D11Texture2D_QueryInterface(s->texture, &IID_IDXGIResource1, (void **)&resource));
    HR(IDXGIResource1_CreateSharedHandle(resource, NULL, DXGI_SHARED_RESOURCE_READ | DXGI_SHARED_RESOURCE_WRITE, NULL, &s->shared));
    VkInstance instance;
    VkPhysicalDevice physical;
    VkImageLayout layout;
    VkImageCreateInfo image_info = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    s->interop->lpVtbl->GetVulkanHandles(s->interop, &instance, &physical, &s->device);
    HR(s->surface->lpVtbl->GetVulkanImageInfo(s->surface, &s->image, &layout, &image_info));
    result = E_FAIL;
    if (layout != VK_IMAGE_LAYOUT_GENERAL || image_info.format != VK_FORMAT_B8G8R8A8_UNORM ||
        image_info.tiling != VK_IMAGE_TILING_OPTIMAL) goto done;
    vulkan = LoadLibraryA("vulkan-1.dll");
    if (!vulkan) goto done;
    PFN_vkGetImageMemoryRequirements requirements_fn = (void *)GetProcAddress(vulkan, "vkGetImageMemoryRequirements");
    PFN_vkGetPhysicalDeviceProperties2 properties_fn = (void *)GetProcAddress(vulkan, "vkGetPhysicalDeviceProperties2");
    if (!requirements_fn || !properties_fn) goto done;
    VkMemoryRequirements requirements;
    requirements_fn(s->device, s->image, &requirements);
    VkPhysicalDeviceIDProperties id = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ID_PROPERTIES};
    VkPhysicalDeviceProperties2 properties = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2, .pNext = &id};
    properties_fn(physical, &properties);
    if (properties.properties.vendorID != 0x10de || memcmp(id.deviceUUID, frame->uuid, 16)) goto done;
    destination_fd = shared_fd(s->shared);
    if (destination_fd < 0) goto done;
    int source_owned = source_fd, destination_owned = destination_fd;
    source_fd = destination_fd = -1;
    s->copy = uurb_gpu_copy_open(source_owned, frame->allocation_bytes, destination_owned,
                               requirements.size, frame->uuid, frame->width, frame->height);
    if (!s->copy) goto done;
    *out = s; s = NULL; result = S_OK;
done:
    if (source_fd >= 0) close(source_fd);
    if (destination_fd >= 0) close(destination_fd);
    if (resource) IDXGIResource1_Release(resource);
    if (vulkan) FreeLibrary(vulkan);
    uurb_capture_texture_close(s);
    return result;
#undef HR
}
HRESULT uurb_capture_texture_update(struct uurb_capture_texture *s)
{
    if (!s) return E_INVALIDARG;
    if (transfer_ownership(&s->transfer, s->interop, s->device, s->image, 0) ||
        uurb_gpu_copy_frame(s->copy) ||
        transfer_ownership(&s->transfer, s->interop, s->device, s->image, 1)) {
        fprintf(stderr, "Capture texture GPU ownership failed; exiting isolated worker\n"); _Exit(4);
    }
    return S_OK;
}
ID3D11Texture2D *uurb_capture_texture_get(struct uurb_capture_texture *s)
{ return s ? s->texture : NULL; }
void uurb_capture_texture_close(struct uurb_capture_texture *s)
{
    if (!s) return;
    if (uurb_gpu_copy_close(s->copy)) _Exit(4);
    if (s->device) transfer_destroy(&s->transfer, s->device);
    if (s->surface) s->surface->lpVtbl->Release(s->surface);
    if (s->interop) s->interop->lpVtbl->Release(s->interop);
    if (s->shared) CloseHandle(s->shared);
    if (s->texture) ID3D11Texture2D_Release(s->texture);
    free(s);
}
