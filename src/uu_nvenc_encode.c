/* Opt-in standard Windows NVENC function-table encoder. Must run in an
 * owned, recoverable worker: underlying GPU timeouts exit it.
 * Accepts a bounded standard synchronous rate-control configuration. Unknown
 * options fail, never silently become a different negotiated codec/profile.
 * The query fixture remains a separately built, unchanged query-only module. */
#define NvEncodeAPICreateInstance uurb_query_create_instance
#define NvEncodeAPIGetMaxSupportedVersion uurb_query_supported_version
#include "uu_nvenc_query.c"
#undef NvEncodeAPICreateInstance
#undef NvEncodeAPIGetMaxSupportedVersion
#include "uu_d3d11_encode_session.h"
#include "native_nvenc_reference_config.h"
#include "native_nvenc_negotiation.h"
#include <stdio.h>
#include <unistd.h>
#ifdef UURB_NVENC_TRACE
#include "uu_nvenc_trace.h"
#endif

#define LIMIT 16
#define PACKET_LIMIT (4u * 1024u * 1024u)
/* Called under guard; bounded lifecycle evidence, never frame payloads,
 * pointers or vendor/account data. No per-frame logging in steady state. */
enum lifecycle_event { LIFE_INIT, LIFE_RECONFIGURE, LIFE_FIRST_FRAME, LIFE_DESTROY };
static void lifecycle(enum lifecycle_event event, const NV_ENC_INITIALIZE_PARAMS *p, NVENCSTATUS status)
{
    static unsigned reports[4];
    static const char *names[] = {"initialize", "reconfigure", "first_frame", "destroy"};
    if (reports[event]++ >= 64) { reports[event] = 64; return; }
    unsigned codec = p ? !memcmp(&p->encodeGUID, &NV_ENC_CODEC_H264_GUID, sizeof(GUID)) ? 1 :
        !memcmp(&p->encodeGUID, &NV_ENC_CODEC_HEVC_GUID, sizeof(GUID)) ? 2 : 0 : 0;
    fprintf(stderr, "UURB_NATIVE_ENCODER {\"event\":\"%s\",\"peer_pid\":%u,\"tick_ms\":%llu,\"status\":%u,"
        "\"width\":%u,\"height\":%u,\"rate_num\":%u,\"rate_den\":%u,\"codec\":%u}\n",
        names[event], (unsigned)getpid(), (unsigned long long)GetTickCount64(), (unsigned)status,
        p ? p->encodeWidth : 0, p ? p->encodeHeight : 0,
        p ? p->frameRateNum : 0, p ? p->frameRateDen : 0, codec);
}
struct input_resource {
    void *token, *mapped;
    uint64_t bridge_token;
    NV_ENC_BUFFER_FORMAT format;
};
struct output_resource {
    void *token;
    unsigned char *bytes;
    uint32_t size, frame_index, average_qp, picture_type;
    uint64_t timestamp, duration;
    int ready, locked;
};
struct encode_state {
    struct encode_state *next;
    void *token;
    struct uurb_d3d11_encoder *bridge;
    NV_ENC_CONFIG config;
    NV_ENC_INITIALIZE_PARAMS init;
    struct input_resource inputs[LIMIT];
    struct output_resource outputs[LIMIT];
    uint64_t last_timestamp;
    unsigned frames;
    int failed, eos;
};
static struct encode_state *encoders;
static int all_zero(const void *data, size_t size)
{
    const unsigned char *bytes = data;
    for (size_t i = 0; i < size; ++i) if (bytes[i]) return 0;
    return 1;
}
#define ZERO_FIELD(params, field) all_zero((params)->field, sizeof((params)->field))

/* All state, including base query sessions, is protected by the shared guard.
 * Handles are never dereferenced and never reused during this process. */
