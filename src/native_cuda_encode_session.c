/* Native SysV synchronous session. Import/register/allocation happen
 * once, not per frame. Driver failures poison the session: close and recreate. */
#include "native_cuda_encode_session.h"
#include "native_nvenc_reference_config.h"
#include "native_nvenc_negotiation.h"
#include <cuda.h>
#include <ffnvcodec/nvEncodeAPI.h>
#include <dlfcn.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

struct uurb_encoder {
    CUcontext context;
    CUexternalMemory memory;
    CUmipmappedArray mipmap;
    NV_ENCODE_API_FUNCTION_LIST api;
    void *encoder;
    NV_ENC_REGISTERED_PTR resource;
    NV_ENC_INPUT_PTR mapped;
    NV_ENC_OUTPUT_PTR bitstream;
    NV_ENC_CONFIG config;
    NV_ENC_INITIALIZE_PARAMS init;
    uint64_t last_timestamp;
    unsigned frames;
    int locked, failed, initialized, pending_idr;
};

static pthread_once_t driver_once = PTHREAD_ONCE_INIT;
static NVENCSTATUS (*driver_version)(uint32_t *);
static NVENCSTATUS (*driver_instance)(NV_ENCODE_API_FUNCTION_LIST *);
static void load_driver(void)
{
    /* A future Windows encoder DLL exports these same names with a different
     * ABI. Never resolve native calls through ELF global symbol interposition.
     * Keep the successfully loaded module alive for all session function tables. */
    void *library = dlopen("libnvidia-encode.so.1", RTLD_NOW | RTLD_LOCAL);
    if (!library) return;
    NVENCSTATUS (*version)(uint32_t *) = (void *)dlsym(library, "NvEncodeAPIGetMaxSupportedVersion");
    NVENCSTATUS (*instance)(NV_ENCODE_API_FUNCTION_LIST *) = (void *)dlsym(library, "NvEncodeAPICreateInstance");
    if (!version || !instance) { dlclose(library); return; }
    driver_version = version;
    driver_instance = instance;
}

static int cuda_ok(CUresult error, const char *operation)
{
    if (error == CUDA_SUCCESS) return 1;
    const char *name = NULL;
    cuGetErrorName(error, &name);
    fprintf(stderr, "%s: %s (%d)\n", operation, name ? name : "CUDA error", error);
    return 0;
}
static int nv_ok(struct uurb_encoder *s, NVENCSTATUS error, const char *operation)
{
    if (error == NV_ENC_SUCCESS) return 1;
    fprintf(stderr, "%s: NVENC error %d %s\n", operation, error,
        s->encoder && s->api.nvEncGetLastErrorString ? s->api.nvEncGetLastErrorString(s->encoder) : "");
    s->failed = 1;
    return 0;
}
#define CU(call) do { if (!cuda_ok((call), #call)) goto done; } while (0)
#define NV(call) do { if (!nv_ok(s, (call), #call)) goto done; } while (0)

static int pop_context(void)
{
    CUcontext previous;
    return cuda_ok(cuCtxPopCurrent(&previous), "cuCtxPopCurrent");
}

