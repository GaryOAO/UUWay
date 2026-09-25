/* Portal-FD-only PipeWire DMA-BUF negotiation. No pixel mapping or CPU fallback.
 * Advertise only modifiers reported by the NVIDIA Vulkan physical device.
 * Optional import-only or synchronous Vulkan/CUDA/NVENC verification. No UU submission. */
#define _GNU_SOURCE
#include <pipewire/pipewire.h>
#include <spa/param/video/format-utils.h>
#include <vulkan/vulkan.h>
#include <libdrm/drm_fourcc.h>
#include <fcntl.h>
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "native_vk_dmabuf_import.h"
#include "native_vk_capture_encode.h"
#include "native_capture_timing.h"
#include "native_gpu_frame_channel.h"
#include "native_cursor_metadata.h"
#include "native_capture_status.h"
#include <sys/stat.h>
#include <time.h>

struct probe {
    struct pw_main_loop *loop;
    struct pw_stream *stream;
    struct spa_video_info_raw video;
    struct uurb_gpu_publisher *publisher;
    unsigned frames, planes;
    uint32_t offsets[4], strides[4];
    int failed, negotiated;
    struct uurb_vk_importer *importer;
    unsigned imported;
    struct uurb_capture_encoder *encoder;
    int hevc;
    int status_fd;
    uint64_t first_frame_ns, last_frame_ns, maximum_gap_ns;
    unsigned delivery_gaps[4];
    struct uurb_capture_timing source_timing;
    unsigned missing_source_header;
    unsigned process_callbacks, maximum_batch;
    struct uurb_cursor_snapshot *cursor;
    unsigned cursor_updates;
    int cursor_mutter;
    int cursor_composite;
    unsigned cursor_frames;
};

static unsigned validation_errors;
static VKAPI_ATTR VkBool32 VKAPI_CALL validation_message(VkDebugUtilsMessageSeverityFlagBitsEXT severity,
    VkDebugUtilsMessageTypeFlagsEXT types, const VkDebugUtilsMessengerCallbackDataEXT *message, void *data)
{
    (void)types; (void)data;
    if (severity & VK_DEBUG_UTILS_MESSAGE_SEVERITY_ERROR_BIT_EXT) validation_errors++;
    fprintf(stderr, "Vulkan validation: %s\n", message->pMessage);
    return VK_FALSE;
}

static uint32_t fourcc(uint32_t format)
{
    switch (format) {
    case SPA_VIDEO_FORMAT_BGRA: return DRM_FORMAT_ARGB8888;
    case SPA_VIDEO_FORMAT_BGRx: return DRM_FORMAT_XRGB8888;
    case SPA_VIDEO_FORMAT_RGBA: return DRM_FORMAT_ABGR8888;
    case SPA_VIDEO_FORMAT_RGBx: return DRM_FORMAT_XBGR8888;
    default: return 0;
    }
}

static void fail(struct probe *p, const char *message)
{
    fprintf(stderr, "%s\n", message);
    p->failed = 1;
    pw_main_loop_quit(p->loop);
}

static void state_changed(void *opaque, enum pw_stream_state old,
                          enum pw_stream_state state, const char *error)
{
    (void)old;
    struct probe *p = opaque;
    if (state == PW_STREAM_STATE_ERROR) fail(p, error ? error : "PipeWire stream error");
}