static void *new_handle(void)
{ return next_token == UINTPTR_MAX ? NULL : (void *)next_token++; }
static struct encode_state *find_encoder(void *token)
{
    for (struct encode_state *s = encoders; s; s = s->next) if (s->token == token) return s;
    return NULL;
}
static NVENCSTATUS missing_encoder(void *token)
{ return find_session(token) ? NV_ENC_ERR_ENCODER_NOT_INITIALIZED : NV_ENC_ERR_INVALID_ENCODERDEVICE; }
static int supported_codec(const GUID *codec)
{ return !memcmp(codec, &NV_ENC_CODEC_H264_GUID, sizeof(*codec)) || !memcmp(codec, &NV_ENC_CODEC_HEVC_GUID, sizeof(*codec)); }
static NVENCSTATUS from_hresult(HRESULT result)
{
    if (SUCCEEDED(result)) return NV_ENC_SUCCESS;
    if (result == E_OUTOFMEMORY) return NV_ENC_ERR_OUT_OF_MEMORY;
    if (result == E_INVALIDARG) return NV_ENC_ERR_INVALID_PARAM;
    if (result == E_NOTIMPL) return NV_ENC_ERR_UNSUPPORTED_PARAM;
    return NV_ENC_ERR_GENERIC;
}
static struct input_resource *find_input(struct encode_state *s, void *token, int mapped)
{
    if (token) for (unsigned i = 0; i < LIMIT; ++i)
        if ((mapped ? s->inputs[i].mapped : s->inputs[i].token) == token) return &s->inputs[i];
    return NULL;
}
static struct output_resource *find_output(struct encode_state *s, void *token)
{
    if (token) for (unsigned i = 0; i < LIMIT; ++i)
        if (s->outputs[i].token == token) return &s->outputs[i];
    return NULL;
}
static NVENCSTATUS NVENCAPI encode_initialize(void *token, NV_ENC_INITIALIZE_PARAMS *params)
{
    if (!params || !params->encodeConfig) return NV_ENC_ERR_INVALID_PTR;
    if (params->version != NV_ENC_INITIALIZE_PARAMS_VER || params->encodeConfig->version != NV_ENC_CONFIG_VER)
        return NV_ENC_ERR_INVALID_VERSION;
    AcquireSRWLockExclusive(&guard);
    struct session *query_session = find_session(token);
    struct encode_state *s = NULL;
    NVENCSTATUS status = NV_ENC_ERR_INVALID_ENCODERDEVICE;
    if (!query_session) goto done;
    status = NV_ENC_ERR_INVALID_CALL;
    if (find_encoder(token)) goto done;
    status = NV_ENC_ERR_UNSUPPORTED_PARAM;
    if (!supported_codec(&params->encodeGUID) || !params->encodeConfig->rcParams.averageBitRate ||
        memcmp(&params->presetGUID, &NV_ENC_PRESET_P1_GUID, sizeof(GUID))) goto done;
    NV_ENC_PRESET_CONFIG preset = {.version = NV_ENC_PRESET_CONFIG_VER};
    preset.presetCfg.version = NV_ENC_CONFIG_VER;
    status = uurb_nvenc_query_preset(query_session->native, &params->encodeGUID, &params->presetGUID,
        1, NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY, &preset);
    if (status) goto done;
    s = calloc(1, sizeof(*s));
    status = NV_ENC_ERR_OUT_OF_MEMORY;
    if (!s) goto done;
    int hevc = !memcmp(&params->encodeGUID, &NV_ENC_CODEC_HEVC_GUID, sizeof(GUID));
#ifdef UURB_NVENC_TRACE
    uurb_nvenc_reference_config(&preset.presetCfg, params->encodeWidth, params->encodeHeight,
        hevc, params->encodeConfig->rcParams.averageBitRate, &s->config, &s->init);
    NV_ENC_INITIALIZE_PARAMS expected = s->init;
    trace_config_diff(params, &expected, params->encodeConfig, &s->config, hevc);
#endif
    status = NV_ENC_ERR_UNSUPPORTED_PARAM;
    if (!uurb_nvenc_negotiate(params, &preset.presetCfg, hevc, &s->config, &s->init)) goto done;
    status = from_hresult(uurb_d3d11_encoder_open(query_session->device, params->encodeWidth,
        params->encodeHeight, hevc, s->config.rcParams.averageBitRate, &s->bridge));
    if (status) goto done;
    status = from_hresult(uurb_d3d11_encoder_initialize(s->bridge, &s->init));
    if (status) goto done;
    s->token = token; s->next = encoders; encoders = s; s = NULL;
done:
    if (s && FAILED(uurb_d3d11_encoder_close(s->bridge))) _Exit(4);
    free(s);
    lifecycle(LIFE_INIT, params, status);
    ReleaseSRWLockExclusive(&guard);
    return status;
}

