#ifndef UURB_NVENC_REFERENCE_CONFIG_H
#define UURB_NVENC_REFERENCE_CONFIG_H
#include <ffnvcodec/nvEncodeAPI.h>
/* The intentionally narrow reference contract shared by native encoder and
 * experimental Windows ABI validation. It is not arbitrary UU negotiation. */
static inline void uurb_nvenc_reference_config(const NV_ENC_CONFIG *preset,
    unsigned width, unsigned height, int hevc, unsigned bitrate,
    NV_ENC_CONFIG *config, NV_ENC_INITIALIZE_PARAMS *init)
{
    (*config) = *preset;
    (*config).gopLength = 60;
    (*config).frameIntervalP = 1;
    (*config).rcParams.rateControlMode = NV_ENC_PARAMS_RC_CBR;
    (*config).rcParams.averageBitRate = bitrate;
    (*config).rcParams.maxBitRate = bitrate;
    (*config).rcParams.vbvBufferSize = bitrate / 60;
    (*config).rcParams.vbvInitialDelay = bitrate / 60;
    (*config).rcParams.enableLookahead = 0;
    (*config).rcParams.zeroReorderDelay = 1;
    NV_ENC_CONFIG_H264_VUI_PARAMETERS *vui = hevc
        ? &(*config).encodeCodecConfig.hevcConfig.hevcVUIParameters
        : &(*config).encodeCodecConfig.h264Config.h264VUIParameters;
    vui->videoSignalTypePresentFlag = 1;
    vui->videoFormat = NV_ENC_VUI_VIDEO_FORMAT_UNSPECIFIED;
    vui->videoFullRangeFlag = 0;
    vui->colourDescriptionPresentFlag = 1;
    vui->colourPrimaries = NV_ENC_VUI_COLOR_PRIMARIES_BT709;
    vui->transferCharacteristics = NV_ENC_VUI_TRANSFER_CHARACTERISTIC_BT709;
    vui->colourMatrix = NV_ENC_VUI_MATRIX_COEFFS_BT709;
    (*init) = (NV_ENC_INITIALIZE_PARAMS){
        .version = NV_ENC_INITIALIZE_PARAMS_VER, .encodeGUID = hevc ? NV_ENC_CODEC_HEVC_GUID : NV_ENC_CODEC_H264_GUID,
        .presetGUID = NV_ENC_PRESET_P1_GUID, .encodeWidth = width, .encodeHeight = height,
        .darWidth = width, .darHeight = height, .frameRateNum = 60, .frameRateDen = 1,
        .enablePTD = 1, .encodeConfig = &(*config),
        .maxEncodeWidth = width, .maxEncodeHeight = height,
        .tuningInfo = NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY};
}
#endif
