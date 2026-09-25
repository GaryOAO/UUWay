#define _GNU_SOURCE
#define COBJMACROS
#include <windows.h>
#include <winternl.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <vulkan/vulkan.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include "uu_d3d11_encode_session.h"
#include <ffnvcodec/nvEncodeAPI.h>
#include "uu_d3d11_frame_adapter.h"
int uurb_wine11_gpu_fd(void *, void **);
#include "uu_dxvk_gpu_interop.h"

struct registered_source {
    uint64_t token;
    struct uurb_d3d11_frame_adapter *adapter;
};
struct uurb_d3d11_encoder {
    ID3D11Device *device;
    ID3D11DeviceContext *context;
    ID3D11Texture2D *output;
    ID3D11Query *completed;
    InteropDevice *interop;
    InteropSurface *surface;
    HANDLE shared;
    VkDevice vkdevice;
    VkImage image;
    struct transfer_state transfer;
    struct registered_source sources[16];
    struct uurb_encoder *native;
    int fd, hevc, failed, locked, external_owned, have_timestamp;
    unsigned width, height, bitrate;
    uint64_t bytes, last_timestamp;
    unsigned char uuid[16];
};
/* Process-wide monotonically increasing tokens reject stale and cross-session
 * resources even if an allocator reuses a source or encoder's address. */
static SRWLOCK token_guard = SRWLOCK_INIT;
static uint64_t next_token = 1;

static struct registered_source *find_source(struct uurb_d3d11_encoder *s, uint64_t token)
{
    if (token) for (unsigned i = 0; i < 16; ++i)
        if (s->sources[i].token == token) return &s->sources[i];
    return NULL;
}
static int idle(struct uurb_d3d11_encoder *s)
{ return s && !s->failed && !s->locked && !s->external_owned; }

HRESULT uurb_d3d11_encoder_open(ID3D11Device *device, unsigned width, unsigned height,
    int hevc, unsigned bitrate, struct uurb_d3d11_encoder **out)
{
    if (!device || !out || width < 2 || height < 2 || width > 4096 || height > 4096 ||
        (width & 1) || (height & 1) || (hevc != 0 && hevc != 1) || !bitrate) return E_INVALIDARG;
    const char *(CDECL *wine_version)(void) =
        (void *)GetProcAddress(GetModuleHandleA("ntdll.dll"), "wine_get_version");
    if (!wine_version || strcmp(wine_version(), "11.0")) return E_NOTIMPL;
    struct uurb_d3d11_encoder *s = calloc(1, sizeof(*s));
    if (!s) return E_OUTOFMEMORY;
    s->fd = -1; s->width = width; s->height = height; s->hevc = hevc; s->bitrate = bitrate;
    s->device = device; ID3D11Device_AddRef(device);
    ID3D11Device_GetImmediateContext(device, &s->context);
    HRESULT result = E_FAIL;
    IDXGIResource1 *resource = NULL;
    HMODULE vulkan = NULL;
#define CHECK(call) do { result = (call); if (FAILED(result)) goto done; } while (0)
    CHECK(ID3D11Device_QueryInterface(device, &interop_device_iid, (void **)&s->interop));
    D3D11_TEXTURE2D_DESC desc = {.Width = width, .Height = height, .MipLevels = 1, .ArraySize = 1,
        .Format = DXGI_FORMAT_B8G8R8A8_UNORM, .SampleDesc = {1, 0}, .Usage = D3D11_USAGE_DEFAULT,
        .BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE,
        .MiscFlags = D3D11_RESOURCE_MISC_SHARED | D3D11_RESOURCE_MISC_SHARED_NTHANDLE};
    CHECK(ID3D11Device_CreateTexture2D(device, &desc, NULL, &s->output));
    D3D11_QUERY_DESC query = {.Query = D3D11_QUERY_EVENT};
    CHECK(ID3D11Device_CreateQuery(device, &query, &s->completed));
    s->interop->lpVtbl->FlushRenderingCommands(s->interop);
    CHECK(ID3D11Texture2D_QueryInterface(s->output, &interop_surface_iid, (void **)&s->surface));
    CHECK(ID3D11Texture2D_QueryInterface(s->output, &IID_IDXGIResource1, (void **)&resource));
    CHECK(IDXGIResource1_CreateSharedHandle(resource, NULL, DXGI_SHARED_RESOURCE_READ, NULL, &s->shared));
    VkInstance instance;
    VkPhysicalDevice physical;
    VkImageLayout layout;
    VkImageCreateInfo image_info = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    s->interop->lpVtbl->GetVulkanHandles(s->interop, &instance, &physical, &s->vkdevice);
    CHECK(s->surface->lpVtbl->GetVulkanImageInfo(s->surface, &s->image, &layout, &image_info));
    result = E_FAIL;
    if (layout != VK_IMAGE_LAYOUT_GENERAL || image_info.format != VK_FORMAT_B8G8R8A8_UNORM ||
        image_info.tiling != VK_IMAGE_TILING_OPTIMAL) goto done;
    vulkan = LoadLibraryA("vulkan-1.dll");
    if (!vulkan) goto done;
    PFN_vkGetImageMemoryRequirements requirements_fn = (void *)GetProcAddress(vulkan, "vkGetImageMemoryRequirements");
    PFN_vkGetPhysicalDeviceProperties2 properties_fn = (void *)GetProcAddress(vulkan, "vkGetPhysicalDeviceProperties2");
    if (!requirements_fn || !properties_fn) goto done;
    VkMemoryRequirements requirements;
    requirements_fn(s->vkdevice, s->image, &requirements);
    s->bytes = requirements.size;
    VkPhysicalDeviceIDProperties id = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ID_PROPERTIES};
    VkPhysicalDeviceProperties2 properties = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2, .pNext = &id};
    properties_fn(physical, &properties);
    if (properties.properties.vendorID != 0x10de) goto done;
    memcpy(s->uuid, id.deviceUUID, 16);
    s->fd = shared_fd(s->shared);
    if (s->fd < 0) goto done;
    *out = s; s = NULL; result = S_OK;