static NVENCSTATUS NVENCAPI register_resource(void *token, NV_ENC_REGISTER_RESOURCE *params)
{
    if (!params || !params->resourceToRegister) return NV_ENC_ERR_INVALID_PTR;
    if (params->version != NV_ENC_REGISTER_RESOURCE_VER) return NV_ENC_ERR_INVALID_VERSION;
    if (params->resourceType != NV_ENC_INPUT_RESOURCE_TYPE_DIRECTX || params->subResourceIndex || params->pitch ||
        params->pInputFencePoint || params->bufferUsage != NV_ENC_INPUT_IMAGE ||
        !ZERO_FIELD(params, chromaOffsetIn) || !ZERO_FIELD(params, reserved1) || !ZERO_FIELD(params, reserved2) ||
        (params->bufferFormat != NV_ENC_BUFFER_FORMAT_NV12 && params->bufferFormat != NV_ENC_BUFFER_FORMAT_ARGB))
        return NV_ENC_ERR_UNSUPPORTED_PARAM;
    AcquireSRWLockExclusive(&guard);
    struct encode_state *s = find_encoder(token);
    ID3D11Texture2D *texture = NULL;
    NVENCSTATUS status = missing_encoder(token);
    if (!s) goto done;
    status = NV_ENC_ERR_INVALID_CALL;
    if (s->failed || s->eos) goto done;
    status = NV_ENC_ERR_INVALID_PARAM;
    if (FAILED(IUnknown_QueryInterface((IUnknown *)params->resourceToRegister, &IID_ID3D11Texture2D, (void **)&texture))) goto done;
    D3D11_TEXTURE2D_DESC desc;
    ID3D11Texture2D_GetDesc(texture, &desc);
#ifdef UURB_NVENC_TRACE
    fprintf(stderr, "UURB_NVENC_TRACE {\"call\":\"input_texture\",\"width\":%u,\"height\":%u,\"format\":%u,\"bind\":%u,\"usage\":%u,\"mips\":%u,\"array\":%u,\"samples\":%u,\"misc\":%u,\"cpu_access\":%u}\n",
        desc.Width, desc.Height, desc.Format, desc.BindFlags, desc.Usage, desc.MipLevels,
        desc.ArraySize, desc.SampleDesc.Count, desc.MiscFlags, desc.CPUAccessFlags);
#endif
    if (params->width != desc.Width || params->height != desc.Height ||
        (params->bufferFormat == NV_ENC_BUFFER_FORMAT_NV12 ? desc.Format != DXGI_FORMAT_NV12 : desc.Format != DXGI_FORMAT_B8G8R8A8_UNORM)) goto done;
    unsigned index;
    for (index = 0; index < LIMIT && s->inputs[index].token; ++index) {}
    status = NV_ENC_ERR_OUT_OF_MEMORY;
    if (index == LIMIT || next_token == UINTPTR_MAX) goto done;
    uint64_t bridge_token;
    status = from_hresult(uurb_d3d11_encoder_register(s->bridge, texture, &bridge_token));
    if (status) goto done;
    s->inputs[index] = (struct input_resource){new_handle(), NULL, bridge_token, params->bufferFormat};
    params->registeredResource = s->inputs[index].token;
done:
    if (texture) ID3D11Texture2D_Release(texture);
    ReleaseSRWLockExclusive(&guard);
    return status;
}
static NVENCSTATUS NVENCAPI unregister_resource(void *token, NV_ENC_REGISTERED_PTR resource)
{
    AcquireSRWLockExclusive(&guard);
    struct encode_state *s = find_encoder(token);
    struct input_resource *input = s ? find_input(s, resource, 0) : NULL;
    NVENCSTATUS status = !s ? missing_encoder(token) : NV_ENC_ERR_RESOURCE_NOT_REGISTERED;
    if (!input) goto done;
    status = NV_ENC_ERR_INVALID_CALL;
    if (input->mapped || s->failed) goto done;
    status = from_hresult(uurb_d3d11_encoder_unregister(s->bridge, input->bridge_token));
    if (!status) *input = (struct input_resource){0};
done:
    ReleaseSRWLockExclusive(&guard);
    return status;
}
static NVENCSTATUS NVENCAPI map_resource(void *token, NV_ENC_MAP_INPUT_RESOURCE *params)
{
    if (!params) return NV_ENC_ERR_INVALID_PTR;
    if (params->version != NV_ENC_MAP_INPUT_RESOURCE_VER) return NV_ENC_ERR_INVALID_VERSION;
    if (params->subResourceIndex || params->inputResource ||
        !ZERO_FIELD(params, reserved1) || !ZERO_FIELD(params, reserved2)) return NV_ENC_ERR_UNSUPPORTED_PARAM;
    AcquireSRWLockExclusive(&guard);
    struct encode_state *s = find_encoder(token);
    struct input_resource *input = s ? find_input(s, params->registeredResource, 0) : NULL;
    NVENCSTATUS status = !s ? missing_encoder(token) : NV_ENC_ERR_RESOURCE_NOT_REGISTERED;
    if (!input) goto done;
    status = NV_ENC_ERR_INVALID_CALL;
    if (input->mapped || s->failed || s->eos) goto done;
    status = NV_ENC_ERR_OUT_OF_MEMORY;
    if (!(input->mapped = new_handle())) goto done;
    params->mappedResource = input->mapped; params->mappedBufferFmt = input->format;
    status = NV_ENC_SUCCESS;
done:
    ReleaseSRWLockExclusive(&guard);
    return status;
}
static NVENCSTATUS NVENCAPI unmap_resource(void *token, NV_ENC_INPUT_PTR resource)
{
    AcquireSRWLockExclusive(&guard);
    struct encode_state *s = find_encoder(token);
    struct input_resource *input = s ? find_input(s, resource, 1) : NULL;
    NVENCSTATUS status = !s ? missing_encoder(token) : NV_ENC_ERR_RESOURCE_NOT_MAPPED;
    if (input) { input->mapped = NULL; status = NV_ENC_SUCCESS; }
    ReleaseSRWLockExclusive(&guard);
    return status;
}
static NVENCSTATUS NVENCAPI create_bitstream(void *token, NV_ENC_CREATE_BITSTREAM_BUFFER *params)
{
    if (!params) return NV_ENC_ERR_INVALID_PTR;
    if (params->version != NV_ENC_CREATE_BITSTREAM_BUFFER_VER) return NV_ENC_ERR_INVALID_VERSION;
    if (params->size || params->memoryHeap || params->reserved ||
        !ZERO_FIELD(params, reserved1) || !ZERO_FIELD(params, reserved2)) return NV_ENC_ERR_UNSUPPORTED_PARAM;
    AcquireSRWLockExclusive(&guard);
    struct encode_state *s = find_encoder(token);
    NVENCSTATUS status = missing_encoder(token);
    if (!s) goto done;
    status = NV_ENC_ERR_INVALID_CALL;
    if (s->failed || s->eos) goto done;
    unsigned index;
    for (index = 0; index < LIMIT && s->outputs[index].token; ++index) {}
    status = NV_ENC_ERR_OUT_OF_MEMORY;
    if (index == LIMIT || next_token == UINTPTR_MAX) goto done;
    /* Bounded, persistent COMPRESSED storage; raw pixels never enter host RAM.
     * Allocation before Encode prevents losing a submitted frame to malloc. */
    unsigned char *bytes = malloc(PACKET_LIMIT);
    if (!bytes) goto done;
    s->outputs[index] = (struct output_resource){.token = new_handle(), .bytes = bytes};
    params->bitstreamBuffer = s->outputs[index].token; params->bitstreamBufferPtr = NULL;
    status = NV_ENC_SUCCESS;
done:
    ReleaseSRWLockExclusive(&guard);
    return status;
}
static NVENCSTATUS NVENCAPI destroy_bitstream(void *token, NV_ENC_OUTPUT_PTR resource)
{
    AcquireSRWLockExclusive(&guard);
    struct encode_state *s = find_encoder(token);
    struct output_resource *output = s ? find_output(s, resource) : NULL;
    NVENCSTATUS status = !s ? missing_encoder(token) : NV_ENC_ERR_INVALID_PARAM;
    if (!output) goto done;
    status = NV_ENC_ERR_INVALID_CALL;
    if (output->locked) goto done;
    free(output->bytes); *output = (struct output_resource){0}; status = NV_ENC_SUCCESS;
done:
    ReleaseSRWLockExclusive(&guard);
    return status;
}
static NVENCSTATUS NVENCAPI encode_picture(void *token, NV_ENC_PIC_PARAMS *params)
{
    if (!params) return NV_ENC_ERR_INVALID_PTR;
    if (params->version != NV_ENC_PIC_PARAMS_VER) return NV_ENC_ERR_INVALID_VERSION;
    AcquireSRWLockExclusive(&guard);
    struct encode_state *s = find_encoder(token);
    NVENCSTATUS status = missing_encoder(token);
    if (!s) goto done;
    status = NV_ENC_ERR_INVALID_CALL;
    if (s->failed || s->eos) goto done;
    if (params->encodePicFlags == NV_ENC_PIC_FLAG_EOS) {
        /* Synchronous, zero-delay mode has no queued input to flush. Outputs
         * already copied remain readable; no future input is accepted. */
        NV_ENC_PIC_PARAMS eos = {.version = NV_ENC_PIC_PARAMS_VER, .encodePicFlags = NV_ENC_PIC_FLAG_EOS};
        status = NV_ENC_ERR_UNSUPPORTED_PARAM;
        if (memcmp(params, &eos, sizeof(eos))) goto done;
        s->eos = 1; status = NV_ENC_SUCCESS; goto done;
    }
    struct input_resource *input = find_input(s, params->inputBuffer, 1);
    struct output_resource *output = find_output(s, params->outputBitstream);
    status = NV_ENC_ERR_INVALID_PARAM;
    if (!input || !output || params->inputWidth != s->init.encodeWidth || params->inputHeight != s->init.encodeHeight ||
        params->bufferFmt != input->format || (s->frames && params->inputTimeStamp <= s->last_timestamp)) goto done;
    status = NV_ENC_ERR_INVALID_CALL;
    if (output->locked || output->ready) goto done;
    /* Refuse every unimplemented per-picture option (SEI, QP maps, etc.). */
    NV_ENC_PIC_PARAMS expected = {.version = NV_ENC_PIC_PARAMS_VER,
        .inputWidth = params->inputWidth, .inputHeight = params->inputHeight,
        .frameIdx = params->frameIdx, .inputTimeStamp = params->inputTimeStamp, .inputDuration = params->inputDuration,
        .inputBuffer = params->inputBuffer, .outputBitstream = params->outputBitstream,
        .bufferFmt = params->bufferFmt, .pictureStruct = NV_ENC_PIC_STRUCT_FRAME, .encodePicFlags = params->encodePicFlags};
    status = NV_ENC_ERR_UNSUPPORTED_PARAM;
    if (memcmp(params, &expected, sizeof(expected)) ||
        (params->encodePicFlags & ~(NV_ENC_PIC_FLAG_FORCEIDR | NV_ENC_PIC_FLAG_OUTPUT_SPSPPS)) ||
        ((params->encodePicFlags & NV_ENC_PIC_FLAG_OUTPUT_SPSPPS) && !(params->encodePicFlags & NV_ENC_PIC_FLAG_FORCEIDR))) goto done;
    struct uurb_packet packet;
    status = from_hresult(uurb_d3d11_encoder_encode(s->bridge, input->bridge_token,
        params->inputTimeStamp, !!(params->encodePicFlags & NV_ENC_PIC_FLAG_FORCEIDR), &packet));
    if (status) { s->failed = 1; goto done; }
    if (packet.size > PACKET_LIMIT) {
        uurb_d3d11_encoder_release(s->bridge); s->failed = 1;
        status = NV_ENC_ERR_NOT_ENOUGH_BUFFER; goto done;
    }
    memcpy(output->bytes, packet.data, packet.size);
    output->size = packet.size; output->timestamp = packet.timestamp; output->duration = params->inputDuration;
    output->frame_index = params->frameIdx; output->average_qp = packet.average_qp; output->picture_type = packet.picture_type;
    status = from_hresult(uurb_d3d11_encoder_release(s->bridge));
    if (status) { s->failed = 1; goto done; }
    output->ready = 1; s->last_timestamp = params->inputTimeStamp; s->frames++;
    if (s->frames == 1) lifecycle(LIFE_FIRST_FRAME, &s->init, NV_ENC_SUCCESS);
done:
    ReleaseSRWLockExclusive(&guard);
    return status;
}
static NVENCSTATUS NVENCAPI lock_bitstream(void *token, NV_ENC_LOCK_BITSTREAM *params)
{
    if (!params) return NV_ENC_ERR_INVALID_PTR;
    if (params->version != NV_ENC_LOCK_BITSTREAM_VER) return NV_ENC_ERR_INVALID_VERSION;
    if (params->getRCStats || params->sliceOffsets || params->outputStatsPtr || params->outputStatsPtrSize ||
        params->reservedBitFields || params->reserved || !ZERO_FIELD(params, reserved1) ||
        !ZERO_FIELD(params, reserved2) || !ZERO_FIELD(params, reservedInternal))
        return NV_ENC_ERR_UNSUPPORTED_PARAM;
    AcquireSRWLockExclusive(&guard);
    struct encode_state *s = find_encoder(token);
    struct output_resource *output = s ? find_output(s, params->outputBitstream) : NULL;
    NVENCSTATUS status = !s ? missing_encoder(token) : NV_ENC_ERR_INVALID_PARAM;
    if (!output) goto done;
    status = NV_ENC_ERR_INVALID_CALL;
    if (s->failed || output->locked) goto done;
    status = NV_ENC_ERR_LOCK_BUSY;
    if (!output->ready) goto done;
    NV_ENC_LOCK_BITSTREAM value = {.version = NV_ENC_LOCK_BITSTREAM_VER, .doNotWait = params->doNotWait,
        .outputBitstream = params->outputBitstream, .frameIdx = output->frame_index,
        .bitstreamSizeInBytes = output->size, .outputTimeStamp = output->timestamp,
        .outputDuration = output->duration, .bitstreamBufferPtr = output->bytes,
        .pictureType = output->picture_type, .pictureStruct = NV_ENC_PIC_STRUCT_FRAME,
        .frameAvgQP = output->average_qp, .frameIdxDisplay = output->frame_index};
    *params = value; output->locked = 1; status = NV_ENC_SUCCESS;
#if defined(UURB_NVENC_TRACE) && defined(UURB_NVENC_DETECTOR_CONFIG)
    trace_synthetic_packet(&s->init, output->bytes, output->size);
#endif
done:
    ReleaseSRWLockExclusive(&guard);
    return status;
}
static NVENCSTATUS NVENCAPI unlock_bitstream(void *token, NV_ENC_OUTPUT_PTR resource)
{
    AcquireSRWLockExclusive(&guard);
    struct encode_state *s = find_encoder(token);
    struct output_resource *output = s ? find_output(s, resource) : NULL;
    NVENCSTATUS status = !s ? missing_encoder(token) : NV_ENC_ERR_INVALID_PARAM;
    if (output) {
        status = NV_ENC_ERR_INVALID_CALL;
        if (output->locked) { output->locked = output->ready = 0; output->size = 0; status = NV_ENC_SUCCESS; }
    }
    ReleaseSRWLockExclusive(&guard);
    return status;
}
static NVENCSTATUS NVENCAPI encode_destroy(void *token)
{
    AcquireSRWLockExclusive(&guard);
    struct session **query_link = &sessions;
    while (*query_link && (*query_link)->token != token) query_link = &(*query_link)->next;
    NVENCSTATUS status = NV_ENC_ERR_INVALID_ENCODERDEVICE;
    if (!*query_link) goto done;
    struct encode_state **link = &encoders;
    while (*link && (*link)->token != token) link = &(*link)->next;
    if (*link) {
        struct encode_state *s = *link;
        if (FAILED(uurb_d3d11_encoder_close(s->bridge))) _Exit(4);
        lifecycle(LIFE_DESTROY, &s->init, NV_ENC_SUCCESS);
        for (unsigned i = 0; i < LIMIT; ++i) free(s->outputs[i].bytes);
        *link = s->next; free(s);
    }
    struct session *query_session = *query_link;
    status = uurb_nvenc_query_close(query_session->native);
    *query_link = query_session->next; session_count--;
    ID3D11Device_Release(query_session->device); free(query_session);
done:
    ReleaseSRWLockExclusive(&guard);
    return status;
}

