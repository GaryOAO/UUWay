#define _GNU_SOURCE
#include "native_vaapi_encoder.h"
#include "native_nvenc_negotiation.h"
#include "native_spatial_aq.h"
#include <libavcodec/avcodec.h>
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_vaapi.h>
#include <libavutil/opt.h>
#include <libswscale/swscale.h>
#include <vulkan/vulkan.h>
#include <dlfcn.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct uurb_encoder {
    AVBufferRef *device, *frames;
    AVCodecContext *codec;
    AVFrame *software;
    AVPacket *packet;
    struct SwsContext *scale;
    NV_ENC_CONFIG config;
    NV_ENC_INITIALIZE_PARAMS init;
    unsigned char uuid[16];
    void *mapped;
    size_t mapped_size;
    uint64_t last_timestamp, frames_sent;
    int locked, failed, pending_idr;
};

/* Match the actual Vulkan GPU used by DXVK/PipeWire, not renderD128 by guess. */
int uurb_vaapi_device(const unsigned char uuid[16], char node[128])
{
    /* Winelib imports Vulkan with the Windows ABI in the same process.
     * Never resolve native calls via the global ELF symbol namespace. */
    void *library = dlopen("libvulkan.so.1", RTLD_NOW | RTLD_LOCAL);
    if (!library) return 1;
    PFN_vkCreateInstance create_instance = (void *)dlsym(library, "vkCreateInstance");
    PFN_vkEnumeratePhysicalDevices enumerate = (void *)dlsym(library, "vkEnumeratePhysicalDevices");
    PFN_vkGetPhysicalDeviceProperties2 properties = (void *)dlsym(library, "vkGetPhysicalDeviceProperties2");
    PFN_vkDestroyInstance destroy_instance = (void *)dlsym(library, "vkDestroyInstance");
    if (!create_instance || !enumerate || !properties || !destroy_instance) { dlclose(library); return 1; }
    VkInstance instance;
    VkApplicationInfo app = {.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .apiVersion = VK_API_VERSION_1_1};
    VkInstanceCreateInfo create = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app};
    if (create_instance(&create, NULL, &instance) != VK_SUCCESS) { dlclose(library); return 1; }
    VkPhysicalDevice devices[32]; uint32_t count = 32; int result = 1, matches = 0;
    if (enumerate(instance, &count, devices) != VK_SUCCESS) goto done;
    for (unsigned i = 0; i < count; ++i) {
        VkPhysicalDeviceDrmPropertiesEXT drm = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRM_PROPERTIES_EXT};
        VkPhysicalDeviceIDProperties id = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ID_PROPERTIES, .pNext = &drm};
        VkPhysicalDeviceProperties2 props = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2, .pNext = &id};
        properties(devices[i], &props);
        if (props.properties.vendorID != 0x1002 || !drm.hasRender || (uuid && memcmp(uuid, id.deviceUUID, 16))) continue;
        snprintf(node, 128, "/dev/dri/renderD%lld", (long long)drm.renderMinor);
        ++matches;
    }
    result = matches != 1;
done:
    destroy_instance(instance, NULL);
    dlclose(library);
    return result;
}

static void codec_close(struct uurb_encoder *s)
{
    avcodec_free_context(&s->codec);
    av_buffer_unref(&s->frames);
}