static struct uurb_encoder *encoder_open(int fd, uint64_t bytes,
    const unsigned char uuid[16], unsigned width, unsigned height, int hevc, unsigned bitrate,
    const NV_ENC_INITIALIZE_PARAMS *requested)
{
    struct uurb_encoder *s = calloc(1, sizeof(*s));
    int ready = 0, current = 0;
    if (!s || fd < 0 || !bytes || !uuid || width < 2 || height < 2 ||
        width > 8192 || height > 8192 || width % 2 || height % 2 ||
        (uint64_t)width * height > 16777216 || (hevc != 0 && hevc != 1) || !bitrate) goto done;
    pthread_once(&driver_once, load_driver);
    if (!driver_version || !driver_instance) {
        fprintf(stderr, "Native NVENC driver entry points unavailable\n"); goto done;
    }
    CU(cuInit(0));
    int count = 0, selected = -1;
    CU(cuDeviceGetCount(&count));
    for (int index = 0; index < count; ++index) {
        CUuuid candidate;
        CU(cuDeviceGetUuid(&candidate, index));
        if (!memcmp(candidate.bytes, uuid, 16)) { selected = index; break; }
    }
    if (selected < 0) { fprintf(stderr, "No CUDA device matches the Vulkan device UUID\n"); goto done; }
    CU(cuCtxCreate(&s->context, 0, selected));
    current = 1;
    CUDA_EXTERNAL_MEMORY_HANDLE_DESC external = {0};
    external.type = CU_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD;
    external.handle.fd = fd; external.size = bytes; external.flags = CUDA_EXTERNAL_MEMORY_DEDICATED;
    CU(cuImportExternalMemory(&s->memory, &external));
    fd = -1;
    CUDA_EXTERNAL_MEMORY_MIPMAPPED_ARRAY_DESC desc = {0};
    desc.arrayDesc.Width = width; desc.arrayDesc.Height = height;
    desc.arrayDesc.Format = CU_AD_FORMAT_UNSIGNED_INT8; desc.arrayDesc.NumChannels = 4;
    desc.arrayDesc.Flags = CUDA_ARRAY3D_SURFACE_LDST | CUDA_ARRAY3D_COLOR_ATTACHMENT;
    desc.numLevels = 1;
    CU(cuExternalMemoryGetMappedMipmappedArray(&s->mipmap, s->memory, &desc));
    CUarray array;
    CU(cuMipmappedArrayGetLevel(&array, s->mipmap, 0));
    s->api.version = NV_ENCODE_API_FUNCTION_LIST_VER;
    uint32_t supported = 0;
    NV(driver_version(&supported));
    if (supported < ((NVENCAPI_MAJOR_VERSION << 4) | NVENCAPI_MINOR_VERSION)) {
        fprintf(stderr, "Driver does not support the pinned NVENC API\n"); goto done;
    }
    NV(driver_instance(&s->api));
    NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS session = {
        .version = NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS_VER,
        .deviceType = NV_ENC_DEVICE_TYPE_CUDA, .device = s->context, .apiVersion = NVENCAPI_VERSION};
    NV(s->api.nvEncOpenEncodeSessionEx(&session, &s->encoder));
    GUID codec = hevc ? NV_ENC_CODEC_HEVC_GUID : NV_ENC_CODEC_H264_GUID;
    NV_ENC_PRESET_CONFIG preset = {.version = NV_ENC_PRESET_CONFIG_VER};
    preset.presetCfg.version = NV_ENC_CONFIG_VER;
    NV(s->api.nvEncGetEncodePresetConfigEx(s->encoder, codec, NV_ENC_PRESET_P1_GUID,
        NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY, &preset));
    if (requested) {
        s->config = *requested->encodeConfig;
        s->init = *requested;
        s->init.encodeConfig = &s->config;
    } else uurb_nvenc_reference_config(&preset.presetCfg, width, height, hevc, bitrate, &s->config, &s->init);
    NV(s->api.nvEncInitializeEncoder(s->encoder, &s->init));
    s->initialized = 1;
    NV_ENC_REGISTER_RESOURCE resource = {.version = NV_ENC_REGISTER_RESOURCE_VER,
        .resourceType = NV_ENC_INPUT_RESOURCE_TYPE_CUDAARRAY,
        .width = width, .height = height, .pitch = width * 4,
        .resourceToRegister = array, .bufferFormat = NV_ENC_BUFFER_FORMAT_ARGB,
        .bufferUsage = NV_ENC_INPUT_IMAGE};
    NV(s->api.nvEncRegisterResource(s->encoder, &resource));
    s->resource = resource.registeredResource;
    NV_ENC_CREATE_BITSTREAM_BUFFER bitstream = {.version = NV_ENC_CREATE_BITSTREAM_BUFFER_VER};
    NV(s->api.nvEncCreateBitstreamBuffer(s->encoder, &bitstream));
    s->bitstream = bitstream.bitstreamBuffer;
    fprintf(stderr, "CUDA imported shared GPU image; exact Vulkan/CUDA UUID match; %s persistent session\n",
        hevc ? "HEVC" : "H264");
    ready = 1;
done:
    if (fd >= 0) close(fd);
    if (current && !pop_context()) ready = 0;
    if (!ready) {
        if (uurb_encoder_close(s)) _Exit(4);
        return NULL;
    }
    return s;
}

struct uurb_encoder *uurb_encoder_open(int fd, uint64_t bytes,
    const unsigned char uuid[16], unsigned width, unsigned height, int hevc, unsigned bitrate)
{ return encoder_open(fd, bytes, uuid, width, height, hevc, bitrate, NULL); }

