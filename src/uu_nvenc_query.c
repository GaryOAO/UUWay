/* Query-only Windows NVENC ABI adapter. Built under a NON-production DLL name.
 * It cannot encode; InitializeEncoder deliberately returns UNIMPLEMENTED.
 * Driver capabilities returned here are NOT claims of bridge format support. */
#define _GNU_SOURCE
#define COBJMACROS
#include <windows.h>
#include <d3d11.h>
#include <vulkan/vulkan.h>
#include <stddef.h>
#include "native_nvenc_query.h"

typedef struct Interop Interop;
typedef struct {
    HRESULT (STDMETHODCALLTYPE *QueryInterface)(Interop *, REFIID, void **);
    ULONG (STDMETHODCALLTYPE *AddRef)(Interop *);
    ULONG (STDMETHODCALLTYPE *Release)(Interop *);
    void (STDMETHODCALLTYPE *GetVulkanHandles)(Interop *, VkInstance *, VkPhysicalDevice *, VkDevice *);
} InteropVtbl;
struct Interop { const InteropVtbl *lpVtbl; };
static const GUID interop_iid = {0xe2ef5fa5,0xdc21,0x4af7,{0x90,0xc4,0xf6,0x7e,0xf6,0xa0,0x93,0x23}};
struct session {
    struct session *next;
    void *token, *native;
    ID3D11Device *device;
};
/* Deliberately serialized reference adapter; no untested async/thread claims. */
static SRWLOCK guard = SRWLOCK_INIT;
static struct session *sessions;
static uintptr_t next_token = 0x55550001;
static unsigned session_count;
_Static_assert(sizeof(NV_ENCODE_API_FUNCTION_LIST) == 0x9f8, "UU function table ABI");
_Static_assert(offsetof(NV_ENCODE_API_FUNCTION_LIST, nvEncOpenEncodeSessionEx) == 0xf0, "UU open offset");

static struct session *find_session(void *token)
{
    for (struct session *s = sessions; s; s = s->next) if (s->token == token) return s;
    return NULL;
}

static NVENCSTATUS NVENCAPI open_session(NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS *params, void **output)
{
    if (!params || !output) return NV_ENC_ERR_INVALID_PTR;
    *output = NULL;
    if (params->version != NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS_VER || params->apiVersion != NVENCAPI_VERSION)
        return NV_ENC_ERR_INVALID_VERSION;
    if (params->deviceType != NV_ENC_DEVICE_TYPE_DIRECTX || !params->device)
        return NV_ENC_ERR_UNSUPPORTED_DEVICE;
    NVENCSTATUS status = NV_ENC_ERR_UNSUPPORTED_DEVICE;
    struct session *s = calloc(1, sizeof(*s));
    Interop *interop = NULL;
    HMODULE vulkan = NULL;
    if (!s) return NV_ENC_ERR_OUT_OF_MEMORY;
    if (FAILED(IUnknown_QueryInterface((IUnknown *)params->device, &IID_ID3D11Device, (void **)&s->device)) ||
        FAILED(ID3D11Device_QueryInterface(s->device, &interop_iid, (void **)&interop))) goto done;
    VkInstance instance;
    VkPhysicalDevice physical;
    VkDevice device;
    interop->lpVtbl->GetVulkanHandles(interop, &instance, &physical, &device);
    vulkan = LoadLibraryA("vulkan-1.dll");
    PFN_vkGetPhysicalDeviceProperties2 properties_fn = (void *)GetProcAddress(vulkan, "vkGetPhysicalDeviceProperties2");
    if (!properties_fn) goto done;
    VkPhysicalDeviceIDProperties id = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ID_PROPERTIES};
    VkPhysicalDeviceProperties2 properties = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2, .pNext = &id};
    properties_fn(physical, &properties);
    AcquireSRWLockExclusive(&guard);
    if (session_count >= 16 || next_token == UINTPTR_MAX) status = NV_ENC_ERR_OUT_OF_MEMORY;
    else status = uurb_nvenc_query_open(id.deviceUUID, &s->native);
    if (status == NV_ENC_SUCCESS) {
        s->token = (void *)next_token++;
        s->next = sessions; sessions = s; session_count++;
        *output = s->token;
    }
    ReleaseSRWLockExclusive(&guard);
