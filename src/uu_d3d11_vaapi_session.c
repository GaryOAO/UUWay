/* Explicit AMD backend: D3D11 readback -> VA-API hardware encoder. */
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
#include "native_vaapi_encoder.h"

struct registered_source { uint64_t token; struct uurb_d3d11_frame_adapter *adapter; };
struct uurb_d3d11_encoder {
    ID3D11Device *device;
    ID3D11DeviceContext *context;
    ID3D11Texture2D *output, *staging;
    struct registered_source sources[16];
    struct uurb_encoder *native;
    int hevc, failed, locked, have_timestamp;
    unsigned width, height, bitrate;
    uint64_t last_timestamp;
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
{ return s && !s->failed && !s->locked; }

HRESULT uurb_d3d11_encoder_open(ID3D11Device *device, unsigned width, unsigned height,
    int hevc, unsigned bitrate, struct uurb_d3d11_encoder **out)
{
    if (!device || !out || width < 2 || height < 2 || width > 4096 || height > 4096 ||
        ((width | height) & 1) || (hevc != 0 && hevc != 1) || !bitrate) return E_INVALIDARG;
    *out = NULL;
    struct uurb_d3d11_encoder *s = calloc(1, sizeof(*s));
    if (!s) return E_OUTOFMEMORY;
    s->device = device; ID3D11Device_AddRef(device);
    s->width = width; s->height = height; s->hevc = hevc; s->bitrate = bitrate;
    ID3D11Device_GetImmediateContext(device, &s->context);
    D3D11_TEXTURE2D_DESC desc = {.Width = width, .Height = height, .MipLevels = 1, .ArraySize = 1,
        .Format = DXGI_FORMAT_B8G8R8A8_UNORM, .SampleDesc = {1, 0}, .Usage = D3D11_USAGE_DEFAULT,
        .BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE};
    HRESULT hr = ID3D11Device_CreateTexture2D(device, &desc, NULL, &s->output);
    if (FAILED(hr)) goto done;
    desc.Usage = D3D11_USAGE_STAGING; desc.BindFlags = 0; desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    hr = ID3D11Device_CreateTexture2D(device, &desc, NULL, &s->staging);
    if (FAILED(hr)) goto done;
    /* This ABI is also used by uu_nvenc_query.c. Native Vulkan identifies the
     * real GPU even when DXGI presents the NVENC compatibility adapter ID. */
    typedef struct Interop Interop;
    typedef struct {
        HRESULT (STDMETHODCALLTYPE *QueryInterface)(Interop *, REFIID, void **);
        ULONG (STDMETHODCALLTYPE *AddRef)(Interop *);
        ULONG (STDMETHODCALLTYPE *Release)(Interop *);
        void (STDMETHODCALLTYPE *GetVulkanHandles)(Interop *, VkInstance *, VkPhysicalDevice *, VkDevice *);
    } VTable;
    struct Interop { const VTable *lpVtbl; };
    static const GUID iid = {0xe2ef5fa5,0xdc21,0x4af7,{0x90,0xc4,0xf6,0x7e,0xf6,0xa0,0x93,0x23}};
    Interop *interop = NULL;
    hr = ID3D11Device_QueryInterface(device, &iid, (void **)&interop);
    if (FAILED(hr)) goto done;
    VkInstance instance; VkPhysicalDevice physical; VkDevice logical;
    interop->lpVtbl->GetVulkanHandles(interop, &instance, &physical, &logical);
    interop->lpVtbl->Release(interop);
    HMODULE module = LoadLibraryA("vulkan-1.dll");
    PFN_vkGetPhysicalDeviceProperties2 props_fn = module ? (void *)GetProcAddress(module, "vkGetPhysicalDeviceProperties2") : NULL;
    hr = E_FAIL;
    if (props_fn) {
        VkPhysicalDeviceIDProperties id = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ID_PROPERTIES};
        VkPhysicalDeviceProperties2 props = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2, .pNext = &id};
        props_fn(physical, &props);
        if (props.properties.vendorID == 0x1002) { memcpy(s->uuid, id.deviceUUID, 16); hr = S_OK; }
    }
    if (module) FreeLibrary(module);
    if (FAILED(hr)) goto done;
    *out = s; s = NULL;
done:
    uurb_d3d11_encoder_close(s);
    return hr;
}
HRESULT uurb_d3d11_encoder_initialize(struct uurb_d3d11_encoder *s, const NV_ENC_INITIALIZE_PARAMS *p)
{
    if (!s || !p || p->encodeWidth != s->width || p->encodeHeight != s->height ||
        memcmp(&p->encodeGUID, s->hevc ? &NV_ENC_CODEC_HEVC_GUID : &NV_ENC_CODEC_H264_GUID, sizeof(GUID))) return E_INVALIDARG;
    if (!idle(s) || s->native || s->have_timestamp) return DXGI_ERROR_INVALID_CALL;
    s->native = uurb_vaapi_open(s->uuid, p);
    return s->native ? S_OK : E_FAIL;
}
int uurb_d3d11_encoder_sequence(struct uurb_d3d11_encoder *s, void *bytes, uint32_t cap, uint32_t *size)
{ return uurb_encoder_sequence(s ? s->native : NULL, bytes, cap, size); }
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
    if (!idle(s) || !s->native) return DXGI_ERROR_INVALID_CALL;
    struct registered_source *source = find_source(s, token);
    if (!source || (s->have_timestamp && timestamp <= s->last_timestamp)) return E_INVALIDARG;
    HRESULT hr = uurb_d3d11_frame_adapter_submit(source->adapter, s->context);
    if (FAILED(hr)) return hr;
    ID3D11DeviceContext_CopyResource(s->context, (ID3D11Resource *)s->staging, (ID3D11Resource *)s->output);
    D3D11_MAPPED_SUBRESOURCE mapped;
    /* Map waits for the copy; no unbounded polling or stale GPU reads. The
     * enclosing isolated worker retains the supervisor's timeout policy. */
    hr = ID3D11DeviceContext_Map(s->context, (ID3D11Resource *)s->staging, 0, D3D11_MAP_READ, 0, &mapped);
    if (FAILED(hr)) return hr;
    int result = uurb_vaapi_encode_bgra(s->native, mapped.pData, mapped.RowPitch, timestamp, force_idr, packet);
    ID3D11DeviceContext_Unmap(s->context, (ID3D11Resource *)s->staging, 0);
    if (result) { s->failed = 1; return E_FAIL; }
    s->locked = 1; s->last_timestamp = timestamp; s->have_timestamp = 1;
    return S_OK;
}
HRESULT uurb_d3d11_encoder_release(struct uurb_d3d11_encoder *s)
{
    if (!s || s->failed || !s->locked) return DXGI_ERROR_INVALID_CALL;
    if (uurb_encoder_release_packet(s->native)) { s->failed = 1; return E_FAIL; }
    s->locked = 0;
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
    uurb_encoder_close(s->native);
    for (unsigned i = 0; i < 16; ++i) uurb_d3d11_frame_adapter_destroy(s->sources[i].adapter);
    if (s->staging) ID3D11Texture2D_Release(s->staging);
    if (s->output) ID3D11Texture2D_Release(s->output);
    if (s->context) ID3D11DeviceContext_Release(s->context);
    if (s->device) ID3D11Device_Release(s->device);
    free(s);
    return S_OK;
}