static NVENCSTATUS NVENCAPI reconfigure_encoder(void *token, NV_ENC_RECONFIGURE_PARAMS *params)
{
    if (!params || !params->reInitEncodeParams.encodeConfig) return NV_ENC_ERR_INVALID_PTR;
    if (params->version != NV_ENC_RECONFIGURE_PARAMS_VER ||
        params->reInitEncodeParams.version != NV_ENC_INITIALIZE_PARAMS_VER ||
        params->reInitEncodeParams.encodeConfig->version != NV_ENC_CONFIG_VER) return NV_ENC_ERR_INVALID_VERSION;
    AcquireSRWLockExclusive(&guard);
    struct encode_state *s = find_encoder(token);
    NVENCSTATUS status = missing_encoder(token);
    if (!s) goto done;
    status = NV_ENC_ERR_INVALID_CALL;
    if (s->failed || s->eos) goto done;
    status = NV_ENC_ERR_UNSUPPORTED_PARAM;
    unsigned mismatch = uurb_nvenc_reconfigure_mismatch(&s->config, &s->init, params);
    if (mismatch) {
        static unsigned reports;
        if (reports < 32) {
            ++reports;
            const NV_ENC_INITIALIZE_PARAMS *p = &params->reInitEncodeParams;
            const NV_ENC_CONFIG *c = p->encodeConfig;
            /* Named public SDK fields only. Reserved and pointer differences
             * are booleans, never byte dumps or process addresses. */
            fprintf(stderr, "UURB_NATIVE_ENCODER {\"event\":\"reconfigure_init_diff\",\"peer_pid\":%u,\"fields\":{", (unsigned)getpid());
            int first = 1;
#define INIT_DIFF(field) do { if (p->field != s->init.field) { \
    fprintf(stderr, "%s\"%s\":[%llu,%llu]", first ? "" : ",", #field, \
        (unsigned long long)s->init.field, (unsigned long long)p->field); first = 0; } } while (0)
            INIT_DIFF(enableEncodeAsync); INIT_DIFF(enablePTD); INIT_DIFF(reportSliceOffsets);
            INIT_DIFF(enableSubFrameWrite); INIT_DIFF(enableExternalMEHints); INIT_DIFF(enableMEOnlyMode);
            INIT_DIFF(enableWeightedPrediction); INIT_DIFF(splitEncodeMode); INIT_DIFF(enableOutputInVidmem);
            INIT_DIFF(enableReconFrameOutput); INIT_DIFF(enableOutputStats); INIT_DIFF(enableUniDirectionalB);
            INIT_DIFF(bufferFormat); INIT_DIFF(numStateBuffers); INIT_DIFF(outputStatsLevel); INIT_DIFF(tuningInfo);
#undef INIT_DIFF
            fprintf(stderr, "},\"codec_changed\":%u,\"preset_changed\":%u,\"outer_reserved_nonzero\":%u,"
                "\"private_pointer_changed\":%u,\"init_reserved_changed\":%u}\n",
                !!memcmp(&p->encodeGUID, &s->init.encodeGUID, sizeof(GUID)),
                !!memcmp(&p->presetGUID, &s->init.presetGUID, sizeof(GUID)),
                !!(params->reserved || params->reserved1 || params->reserved2),
                p->privData != s->init.privData,
                p->reserved != s->init.reserved ||
                    !!memcmp(p->reserved1, s->init.reserved1, sizeof(p->reserved1)) ||
                    !!memcmp(p->reserved2, s->init.reserved2, sizeof(p->reserved2)));
            fprintf(stderr, "UURB_NATIVE_ENCODER {\"event\":\"reconfigure_refused\",\"peer_pid\":%u,"
                "\"mismatch\":%u,\"geometry_changed\":%u,\"aspect_changed\":%u,\"maximum_changed\":%u,"
                "\"old_preset\":%u,\"new_preset\":%u,\"old_gop\":%u,\"new_gop\":%u,\"old_rc\":%u,\"new_rc\":%u,"
                "\"average_bitrate\":%u,\"max_bitrate\":%u,\"vbv\":%u,\"vbv_delay\":%u,"
                "\"old_interval\":%d,\"new_interval\":%d,\"old_aq\":%u,\"new_aq\":%u,"
                "\"old_multipass\":%u,\"new_multipass\":%u}\n",
                (unsigned)getpid(), mismatch,
                p->encodeWidth != s->init.encodeWidth || p->encodeHeight != s->init.encodeHeight,
                p->darWidth != s->init.darWidth || p->darHeight != s->init.darHeight,
                p->maxEncodeWidth != s->init.maxEncodeWidth || p->maxEncodeHeight != s->init.maxEncodeHeight,
                uurb_nvenc_preset_number(&s->init.presetGUID), uurb_nvenc_preset_number(&p->presetGUID),
                s->config.gopLength, c->gopLength, s->config.rcParams.rateControlMode, c->rcParams.rateControlMode,
                c->rcParams.averageBitRate, c->rcParams.maxBitRate, c->rcParams.vbvBufferSize, c->rcParams.vbvInitialDelay,
                s->config.frameIntervalP, c->frameIntervalP, s->config.rcParams.enableAQ, c->rcParams.enableAQ,
                s->config.rcParams.multiPass, c->rcParams.multiPass);
        }
        goto done;
    }
    status = from_hresult(uurb_d3d11_encoder_reconfigure_config(s->bridge, params));
    if (status) { s->failed = 1; goto done; }
    s->config = *params->reInitEncodeParams.encodeConfig;
    s->init = params->reInitEncodeParams;
    s->init.encodeConfig = &s->config;