static int codec_open(struct uurb_encoder *s)
{
    int hevc = !memcmp(&s->init.encodeGUID, &NV_ENC_CODEC_HEVC_GUID, sizeof(GUID));
    const AVCodec *codec = avcodec_find_encoder_by_name(hevc ? "hevc_vaapi" : "h264_vaapi");
    if (!codec || !(s->codec = avcodec_alloc_context3(codec)) ||
        !(s->frames = av_hwframe_ctx_alloc(s->device))) return 1;
    AVHWFramesContext *frames = (void *)s->frames->data;
    frames->format = AV_PIX_FMT_VAAPI; frames->sw_format = AV_PIX_FMT_NV12;
    frames->width = s->init.encodeWidth; frames->height = s->init.encodeHeight;
    frames->initial_pool_size = 4;
    if (av_hwframe_ctx_init(s->frames) < 0) return 1;
    AVCodecContext *c = s->codec;
    c->width = s->init.encodeWidth; c->height = s->init.encodeHeight;
    c->pix_fmt = AV_PIX_FMT_VAAPI;
    c->hw_frames_ctx = av_buffer_ref(s->frames);
    if (!c->hw_frames_ctx) return 1;
    c->time_base = (AVRational){s->init.frameRateDen, s->init.frameRateNum};
    c->framerate = (AVRational){s->init.frameRateNum, s->init.frameRateDen};
    c->bit_rate = s->config.rcParams.averageBitRate;
    c->rc_max_rate = s->config.rcParams.maxBitRate;
    c->rc_buffer_size = s->config.rcParams.vbvBufferSize;
    c->rc_initial_buffer_occupancy = s->config.rcParams.vbvInitialDelay;
    c->gop_size = s->config.gopLength > INT_MAX ? INT_MAX : (int)s->config.gopLength;
    c->max_b_frames = 0;
    c->i_quant_factor = 1.0;
    c->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
    const NV_ENC_CONFIG_H264_VUI_PARAMETERS *vui = hevc ? &s->config.encodeCodecConfig.hevcConfig.hevcVUIParameters
                                                        : &s->config.encodeCodecConfig.h264Config.h264VUIParameters;
    c->color_range = AVCOL_RANGE_MPEG;
    c->color_primaries = vui->colourDescriptionPresentFlag ? (enum AVColorPrimaries)vui->colourPrimaries : AVCOL_PRI_UNSPECIFIED;
    c->color_trc = vui->colourDescriptionPresentFlag ? (enum AVColorTransferCharacteristic)vui->transferCharacteristics : AVCOL_TRC_UNSPECIFIED;
    c->colorspace = vui->colourDescriptionPresentFlag ? (enum AVColorSpace)vui->colourMatrix : AVCOL_SPC_UNSPECIFIED;
    AVDictionary *options = NULL;
    av_dict_set(&options, "rc_mode", s->config.rcParams.rateControlMode == NV_ENC_PARAMS_RC_CBR ? "CBR" : "VBR", 0);
    av_dict_set(&options, "async_depth", "1", 0);
    av_dict_set(&options, "idr_interval", "0", 0);
    av_dict_set(&options, "aud", (hevc ? s->config.encodeCodecConfig.hevcConfig.outputAUD : s->config.encodeCodecConfig.h264Config.outputAUD) ? "1" : "0", 0);
    int result = avcodec_open2(c, codec, &options);
    int leftover = av_dict_count(options);
    av_dict_free(&options);
    if (result < 0 || leftover || c->extradata_size <= 0 || c->extradata_size > 65536) return 1;
    s->pending_idr = 1;
    return 0;
}

struct uurb_encoder *uurb_vaapi_open(const unsigned char uuid[16], const NV_ENC_INITIALIZE_PARAMS *p)
{
    if (!p || !uurb_nvenc_valid_rates(p) || p->encodeWidth < 2 || p->encodeHeight < 2 ||
        p->encodeWidth > 4096 || p->encodeHeight > 4096 || ((p->encodeWidth | p->encodeHeight) & 1) ||
        (memcmp(&p->encodeGUID, &NV_ENC_CODEC_H264_GUID, sizeof(GUID)) &&
         memcmp(&p->encodeGUID, &NV_ENC_CODEC_HEVC_GUID, sizeof(GUID)))) return NULL;
    /* Reject features for which this backend has no semantic equivalent. */
    const NV_ENC_CONFIG *cfg = p->encodeConfig;
    if (cfg->frameIntervalP != 1 || cfg->rcParams.enableLookahead || cfg->rcParams.enableTemporalAQ ||
        cfg->rcParams.multiPass != NV_ENC_MULTI_PASS_DISABLED || p->enableEncodeAsync) return NULL;
    if (cfg->rcParams.enableAQ && (p->encodeWidth < 128 || p->encodeHeight < 64)) return NULL;
    char node[128];
    if (uurb_vaapi_device(uuid, node)) return NULL;
    struct uurb_encoder *s = calloc(1, sizeof(*s));
    if (!s) return NULL;
    if (uuid) memcpy(s->uuid, uuid, 16);
    s->init = *p; s->config = *cfg; s->init.encodeConfig = &s->config;
    if (av_hwdevice_ctx_create(&s->device, AV_HWDEVICE_TYPE_VAAPI, node, NULL, 0) < 0) goto fail;
    if (cfg->rcParams.enableAQ) {
        AVHWDeviceContext *device = (void *)s->device->data;
        VADisplay display = ((AVVAAPIDeviceContext *)device->hwctx)->display;
        VAConfigAttrib attr = {.type = VAConfigAttribEncROI};
        VAProfile profile = !memcmp(&p->encodeGUID, &NV_ENC_CODEC_HEVC_GUID, sizeof(GUID)) ? VAProfileHEVCMain : VAProfileH264High;
        if (vaGetConfigAttributes(display, profile, VAEntrypointEncSlice, &attr, 1) != VA_STATUS_SUCCESS ||
            attr.value == VA_ATTRIB_NOT_SUPPORTED) goto fail;
        VAConfigAttribValEncROI roi = {.value = attr.value};
        if (roi.bits.num_roi_regions < 32 || !roi.bits.roi_rc_qp_delta_support) goto fail;
    }
    if (codec_open(s)) goto fail;
    s->software = av_frame_alloc(); s->packet = av_packet_alloc();
    if (!s->software || !s->packet) goto fail;
    s->software->format = AV_PIX_FMT_NV12; s->software->width = p->encodeWidth; s->software->height = p->encodeHeight;
    if (av_frame_get_buffer(s->software, 32) < 0) goto fail;
    s->scale = sws_getContext(p->encodeWidth, p->encodeHeight, AV_PIX_FMT_BGRA,
                             p->encodeWidth, p->encodeHeight, AV_PIX_FMT_NV12, SWS_BILINEAR, NULL, NULL, NULL);
    if (!s->scale || sws_setColorspaceDetails(s->scale, sws_getCoefficients(SWS_CS_ITU709), 1,
                    sws_getCoefficients(SWS_CS_ITU709), 0, 0, 1 << 16, 1 << 16) < 0) goto fail;
    return s;
fail:
    uurb_encoder_close(s);
    return NULL;
}