done:
    if (resource) IDXGIResource1_Release(resource);
    if (vulkan) FreeLibrary(vulkan);
    if (FAILED(uurb_d3d11_encoder_close(s))) return E_FAIL;
    return result;
}

HRESULT uurb_d3d11_encoder_initialize(struct uurb_d3d11_encoder *s, const struct _NV_ENC_INITIALIZE_PARAMS *params)
{
    if (!params || !s || params->encodeWidth != s->width || params->encodeHeight != s->height ||
        memcmp(&params->encodeGUID, s->hevc ? &NV_ENC_CODEC_HEVC_GUID : &NV_ENC_CODEC_H264_GUID, sizeof(GUID)))
        return E_INVALIDARG;
    if (!idle(s) || s->native || s->have_timestamp) return DXGI_ERROR_INVALID_CALL;
    if (transfer_ownership(&s->transfer, s->interop, s->vkdevice, s->image, 0)) goto failed;
    s->external_owned = 1;
    s->native = uurb_encoder_open_config(fcntl(s->fd, F_DUPFD_CLOEXEC, 0), s->bytes, s->uuid, params);
    if (!s->native) goto failed;
    if (transfer_ownership(&s->transfer, s->interop, s->vkdevice, s->image, 1)) goto failed;
    s->external_owned = 0;
    return S_OK;
failed:
    s->failed = 1;
    return E_FAIL;
}
int uurb_d3d11_encoder_sequence(struct uurb_d3d11_encoder *s, void *bytes, uint32_t capacity, uint32_t *size)
{
    return uurb_encoder_sequence(s ? s->native : NULL, bytes, capacity, size);
}