done:
    if (interop) interop->lpVtbl->Release(interop);
    if (vulkan) FreeLibrary(vulkan);
    if (status != NV_ENC_SUCCESS) {
        if (s->device) ID3D11Device_Release(s->device);
        free(s);
    }
    return status;
}

static NVENCSTATUS query(void *token, unsigned op, const GUID *codec,
                         void *output, uint32_t capacity, uint32_t *count)
{
    if (op == UURB_CAP ? !output : !count) return NV_ENC_ERR_INVALID_PTR;
    if (op == UURB_CODECS || op == UURB_PROFILES || op == UURB_FORMATS || op == UURB_PRESETS) {
        if (!output) return NV_ENC_ERR_INVALID_PTR;
        if (!capacity || capacity > 128) return NV_ENC_ERR_INVALID_PARAM;
    }
    AcquireSRWLockExclusive(&guard);
    struct session *s = find_session(token);
    NVENCSTATUS status = s ? uurb_nvenc_query(s->native, op, codec, output, capacity, count)
                           : NV_ENC_ERR_INVALID_ENCODERDEVICE;
    ReleaseSRWLockExclusive(&guard);
    return status;
}
static NVENCSTATUS NVENCAPI codec_count(void *s, uint32_t *n) { return query(s,UURB_CODEC_COUNT,NULL,NULL,0,n); }
static NVENCSTATUS NVENCAPI codecs(void *s, GUID *out, uint32_t capacity, uint32_t *n)
{ return query(s,UURB_CODECS,NULL,out,capacity,n); }
#define COUNTS(name, op) static NVENCSTATUS NVENCAPI name(void *s, GUID codec, uint32_t *n) \
    { return query(s,op,&codec,NULL,0,n); }
#define ARRAYS(name, op, type) static NVENCSTATUS NVENCAPI name(void *s, GUID codec, type *out, uint32_t cap, uint32_t *n) \
    { return query(s,op,&codec,out,cap,n); }
