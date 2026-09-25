/* Native CUDA/NVENC capability queries. No Windows pointer/function table is
 * ever passed to the Linux driver; DirectX devices are matched by GPU UUID. */
#include "native_nvenc_query.h"
#include <cuda.h>
#include <dlfcn.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>

struct query_session {
    CUcontext context;
    void *encoder;
    NV_ENCODE_API_FUNCTION_LIST api;
};
_Static_assert(sizeof(GUID) == 16, "GUID ABI");
_Static_assert(sizeof(NV_ENCODE_API_FUNCTION_LIST) == 0x9f8, "Audited NVENC 13 function table ABI");
static pthread_once_t load_once = PTHREAD_ONCE_INIT;
static NVENCSTATUS (*native_version)(uint32_t *);
static NVENCSTATUS (*native_instance)(NV_ENCODE_API_FUNCTION_LIST *);
static void load_driver(void)
{
    /* Resolve on the native library handle: direct linkage would interpose the
     * identically named Windows exports and call them with the wrong ABI. */
    void *library = dlopen("libnvidia-encode.so.1", RTLD_NOW | RTLD_LOCAL);
    if (!library) return;
    native_version = (void *)dlsym(library, "NvEncodeAPIGetMaxSupportedVersion");
    native_instance = (void *)dlsym(library, "NvEncodeAPICreateInstance");
    /* Process-lifetime driver module, as for CUDA; no per-session dlopen leak. */
}

int uurb_nvenc_query_version(uint32_t *version)
{
    if (!version) return NV_ENC_ERR_INVALID_PTR;
    pthread_once(&load_once, load_driver);
    if (!native_version || !native_instance) return NV_ENC_ERR_NO_ENCODE_DEVICE;
    uint32_t supported = 0;
    NVENCSTATUS status = native_version(&supported);
    if (status != NV_ENC_SUCCESS) return status;
    if (supported < 0xd0) return NV_ENC_ERR_INVALID_VERSION;
    *version = 0xd0; /* This adapter implements only the audited API 13.0 ABI. */
    return NV_ENC_SUCCESS;
}

int uurb_nvenc_query_open(const unsigned char uuid[16], void **output)
{
    if (!output || !uuid) return NV_ENC_ERR_INVALID_PTR;
    *output = NULL;
    uint32_t version;
    int status = uurb_nvenc_query_version(&version);
    if (status) return status;
    if (cuInit(0) != CUDA_SUCCESS) return NV_ENC_ERR_NO_ENCODE_DEVICE;
    int count;
    if (cuDeviceGetCount(&count) != CUDA_SUCCESS) return NV_ENC_ERR_NO_ENCODE_DEVICE;
    CUdevice selected = -1;
    for (int i = 0; i < count; ++i) {
        CUdevice device;
        CUuuid candidate;
        if (cuDeviceGet(&device, i) != CUDA_SUCCESS || cuDeviceGetUuid(&candidate, device) != CUDA_SUCCESS)
            return NV_ENC_ERR_NO_ENCODE_DEVICE;
        if (!memcmp(candidate.bytes, uuid, 16)) { selected = device; break; }
    }
    if (selected < 0) return NV_ENC_ERR_NO_ENCODE_DEVICE;
    struct query_session *s = calloc(1, sizeof(*s));
    if (!s) return NV_ENC_ERR_OUT_OF_MEMORY;
    if (cuCtxCreate(&s->context, 0, selected) != CUDA_SUCCESS) { free(s); return NV_ENC_ERR_INVALID_DEVICE; }
    s->api.version = NV_ENCODE_API_FUNCTION_LIST_VER;
    status = native_instance(&s->api);
    if (status == NV_ENC_SUCCESS) {
        NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS params = {
            .version = NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS_VER,
            .deviceType = NV_ENC_DEVICE_TYPE_CUDA, .device = s->context, .apiVersion = NVENCAPI_VERSION};
        status = s->api.nvEncOpenEncodeSessionEx(&params, &s->encoder);
    }
    CUcontext popped;
    if (cuCtxPopCurrent(&popped) != CUDA_SUCCESS) status = NV_ENC_ERR_GENERIC;
    if (status) { uurb_nvenc_query_close(s); return status; }
    *output = s;
    return NV_ENC_SUCCESS;
}

int uurb_nvenc_query_close(void *session)
{
    struct query_session *s = session;
    if (!s) return NV_ENC_ERR_INVALID_ENCODERDEVICE;
    if (cuCtxPushCurrent(s->context) != CUDA_SUCCESS) return NV_ENC_ERR_GENERIC;
    int status = s->encoder ? s->api.nvEncDestroyEncoder(s->encoder) : NV_ENC_SUCCESS;
    CUcontext popped;
    if (cuCtxPopCurrent(&popped) != CUDA_SUCCESS) status = NV_ENC_ERR_GENERIC;
    if (cuCtxDestroy(s->context) != CUDA_SUCCESS) status = NV_ENC_ERR_GENERIC;
    free(s);
    return status;
}