static void format_changed(void *opaque, uint32_t id, const struct spa_pod *format)
{
    struct probe *p = opaque;
    if (id != SPA_PARAM_Format || !format) return;
    struct spa_video_info_raw video = {0};
    int parsed = spa_format_video_raw_parse(format, &video);
    const struct spa_pod_prop *modifier = spa_pod_find_prop(format, NULL, SPA_FORMAT_VIDEO_modifier);
    fprintf(stderr, "Negotiated video: parse=%d flags=%u format=%u size=%ux%u modifier_type=%u\n",
            parsed, video.flags, video.format, video.size.width, video.size.height,
            modifier ? modifier->value.type : 0);
    if (modifier && spa_pod_is_choice(&modifier->value))
        fprintf(stderr, "Modifier choice type=%u values=%u\n",
                SPA_POD_CHOICE_TYPE(&modifier->value), (unsigned)SPA_POD_CHOICE_N_VALUES(&modifier->value));
    if (parsed < 0 ||
        !(video.flags & SPA_VIDEO_FLAG_MODIFIER) ||
        !modifier || !fourcc(video.format) ||
        !video.size.width || !video.size.height || video.size.width > 8192 || video.size.height > 8192) {
        fail(p, "No fixed explicit DMA-BUF video format; refusing fallback");
        return;
    }
    p->video = video;
    if (p->publisher) {
        p->publisher->rate_num = video.framerate.num;
        p->publisher->rate_den = video.framerate.denom;
        p->publisher->max_rate_num = video.max_framerate.num;
        p->publisher->max_rate_den = video.max_framerate.denom;
    }
    uint8_t storage[512];
    struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(storage, sizeof(storage));
    const struct spa_pod *params[] = {
        spa_pod_builder_add_object(&builder, SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
            SPA_PARAM_BUFFERS_dataType, SPA_POD_Int(1u << SPA_DATA_DmaBuf)),
        spa_pod_builder_add_object(&builder, SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
            SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Header),
            SPA_PARAM_META_size, SPA_POD_Int(sizeof(struct spa_meta_header))),
        spa_pod_builder_add_object(&builder, SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
            SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Cursor),
            SPA_PARAM_META_size, SPA_POD_Int(sizeof(struct spa_meta_cursor) + sizeof(struct spa_meta_bitmap) + UURB_CURSOR_PIXEL_BYTES))};
    if (pw_stream_update_params(p->stream, params, p->cursor ? 3 : 2) < 0) {
        fail(p, "DMA-BUF buffer negotiation failed");
        return;
    }
    /* The producer fixes its allocation modifier after buffer negotiation.
     * The initial Format can still carry the offered enum; never process its
     * default as if it were the actual allocation layout. */
    p->negotiated = !spa_pod_is_choice(&modifier->value) ||
        SPA_POD_CHOICE_TYPE(&modifier->value) == SPA_CHOICE_None;
}

static void record_delivery(struct probe *p, const struct spa_meta_header *header, uint64_t delivered_ns)
{
    if (header) uurb_capture_timing_add(&p->source_timing, header->pts);
    else p->missing_source_header++;
    if (!p->frames) p->first_frame_ns = delivered_ns;
    else {
        uint64_t gap = delivered_ns - p->last_frame_ns;
        if (gap > p->maximum_gap_ns) p->maximum_gap_ns = gap;
        p->delivery_gaps[gap < 12000000 ? 0 : gap < 24000000 ? 1 : gap < 40000000 ? 2 : 3]++;
    }
    p->last_frame_ns = delivered_ns;
    p->frames++;
}

