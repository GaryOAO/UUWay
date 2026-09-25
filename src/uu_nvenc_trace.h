#ifndef UURB_NVENC_TRACE_H
#define UURB_NVENC_TRACE_H
/* Compile-time diagnostic only. Fixed scalar fields, never raw memory,
 * pointers, application strings, pixels, passwords or account identifiers. */
#include <stdio.h>
#include <string.h>
#ifdef UURB_NVENC_DETECTOR_CONFIG
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>
/* Opt-in synthetic packet export, never enabled by normal builds. Only write
 * bounded compressed packets to an owner-only directory explicitly provided
 * by the offline detector runner. No packet bytes enter diagnostic logs. */
static void trace_synthetic_packet(const NV_ENC_INITIALIZE_PARAMS *init, const void *data, uint32_t bytes)
{
    const char *directory = getenv("UURB_NVENC_SYNTHETIC_OUTPUT");
    if (!directory || !*directory) return;
    static unsigned counter;
    unsigned index = counter++;
    int saved = 0;
    const char *codec = !memcmp(&init->encodeGUID, &NV_ENC_CODEC_HEVC_GUID, sizeof(GUID)) ? "hevc" : "h264";
    char filename[80];
    snprintf(filename, sizeof(filename), "packet-%u.%s", index, codec);
    int folder = open(directory, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    struct stat info;
    if (folder >= 0 && !fstat(folder, &info) && info.st_uid == geteuid() && !(info.st_mode & 077) &&
        index < 16 && bytes && bytes <= 4u * 1024u * 1024u) {
        int fd = openat(folder, filename, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
        if (fd >= 0) {
            size_t written = 0;
            while (written < bytes) {
                ssize_t n = write(fd, (const unsigned char *)data + written, bytes - written);
                if (n < 0 && errno == EINTR) continue;
                if (n <= 0) break;
                written += n;
            }
            int closed = close(fd);
            saved = written == bytes && !closed;
        }
    }
    if (folder >= 0) close(folder);
    fprintf(stderr, "UURB_NVENC_TRACE {\"call\":\"synthetic_packet\",\"saved\":%s,\"index\":%u,\"codec\":\"%s\",\"bytes\":%u,\"width\":%u,\"height\":%u}\n",
        saved ? "true" : "false", index, codec, bytes, init->encodeWidth, init->encodeHeight);
}
#endif
static NV_ENCODE_API_FUNCTION_LIST trace_actual;
/* Only named SDK scalar fields are logged. The final booleans detect any
 * additional layout/reserved/pointer difference without disclosing its bytes. */
static void trace_config_diff(const NV_ENC_INITIALIZE_PARAMS *p, const NV_ENC_INITIALIZE_PARAMS *expected,
    const NV_ENC_CONFIG *c, const NV_ENC_CONFIG *reference, int hevc)
{
    NV_ENC_INITIALIZE_PARAMS checked_init = *expected;
    NV_ENC_CONFIG checked_config = *reference;
    checked_init.encodeConfig = p->encodeConfig;
    fprintf(stderr, "UURB_NVENC_TRACE {\"call\":\"config_diff\",\"fields\":{");
    int first = 1;
#define DIFF(requested, checked, field) do { \
    if ((requested)->field != (checked)->field) { \
        fprintf(stderr, "%s\"%s\":[%lld,%lld]", first ? "" : ",", #field, \
            (long long)(checked)->field, (long long)(requested)->field); first = 0; \
        (checked)->field = (requested)->field; \
    } \
} while (0)
#define I(field) DIFF(p, &checked_init, field)
    I(reportSliceOffsets); I(enableSubFrameWrite); I(enableExternalMEHints); I(enableMEOnlyMode);
    I(enableWeightedPrediction); I(splitEncodeMode); I(enableOutputInVidmem); I(enableReconFrameOutput);
    I(enableOutputStats); I(enableUniDirectionalB); I(bufferFormat); I(numStateBuffers); I(outputStatsLevel);
    I(encodeWidth); I(encodeHeight); I(darWidth); I(darHeight); I(maxEncodeWidth); I(maxEncodeHeight);
    I(frameRateNum); I(frameRateDen); I(enableEncodeAsync); I(enablePTD); I(tuningInfo);
#undef I
#define C(field) DIFF(c, &checked_config, field)
    C(gopLength); C(frameIntervalP); C(monoChromeEncoding); C(frameFieldMode); C(mvPrecision);
    C(rcParams.version); C(rcParams.rateControlMode); C(rcParams.averageBitRate); C(rcParams.maxBitRate);
    C(rcParams.vbvBufferSize); C(rcParams.vbvInitialDelay); C(rcParams.enableMinQP); C(rcParams.enableMaxQP);
    C(rcParams.enableInitialRCQP); C(rcParams.enableAQ); C(rcParams.enableLookahead); C(rcParams.disableIadapt);
    C(rcParams.disableBadapt); C(rcParams.enableTemporalAQ); C(rcParams.zeroReorderDelay); C(rcParams.enableNonRefP);
    C(rcParams.strictGOPTarget); C(rcParams.aqStrength); C(rcParams.enableExtLookahead);
#define Q(field) C(rcParams.field.qpInterP); C(rcParams.field.qpInterB); C(rcParams.field.qpIntra)
    Q(constQP); Q(minQP); Q(maxQP); Q(initialRCQP);
#undef Q
    C(rcParams.temporallayerIdxMask); C(rcParams.targetQuality); C(rcParams.targetQualityLSB);
    C(rcParams.lookaheadDepth); C(rcParams.lowDelayKeyFrameScale); C(rcParams.qpMapMode); C(rcParams.multiPass);
    C(rcParams.alphaLayerBitrateRatio); C(rcParams.lookaheadLevel);
#define COMMON_CODEC(F) \
    F(level); F(outputBufferingPeriodSEI); F(outputPictureTimingSEI); F(outputAUD); \
    F(disableSPSPPS); F(repeatSPSPPS); F(enableIntraRefresh); F(enableLTR); F(useConstrainedIntraPred); \
    F(enableFillerDataInsertion); F(enableConstrainedEncoding); F(singleSliceIntraRefresh); \
    F(outputRecoveryPointSEI); F(idrPeriod); F(intraRefreshPeriod); F(intraRefreshCnt); F(ltrNumFrames); \
    F(spsId); F(ppsId); F(sliceMode); F(sliceModeData); F(chromaFormatIDC); F(ltrTrustMode); \
    F(useBFramesAsRef); F(numRefL0); F(numRefL1); F(tfLevel); F(disableDeblockingFilterIDC); \
    F(inputBitDepth); F(outputBitDepth); F(numTemporalLayers)
    if (hevc) {
#define H(field) C(encodeCodecConfig.hevcConfig.field)
        COMMON_CODEC(H);
        H(tier); H(minCUSize); H(maxCUSize); H(disableDeblockAcrossSliceBoundary);
        H(enableAlphaLayerEncoding); H(outputTimeCodeSEI); H(enableTemporalSVC); H(enableMVHEVC);
        H(outputHevc3DReferenceDisplayInfo); H(outputMaxCll); H(outputMasteringDisplay);
        H(maxNumRefFramesInDPB); H(vpsId); H(maxTemporalLayersMinus1); H(numViews);
#undef H
    } else {
#define H(field) C(encodeCodecConfig.h264Config.field)
        COMMON_CODEC(H);
        H(enableTemporalSVC); H(enableStereoMVC); H(hierarchicalPFrames); H(hierarchicalBFrames);
        H(outputFramePackingSEI); H(enableVFR); H(qpPrimeYZeroTransformBypassFlag);
        H(disableSVCPrefixNalu); H(enableScalabilityInfoSEI); H(enableTimeCode); H(separateColourPlaneFlag);
        H(adaptiveTransformMode); H(fmoMode); H(bdirectMode); H(entropyCodingMode); H(stereoMode);
        H(maxNumRefFrames); H(maxTemporalLayers);
#undef H
    }
#undef COMMON_CODEC
#undef C
    const NV_ENC_CONFIG_H264_VUI_PARAMETERS *v = hevc
        ? &c->encodeCodecConfig.hevcConfig.hevcVUIParameters : &c->encodeCodecConfig.h264Config.h264VUIParameters;
    NV_ENC_CONFIG_H264_VUI_PARAMETERS *r = hevc
        ? &checked_config.encodeCodecConfig.hevcConfig.hevcVUIParameters : &checked_config.encodeCodecConfig.h264Config.h264VUIParameters;
#define V(field) DIFF(v, r, field)
    V(overscanInfoPresentFlag); V(overscanInfo); V(videoSignalTypePresentFlag); V(videoFormat);
    V(videoFullRangeFlag); V(colourDescriptionPresentFlag); V(colourPrimaries); V(transferCharacteristics);
    V(colourMatrix); V(chromaSampleLocationFlag); V(chromaSampleLocationTop); V(chromaSampleLocationBot);
    V(bitstreamRestrictionFlag); V(timingInfoPresentFlag); V(numUnitInTicks); V(timeScale);
#undef V
#undef DIFF
    fprintf(stderr, "},\"unreviewed_init_difference\":%s,\"unreviewed_config_difference\":%s}\n",
        memcmp(p, &checked_init, sizeof(*p)) ? "true" : "false",
        memcmp(c, &checked_config, sizeof(*c)) ? "true" : "false");
}
static NVENCSTATUS NVENCAPI trace_open(NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS *p, void **out)
{
    NVENCSTATUS status = trace_actual.nvEncOpenEncodeSessionEx(p, out);
    fprintf(stderr, "UURB_NVENC_TRACE {\"call\":\"open\",\"version\":%u,\"api\":%u,\"device_type\":%u,\"status\":%u}\n",
        p ? p->version : 0, p ? p->apiVersion : 0, p ? p->deviceType : 0, status);
    return status;
}
static NVENCSTATUS NVENCAPI trace_caps(void *s, GUID codec, NV_ENC_CAPS_PARAM *p, int *out)
{
    NVENCSTATUS status = trace_actual.nvEncGetEncodeCaps(s, codec, p, out);
    fprintf(stderr, "UURB_NVENC_TRACE {\"call\":\"caps\",\"codec\":%u,\"cap\":%d,\"value\":%d,\"status\":%u}\n",
        codec.Data1, p ? (int)p->capsToQuery : -1, !status && out ? *out : 0, status);
    return status;
}
static NVENCSTATUS NVENCAPI trace_preset(void *s, GUID codec, GUID preset, NV_ENC_TUNING_INFO tuning, NV_ENC_PRESET_CONFIG *out)
{
    NVENCSTATUS status = trace_actual.nvEncGetEncodePresetConfigEx(s, codec, preset, tuning, out);
    fprintf(stderr, "UURB_NVENC_TRACE {\"call\":\"preset\",\"codec\":%u,\"preset\":%u,\"tuning\":%u,\"status\":%u}\n",
        codec.Data1, preset.Data1, tuning, status);
    return status;
}
static NVENCSTATUS NVENCAPI trace_initialize(void *s, NV_ENC_INITIALIZE_PARAMS *p)
{
    NVENCSTATUS status = trace_actual.nvEncInitializeEncoder(s, p);
    if (p && p->version == NV_ENC_INITIALIZE_PARAMS_VER && p->encodeConfig && p->encodeConfig->version == NV_ENC_CONFIG_VER) {
        const NV_ENC_CONFIG *c = p->encodeConfig;
        fprintf(stderr, "UURB_NVENC_TRACE {\"call\":\"initialize\",\"status\":%u,\"codec\":%u,\"preset\":%u,"
            "\"width\":%u,\"height\":%u,\"dar_width\":%u,\"dar_height\":%u,\"max_width\":%u,\"max_height\":%u,"
            "\"fps_num\":%u,\"fps_den\":%u,\"async\":%u,\"ptd\":%u,\"tuning\":%u,"
            "\"profile\":%u,\"gop\":%u,\"interval_p\":%d,\"rc_mode\":%u,\"average_bitrate\":%u,"
            "\"max_bitrate\":%u,\"vbv\":%u,\"vbv_delay\":%u,\"lookahead\":%u,\"zero_reorder\":%u}\n",
            status, p->encodeGUID.Data1, p->presetGUID.Data1, p->encodeWidth, p->encodeHeight,
            p->darWidth, p->darHeight, p->maxEncodeWidth, p->maxEncodeHeight, p->frameRateNum, p->frameRateDen,
            p->enableEncodeAsync, p->enablePTD, p->tuningInfo, c->profileGUID.Data1, c->gopLength, c->frameIntervalP,
            c->rcParams.rateControlMode, c->rcParams.averageBitRate, c->rcParams.maxBitRate,
            c->rcParams.vbvBufferSize, c->rcParams.vbvInitialDelay, c->rcParams.enableLookahead, c->rcParams.zeroReorderDelay);
    } else fprintf(stderr, "UURB_NVENC_TRACE {\"call\":\"initialize_invalid_layout\",\"status\":%u}\n", status);
    return status;
}
static NVENCSTATUS NVENCAPI trace_register(void *s, NV_ENC_REGISTER_RESOURCE *p)
{
    NVENCSTATUS status = trace_actual.nvEncRegisterResource(s, p);
    fprintf(stderr, "UURB_NVENC_TRACE {\"call\":\"register\",\"status\":%u,\"version\":%u,\"format\":%u,\"width\":%u,\"height\":%u}\n",
        status, p ? p->version : 0, p ? p->bufferFormat : 0, p ? p->width : 0, p ? p->height : 0);
    return status;
}
static NVENCSTATUS NVENCAPI trace_picture(void *s, NV_ENC_PIC_PARAMS *p)
{
    NVENCSTATUS status = trace_actual.nvEncEncodePicture(s, p);
    fprintf(stderr, "UURB_NVENC_TRACE {\"call\":\"picture\",\"status\":%u,\"version\":%u,\"flags\":%u,\"format\":%u}\n",
        status, p ? p->version : 0, p ? p->encodePicFlags : 0, p ? p->bufferFmt : 0);
    return status;
}
static NVENCSTATUS NVENCAPI trace_sequence(void *s, NV_ENC_SEQUENCE_PARAM_PAYLOAD *p)
{
    NVENCSTATUS status = trace_actual.nvEncGetSequenceParams(s, p);
    fprintf(stderr, "UURB_NVENC_TRACE {\"call\":\"sequence\",\"status\":%u,\"capacity\":%u,\"bytes\":%u}\n",
        status, p ? p->inBufferSize : 0, !status && p && p->outSPSPPSPayloadSize ? *p->outSPSPPSPayloadSize : 0);
    return status;
}
static NVENCSTATUS NVENCAPI trace_output(void *s, NV_ENC_CREATE_BITSTREAM_BUFFER *p)
{
    NVENCSTATUS status = trace_actual.nvEncCreateBitstreamBuffer(s, p);
    fprintf(stderr, "UURB_NVENC_TRACE {\"call\":\"create_bitstream\",\"status\":%u}\n", status);
    return status;
}
static void trace_attach(NV_ENCODE_API_FUNCTION_LIST *api)
{
#ifdef UURB_NVENC_DETECTOR_CONFIG
    fprintf(stderr, "UURB_NVENC_TRACE {\"call\":\"policy\",\"exact_detector_config_enabled\":true}\n");
#else
    fprintf(stderr, "UURB_NVENC_TRACE {\"call\":\"policy\",\"exact_detector_config_enabled\":false}\n");
#endif
    trace_actual = *api;
    api->nvEncOpenEncodeSessionEx = trace_open;
    api->nvEncGetEncodeCaps = trace_caps;
    api->nvEncGetEncodePresetConfigEx = trace_preset;
    api->nvEncInitializeEncoder = trace_initialize;
    api->nvEncRegisterResource = trace_register;
    api->nvEncEncodePicture = trace_picture;
    api->nvEncGetSequenceParams = trace_sequence;
    api->nvEncCreateBitstreamBuffer = trace_output;
}
#endif