COUNTS(profile_count, UURB_PROFILE_COUNT)
COUNTS(format_count, UURB_FORMAT_COUNT)
COUNTS(preset_count, UURB_PRESET_COUNT)
ARRAYS(profiles, UURB_PROFILES, GUID)
ARRAYS(formats, UURB_FORMATS, NV_ENC_BUFFER_FORMAT)
ARRAYS(presets, UURB_PRESETS, GUID)
static NVENCSTATUS NVENCAPI caps(void *s, GUID codec, NV_ENC_CAPS_PARAM *params, int *out)
{
    if (!params || !out) return NV_ENC_ERR_INVALID_PTR;
    if (params->version != NV_ENC_CAPS_PARAM_VER) return NV_ENC_ERR_INVALID_VERSION;
    return query(s,UURB_CAP,&codec,out,params->capsToQuery,NULL);
}
static NVENCSTATUS NVENCAPI initialize(void *token, NV_ENC_INITIALIZE_PARAMS *params)
{
    if (!params) return NV_ENC_ERR_INVALID_PTR;
    if (params->version != NV_ENC_INITIALIZE_PARAMS_VER) return NV_ENC_ERR_INVALID_VERSION;
    AcquireSRWLockExclusive(&guard);
    NVENCSTATUS status = find_session(token) ? NV_ENC_ERR_UNIMPLEMENTED : NV_ENC_ERR_INVALID_ENCODERDEVICE;
    ReleaseSRWLockExclusive(&guard);
    /* No flag rewriting, CPU fallback, or success without a texture adapter. */
    return status;
}
static NVENCSTATUS preset_config(void *token, const GUID *codec, const GUID *preset,
                                 int extended, NV_ENC_TUNING_INFO tuning, NV_ENC_PRESET_CONFIG *out)
{
    if (!out) return NV_ENC_ERR_INVALID_PTR;
    if (out->version != NV_ENC_PRESET_CONFIG_VER || out->presetCfg.version != NV_ENC_CONFIG_VER)
        return NV_ENC_ERR_INVALID_VERSION;
    /* Commit output only on success, keeping Windows caller memory unchanged
     * on invalid handles, unsupported presets or driver failures. */
    NV_ENC_PRESET_CONFIG value = {.version = NV_ENC_PRESET_CONFIG_VER};
    value.presetCfg.version = NV_ENC_CONFIG_VER;
    AcquireSRWLockExclusive(&guard);
    struct session *s = find_session(token);
    NVENCSTATUS status = s ? uurb_nvenc_query_preset(s->native, codec, preset, extended, tuning, &value)
                           : NV_ENC_ERR_INVALID_ENCODERDEVICE;
    ReleaseSRWLockExclusive(&guard);
    if (status == NV_ENC_SUCCESS) *out = value;
    return status;
}
static NVENCSTATUS NVENCAPI get_preset(void *token, GUID codec, GUID preset, NV_ENC_PRESET_CONFIG *out)
{ return preset_config(token, &codec, &preset, 0, NV_ENC_TUNING_INFO_UNDEFINED, out); }
static NVENCSTATUS NVENCAPI get_preset_ex(void *token, GUID codec, GUID preset,
                                         NV_ENC_TUNING_INFO tuning, NV_ENC_PRESET_CONFIG *out)
{ return preset_config(token, &codec, &preset, 1, tuning, out); }
static NVENCSTATUS NVENCAPI destroy(void *token)
{
    AcquireSRWLockExclusive(&guard);
    struct session **link = &sessions;
    while (*link && (*link)->token != token) link = &(*link)->next;
    struct session *s = *link;
    NVENCSTATUS status = NV_ENC_ERR_INVALID_ENCODERDEVICE;
    if (s) {
        status = uurb_nvenc_query_close(s->native);
        *link = s->next; session_count--;
        ID3D11Device_Release(s->device);
        free(s);
    }
    ReleaseSRWLockExclusive(&guard);
    return status;
}

NVENCSTATUS NVENCAPI NvEncodeAPIGetMaxSupportedVersion(uint32_t *version)
{ return uurb_nvenc_query_version(version); }

NVENCSTATUS NVENCAPI NvEncodeAPICreateInstance(NV_ENCODE_API_FUNCTION_LIST *api)
{
    if (!api) return NV_ENC_ERR_INVALID_PTR;
    if (api->version != NV_ENCODE_API_FUNCTION_LIST_VER) return NV_ENC_ERR_INVALID_VERSION;
    if (api->reserved) return NV_ENC_ERR_INVALID_PARAM;
    uint32_t version;
    NVENCSTATUS status = uurb_nvenc_query_version(&version);
    if (status) return status;
    *api = (NV_ENCODE_API_FUNCTION_LIST){.version = NV_ENCODE_API_FUNCTION_LIST_VER,
        .nvEncOpenEncodeSessionEx = open_session, .nvEncDestroyEncoder = destroy,
        .nvEncGetEncodeGUIDCount = codec_count, .nvEncGetEncodeGUIDs = codecs,
        .nvEncGetEncodeProfileGUIDCount = profile_count, .nvEncGetEncodeProfileGUIDs = profiles,
        .nvEncGetInputFormatCount = format_count, .nvEncGetInputFormats = formats,
        .nvEncGetEncodePresetCount = preset_count, .nvEncGetEncodePresetGUIDs = presets,
        .nvEncGetEncodeCaps = caps, .nvEncInitializeEncoder = initialize,
        .nvEncGetEncodePresetConfig = get_preset, .nvEncGetEncodePresetConfigEx = get_preset_ex};
    return NV_ENC_SUCCESS;
}
