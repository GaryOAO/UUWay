#include "native_nvenc_query.h"
#include "native_vaapi_encoder.h"
#include "native_nvenc_reference_config.h"
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_vaapi.h>
#include <va/va.h>
#include <stdlib.h>
#include <string.h>

struct query_session {
    unsigned char uuid[16];
    AVBufferRef *device;
    int supported[2], width[2], height[2], rates[2];
};
static int codec_index(const GUID *codec)
{
    if (!codec) return -1;
    if (!memcmp(codec, &NV_ENC_CODEC_H264_GUID, sizeof(GUID))) return 0;
    if (!memcmp(codec, &NV_ENC_CODEC_HEVC_GUID, sizeof(GUID))) return 1;
    return -1;
}
int uurb_nvenc_query_version(uint32_t *version)
{
    if (!version) return NV_ENC_ERR_INVALID_PTR;
    char node[128];
    if (uurb_vaapi_device(NULL, node)) return NV_ENC_ERR_NO_ENCODE_DEVICE;
    *version = 0xd0;
    return NV_ENC_SUCCESS;
}
int uurb_nvenc_query_open(const unsigned char uuid[16], void **out)
{
    if (!uuid || !out) return NV_ENC_ERR_INVALID_PTR;
    *out = NULL;
    char node[128];
    if (uurb_vaapi_device(uuid, node)) return NV_ENC_ERR_NO_ENCODE_DEVICE;
    struct query_session *s = calloc(1, sizeof(*s));
    if (!s) return NV_ENC_ERR_OUT_OF_MEMORY;
    memcpy(s->uuid, uuid, 16);
    if (av_hwdevice_ctx_create(&s->device, AV_HWDEVICE_TYPE_VAAPI, node, NULL, 0) < 0) goto fail;
    AVHWDeviceContext *hw = (void *)s->device->data;
    VADisplay display = ((AVVAAPIDeviceContext *)hw->hwctx)->display;
    const VAProfile profiles[] = {VAProfileH264High, VAProfileHEVCMain};
    for (int i = 0; i < 2; ++i) {
        VAConfigAttrib attrs[] = {{.type = VAConfigAttribRTFormat}, {.type = VAConfigAttribRateControl},
            {.type = VAConfigAttribMaxPictureWidth}, {.type = VAConfigAttribMaxPictureHeight}};
        if (vaGetConfigAttributes(display, profiles[i], VAEntrypointEncSlice, attrs, 4) != VA_STATUS_SUCCESS ||
            attrs[0].value == VA_ATTRIB_NOT_SUPPORTED || !(attrs[0].value & VA_RT_FORMAT_YUV420) ||
            attrs[1].value == VA_ATTRIB_NOT_SUPPORTED) continue;
        s->rates[i] = ((attrs[1].value & VA_RC_CBR) ? NV_ENC_PARAMS_RC_CBR : 0) |
                      ((attrs[1].value & VA_RC_VBR) ? NV_ENC_PARAMS_RC_VBR : 0);
        s->width[i] = attrs[2].value == VA_ATTRIB_NOT_SUPPORTED ? 1920 : (int)attrs[2].value;
        s->height[i] = attrs[3].value == VA_ATTRIB_NOT_SUPPORTED ? 1080 : (int)attrs[3].value;
        s->supported[i] = s->rates[i] != 0;
    }
    if (!s->supported[0]) goto fail;
    *out = s;
    return NV_ENC_SUCCESS;
fail:
    av_buffer_unref(&s->device); free(s);
    return NV_ENC_ERR_NO_ENCODE_DEVICE;
}
int uurb_nvenc_query_close(void *session)
{
    struct query_session *s = session;
    if (!s) return NV_ENC_ERR_INVALID_ENCODERDEVICE;
    av_buffer_unref(&s->device); free(s);
    return NV_ENC_SUCCESS;
}
int uurb_nvenc_query(void *session, unsigned op, const GUID *codec, void *out, uint32_t cap, uint32_t *count)
{
    struct query_session *s = session;
    if (!s) return NV_ENC_ERR_INVALID_ENCODERDEVICE;
    int index = codec_index(codec);
    if (op >= UURB_PROFILE_COUNT && (index < 0 || !s->supported[index])) return NV_ENC_ERR_UNSUPPORTED_PARAM;
    if (op == UURB_CAP) {
        if (!out) return NV_ENC_ERR_INVALID_PTR;
        int value = 0;
        switch (cap) {
        case NV_ENC_CAPS_WIDTH_MAX: value = s->width[index]; break;
        case NV_ENC_CAPS_HEIGHT_MAX: value = s->height[index]; break;
        case NV_ENC_CAPS_WIDTH_MIN: case NV_ENC_CAPS_HEIGHT_MIN: value = 16; break;
        case NV_ENC_CAPS_SUPPORTED_RATECONTROL_MODES: value = s->rates[index]; break;
        case NV_ENC_CAPS_SUPPORT_DYN_BITRATE_CHANGE: value = 1; break;
        /* Conservative advertised level; the actual VA driver validates geometry. */
        case NV_ENC_CAPS_LEVEL_MAX: value = index ? 153 : 51; break;
        case NV_ENC_CAPS_LEVEL_MIN: value = index ? 30 : 10; break;
        case NV_ENC_CAPS_DYNAMIC_QUERY_ENCODER_CAPACITY: value = 100; break;
        default: value = 0; break;
        }
        *(int *)out = value;
        return NV_ENC_SUCCESS;
    }
    if (!count) return NV_ENC_ERR_INVALID_PTR;
    GUID guids[2]; NV_ENC_BUFFER_FORMAT formats[] = {NV_ENC_BUFFER_FORMAT_NV12, NV_ENC_BUFFER_FORMAT_ARGB};
    unsigned n = 1; size_t size = sizeof(GUID); const void *data = guids;
    switch (op) {
    case UURB_CODEC_COUNT: case UURB_CODECS:
        n = 0;
        if (s->supported[0]) guids[n++] = NV_ENC_CODEC_H264_GUID;
        if (s->supported[1]) guids[n++] = NV_ENC_CODEC_HEVC_GUID;
        break;
    case UURB_PROFILE_COUNT: case UURB_PROFILES: guids[0] = NV_ENC_CODEC_PROFILE_AUTOSELECT_GUID; break;
    case UURB_PRESET_COUNT: case UURB_PRESETS: guids[0] = NV_ENC_PRESET_P1_GUID; break;
    case UURB_FORMAT_COUNT: case UURB_FORMATS: n = 2; data = formats; size = sizeof(formats[0]); break;
    default: return NV_ENC_ERR_INVALID_PARAM;
    }
    if (op == UURB_CODECS || op == UURB_PROFILES || op == UURB_PRESETS || op == UURB_FORMATS) {
        if (!out) return NV_ENC_ERR_INVALID_PTR;
        if (cap < n) return NV_ENC_ERR_NOT_ENOUGH_BUFFER;
        memcpy(out, data, n * size);
    }
    *count = n;
    return NV_ENC_SUCCESS;
}
int uurb_nvenc_query_preset(void *session, const GUID *codec, const GUID *preset, int extended,
                            NV_ENC_TUNING_INFO tuning, NV_ENC_PRESET_CONFIG *out)
{
    struct query_session *s = session;
    int i = codec_index(codec);
    if (!s) return NV_ENC_ERR_INVALID_ENCODERDEVICE;
    if (!out || !preset) return NV_ENC_ERR_INVALID_PTR;
    if (i < 0 || !s->supported[i] || memcmp(preset, &NV_ENC_PRESET_P1_GUID, sizeof(GUID)) ||
        !extended || tuning != NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY) return NV_ENC_ERR_UNSUPPORTED_PARAM;
    *out = (NV_ENC_PRESET_CONFIG){.version = NV_ENC_PRESET_CONFIG_VER, .presetCfg = {.version = NV_ENC_CONFIG_VER}};
    out->presetCfg.profileGUID = NV_ENC_CODEC_PROFILE_AUTOSELECT_GUID;
    return NV_ENC_SUCCESS;
}
int uurb_nvenc_query_sequence(void *session, const NV_ENC_INITIALIZE_PARAMS *p, void *bytes, uint32_t capacity, uint32_t *size)
{
    struct query_session *s = session;
    if (!s) return NV_ENC_ERR_INVALID_ENCODERDEVICE;
    struct uurb_encoder *encoder = uurb_vaapi_open(s->uuid, p);
    if (!encoder) return NV_ENC_ERR_UNSUPPORTED_PARAM;
    int result = uurb_encoder_sequence(encoder, bytes, capacity, size);
    uurb_encoder_close(encoder);
    return result;
}