static void process_frame(struct probe *p, struct pw_buffer *frame)
{
    struct spa_buffer *buffer = frame->buffer;
    /* Cursor-only buffers carry zero video bytes. Process their metadata before
     * deciding whether a GPU frame needs encoding. Never map desktop pixels. */
    if (p->cursor && buffer) {
        struct spa_meta *meta = spa_buffer_find_meta(buffer, SPA_META_Cursor);
        int update = meta ? uurb_cursor_metadata(p->cursor, meta->data, meta->size, p->cursor_mutter) : 0;
        if (update < 0) {
            pw_stream_queue_buffer(p->stream, frame);
            fail(p, "Invalid bounded cursor metadata");
            return;
        }
        p->cursor_updates += update;
        if (uurb_cursor_only_buffer(buffer)) {
            /* Only opt-in GPU composition delivers a new video image for a
             * cursor-only source event. No source FD is read or retained here;
             * re-render from this encoder generation's clean GPU background. */
            if (p->cursor_composite && update && p->frames) {
                const struct spa_meta_header *header = spa_buffer_find_meta_data(buffer, SPA_META_Header, sizeof(*header));
                struct timespec now;
                if (!header || header->pts < 0 || (header->flags & SPA_META_HEADER_FLAG_DISCONT) ||
                    clock_gettime(CLOCK_MONOTONIC, &now) < 0 ||
                    uurb_capture_encoder_cursor_frame(p->encoder, (uint64_t)header->pts)) {
                    pw_stream_queue_buffer(p->stream, frame);
                    fail(p, "Cursor-only GPU delivery requires valid source PTS and retained background");
                    return;
                }
                record_delivery(p, header, (uint64_t)now.tv_sec * 1000000000ull + now.tv_nsec);
                p->cursor_frames++;
            }
            pw_stream_queue_buffer(p->stream, frame);
            return;
        }
    }
    int valid = p->negotiated && buffer && buffer->n_datas && buffer->n_datas <= 4;
    const struct spa_meta_header *header = buffer ?
        spa_buffer_find_meta_data(buffer, SPA_META_Header, sizeof(struct spa_meta_header)) : NULL;
    if (header && (header->flags & SPA_META_HEADER_FLAG_CORRUPTED)) valid = 0;
    /* Empty frame notifications are not captured frames. */
    int empty = 0;
    for (unsigned i = 0; valid && i < buffer->n_datas; i++) {
        struct spa_data *plane = &buffer->datas[i];
        if (plane->type != SPA_DATA_DmaBuf || plane->fd < 0 || plane->fd > INT_MAX ||
            fcntl((int)plane->fd, F_GETFD) < 0 || !plane->chunk ||
            plane->chunk->stride <= 0 || (plane->chunk->flags & SPA_CHUNK_FLAG_CORRUPTED)) {
            valid = 0;
            break;
        }
        if (!plane->chunk->size) empty = 1;
        if (plane->mapoffset > UINT32_MAX - plane->chunk->offset) { valid = 0; break; }
        p->offsets[i] = plane->mapoffset + plane->chunk->offset;
        p->strides[i] = plane->chunk->stride;
    }
    if (valid && !empty && p->importer) {
        if (buffer->n_datas != 1 || uurb_vk_importer_check(p->importer, buffer->datas[0].fd,
            p->video.size.width, p->video.size.height, fourcc(p->video.format),
            p->video.modifier, p->offsets[0], p->strides[0])) valid = 0;
        else p->imported++;
    }
    struct timespec now = {0};
    if (valid && !empty && clock_gettime(CLOCK_MONOTONIC, &now) < 0) valid = 0;
    uint64_t delivered_ns = (uint64_t)now.tv_sec * 1000000000ull + now.tv_nsec;
    if (valid && !empty && p->encoder) {
        if (!header || header->pts < 0 || (p->frames && (header->flags & SPA_META_HEADER_FLAG_DISCONT))) {
            fprintf(stderr, "Encoding requires continuous PipeWire source PTS; refusing a substituted clock\n");
            valid = 0;
        } else if (buffer->n_datas != 1 ||
            uurb_capture_encoder_frame(p->encoder, frame, buffer->datas[0].fd, p->video.size.width,
                p->video.size.height, fourcc(p->video.format), p->video.modifier, p->offsets[0], p->strides[0],
                (uint64_t)header->pts)) valid = 0;
    }
    if (valid && !empty) {
        if (!p->frames && p->status_fd >= 3) {
            /* The relay sink has already received a matching consumer ACK.
             * Import-only and local-encode modes cannot report this evidence. */
            if (uurb_capture_status_send(p->status_fd, p->video.size.width, p->video.size.height))
                fprintf(stderr, "Capture status unavailable; continuing GPU delivery\n");
            close(p->status_fd);
            p->status_fd = -1;
        }
        record_delivery(p, header, delivered_ns);
        p->planes = buffer->n_datas;
    }
    pw_stream_queue_buffer(p->stream, frame);
    if (!valid) fail(p, "Invalid/non-DMA-BUF plane metadata; refusing fallback");
}

static void process(void *opaque)
{
    struct probe *p = opaque;
    unsigned batch = 0;
    struct pw_buffer *frame;
    p->process_callbacks++;
    /* Drain coalesced process notifications. Bound work per dispatch so the
     * control loop can still handle its watchdog and stream lifecycle events. */
    while (batch < 16 && !p->failed && (frame = pw_stream_dequeue_buffer(p->stream))) {
        process_frame(p, frame);
        batch++;
    }
    if (batch > p->maximum_batch) p->maximum_batch = batch;
}

static void timer(void *opaque, uint64_t expirations)
{
    (void)expirations;
    pw_main_loop_quit(((struct probe *)opaque)->loop);
}

static void stop_signal(void *opaque, int number)
{
    (void)number;
    /* Runs on the PipeWire loop, not in an asynchronous signal handler.
     * Finish any outstanding bounded frame exchange, publish END and release
     * GPU/PipeWire resources on the normal cleanup path. Supervisors retain
     * their existing kill deadline if a driver or peer fails to return. */
    pw_main_loop_quit(((struct probe *)opaque)->loop);
}

static void remove_buffer(void *opaque, struct pw_buffer *buffer)
{
    struct probe *p = opaque;
    uurb_capture_encoder_forget(p->encoder, buffer);
}