done:
    lifecycle(LIFE_RECONFIGURE, &params->reInitEncodeParams, status);
    ReleaseSRWLockExclusive(&guard);
    return status;
}

static NVENCSTATUS validate_sequence_payload(const NV_ENC_SEQUENCE_PARAM_PAYLOAD *params)
{
    if (!params || !params->spsppsBuffer || !params->outSPSPPSPayloadSize) return NV_ENC_ERR_INVALID_PTR;
    if (params->version != NV_ENC_SEQUENCE_PARAM_PAYLOAD_VER) return NV_ENC_ERR_INVALID_VERSION;
    if (!params->inBufferSize || params->inBufferSize > 65536) return NV_ENC_ERR_INVALID_PARAM;
    if (params->spsId || params->ppsId || !ZERO_FIELD(params, reserved) || !ZERO_FIELD(params, reserved2))
        return NV_ENC_ERR_UNSUPPORTED_PARAM;
    return NV_ENC_SUCCESS;
}

static NVENCSTATUS NVENCAPI sequence_params(void *token, NV_ENC_SEQUENCE_PARAM_PAYLOAD *params)
{
    NVENCSTATUS valid = validate_sequence_payload(params);
    if (valid) return valid;
    AcquireSRWLockExclusive(&guard);
    struct encode_state *s = find_encoder(token);
    NVENCSTATUS status = missing_encoder(token);
    if (s) {
        status = s->failed || s->eos ? NV_ENC_ERR_INVALID_CALL :
            uurb_d3d11_encoder_sequence(s->bridge, params->spsppsBuffer,
                                       params->inBufferSize, params->outSPSPPSPayloadSize);
    }
    ReleaseSRWLockExclusive(&guard);
    return status;
}