HRESULT uurb_d3d11_encoder_register(struct uurb_d3d11_encoder *s, ID3D11Texture2D *source, uint64_t *token)
{
    if (!source || !token) return E_INVALIDARG;
    if (!idle(s)) return DXGI_ERROR_INVALID_CALL;
    unsigned index;
    for (index = 0; index < 16 && s->sources[index].token; ++index) {}
    if (index == 16) return E_OUTOFMEMORY;
    struct uurb_d3d11_frame_adapter *adapter = NULL;
    HRESULT result = uurb_d3d11_frame_adapter_create(s->device, source, s->output, &adapter);
    if (FAILED(result)) return result;
    AcquireSRWLockExclusive(&token_guard);
    uint64_t value = next_token == UINT64_MAX ? 0 : next_token++;
    ReleaseSRWLockExclusive(&token_guard);
    if (!value) { uurb_d3d11_frame_adapter_destroy(adapter); return E_OUTOFMEMORY; }
    s->sources[index] = (struct registered_source){value, adapter};
    *token = value;
    return S_OK;
}
HRESULT uurb_d3d11_encoder_unregister(struct uurb_d3d11_encoder *s, uint64_t token)
{
    if (!idle(s)) return DXGI_ERROR_INVALID_CALL;
    struct registered_source *source = find_source(s, token);
    if (!source) return E_INVALIDARG;
    uurb_d3d11_frame_adapter_destroy(source->adapter);
    *source = (struct registered_source){0};
    return S_OK;
}
HRESULT uurb_d3d11_encoder_encode(struct uurb_d3d11_encoder *s, uint64_t token,
    uint64_t timestamp, int force_idr, struct uurb_packet *packet)
{
    if (!packet) return E_INVALIDARG;
    memset(packet, 0, sizeof(*packet));
    if (!idle(s)) return DXGI_ERROR_INVALID_CALL;
    struct registered_source *source = find_source(s, token);
    if (!source || (s->have_timestamp && timestamp <= s->last_timestamp)) return E_INVALIDARG;
    HRESULT result = uurb_d3d11_frame_adapter_submit(source->adapter, s->context);
    if (FAILED(result)) goto failed;
    ID3D11DeviceContext_End(s->context, (ID3D11Asynchronous *)s->completed);
    ID3D11DeviceContext_Flush(s->context);
    ULONGLONG deadline = GetTickCount64() + 5000;
    while ((result = ID3D11DeviceContext_GetData(s->context,
        (ID3D11Asynchronous *)s->completed, NULL, 0, 0)) == S_FALSE && GetTickCount64() < deadline) Sleep(1);
    if (result != S_OK) {
        fprintf(stderr, "D3D11 bridge completion fence failed; terminating isolated worker\n");
        _Exit(4); /* Source/output may still be in use. Never free or recycle. */
    }
    if (transfer_ownership(&s->transfer, s->interop, s->vkdevice, s->image, 0)) goto failed;
    s->external_owned = 1;
    if (!s->native) {
        s->native = uurb_encoder_open(fcntl(s->fd, F_DUPFD_CLOEXEC, 0), s->bytes, s->uuid,
            s->width, s->height, s->hevc, s->bitrate);
        if (!s->native) goto failed;
    }
    if (uurb_encoder_encode(s->native, timestamp, force_idr, packet)) goto failed;
    s->locked = 1; s->last_timestamp = timestamp; s->have_timestamp = 1;
    return S_OK;
failed:
    s->failed = 1;
    return E_FAIL;
}
HRESULT uurb_d3d11_encoder_release(struct uurb_d3d11_encoder *s)
{
    if (!s || s->failed || !s->locked) return DXGI_ERROR_INVALID_CALL;
    if (uurb_encoder_release_packet(s->native) ||
        transfer_ownership(&s->transfer, s->interop, s->vkdevice, s->image, 1)) {
        s->failed = 1; return E_FAIL;
    }
    s->locked = 0; s->external_owned = 0;
    return S_OK;
}
HRESULT uurb_d3d11_encoder_reconfigure(struct uurb_d3d11_encoder *s, unsigned bitrate)
{
    if (!bitrate) return E_INVALIDARG;
    if (!idle(s)) return DXGI_ERROR_INVALID_CALL;
    if (s->native && uurb_encoder_reconfigure(s->native, bitrate)) {
        s->failed = 1; return E_FAIL;
    }
    s->bitrate = bitrate;
    return S_OK;
}
HRESULT uurb_d3d11_encoder_reconfigure_config(struct uurb_d3d11_encoder *s, const NV_ENC_RECONFIGURE_PARAMS *params)
{
    if (!params || !params->reInitEncodeParams.encodeConfig) return E_INVALIDARG;
    if (!idle(s) || !s->native) return DXGI_ERROR_INVALID_CALL;
    if (uurb_encoder_reconfigure_config(s->native, params)) { s->failed = 1; return E_FAIL; }
    s->bitrate = params->reInitEncodeParams.encodeConfig->rcParams.averageBitRate;
    return S_OK;
}
HRESULT uurb_d3d11_encoder_close(struct uurb_d3d11_encoder *s)
{
    if (!s) return S_OK;
    int failed = 0;
    if (s->native) {
        if (!s->external_owned && transfer_ownership(&s->transfer, s->interop, s->vkdevice, s->image, 0)) _Exit(4);
        s->external_owned = 1;
        if (uurb_encoder_close(s->native)) _Exit(4);
    }
    if (s->external_owned && transfer_ownership(&s->transfer, s->interop, s->vkdevice, s->image, 1)) _Exit(4);
    for (unsigned i = 0; i < 16; ++i) uurb_d3d11_frame_adapter_destroy(s->sources[i].adapter);
    transfer_destroy(&s->transfer, s->vkdevice);
    if (s->fd >= 0 && close(s->fd)) failed = 1;
    if (s->shared) CloseHandle(s->shared);
    if (s->surface) s->surface->lpVtbl->Release(s->surface);
    if (s->interop) s->interop->lpVtbl->Release(s->interop);
    if (s->completed) ID3D11Query_Release(s->completed);
    if (s->output) ID3D11Texture2D_Release(s->output);
    if (s->context) ID3D11DeviceContext_Release(s->context);
    if (s->device) ID3D11Device_Release(s->device);
    free(s);
    return failed ? E_FAIL : S_OK;
}
