#ifndef UURB_NVENC_NEGOTIATION_H
#define UURB_NVENC_NEGOTIATION_H
#include "native_nvenc_reference_config.h"
#include <stdint.h>
#include <string.h>

static inline unsigned uurb_nvenc_preset_number(const GUID *preset)
{
    const GUID *known[] = {&NV_ENC_PRESET_P1_GUID, &NV_ENC_PRESET_P2_GUID, &NV_ENC_PRESET_P3_GUID,
        &NV_ENC_PRESET_P4_GUID, &NV_ENC_PRESET_P5_GUID, &NV_ENC_PRESET_P6_GUID, &NV_ENC_PRESET_P7_GUID};
    if (preset) for (unsigned i = 0; i < 7; ++i)
        if (!memcmp(preset, known[i], sizeof(GUID))) return i + 1;
    return 0;
}
static inline int uurb_nvenc_tuning_supported(NV_ENC_TUNING_INFO tuning)
{
    return tuning == NV_ENC_TUNING_INFO_HIGH_QUALITY || tuning == NV_ENC_TUNING_INFO_LOW_LATENCY ||
        tuning == NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY;
}

/* Standard SDK fields, not a UU-version/configuration allowlist. Only a
 * synchronous 8-bit, no-reorder P1/ULL subset is exposed. Accepted structures
 * are copied unchanged, including caller-selected rate control and VUI.
 * The final comparison rejects unknown flags, reserved data and pointers. */
static inline int uurb_nvenc_valid_rates(const NV_ENC_INITIALIZE_PARAMS *p)
{
    if (!p || !p->encodeConfig || !p->frameRateDen || !p->frameRateNum ||
        (uint64_t)p->frameRateNum > (uint64_t)p->frameRateDen * 240 ||
        p->frameRateNum < p->frameRateDen) return 0;
    const NV_ENC_RC_PARAMS *rc = &p->encodeConfig->rcParams;
    return (rc->rateControlMode == NV_ENC_PARAMS_RC_CBR || rc->rateControlMode == NV_ENC_PARAMS_RC_VBR) &&
        rc->averageBitRate && rc->averageBitRate <= 1000000000u && rc->maxBitRate <= 1000000000u &&
        (!rc->maxBitRate || rc->maxBitRate >= rc->averageBitRate) &&
        rc->vbvInitialDelay <= rc->vbvBufferSize;
}

static inline void uurb_nvenc_copy_rates(NV_ENC_CONFIG *to, const NV_ENC_CONFIG *from)
{
    to->rcParams.averageBitRate = from->rcParams.averageBitRate;
    to->rcParams.maxBitRate = from->rcParams.maxBitRate;
    to->rcParams.vbvBufferSize = from->rcParams.vbvBufferSize;
    to->rcParams.vbvInitialDelay = from->rcParams.vbvInitialDelay;
}

static inline int uurb_nvenc_negotiate(const NV_ENC_INITIALIZE_PARAMS *p, const NV_ENC_CONFIG *preset,
    int hevc, NV_ENC_CONFIG *config, NV_ENC_INITIALIZE_PARAMS *init)
{
    if (!uurb_nvenc_valid_rates(p) || !p->encodeConfig->gopLength ||
        p->encodeWidth < 2 || p->encodeHeight < 2 || p->encodeWidth > 4096 || p->encodeHeight > 4096 ||
        (p->encodeWidth & 1) || (p->encodeHeight & 1)) return 0;
    NV_ENC_CONFIG expected;
    NV_ENC_INITIALIZE_PARAMS expected_init;
    const NV_ENC_CONFIG *c = p->encodeConfig;
    uurb_nvenc_reference_config(preset, p->encodeWidth, p->encodeHeight, hevc,
                               c->rcParams.averageBitRate, &expected, &expected_init);
    expected_init.frameRateNum = p->frameRateNum;
    expected_init.frameRateDen = p->frameRateDen;
    expected_init.encodeConfig = p->encodeConfig;
    expected.gopLength = c->gopLength;
    uurb_nvenc_copy_rates(&expected, c);
    expected.rcParams.rateControlMode = c->rcParams.rateControlMode;
    expected.rcParams.enableAQ = c->rcParams.enableAQ;
    expected.rcParams.aqStrength = c->rcParams.aqStrength;
    expected.rcParams.disableIadapt = c->rcParams.disableIadapt;
    expected.rcParams.disableBadapt = c->rcParams.disableBadapt;
    expected.rcParams.zeroReorderDelay = c->rcParams.zeroReorderDelay;
    if (c->rcParams.multiPass != NV_ENC_MULTI_PASS_DISABLED &&
        c->rcParams.multiPass != NV_ENC_TWO_PASS_QUARTER_RESOLUTION &&
        c->rcParams.multiPass != NV_ENC_TWO_PASS_FULL_RESOLUTION) return 0;
    expected.rcParams.multiPass = c->rcParams.multiPass;
    const NV_ENC_CONFIG_H264_VUI_PARAMETERS *vui;
    NV_ENC_CONFIG_H264_VUI_PARAMETERS *wanted;
#define COMMON(dst, src, refs) do { \
    if ((src)->refs > 16 || ((src)->inputBitDepth && (src)->inputBitDepth != NV_ENC_BIT_DEPTH_8) || \
        ((src)->outputBitDepth && (src)->outputBitDepth != NV_ENC_BIT_DEPTH_8)) return 0; \
    (dst)->refs = (src)->refs; \
    (dst)->inputBitDepth = (src)->inputBitDepth; (dst)->outputBitDepth = (src)->outputBitDepth; \
    if ((src)->sliceMode == 3 && (src)->sliceModeData == 1) { (dst)->sliceMode = 3; (dst)->sliceModeData = 1; } \
    (dst)->repeatSPSPPS = (src)->repeatSPSPPS; (dst)->outputAUD = (src)->outputAUD; \
} while (0)
    if (hevc) {
        COMMON(&expected.encodeCodecConfig.hevcConfig, &c->encodeCodecConfig.hevcConfig, maxNumRefFramesInDPB);
        vui = &c->encodeCodecConfig.hevcConfig.hevcVUIParameters;
        wanted = &expected.encodeCodecConfig.hevcConfig.hevcVUIParameters;
    } else {
        COMMON(&expected.encodeCodecConfig.h264Config, &c->encodeCodecConfig.h264Config, maxNumRefFrames);
        vui = &c->encodeCodecConfig.h264Config.h264VUIParameters;
        wanted = &expected.encodeCodecConfig.h264Config.h264VUIParameters;
    }