static const struct pw_stream_events events = {
    PW_VERSION_STREAM_EVENTS, .state_changed = state_changed,
    .param_changed = format_changed, .process = process, .remove_buffer = remove_buffer};

static const struct spa_pod *offer(struct spa_pod_builder *builder,
                                  VkPhysicalDevice gpu, VkFormat vkformat, uint32_t spaformat, int encode,
                                  unsigned max_fps, const struct spa_rectangle *size)
{
    VkDrmFormatModifierPropertiesListEXT list = {
        .sType = VK_STRUCTURE_TYPE_DRM_FORMAT_MODIFIER_PROPERTIES_LIST_EXT};
    VkFormatProperties2 properties = {.sType = VK_STRUCTURE_TYPE_FORMAT_PROPERTIES_2, .pNext = &list};
    vkGetPhysicalDeviceFormatProperties2(gpu, vkformat, &properties);
    if (!list.drmFormatModifierCount || list.drmFormatModifierCount > 128) return NULL;
    VkDrmFormatModifierPropertiesEXT values[128];
    list.pDrmFormatModifierProperties = values;
    vkGetPhysicalDeviceFormatProperties2(gpu, vkformat, &properties);
    uint64_t modifiers[128];
    unsigned count = 0;
    for (unsigned i = 0; i < list.drmFormatModifierCount; i++) {
        if ((values[i].drmFormatModifierTilingFeatures & VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT) &&
            (!encode || ((values[i].drmFormatModifierTilingFeatures & VK_FORMAT_FEATURE_BLIT_SRC_BIT) &&
                         values[i].drmFormatModifierPlaneCount == 1)) &&
            values[i].drmFormatModifierPlaneCount > 0 && values[i].drmFormatModifierPlaneCount <= 4)
            modifiers[count++] = values[i].drmFormatModifier;
    }
    if (!count) return NULL;
    struct spa_pod_frame object, choice;
    spa_pod_builder_push_object(builder, &object, SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat);
    spa_pod_builder_add(builder, SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_video),
        SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
        SPA_FORMAT_VIDEO_format, SPA_POD_Id(spaformat), 0);
    spa_pod_builder_prop(builder, SPA_FORMAT_VIDEO_modifier,
                         SPA_POD_PROP_FLAG_MANDATORY | SPA_POD_PROP_FLAG_DONT_FIXATE);
    spa_pod_builder_push_choice(builder, &choice, SPA_CHOICE_Enum, 0);
    spa_pod_builder_long(builder, modifiers[0]);
    for (unsigned i = 0; i < count; i++) spa_pod_builder_long(builder, modifiers[i]);
    spa_pod_builder_pop(builder, &choice);
    if (size->width) spa_pod_builder_add(builder, SPA_FORMAT_VIDEO_size, SPA_POD_Rectangle(size), 0);
    else spa_pod_builder_add(builder, SPA_FORMAT_VIDEO_size, SPA_POD_CHOICE_RANGE_Rectangle(
            &SPA_RECTANGLE(1920,1080), &SPA_RECTANGLE(1,1), &SPA_RECTANGLE(8192,8192)), 0);
    spa_pod_builder_add(builder,
        SPA_FORMAT_VIDEO_framerate, SPA_POD_CHOICE_RANGE_Fraction(
            &SPA_FRACTION(60,1), &SPA_FRACTION(0,1), &SPA_FRACTION(120,1)), 0);
    if (max_fps) spa_pod_builder_add(builder, SPA_FORMAT_VIDEO_maxFramerate,
        SPA_POD_Fraction(&SPA_FRACTION(max_fps, 1)), 0);
    return spa_pod_builder_pop(builder, &object);
}

static int number(const char *text, unsigned long max, unsigned long *value)
{
    char *end;
    errno = 0;
    *value = strtoul(text, &end, 10);
    return !errno && *text && text[0] != '-' && !*end && *value <= max;
}