int uurb_nvenc_query(void *session, unsigned op, const GUID *codec,
                     void *output, uint32_t capacity_or_cap, uint32_t *count)
{
    struct query_session *s = session;
    if (!s) return NV_ENC_ERR_INVALID_ENCODERDEVICE;
    if (op > UURB_CAP || (op >= UURB_PROFILE_COUNT && !codec)) return NV_ENC_ERR_INVALID_PARAM;
    if (cuCtxPushCurrent(s->context) != CUDA_SUCCESS) return NV_ENC_ERR_GENERIC;
    int status = NV_ENC_ERR_INVALID_PARAM;
    switch (op) {
    case UURB_CODEC_COUNT: status = s->api.nvEncGetEncodeGUIDCount(s->encoder, count); break;
    case UURB_CODECS: status = s->api.nvEncGetEncodeGUIDs(s->encoder, output, capacity_or_cap, count); break;
    case UURB_PROFILE_COUNT: status = s->api.nvEncGetEncodeProfileGUIDCount(s->encoder, *codec, count); break;
    case UURB_PROFILES: status = s->api.nvEncGetEncodeProfileGUIDs(s->encoder, *codec, output, capacity_or_cap, count); break;
    case UURB_FORMAT_COUNT: status = s->api.nvEncGetInputFormatCount(s->encoder, *codec, count); break;
    case UURB_FORMATS: status = s->api.nvEncGetInputFormats(s->encoder, *codec, output, capacity_or_cap, count); break;
    case UURB_PRESET_COUNT: status = s->api.nvEncGetEncodePresetCount(s->encoder, *codec, count); break;
    case UURB_PRESETS: status = s->api.nvEncGetEncodePresetGUIDs(s->encoder, *codec, output, capacity_or_cap, count); break;
    case UURB_CAP: {
        NV_ENC_CAPS_PARAM params = {.version = NV_ENC_CAPS_PARAM_VER, .capsToQuery = capacity_or_cap};
        status = s->api.nvEncGetEncodeCaps(s->encoder, *codec, &params, output);
        break;
    }
    }
    CUcontext popped;
    if (cuCtxPopCurrent(&popped) != CUDA_SUCCESS) status = NV_ENC_ERR_GENERIC;
    return status;
}

int uurb_nvenc_query_preset(void *session, const GUID *codec, const GUID *preset,
                            int extended, NV_ENC_TUNING_INFO tuning, NV_ENC_PRESET_CONFIG *config)
{
    struct query_session *s = session;
    if (!s) return NV_ENC_ERR_INVALID_ENCODERDEVICE;
    if (!codec || !preset || !config) return NV_ENC_ERR_INVALID_PTR;
    if (cuCtxPushCurrent(s->context) != CUDA_SUCCESS) return NV_ENC_ERR_GENERIC;
    int status = extended
        ? s->api.nvEncGetEncodePresetConfigEx(s->encoder, *codec, *preset, tuning, config)
        : s->api.nvEncGetEncodePresetConfig(s->encoder, *codec, *preset, config);
    CUcontext popped;
    if (cuCtxPopCurrent(&popped) != CUDA_SUCCESS) status = NV_ENC_ERR_GENERIC;
    return status;
}

int uurb_nvenc_query_sequence(void *session, const NV_ENC_INITIALIZE_PARAMS *requested,
                             void *bytes, uint32_t capacity, uint32_t *size)
{
    struct query_session *s = session;
    if (!s) return NV_ENC_ERR_INVALID_ENCODERDEVICE;
    if (!requested || !requested->encodeConfig || !bytes || !size) return NV_ENC_ERR_INVALID_PTR;
    if (!capacity || capacity > 65536) return NV_ENC_ERR_INVALID_PARAM;
    if (!s->api.nvEncGetSequenceParamEx) return NV_ENC_ERR_UNIMPLEMENTED;
    NV_ENC_CONFIG config = *requested->encodeConfig;
    NV_ENC_INITIALIZE_PARAMS init = *requested;
    init.encodeConfig = &config;
    unsigned char buffer[65536];
    uint32_t written = 0;
    NV_ENC_SEQUENCE_PARAM_PAYLOAD payload = {.version = NV_ENC_SEQUENCE_PARAM_PAYLOAD_VER,
        .inBufferSize = sizeof(buffer), .spsppsBuffer = buffer, .outSPSPPSPayloadSize = &written};
    if (cuCtxPushCurrent(s->context) != CUDA_SUCCESS) return NV_ENC_ERR_GENERIC;
    int status = s->api.nvEncGetSequenceParamEx(s->encoder, &init, &payload);
    CUcontext popped;
    if (cuCtxPopCurrent(&popped) != CUDA_SUCCESS) return NV_ENC_ERR_GENERIC;
    if (status) return status;
    if (!written || written > sizeof(buffer)) return NV_ENC_ERR_GENERIC;
    if (written > capacity) return NV_ENC_ERR_NOT_ENOUGH_BUFFER;
    memcpy(bytes, buffer, written);
    *size = written;
    return NV_ENC_SUCCESS;
}