static NVENCSTATUS NVENCAPI sequence_params_ex(void *token, NV_ENC_INITIALIZE_PARAMS *params,
                                               NV_ENC_SEQUENCE_PARAM_PAYLOAD *payload)
{
    NVENCSTATUS status = validate_sequence_payload(payload);
    if (status) return status;
    AcquireSRWLockExclusive(&guard);
    struct encode_state *s = find_encoder(token);
    struct session *query = find_session(token);
    status = NV_ENC_ERR_INVALID_ENCODERDEVICE;
    if (!query) goto done;
    /* SDK contract: once initialized, ignore encInitParams completely. Query
     * the active encoder, not its separate uninitialized capability session. */
    if (s) {
        status = s->failed || s->eos ? NV_ENC_ERR_INVALID_CALL :
            uurb_d3d11_encoder_sequence(s->bridge, payload->spsppsBuffer,
                                       payload->inBufferSize, payload->outSPSPPSPayloadSize);
        goto done;
    }
    status = NV_ENC_ERR_INVALID_PTR;
    if (!params || !params->encodeConfig) goto done;
    status = NV_ENC_ERR_INVALID_VERSION;
    if (params->version != NV_ENC_INITIALIZE_PARAMS_VER || params->encodeConfig->version != NV_ENC_CONFIG_VER)
        goto done;
    status = NV_ENC_ERR_UNSUPPORTED_PARAM;
    if (!supported_codec(&params->encodeGUID) || !params->encodeConfig->rcParams.averageBitRate ||
        memcmp(&params->presetGUID, &NV_ENC_PRESET_P1_GUID, sizeof(GUID))) goto done;
    NV_ENC_PRESET_CONFIG preset = {.version = NV_ENC_PRESET_CONFIG_VER};
    preset.presetCfg.version = NV_ENC_CONFIG_VER;
    status = uurb_nvenc_query_preset(query->native, &params->encodeGUID, &params->presetGUID,
        1, NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY, &preset);
    if (status) goto done;
    NV_ENC_CONFIG config;
    NV_ENC_INITIALIZE_PARAMS init;
    int hevc = !memcmp(&params->encodeGUID, &NV_ENC_CODEC_HEVC_GUID, sizeof(GUID));
    status = NV_ENC_ERR_UNSUPPORTED_PARAM;
    if (!uurb_nvenc_negotiate(params, &preset.presetCfg, hevc, &config, &init)) goto done;
    status = uurb_nvenc_query_sequence(query->native, &init, payload->spsppsBuffer,
                                      payload->inBufferSize, payload->outSPSPPSPayloadSize);
done:
    ReleaseSRWLockExclusive(&guard);
    return status;
}