int main(int argc, char **argv)
{
    const char *composite_setting = getenv("UURB_CURSOR_COMPOSITE");
    if (composite_setting && strcmp(composite_setting, "1")) {
        fprintf(stderr, "UURB_CURSOR_COMPOSITE must be absent or exactly 1\n"); return 2;
    }
    unsigned long target_serial = 0;
    const char *serial_setting = getenv("UURB_CAPTURE_TARGET_SERIAL");
    if (serial_setting && (strspn(serial_setting, "0123456789") != strlen(serial_setting) ||
        !number(serial_setting, ULONG_MAX, &target_serial) || !target_serial)) {
        fprintf(stderr, "UURB_CAPTURE_TARGET_SERIAL must be a positive bounded decimal serial\n"); return 2;
    }
    unsigned long duration = 8;
    const char *duration_setting = getenv("UURB_CAPTURE_DURATION_SECONDS");
    if (duration_setting && !number(duration_setting, 3600, &duration)) {
        fprintf(stderr, "Capture duration must be 0 (supervised) or from 1 to 3600 seconds\n"); return 2;
    }
    struct spa_rectangle requested_size = {0};
    const char *size_setting = getenv("UURB_CAPTURE_SIZE");
    if (size_setting) {
        unsigned width = 0, height = 0;
        int consumed = 0;
        if (strlen(size_setting) > 9 || sscanf(size_setting, "%ux%u%n", &width, &height, &consumed) != 2 ||
            size_setting[consumed] || width < 2 || height < 2 || width > 4096 || height > 4096 ||
            width % 2 || height % 2) {
            fprintf(stderr, "UURB_CAPTURE_SIZE must be even WIDTHxHEIGHT from 2 to 4096\n"); return 2;
        }
        requested_size = SPA_RECTANGLE(width, height);
    }
    unsigned long requested_max_fps = 0;
    const char *fps_setting = getenv("UURB_CAPTURE_MAX_FPS");
    if (fps_setting && (!number(fps_setting, 120, &requested_max_fps) || !requested_max_fps)) {
        fprintf(stderr, "UURB_CAPTURE_MAX_FPS must be an integer from 1 to 120\n"); return 2;
    }
    unsigned long fd, node;
    int import_check = argc == 4 && strcmp(argv[3], "--vulkan-import") == 0;
    int encode = argc == 5 && (!strcmp(argv[3], "--encode-h264") || !strcmp(argv[3], "--encode-hevc"));
    int relay = argc == 5 && !strcmp(argv[3], "--gpu-relay");
    unsigned long output_fd = 0;
    struct stat output_info;
    if ((argc != 3 && !import_check && !encode && !relay) || !number(argv[1], INT_MAX, &fd) || fd < 3 || fcntl(fd, F_GETFD) < 0 ||
        !number(argv[2], UINT32_MAX - 1, &node) || !node) {
        fprintf(stderr, "usage: uu-pipewire-native-probe PORTAL_FD NODE_ID\n"); return 2;
    }
    if (encode && (!number(argv[4], INT_MAX, &output_fd) || output_fd < 3 || output_fd == fd ||
        fstat(output_fd, &output_info) < 0 || !S_ISREG(output_info.st_mode) ||
        (fcntl(output_fd, F_GETFL) & O_ACCMODE) == O_RDONLY)) {
        fprintf(stderr, "Encoding requires an explicit writable regular output FD\n"); return 2;
    }
    if (relay && (!number(argv[4], INT_MAX, &output_fd) || output_fd < 3 || output_fd == fd || uurb_gpu_channel_check(output_fd))) {
        fprintf(stderr, "GPU relay requires a private connected same-user seqpacket FD\n"); return 2;
    }
    int status_fd = -1;
    const char *status_setting = getenv("UURB_CAPTURE_STATUS_FD");
    if (status_setting) {
        unsigned long value;
        if (!relay || !number(status_setting, INT_MAX, &value) || value < 3 ||
            value == fd || value == output_fd || uurb_gpu_channel_check((int)value)) {
            fprintf(stderr, "Capture status requires a separate private seqpacket FD and GPU relay\n"); return 2;
        }
        status_fd = (int)value;
    }
    /* Block before Vulkan/CUDA can create threads, so a process-directed stop
     * cannot land on an unblocked driver thread instead of the loop signalfd.
     * This standalone process exits on all paths; no caller mask is inherited
     * beyond it. The supervisor's SIGKILL deadline remains effective. */
    sigset_t stop_signals;
    sigemptyset(&stop_signals);
    sigaddset(&stop_signals, SIGTERM);
    sigaddset(&stop_signals, SIGINT);
    if (sigprocmask(SIG_BLOCK, &stop_signals, NULL)) return 1;
    struct uurb_gpu_publisher publisher = {.socket_fd = (int)output_fd};
    int result = 1;
    VkInstance instance = VK_NULL_HANDLE;
    VkDebugUtilsMessengerEXT messenger = VK_NULL_HANDLE;
    int validate = getenv("UURB_VK_VALIDATE") && !strcmp(getenv("UURB_VK_VALIDATE"), "1");
    const char *layer = "VK_LAYER_KHRONOS_validation", *debug_extension = VK_EXT_DEBUG_UTILS_EXTENSION_NAME;
    VkDebugUtilsMessengerCreateInfoEXT debug = {.sType = VK_STRUCTURE_TYPE_DEBUG_UTILS_MESSENGER_CREATE_INFO_EXT,
        .messageSeverity = VK_DEBUG_UTILS_MESSAGE_SEVERITY_ERROR_BIT_EXT | VK_DEBUG_UTILS_MESSAGE_SEVERITY_WARNING_BIT_EXT,
        .messageType = VK_DEBUG_UTILS_MESSAGE_TYPE_GENERAL_BIT_EXT | VK_DEBUG_UTILS_MESSAGE_TYPE_VALIDATION_BIT_EXT |
            VK_DEBUG_UTILS_MESSAGE_TYPE_PERFORMANCE_BIT_EXT, .pfnUserCallback = validation_message};
    VkApplicationInfo app = {.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .apiVersion = VK_API_VERSION_1_1};
    VkInstanceCreateInfo create = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app,
        .enabledLayerCount = validate ? 1 : 0, .ppEnabledLayerNames = &layer,
        .enabledExtensionCount = validate ? 1 : 0, .ppEnabledExtensionNames = &debug_extension,
        .pNext = validate ? &debug : NULL};
    if (vkCreateInstance(&create, NULL, &instance) != VK_SUCCESS) return 1;
    if (validate) {
        PFN_vkCreateDebugUtilsMessengerEXT setup = (void *)vkGetInstanceProcAddr(instance, "vkCreateDebugUtilsMessengerEXT");
        if (!setup || setup(instance, &debug, NULL, &messenger) != VK_SUCCESS) goto vulkan_done;
    }
    uint32_t count = 16;
    VkPhysicalDevice devices[16], selected = VK_NULL_HANDLE;
    if (vkEnumeratePhysicalDevices(instance, &count, devices) != VK_SUCCESS) goto vulkan_done;
    for (unsigned i = 0; i < count; i++) {
        VkPhysicalDeviceProperties properties;
        vkGetPhysicalDeviceProperties(devices[i], &properties);
        if (properties.vendorID == 0x10de) {
            if (selected) { fprintf(stderr, "Multiple NVIDIA GPUs: explicit selection required\n"); goto vulkan_done; }
            selected = devices[i];
        }
    }
    if (!selected) { fprintf(stderr, "No NVIDIA Vulkan physical device\n"); goto vulkan_done; }
    uint8_t storage[16384];
    struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(storage, sizeof(storage));
    const struct spa_pod *params[4];
    unsigned nparams = 0;
    const VkFormat vkformats[] = {VK_FORMAT_B8G8R8A8_UNORM, VK_FORMAT_B8G8R8A8_UNORM,
                                  VK_FORMAT_R8G8B8A8_UNORM, VK_FORMAT_R8G8B8A8_UNORM};
    const uint32_t formats[] = {SPA_VIDEO_FORMAT_BGRA, SPA_VIDEO_FORMAT_BGRx,
                               SPA_VIDEO_FORMAT_RGBA, SPA_VIDEO_FORMAT_RGBx};
    for (unsigned i = 0; i < 4; i++) {
        const struct spa_pod *param = offer(&builder, selected, vkformats[i], formats[i], encode || relay, requested_max_fps, &requested_size);
        if (param) params[nparams++] = param;
    }
    if (!nparams) { fprintf(stderr, "No sampleable explicit Vulkan modifiers\n"); goto vulkan_done; }
    pw_init(NULL, NULL);
    struct probe p = {.status_fd = status_fd, .publisher = relay ? &publisher : NULL};
    struct pw_context *context = NULL;
    struct pw_core *core = NULL;
    struct spa_source *deadline = NULL, *termination = NULL, *interruption = NULL;
    struct spa_hook listener;
    const char *cursor_fd = getenv("UURB_CURSOR_STATE_FD");
    const char *cursor_backend = getenv("UURB_CURSOR_METADATA_BACKEND");
    if (cursor_backend && strcmp(cursor_backend, "spa") && strcmp(cursor_backend, "mutter")) {
        fprintf(stderr, "Unsupported explicit cursor metadata backend\n");
        goto pw_done;
    }
    p.cursor_mutter = cursor_backend && !strcmp(cursor_backend, "mutter");
    if (cursor_fd) {
        unsigned long value;
        if (!relay || !number(cursor_fd, INT_MAX, &value) || value == fd || value == output_fd || (int)value == status_fd ||
            !(p.cursor = uurb_cursor_map((int)value))) {
            fprintf(stderr, "Cursor metadata requires an explicit private writable state FD\n");
            goto pw_done;
        }
    }
    if ((composite_setting && (!p.cursor || !relay)) ||
        (p.cursor && (p.cursor->header.reserved[0] != (composite_setting ? UURB_CURSOR_VIDEO_COMPOSITED : 0) ||
                      p.cursor->header.reserved[1] || p.cursor->header.reserved[2]))) {
        fprintf(stderr, "GPU cursor composition requires matching explicit metadata snapshot policy\n");
        goto pw_done;
    }
    if (relay) {
        p.encoder = uurb_capture_sink_open(selected, uurb_gpu_publish, &publisher);
        if (!p.encoder) goto pw_done;
        if (composite_setting) {
            if (uurb_capture_encoder_composite_cursor(p.encoder, p.cursor)) goto pw_done;
            p.cursor_composite = 1;
        }
    }
    if (encode) {
        p.hevc = !strcmp(argv[3], "--encode-hevc");
        p.encoder = uurb_capture_encoder_open(selected, p.hevc, output_fd);
        if (!p.encoder) goto pw_done;
    }
    if (import_check) {
        p.importer = uurb_vk_importer_open(selected);
        if (!p.importer) goto pw_done;
    }
    p.loop = pw_main_loop_new(NULL);
    if (!p.loop) goto pw_done;
    termination = pw_loop_add_signal(pw_main_loop_get_loop(p.loop), SIGTERM, stop_signal, &p);
    interruption = pw_loop_add_signal(pw_main_loop_get_loop(p.loop), SIGINT, stop_signal, &p);
    if (!termination || !interruption) goto pw_done;
    context = pw_context_new(pw_main_loop_get_loop(p.loop), NULL, 0);
    if (!context) goto pw_done;
    /* connect_fd takes ownership; never connect to the unrestricted default bus. */
    core = pw_context_connect_fd(context, (int)fd, NULL, 0);
    if (!core) goto pw_done;
    struct pw_properties *stream_properties = pw_properties_new(
        PW_KEY_MEDIA_TYPE, "Video", PW_KEY_MEDIA_CATEGORY, "Capture", PW_KEY_MEDIA_ROLE, "Screen", NULL);
    if (!stream_properties) goto pw_done;
    if (target_serial && pw_properties_setf(stream_properties, PW_KEY_TARGET_OBJECT, "%lu", target_serial) < 0) {
        pw_properties_free(stream_properties); goto pw_done;
    }
    p.stream = pw_stream_new(core, "uurb-native-dmabuf-probe", stream_properties);
    if (!p.stream) goto pw_done;
    pw_stream_add_listener(p.stream, &listener, &events, &p);
    /* No MAP_BUFFERS: encoding imports GPU memory without mapping pixels. */
    /* Modern Portals supply a non-reused object.serial. Never silently fall
     * back to a reused node ID when a precise serial was explicitly provided. */
    if (pw_stream_connect(p.stream, PW_DIRECTION_INPUT, target_serial ? PW_ID_ANY : node, PW_STREAM_FLAG_AUTOCONNECT,
                           params, nparams) < 0) goto pw_done;
    deadline = pw_loop_add_timer(pw_main_loop_get_loop(p.loop), timer, &p);
    if (!deadline) goto pw_done;
    struct timespec limit = {.tv_sec = duration};
    /* Explicit continuous capture is terminated by disconnect/service cleanup,
     * not an hourly timer. Zero leaves the newly created timer disarmed. */
    if (duration) pw_loop_update_timer(pw_main_loop_get_loop(p.loop), deadline, &limit, NULL, false);
    pw_main_loop_run(p.loop);
    if (relay && !p.failed && uurb_gpu_publish_end(&publisher)) p.failed = 1;
    if (!p.failed && p.frames && !validation_errors) {
        struct uurb_capture_stats stats = uurb_capture_encoder_stats(p.encoder);
        printf("{\"frames\":%u,\"width\":%u,\"height\":%u,\"spa_format\":%u,\"drm_fourcc\":%u,"
               "\"drm_modifier\":\"0x%016" PRIx64 "\",\"memory_planes\":%u,"
               "\"dmabuf_only\":true,\"pixels_mapped\":false,\"gpu_relayed\":%s,\"encoded\":%s,\"codec\":\"%s\",\"vulkan_imported_frames\":%u,\"validation_enabled\":%s,\"planes\":[",
               p.frames, p.video.size.width, p.video.size.height, p.video.format, fourcc(p.video.format), p.video.modifier, p.planes,
               relay ? "true" : "false", encode ? "true" : "false", encode ? (p.hevc ? "hevc" : "h264") : "none", (encode || relay) ? p.frames : p.imported,
               validate ? "true" : "false");
        for (unsigned i = 0; i < p.planes; i++)
            printf("%s{\"offset\":%u,\"stride\":%u}", i ? "," : "", p.offsets[i], p.strides[i]);
        double span = (double)(p.last_frame_ns - p.first_frame_ns) / 1000000000.0;
        printf("],\"gpu_import_allocations\":%u,\"frame_processing_us_avg\":%.3f,\"frame_processing_us_max\":%.3f,"
               "\"delivery_span_seconds\":%.6f,\"delivery_fps\":%.3f,\"delivery_gap_us_max\":%.3f,"
               "\"delivery_gap_bins_ms\":{\"lt12\":%u,\"12to24\":%u,\"24to40\":%u,\"ge40\":%u},"
               "\"negotiated_framerate\":[%u,%u],\"negotiated_max_framerate\":[%u,%u],\"requested_max_fps\":%lu,"
               "\"source_pts_samples\":%u,\"source_pts_invalid\":%u,\"source_header_missing\":%u,\"source_pts_fps\":%.3f,"
               "\"source_gap_bins_ms\":{\"lt12\":%u,\"12to24\":%u,\"24to40\":%u,\"ge40\":%u},"
               "\"process_callbacks\":%u,\"maximum_batch\":%u,\"encode_timestamp_source\":\"%s\"}\n",
                stats.imports, stats.average_us, stats.maximum_us, span, span > 0 ? (p.frames - 1) / span : 0,
                (double)p.maximum_gap_ns / 1000, p.delivery_gaps[0], p.delivery_gaps[1], p.delivery_gaps[2], p.delivery_gaps[3],
                p.video.framerate.num, p.video.framerate.denom, p.video.max_framerate.num, p.video.max_framerate.denom,
                requested_max_fps,
                p.source_timing.samples, p.source_timing.invalid, p.missing_source_header,
                uurb_capture_timing_fps(&p.source_timing),
                p.source_timing.gaps[0], p.source_timing.gaps[1], p.source_timing.gaps[2], p.source_timing.gaps[3],
                p.process_callbacks, p.maximum_batch, (encode || relay) ? "pipewire-pts" : "none");
        result = 0;
    } else fprintf(stderr, "Native capture did not deliver valid DMA-BUF frames\n");