struct uurb_encoder *uurb_encoder_open_config(int fd, uint64_t bytes,
    const unsigned char uuid[16], const NV_ENC_INITIALIZE_PARAMS *p)
{
    if (!p || p->version != NV_ENC_INITIALIZE_PARAMS_VER || !p->encodeConfig ||
        p->encodeConfig->version != NV_ENC_CONFIG_VER || p->enableEncodeAsync || !p->enablePTD ||
        p->encodeConfig->frameIntervalP != 1 || p->encodeConfig->rcParams.enableLookahead ||
        p->encodeConfig->rcParams.enableExtLookahead ||
        memcmp(&p->presetGUID, &NV_ENC_PRESET_P1_GUID, sizeof(GUID)) ||
        p->tuningInfo != NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY ||
        (memcmp(&p->encodeGUID, &NV_ENC_CODEC_HEVC_GUID, sizeof(GUID)) &&
         memcmp(&p->encodeGUID, &NV_ENC_CODEC_H264_GUID, sizeof(GUID)))) {
        if (fd >= 0) close(fd);
        return NULL;
    }
    int hevc = !memcmp(&p->encodeGUID, &NV_ENC_CODEC_HEVC_GUID, sizeof(GUID));
    return encoder_open(fd, bytes, uuid, p->encodeWidth, p->encodeHeight, hevc,
                        p->encodeConfig->rcParams.averageBitRate, p);
}

int uurb_encoder_sequence(struct uurb_encoder *s, void *bytes, uint32_t capacity, uint32_t *size)
{
    if (!bytes || !size) return NV_ENC_ERR_INVALID_PTR;
    if (!capacity || capacity > 65536) return NV_ENC_ERR_INVALID_PARAM;
    if (!s || !s->initialized) return NV_ENC_ERR_ENCODER_NOT_INITIALIZED;
    if (s->failed || s->locked || s->mapped) return NV_ENC_ERR_INVALID_CALL;
    unsigned char buffer[65536];
    uint32_t written = 0;
    NV_ENC_SEQUENCE_PARAM_PAYLOAD payload = {.version = NV_ENC_SEQUENCE_PARAM_PAYLOAD_VER,
        .inBufferSize = sizeof(buffer), .spsppsBuffer = buffer, .outSPSPPSPayloadSize = &written};
    if (!cuda_ok(cuCtxPushCurrent(s->context), "sequence cuCtxPushCurrent")) {
        s->failed = 1; return NV_ENC_ERR_GENERIC;
    }
    NVENCSTATUS status = s->api.nvEncGetSequenceParams(s->encoder, &payload);
    if (!pop_context()) { s->failed = 1; return NV_ENC_ERR_GENERIC; }
    if (status) return status;
    if (!written || written > sizeof(buffer)) { s->failed = 1; return NV_ENC_ERR_GENERIC; }
    if (written > capacity) return NV_ENC_ERR_NOT_ENOUGH_BUFFER;
    memcpy(bytes, buffer, written);
    *size = written;
    return NV_ENC_SUCCESS;
}

int uurb_encoder_encode(struct uurb_encoder *s, uint64_t timestamp, int force_idr, struct uurb_packet *packet)
{
    if (packet) memset(packet, 0, sizeof(*packet));
    if (!s || !packet || s->failed || s->locked || s->mapped ||
        (s->frames && timestamp <= s->last_timestamp)) return 1;
    int result = 1, current = 0;
    CU(cuCtxPushCurrent(s->context));
    current = 1;
    NV_ENC_MAP_INPUT_RESOURCE mapped = {.version = NV_ENC_MAP_INPUT_RESOURCE_VER,
        .registeredResource = s->resource};
    NV(s->api.nvEncMapInputResource(s->encoder, &mapped));
    s->mapped = mapped.mappedResource;
    NV_ENC_PIC_PARAMS picture = {
        .version = NV_ENC_PIC_PARAMS_VER, .inputWidth = s->init.encodeWidth, .inputHeight = s->init.encodeHeight,
        .inputPitch = s->init.encodeWidth * 4, .inputBuffer = s->mapped,
        .bufferFmt = mapped.mappedBufferFmt, .outputBitstream = s->bitstream,
        .pictureStruct = NV_ENC_PIC_STRUCT_FRAME,
        .encodePicFlags = (!s->frames || force_idr || s->pending_idr)
            ? NV_ENC_PIC_FLAG_FORCEIDR | NV_ENC_PIC_FLAG_OUTPUT_SPSPPS : 0,
        .inputTimeStamp = timestamp, .inputDuration = 1};
    /* No B frames/lookahead. NEED_MORE_INPUT fails this synchronous contract;
     * never lock an output buffer when no output was produced. */
    NV(s->api.nvEncEncodePicture(s->encoder, &picture));
    NV_ENC_LOCK_BITSTREAM output = {.version = NV_ENC_LOCK_BITSTREAM_VER, .outputBitstream = s->bitstream};
    NV(s->api.nvEncLockBitstream(s->encoder, &output));
    s->locked = 1;
    if (!output.bitstreamSizeInBytes || output.outputTimeStamp != timestamp) goto done;
    *packet = (struct uurb_packet){.data = output.bitstreamBufferPtr, .size = output.bitstreamSizeInBytes,
        .timestamp = output.outputTimeStamp, .idr = output.pictureType == NV_ENC_PIC_TYPE_IDR,
        .average_qp = output.frameAvgQP, .picture_type = output.pictureType};
    s->last_timestamp = timestamp;
    s->frames++;
    s->pending_idr = 0;
    result = 0;
done:
    if (current && !pop_context()) result = 1;
    if (result) s->failed = 1;
    return result;
}

