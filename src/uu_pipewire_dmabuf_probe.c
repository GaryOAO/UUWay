/* Consume only a portal-authorized PipeWire FD. Inspect GPU buffer metadata;
 * never map pixels, convert on CPU, write a screenshot, or connect to RDP. */
#include <gst/gst.h>
#include <gst/app/gstappsink.h>
#include <gst/allocators/gstdmabuf.h>
#include <gst/video/video.h>
#include <gst/video/video-info-dma.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int number(const char *text, unsigned long maximum, unsigned long *out)
{
    char *end;
    errno = 0;
    unsigned long value = strtoul(text, &end, 10);
    if (errno || !*text || *end || text[0] == '-' || value > maximum) return 0;
    *out = value;
    return 1;
}
int main(int argc, char **argv)
{
    unsigned long fd, node;
    int capture_check = argc == 4 && strcmp(argv[3], "--capture-check") == 0;
    if ((argc != 3 && !capture_check) || !number(argv[1], INT_MAX, &fd) || !number(argv[2], UINT_MAX, &node) ||
        fd < 3 || !node || fcntl((int)fd, F_GETFD) == -1) {
        fprintf(stderr, "usage: uu-pipewire-dmabuf-probe PORTAL_FD NODE_ID\n"); return 2;
    }
    gst_init(NULL, NULL);
    int result = 1;
    GstElement *pipeline = gst_pipeline_new(NULL);
    GstElement *source = gst_element_factory_make("pipewiresrc", NULL);
    GstElement *sink = gst_element_factory_make("appsink", NULL);
    if (!pipeline || !source || !sink) {
        if (pipeline) gst_object_unref(pipeline);
        if (source) gst_object_unref(source);
        if (sink) gst_object_unref(sink);
        fprintf(stderr, "Required GStreamer elements are unavailable\n"); return 1;
    }
    g_object_set(source, "fd", (int)fd, "path", argv[2], "always-copy", FALSE, "do-timestamp", TRUE, NULL);
    /* Explicit diagnostic mode only; never an automatic GPU-path fallback. */
    GstCaps *caps = gst_caps_from_string(capture_check ? "video/x-raw" : "video/x-raw(memory:DMABuf)");
    g_object_set(sink, "caps", caps, "sync", FALSE, "max-buffers", 2u, "drop", TRUE,
                 "enable-last-sample", FALSE, NULL);
    gst_caps_unref(caps);
    gst_bin_add_many(GST_BIN(pipeline), source, sink, NULL);
    GstBus *bus = gst_element_get_bus(pipeline);
    if (!gst_element_link(source, sink)) {
        fprintf(stderr, "PipeWire source cannot link with requested %s caps\n",
                capture_check ? "raw capture diagnostic" : "DMA-BUF-only");
        goto done;
    }
    if (gst_element_set_state(pipeline, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
        fprintf(stderr, "PipeWire pipeline failed to enter PLAYING\n");
        goto done;
    }
    unsigned frames = 0, width = 0, height = 0, memories = 0;
    guint32 fourcc = 0;
    guint64 modifier = 0;
    gint64 start = g_get_monotonic_time();
    while (g_get_monotonic_time() - start < 8000000) {
        GstMessage *message = gst_bus_pop_filtered(bus, GST_MESSAGE_ERROR | GST_MESSAGE_EOS);
        if (message) {
            if (GST_MESSAGE_TYPE(message) == GST_MESSAGE_ERROR) {
                GError *error = NULL;
                gst_message_parse_error(message, &error, NULL);
                fprintf(stderr, "PipeWire capture: %s\n", error ? error->message : "unknown error");
                g_clear_error(&error);
            }
            gst_message_unref(message);
            goto done;
        }
        GstSample *sample = gst_app_sink_try_pull_sample(GST_APP_SINK(sink), GST_SECOND / 4);
        if (!sample) continue;
        GstBuffer *buffer = gst_sample_get_buffer(sample);
        GstCaps *negotiated = gst_sample_get_caps(sample);
        if (capture_check) {
            GstVideoInfo raw;
            gst_video_info_init(&raw);
            if (!buffer || !negotiated || !gst_video_info_from_caps(&raw, negotiated)) {
                fprintf(stderr, "Invalid raw capture diagnostic frame\n");
                gst_sample_unref(sample);
                goto done;
            }
            width = GST_VIDEO_INFO_WIDTH(&raw); height = GST_VIDEO_INFO_HEIGHT(&raw);
            frames++;
            gst_sample_unref(sample);
            continue;
        }
        GstVideoInfoDmaDrm info;
        gst_video_info_dma_drm_init(&info);
        gboolean valid = buffer && negotiated &&
            gst_caps_features_contains(gst_caps_get_features(negotiated, 0), GST_CAPS_FEATURE_MEMORY_DMABUF) &&
            gst_video_info_dma_drm_from_caps(&info, negotiated);
        if (valid) {
            valid = GST_VIDEO_INFO_WIDTH(&info.vinfo) > 0 && GST_VIDEO_INFO_HEIGHT(&info.vinfo) > 0 &&
                GST_VIDEO_INFO_WIDTH(&info.vinfo) <= 8192 && GST_VIDEO_INFO_HEIGHT(&info.vinfo) <= 8192;
        }
        guint n = buffer ? gst_buffer_n_memory(buffer) : 0;
        if (!n || n > GST_VIDEO_MAX_PLANES) valid = FALSE;
        for (guint i = 0; valid && i < n; ++i) {
            GstMemory *memory = gst_buffer_peek_memory(buffer, i);
            valid = gst_is_dmabuf_memory(memory) && gst_dmabuf_memory_get_fd(memory) >= 0;
        }
        if (!valid) {
            fprintf(stderr, "Capture did not provide valid explicit DMA-BUF format/modifier metadata; no CPU fallback\n");
            gst_sample_unref(sample);
            goto done;
        }
        width = GST_VIDEO_INFO_WIDTH(&info.vinfo); height = GST_VIDEO_INFO_HEIGHT(&info.vinfo);
        fourcc = info.drm_fourcc; modifier = info.drm_modifier; memories = n;
        frames++;
        gst_sample_unref(sample); /* release immediately; never hold portal buffers across frames */
    }
    if (!frames) { fprintf(stderr, "No capture frames within 8 seconds\n"); goto done; }
    if (capture_check) {
        printf("{\"frames\":%u,\"width\":%u,\"height\":%u,\"capture_check_only\":true,"
               "\"dmabuf_only\":false,\"pixels_mapped\":false,\"encoded\":false}\n", frames, width, height);
        result = 0;
        goto done;
    }
    printf("{\"frames\":%u,\"width\":%u,\"height\":%u,\"memory_planes\":%u,"
           "\"drm_fourcc\":%u,\"drm_modifier\":\"0x%016" G_GINT64_MODIFIER "x\","
           "\"dmabuf_only\":true,\"pixels_mapped\":false,\"encoded\":false}\n",
           frames, width, height, memories, fourcc, modifier);
    result = 0;
done:
    gst_element_set_state(pipeline, GST_STATE_NULL);
    gst_object_unref(bus);
    gst_object_unref(pipeline);
    return result;
}