/* Advertise the tested bridge subset, not all features of the NVIDIA driver. */
static NVENCSTATUS limited_list(void *token, unsigned op, const GUID *codec,
    void *out, uint32_t capacity, uint32_t *count)
{
    if (!count) return NV_ENC_ERR_INVALID_PTR;
    int array = op == UURB_CODECS || op == UURB_PROFILES || op == UURB_FORMATS || op == UURB_PRESETS;
    if (array && !out) return NV_ENC_ERR_INVALID_PTR;
    if (array && (!capacity || capacity > 128)) return NV_ENC_ERR_INVALID_PARAM;
    if (codec && !supported_codec(codec)) return NV_ENC_ERR_UNSUPPORTED_PARAM;
    AcquireSRWLockExclusive(&guard);
    struct session *s = find_session(token);
    NVENCSTATUS status = NV_ENC_ERR_INVALID_ENCODERDEVICE;
    if (!s) goto done;
    unsigned type = (op == UURB_CODEC_COUNT || op == UURB_CODECS) ? UURB_CODECS :
        (op == UURB_FORMAT_COUNT || op == UURB_FORMATS) ? UURB_FORMATS :
        (op == UURB_PRESET_COUNT || op == UURB_PRESETS) ? UURB_PRESETS : UURB_PROFILES;
    union { GUID guids[128]; NV_ENC_BUFFER_FORMAT formats[128]; } values;
    uint32_t size = 0, kept = 0;
    status = uurb_nvenc_query(s->native, type, codec, &values, 128, &size);
    if (status) goto done;
    for (unsigned i = 0; i < size; ++i) {
        if (type == UURB_FORMATS) {
            if (values.formats[i] == NV_ENC_BUFFER_FORMAT_NV12 || values.formats[i] == NV_ENC_BUFFER_FORMAT_ARGB)
                values.formats[kept++] = values.formats[i];
        } else if ((type == UURB_CODECS && supported_codec(&values.guids[i])) ||
            (type == UURB_PRESETS && !memcmp(&values.guids[i], &NV_ENC_PRESET_P1_GUID, sizeof(GUID))) ||
            (type == UURB_PROFILES && !memcmp(&values.guids[i], &NV_ENC_CODEC_PROFILE_AUTOSELECT_GUID, sizeof(GUID))))
            values.guids[kept++] = values.guids[i];
    }
    status = NV_ENC_ERR_NOT_ENOUGH_BUFFER;
    if (array && capacity < kept) goto done;
    if (array) memcpy(out, &values, kept * (type == UURB_FORMATS ? sizeof(NV_ENC_BUFFER_FORMAT) : sizeof(GUID)));
    *count = kept; status = NV_ENC_SUCCESS;
done:
    ReleaseSRWLockExclusive(&guard);
    return status;
}
static NVENCSTATUS NVENCAPI limited_codec_count(void *s, uint32_t *n)
{ return limited_list(s, UURB_CODEC_COUNT, NULL, NULL, 0, n); }
static NVENCSTATUS NVENCAPI limited_codecs(void *s, GUID *out, uint32_t cap, uint32_t *n)
{ return limited_list(s, UURB_CODECS, NULL, out, cap, n); }
#define LIMITED_COUNT(name, op) static NVENCSTATUS NVENCAPI name(void *s, GUID codec, uint32_t *n) \
    { return limited_list(s, op, &codec, NULL, 0, n); }
#define LIMITED_ARRAY(name, op, type) static NVENCSTATUS NVENCAPI name(void *s, GUID codec, type *out, uint32_t cap, uint32_t *n) \
    { return limited_list(s, op, &codec, out, cap, n); }
LIMITED_COUNT(limited_format_count, UURB_FORMAT_COUNT)
LIMITED_COUNT(limited_profile_count, UURB_PROFILE_COUNT)
LIMITED_COUNT(limited_preset_count, UURB_PRESET_COUNT)
LIMITED_ARRAY(limited_formats, UURB_FORMATS, NV_ENC_BUFFER_FORMAT)
LIMITED_ARRAY(limited_profiles, UURB_PROFILES, GUID)
LIMITED_ARRAY(limited_presets, UURB_PRESETS, GUID)
static NVENCSTATUS NVENCAPI limited_caps(void *s, GUID codec, NV_ENC_CAPS_PARAM *params, int *out)
{
    if (!out) return NV_ENC_ERR_INVALID_PTR;
    if (!supported_codec(&codec)) return NV_ENC_ERR_UNSUPPORTED_PARAM;
    int value;
    NVENCSTATUS status = caps(s, codec, params, &value);
    if (status) return status;
    switch (params->capsToQuery) {
    case NV_ENC_CAPS_WIDTH_MAX: case NV_ENC_CAPS_HEIGHT_MAX: value = value < 4096 ? value : 4096; break;
    case NV_ENC_CAPS_WIDTH_MIN: case NV_ENC_CAPS_HEIGHT_MIN: break;
    case NV_ENC_CAPS_SUPPORTED_RATECONTROL_MODES: value &= NV_ENC_PARAMS_RC_CBR | NV_ENC_PARAMS_RC_VBR; break;
    case NV_ENC_CAPS_NUM_MAX_BFRAMES: case NV_ENC_CAPS_SUPPORT_FIELD_ENCODING:
    case NV_ENC_CAPS_SUPPORT_10BIT_ENCODE: case NV_ENC_CAPS_SUPPORT_YUV444_ENCODE:
    case NV_ENC_CAPS_SUPPORT_LOSSLESS_ENCODE: case NV_ENC_CAPS_ASYNC_ENCODE_SUPPORT:
    case NV_ENC_CAPS_SUPPORT_LOOKAHEAD:
    case NV_ENC_CAPS_SUPPORT_DYN_RES_CHANGE: case NV_ENC_CAPS_SUPPORT_ALPHA_LAYER_ENCODING:
    case NV_ENC_CAPS_SUPPORT_YUV422_ENCODE: value = 0; break;
    case NV_ENC_CAPS_SUPPORT_DYN_BITRATE_CHANGE: break;
    default: return NV_ENC_ERR_UNIMPLEMENTED;
    }
    *out = value; return NV_ENC_SUCCESS;
}
static NVENCSTATUS NVENCAPI limited_preset_ex(void *s, GUID codec, GUID preset,
    NV_ENC_TUNING_INFO tuning, NV_ENC_PRESET_CONFIG *out)
{
    if (!supported_codec(&codec) || memcmp(&preset, &NV_ENC_PRESET_P1_GUID, sizeof(GUID)) ||
        tuning != NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY) return NV_ENC_ERR_UNSUPPORTED_PARAM;
    return get_preset_ex(s, codec, preset, tuning, out);
}
static NVENCSTATUS NVENCAPI limited_legacy_preset(void *s, GUID codec, GUID preset, NV_ENC_PRESET_CONFIG *out)
{
    (void)s; (void)codec; (void)preset; (void)out;
    /* Only preset-config-ex with explicit ULL tuning is currently accepted. */
    return NV_ENC_ERR_UNIMPLEMENTED;
}