int uurb_encoder_release_packet(struct uurb_encoder *s)
{
    if (!s || s->failed || !s->locked) return 1;
    int result = 1, current = 0;
    CU(cuCtxPushCurrent(s->context));
    current = 1;
    NV(s->api.nvEncUnlockBitstream(s->encoder, s->bitstream));
    s->locked = 0;
    NV(s->api.nvEncUnmapInputResource(s->encoder, s->mapped));
    s->mapped = NULL;
    CU(cuCtxSynchronize());
    result = 0;
done:
    if (current && !pop_context()) result = 1;
    if (result) s->failed = 1;
    return result;
}

int uurb_encoder_reconfigure(struct uurb_encoder *s, unsigned bitrate)
{
    if (!s || s->failed || s->locked || s->mapped || !bitrate) return 1;
    NV_ENC_CONFIG config = s->config;
    config.rcParams.averageBitRate = config.rcParams.maxBitRate = bitrate;
    config.rcParams.vbvBufferSize = config.rcParams.vbvInitialDelay = bitrate / 60;
    NV_ENC_RECONFIGURE_PARAMS change = {.version = NV_ENC_RECONFIGURE_PARAMS_VER,
        .reInitEncodeParams = s->init, .resetEncoder = 1, .forceIDR = 1};
    change.reInitEncodeParams.encodeConfig = &config;
    return uurb_encoder_reconfigure_config(s, &change);
}

int uurb_encoder_reconfigure_config(struct uurb_encoder *s, const NV_ENC_RECONFIGURE_PARAMS *requested)
{
    if (!s || s->failed || s->locked || s->mapped || !s->initialized ||
        !uurb_nvenc_valid_reconfigure(&s->config, &s->init, requested)) return 1;
    int result = 1, current = 0;
    NV_ENC_CONFIG config = *requested->reInitEncodeParams.encodeConfig;
    NV_ENC_RECONFIGURE_PARAMS change = *requested;
    change.reInitEncodeParams.encodeConfig = &config;
    CU(cuCtxPushCurrent(s->context));
    current = 1;
    NV(s->api.nvEncReconfigureEncoder(s->encoder, &change));
    s->config = config;
    s->init = change.reInitEncodeParams;
    s->init.encodeConfig = &s->config;
    s->pending_idr |= change.forceIDR;
    result = 0;
done:
    if (current && !pop_context()) result = 1;
    if (result) s->failed = 1;
    return result;
}

int uurb_encoder_close(struct uurb_encoder *s)
{
    if (!s) return 0;
    int failed = 0;
    if (s->context) {
        if (!cuda_ok(cuCtxPushCurrent(s->context), "close push context")) return 1;
        if (!cuda_ok(cuCtxSynchronize(), "close synchronize")) failed = 1;
    }
#define CLEAN_NV(call) do { if (!nv_ok(s, (call), #call)) failed = 1; } while (0)
    if (s->locked) CLEAN_NV(s->api.nvEncUnlockBitstream(s->encoder, s->bitstream));
    if (s->mapped) CLEAN_NV(s->api.nvEncUnmapInputResource(s->encoder, s->mapped));
    if (s->initialized && !s->failed) {
        NV_ENC_PIC_PARAMS eos = {.version = NV_ENC_PIC_PARAMS_VER, .encodePicFlags = NV_ENC_PIC_FLAG_EOS};
        CLEAN_NV(s->api.nvEncEncodePicture(s->encoder, &eos));
    }
    if (s->resource) CLEAN_NV(s->api.nvEncUnregisterResource(s->encoder, s->resource));
    if (s->bitstream) CLEAN_NV(s->api.nvEncDestroyBitstreamBuffer(s->encoder, s->bitstream));
    if (s->encoder) CLEAN_NV(s->api.nvEncDestroyEncoder(s->encoder));
    if (s->mipmap && !cuda_ok(cuMipmappedArrayDestroy(s->mipmap), "destroy mipmap")) failed = 1;
    if (s->memory && !cuda_ok(cuDestroyExternalMemory(s->memory), "destroy external memory")) failed = 1;
    if (s->context) {
        if (!pop_context()) failed = 1;
        if (!cuda_ok(cuCtxDestroy(s->context), "destroy context")) failed = 1;
    }
    free(s);
    return failed;
}