pw_done:
    fprintf(stderr, "UURB_CURSOR_METADATA {\"updates\":%u}\n", p.cursor_updates);
    if (p.cursor_composite)
        fprintf(stderr, "UURB_CURSOR_COMPOSITE {\"cursor_only_frames\":%u,\"desktop_frames\":%u}\n",
            p.cursor_frames, p.frames - p.cursor_frames);
    if (deadline) pw_loop_destroy_source(pw_main_loop_get_loop(p.loop), deadline);
    if (termination) pw_loop_destroy_source(pw_main_loop_get_loop(p.loop), termination);
    if (interruption) pw_loop_destroy_source(pw_main_loop_get_loop(p.loop), interruption);
    if (p.stream) pw_stream_destroy(p.stream);
    if (core) pw_core_disconnect(core);
    if (context) pw_context_destroy(context);
    if (p.loop) pw_main_loop_destroy(p.loop);
    uurb_vk_importer_close(p.importer);
    uurb_capture_encoder_close(p.encoder);
    uurb_cursor_unmap(p.cursor);
    pw_deinit();
vulkan_done:
    if (messenger) {
        PFN_vkDestroyDebugUtilsMessengerEXT destroy = (void *)vkGetInstanceProcAddr(instance, "vkDestroyDebugUtilsMessengerEXT");
        destroy(instance, messenger, NULL);
    }
    vkDestroyInstance(instance, NULL);
    return validation_errors ? 1 : result;
}