/* Populate unsupported public slots with correctly typed NVENCAPI functions.
 * A client must receive an explicit error, not call a NULL pointer. Never cast
 * a generic function across incompatible Windows argument/return signatures. */
#define UNSUPPORTED_TWO_ARGUMENTS(X) \
    X(nvEncCreateInputBuffer, NV_ENC_CREATE_INPUT_BUFFER *) \
    X(nvEncDestroyInputBuffer, NV_ENC_INPUT_PTR) \
    X(nvEncLockInputBuffer, NV_ENC_LOCK_INPUT_BUFFER *) \
    X(nvEncUnlockInputBuffer, NV_ENC_INPUT_PTR) \
    X(nvEncGetEncodeStats, NV_ENC_STAT *) \
    X(nvEncRegisterAsyncEvent, NV_ENC_EVENT_PARAMS *) \
    X(nvEncUnregisterAsyncEvent, NV_ENC_EVENT_PARAMS *) \
    X(nvEncInvalidateRefFrames, uint64_t) \
    X(nvEncCreateMVBuffer, NV_ENC_CREATE_MV_BUFFER *) \
    X(nvEncDestroyMVBuffer, NV_ENC_OUTPUT_PTR) \
    X(nvEncRunMotionEstimationOnly, NV_ENC_MEONLY_PARAMS *) \
    X(nvEncRestoreEncoderState, NV_ENC_RESTORE_ENCODER_STATE_PARAMS *) \
    X(nvEncLookaheadPicture, NV_ENC_LOOKAHEAD_PIC_PARAMS *)
#define DECLARE_UNSUPPORTED(name, type) \
    static NVENCSTATUS NVENCAPI unsupported_##name(void *s, type argument) \
    { (void)s; (void)argument; return NV_ENC_ERR_UNIMPLEMENTED; }
UNSUPPORTED_TWO_ARGUMENTS(DECLARE_UNSUPPORTED)
#undef DECLARE_UNSUPPORTED
static NVENCSTATUS NVENCAPI unsupported_open(void *device, uint32_t type, void **out)
{ (void)device; (void)type; (void)out; return NV_ENC_ERR_UNIMPLEMENTED; }
static NVENCSTATUS NVENCAPI unsupported_streams(void *s, NV_ENC_CUSTREAM_PTR input, NV_ENC_CUSTREAM_PTR output)
{ (void)s; (void)input; (void)output; return NV_ENC_ERR_UNIMPLEMENTED; }
static const char *NVENCAPI limited_error_string(void *s)
{ (void)s; return "Experimental bounded bridge; use returned NVENCSTATUS (no per-call error text available)"; }

NVENCSTATUS NVENCAPI NvEncodeAPIGetMaxSupportedVersion(uint32_t *version)
{
    NVENCSTATUS status = uurb_query_supported_version(version);
#ifdef UURB_NVENC_TRACE
    fprintf(stderr, "UURB_NVENC_TRACE {\"call\":\"version\",\"value\":%u,\"status\":%u}\n", !status && version ? *version : 0, status);
#endif
    return status;
}
NVENCSTATUS NVENCAPI NvEncodeAPICreateInstance(NV_ENCODE_API_FUNCTION_LIST *api)
{
    NVENCSTATUS status = uurb_query_create_instance(api);
#ifdef UURB_NVENC_TRACE
    fprintf(stderr, "UURB_NVENC_TRACE {\"call\":\"create\",\"version\":%u,\"status\":%u}\n", api ? api->version : 0, status);
#endif
    if (status) return status;
    api->nvEncInitializeEncoder = encode_initialize;
    api->nvEncRegisterResource = register_resource;
    api->nvEncUnregisterResource = unregister_resource;
    api->nvEncMapInputResource = map_resource;
    api->nvEncUnmapInputResource = unmap_resource;
    api->nvEncCreateBitstreamBuffer = create_bitstream;
    api->nvEncDestroyBitstreamBuffer = destroy_bitstream;
    api->nvEncEncodePicture = encode_picture;
    api->nvEncLockBitstream = lock_bitstream;
    api->nvEncUnlockBitstream = unlock_bitstream;
    api->nvEncDestroyEncoder = encode_destroy;
    api->nvEncReconfigureEncoder = reconfigure_encoder;
    api->nvEncGetEncodeGUIDCount = limited_codec_count;
    api->nvEncGetEncodeGUIDs = limited_codecs;
    api->nvEncGetInputFormatCount = limited_format_count;
    api->nvEncGetInputFormats = limited_formats;
    api->nvEncGetEncodeProfileGUIDCount = limited_profile_count;
    api->nvEncGetEncodeProfileGUIDs = limited_profiles;
    api->nvEncGetEncodePresetCount = limited_preset_count;
    api->nvEncGetEncodePresetGUIDs = limited_presets;
    api->nvEncGetEncodeCaps = limited_caps;
    api->nvEncGetEncodePresetConfigEx = limited_preset_ex;
    api->nvEncGetEncodePresetConfig = limited_legacy_preset;
#define ASSIGN_UNSUPPORTED(name, type) api->name = unsupported_##name;
    UNSUPPORTED_TWO_ARGUMENTS(ASSIGN_UNSUPPORTED)
#undef ASSIGN_UNSUPPORTED
    api->nvEncOpenEncodeSession = unsupported_open;
    api->nvEncSetIOCudaStreams = unsupported_streams;
    api->nvEncGetSequenceParamEx = sequence_params_ex;
    api->nvEncGetLastErrorString = limited_error_string;
    api->nvEncGetSequenceParams = sequence_params;
#ifdef UURB_NVENC_TRACE
    trace_attach(api);
#endif
    return NV_ENC_SUCCESS;
}