struct uurb_encoder *uurb_encoder_open_config(int fd, uint64_t bytes, const unsigned char uuid[16], const NV_ENC_INITIALIZE_PARAMS *p)
{
    struct uurb_encoder *s = NULL;
    struct stat st;
    if (fd < 0 || !p || bytes != (uint64_t)p->encodeWidth * p->encodeHeight * 4 ||
        fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size != (off_t)bytes) goto done;
    s = uurb_vaapi_open(uuid, p);
    if (!s) goto done;
    s->mapped = mmap(NULL, bytes, PROT_READ, MAP_SHARED, fd, 0);
    if (s->mapped == MAP_FAILED) { s->mapped = NULL; uurb_encoder_close(s); s = NULL; }
    else s->mapped_size = bytes;
done:
    if (fd >= 0) close(fd);
    return s;
}

struct uurb_encoder *uurb_encoder_open(int fd, uint64_t bytes, const unsigned char uuid[16], unsigned width, unsigned height, int hevc, unsigned bitrate)
{
    NV_ENC_CONFIG preset = {.version = NV_ENC_CONFIG_VER}, config;
    NV_ENC_INITIALIZE_PARAMS init;
    uurb_nvenc_reference_config(&preset, width, height, hevc, bitrate, &config, &init);
    return uurb_encoder_open_config(fd, bytes, uuid, &init);
}

int uurb_encoder_sequence(struct uurb_encoder *s, void *bytes, uint32_t capacity, uint32_t *size)
{
    if (!s || s->failed) return NV_ENC_ERR_INVALID_ENCODERDEVICE;
    if (!bytes || !size || !capacity || capacity > 65536) return NV_ENC_ERR_INVALID_PARAM;
    if ((unsigned)s->codec->extradata_size > capacity) return NV_ENC_ERR_NOT_ENOUGH_BUFFER;
    memcpy(bytes, s->codec->extradata, s->codec->extradata_size);
    *size = s->codec->extradata_size;
    return NV_ENC_SUCCESS;
}