#undef COMMON
    /* Preserve either explicitly tested BT.709 limited metadata or entirely
     * unspecified color metadata. Never relabel HDR/full-range as BT.709. */
    if (!vui->videoSignalTypePresentFlag && !vui->colourDescriptionPresentFlag &&
        vui->colourPrimaries == NV_ENC_VUI_COLOR_PRIMARIES_UNSPECIFIED &&
        vui->transferCharacteristics == NV_ENC_VUI_TRANSFER_CHARACTERISTIC_UNSPECIFIED &&
        vui->colourMatrix == NV_ENC_VUI_MATRIX_COEFFS_UNSPECIFIED) {
        wanted->videoSignalTypePresentFlag = wanted->colourDescriptionPresentFlag = 0;
        wanted->colourPrimaries = vui->colourPrimaries;
        wanted->transferCharacteristics = vui->transferCharacteristics;
        wanted->colourMatrix = vui->colourMatrix;
    }
    if (memcmp(p, &expected_init, sizeof(*p)) || memcmp(c, &expected, sizeof(*c))) return 0;
    *config = *c;
    *init = *p;
    init->encodeConfig = config;
    return 1;
}

/* Numeric rejection classes: rates=1, initialization/outer fields=2,
 * codec/rate-control configuration outside the supported change set=4. */
static inline unsigned uurb_nvenc_reconfigure_mismatch(const NV_ENC_CONFIG *old_config,
    const NV_ENC_INITIALIZE_PARAMS *old_init, const NV_ENC_RECONFIGURE_PARAMS *p)
{
    if (!old_config || !old_init || !p || !uurb_nvenc_valid_rates(&p->reInitEncodeParams)) return 1;
    NV_ENC_CONFIG expected_config = *old_config;
    uurb_nvenc_copy_rates(&expected_config, p->reInitEncodeParams.encodeConfig);
    NV_ENC_RECONFIGURE_PARAMS expected = {.version = NV_ENC_RECONFIGURE_PARAMS_VER,
        .reInitEncodeParams = *old_init, .resetEncoder = p->resetEncoder, .forceIDR = p->forceIDR};
    expected.reInitEncodeParams.frameRateNum = p->reInitEncodeParams.frameRateNum;
    expected.reInitEncodeParams.frameRateDen = p->reInitEncodeParams.frameRateDen;
    expected.reInitEncodeParams.encodeConfig = p->reInitEncodeParams.encodeConfig;
    /* Caller supplies the full explicit configuration. Permit standard
     * preset/tuning hints without changing GOP, lookahead, sync mode, codec,
     * resolution, references, or any other previously accepted parameter.
     * The native driver remains the final authority on supported transitions. */
    if (uurb_nvenc_preset_number(&p->reInitEncodeParams.presetGUID) &&
        uurb_nvenc_tuning_supported(p->reInitEncodeParams.tuningInfo)) {
        expected.reInitEncodeParams.presetGUID = p->reInitEncodeParams.presetGUID;
        expected.reInitEncodeParams.tuningInfo = p->reInitEncodeParams.tuningInfo;
    }
    return (memcmp(p, &expected, sizeof(*p)) ? 2u : 0u) |
        (memcmp(p->reInitEncodeParams.encodeConfig, &expected_config, sizeof(expected_config)) ? 4u : 0u);
}
static inline int uurb_nvenc_valid_reconfigure(const NV_ENC_CONFIG *old_config,
    const NV_ENC_INITIALIZE_PARAMS *old_init, const NV_ENC_RECONFIGURE_PARAMS *p)
{
    return !uurb_nvenc_reconfigure_mismatch(old_config, old_init, p);
}
#endif