int uurb_vaapi_encode_bgra(struct uurb_encoder *s, const void *pixels, unsigned pitch, uint64_t timestamp, int force_idr, struct uurb_packet *out)
{
    if (!s || !pixels || !out || s->locked || s->failed || pitch < s->init.encodeWidth * 4 || pitch > INT_MAX ||
        (s->frames_sent && timestamp <= s->last_timestamp)) return 1;
    AVFrame *hw = av_frame_alloc();
    if (!hw) return 1;
    int failed = 1;
    if (av_frame_make_writable(s->software) < 0) goto done;
    const uint8_t *src[] = {pixels}; int lines[] = {(int)pitch};
    if (sws_scale(s->scale, src, lines, 0, s->init.encodeHeight, s->software->data, s->software->linesize) != (int)s->init.encodeHeight ||
        av_hwframe_get_buffer(s->frames, hw, 0) < 0 || av_hwframe_transfer_data(hw, s->software, 0) < 0) goto done;
    if (s->config.rcParams.enableAQ) {
        int delta[32];
        uurb_spatial_aq(s->software->data[0], s->software->linesize[0], s->init.encodeWidth,
            s->init.encodeHeight, s->config.rcParams.aqStrength, delta);
        AVFrameSideData *side = av_frame_new_side_data(hw, AV_FRAME_DATA_REGIONS_OF_INTEREST,
            32 * sizeof(AVRegionOfInterest));
        if (!side) goto done;
        AVRegionOfInterest *regions = (void *)side->data;
        for (unsigned i = 0; i < 32; ++i) {
            regions[i] = (AVRegionOfInterest){.self_size = sizeof(*regions),
                .left = (s->init.encodeWidth / 8 * (i % 8)) & ~15u,
                .right = i % 8 == 7 ? s->init.encodeWidth : (s->init.encodeWidth / 8 * (i % 8 + 1)) & ~15u,
                .top = (s->init.encodeHeight / 4 * (i / 8)) & ~15u,
                .bottom = i / 8 == 3 ? s->init.encodeHeight : (s->init.encodeHeight / 4 * (i / 8 + 1)) & ~15u,
                .qoffset = {delta[i], 51}};
        }
    }
    hw->pts = s->frames_sent;
    hw->pict_type = (force_idr || s->pending_idr) ? AV_PICTURE_TYPE_I : AV_PICTURE_TYPE_NONE;
    if (avcodec_send_frame(s->codec, hw) < 0 || avcodec_receive_packet(s->codec, s->packet) < 0) goto done;
    int idr = !!(s->packet->flags & AV_PKT_FLAG_KEY);
    if (idr) {
        int header = s->codec->extradata_size, payload = s->packet->size;
        if (av_grow_packet(s->packet, header) < 0) goto done;
        memmove(s->packet->data + header, s->packet->data, payload);
        memcpy(s->packet->data, s->codec->extradata, header);
    }
    *out = (struct uurb_packet){.data = s->packet->data, .size = s->packet->size, .timestamp = timestamp,
        .idr = idr, .picture_type = idr ? NV_ENC_PIC_TYPE_IDR : NV_ENC_PIC_TYPE_P};
    s->last_timestamp = timestamp; ++s->frames_sent; s->locked = 1; s->pending_idr = 0;
    failed = 0;
done:
    av_frame_free(&hw);
    if (failed) s->failed = 1;
    return failed;
}
int uurb_encoder_encode(struct uurb_encoder *s, uint64_t timestamp, int force_idr, struct uurb_packet *packet)
{ return s ? uurb_vaapi_encode_bgra(s, s->mapped, s->init.encodeWidth * 4, timestamp, force_idr, packet) : 1; }
int uurb_encoder_release_packet(struct uurb_encoder *s)
{
    if (!s || !s->locked) return 1;
    av_packet_unref(s->packet); s->locked = 0;
    return 0;
}
int uurb_encoder_reconfigure_config(struct uurb_encoder *s, const NV_ENC_RECONFIGURE_PARAMS *p)
{
    if (!s || s->locked || s->failed || !uurb_nvenc_valid_reconfigure(&s->config, &s->init, p)) return 1;
    /* FFmpeg VA-API has no public live bitrate API. Reopen between frames;
     * no B frames/delayed packets are in flight, and the new stream starts IDR. */
    codec_close(s);
    s->config = *p->reInitEncodeParams.encodeConfig; s->init = p->reInitEncodeParams; s->init.encodeConfig = &s->config;
    if (codec_open(s)) { s->failed = 1; return 1; }
    return 0;
}
int uurb_encoder_reconfigure(struct uurb_encoder *s, unsigned bitrate)
{
    if (!s || !bitrate) return 1;
    NV_ENC_CONFIG cfg = s->config;
    cfg.rcParams.averageBitRate = cfg.rcParams.maxBitRate = bitrate;
    cfg.rcParams.vbvBufferSize = cfg.rcParams.vbvInitialDelay = bitrate / 60;
    NV_ENC_RECONFIGURE_PARAMS p = {.version = NV_ENC_RECONFIGURE_PARAMS_VER, .reInitEncodeParams = s->init, .forceIDR = 1};
    p.reInitEncodeParams.encodeConfig = &cfg;
    return uurb_encoder_reconfigure_config(s, &p);
}
int uurb_encoder_close(struct uurb_encoder *s)
{
    if (!s) return 0;
    codec_close(s); av_buffer_unref(&s->device);
    av_frame_free(&s->software); av_packet_free(&s->packet); sws_freeContext(s->scale);
    if (s->mapped) munmap(s->mapped, s->mapped_size);
    free(s);
    return 0;
}
