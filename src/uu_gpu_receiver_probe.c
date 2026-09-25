/* Actual native Wayland GPU frames -> D3D11 texture -> Windows NVENC table.
 * Isolated Winelib consumer (not a PE client and not installed in UU). */
#define _GNU_SOURCE
#define COBJMACROS
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <time.h>
#include "uu_d3d11_capture_texture.h"
#include "native_nvenc_reference_config.h"
typedef NVENCSTATUS (NVENCAPI *create_fn)(NV_ENCODE_API_FUNCTION_LIST *);
#define CHECK(test) do { if (!(test)) { fprintf(stderr, "D3D11 receiver failed: %s line %d\n", #test, __LINE__); goto done; } } while (0)
static int number(const char *s)
{
    if (!s || *s < '0' || *s > '9') return -1;
    char *end;
    errno = 0;
    long n = strtol(s, &end, 10);
    return errno || *end || n < 3 || n > INT_MAX ? -1 : (int)n;
}
static int write_packet(int fd, const void *data, size_t size)
{
    const unsigned char *position = data;
    while (size) {
        ssize_t n = write(fd, position, size);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return 1;
        position += n; size -= n;
    }
    return 0;
}
struct timings { double sum, maximum, samples[4096]; unsigned count; };
static double monotonic_us(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) _Exit(4);
    return (double)now.tv_sec * 1000000.0 + now.tv_nsec / 1000.0;
}
static int compare_time(const void *a, const void *b)
{
    double left = *(const double *)a, right = *(const double *)b;
    return (left > right) - (left < right);
}
static void record_time(struct timings *t, double elapsed)
{
    if (t->count == sizeof(t->samples) / sizeof(t->samples[0])) _Exit(4);
    t->samples[t->count++] = elapsed; t->sum += elapsed;
    if (elapsed > t->maximum) t->maximum = elapsed;
}
static void print_times(const char *name, struct timings *t)
{
    qsort(t->samples, t->count, sizeof(double), compare_time);
    printf(",\"%s\":{\"samples\":%u,\"mean\":%.3f,\"p50\":%.3f,\"p95\":%.3f,\"p99\":%.3f,\"max\":%.3f}",
        name, t->count, t->count ? t->sum / t->count : 0,
        t->samples[t->count ? (t->count - 1) * 50 / 100 : 0],
        t->samples[t->count ? (t->count - 1) * 95 / 100 : 0],
        t->samples[t->count ? (t->count - 1) * 99 / 100 : 0], t->maximum);
}
static int argument_checks(ID3D11Device *device, int channel)
{
    struct uurb_gpu_message frame = {.magic = UURB_GPU_CHANNEL_MAGIC, .version = UURB_GPU_CHANNEL_VERSION,
        .kind = UURB_GPU_FRAME, .sequence = 1, .width = 640, .height = 360, .allocation_bytes = 1048576,
        .uuid = {1}, .ready_ns = 1234};
    struct uurb_capture_texture *sentinel = (void *)(uintptr_t)1;
    if (uurb_capture_texture_update(NULL) != E_INVALIDARG || uurb_capture_texture_get(NULL)) return 1;
    uurb_capture_texture_close(NULL);
    for (unsigned i = 0; i < 6; ++i) {
        int fd = i == 5 ? -1 : dup(channel);
        if (i != 5 && fd < 0) return 1;
        struct uurb_capture_texture *out = sentinel;
        struct uurb_gpu_message message = frame;
        if (i == 3) message.width++;
        if (i == 4) message.kind = UURB_GPU_ACK;
        HRESULT status = uurb_capture_texture_open(i == 0 ? NULL : device, fd,
            i == 1 ? NULL : &message, i == 2 ? NULL : &out);
        int consumed = fd < 0 || (fcntl(fd, F_GETFD) < 0 && errno == EBADF);
        if (!consumed) close(fd);
        if (status != E_INVALIDARG || out != sentinel || !consumed) return 1;
    }
    return 0;
}
int main(int argc, char **argv)
{
    if (argc != 4 || (strcmp(argv[3], "h264") && strcmp(argv[3], "hevc"))) return 2;
    int channel = number(argv[1]), output = number(argv[2]), hevc = !strcmp(argv[3], "hevc");
    struct stat stat;
    if (channel < 0 || output < 0 || channel == output || uurb_gpu_channel_check(channel) ||
        fstat(output, &stat) || !S_ISREG(stat.st_mode) || (fcntl(output, F_GETFL) & O_ACCMODE) == O_RDONLY) {
        fprintf(stderr, "Missing inherited private GPU/output descriptors\n"); return 2;
    }
    HMODULE module = LoadLibraryA("uurb-nvenc-encode.dll.so");
    IDXGIFactory1 *factory = NULL;
    IDXGIAdapter1 *adapter = NULL;
    ID3D11Device *device = NULL;
    ID3D11DeviceContext *context = NULL;
    struct uurb_capture_texture *texture = NULL;
    void *encoder = NULL;
    NV_ENCODE_API_FUNCTION_LIST api = {.version = NV_ENCODE_API_FUNCTION_LIST_VER};
    struct uurb_gpu_message first = {0};
    uint64_t frames = 0, timestamp = 0, written = 0;
    NV_ENC_REGISTERED_PTR registered = NULL;
    NV_ENC_OUTPUT_PTR bitstream = NULL;
    int failed = 1, memory_fd = -1;
    double initialization_us = 0;
    struct timings copy_times = {0}, encode_times = {0}, steady_times = {0};
    CHECK(module);
    create_fn create = (void *)GetProcAddress(module, "NvEncodeAPICreateInstance");
    CHECK(create && create(&api) == NV_ENC_SUCCESS);
    CHECK(SUCCEEDED(CreateDXGIFactory1(&IID_IDXGIFactory1, (void **)&factory)));
    for (UINT i = 0; IDXGIFactory1_EnumAdapters1(factory, i, &adapter) == S_OK; ++i) {
        DXGI_ADAPTER_DESC1 description;
        CHECK(SUCCEEDED(IDXGIAdapter1_GetDesc1(adapter, &description)));
        if (description.VendorId == 0x10de && !(description.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)) break;
        IDXGIAdapter1_Release(adapter); adapter = NULL;
    }
    CHECK(adapter);
    D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_11_0;
    CHECK(SUCCEEDED(D3D11CreateDevice((IDXGIAdapter *)adapter, D3D_DRIVER_TYPE_UNKNOWN, NULL,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT, &level, 1, D3D11_SDK_VERSION, &device, NULL, &context)));
    CHECK(!argument_checks(device, channel));
    puts("D3D11_RECEIVER_READY"); fflush(stdout);
    for (;;) {
        struct uurb_gpu_message message;
        CHECK(!uurb_gpu_channel_receive(channel, &message, &memory_fd, 10000));
        CHECK(message.sequence == frames + 1);
        if (message.kind == UURB_GPU_END) { CHECK(frames); failed = 0; break; }
        CHECK(message.kind == UURB_GPU_FRAME && (!frames || message.timestamp > timestamp));
        double frame_start = monotonic_us();
        if (!frames) {
            first = message;
            int fd = memory_fd; memory_fd = -1;
            CHECK(SUCCEEDED(uurb_capture_texture_open(device, fd, &message, &texture)));
            NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS open = {.version = NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS_VER,
                .deviceType = NV_ENC_DEVICE_TYPE_DIRECTX, .device = device, .apiVersion = NVENCAPI_VERSION};
            CHECK(api.nvEncOpenEncodeSessionEx(&open, &encoder) == NV_ENC_SUCCESS);
            NV_ENC_PRESET_CONFIG preset = {.version = NV_ENC_PRESET_CONFIG_VER};
            preset.presetCfg.version = NV_ENC_CONFIG_VER;
            CHECK(api.nvEncGetEncodePresetConfigEx(encoder, hevc ? NV_ENC_CODEC_HEVC_GUID : NV_ENC_CODEC_H264_GUID,
                NV_ENC_PRESET_P1_GUID, NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY, &preset) == NV_ENC_SUCCESS);
            NV_ENC_CONFIG config;
            NV_ENC_INITIALIZE_PARAMS init;
            uurb_nvenc_reference_config(&preset.presetCfg, message.width, message.height, hevc, 20000000, &config, &init);
            CHECK(api.nvEncInitializeEncoder(encoder, &init) == NV_ENC_SUCCESS);
            NV_ENC_REGISTER_RESOURCE resource = {.version = NV_ENC_REGISTER_RESOURCE_VER,
                .resourceType = NV_ENC_INPUT_RESOURCE_TYPE_DIRECTX, .width = message.width, .height = message.height,
                .resourceToRegister = uurb_capture_texture_get(texture), .bufferUsage = NV_ENC_INPUT_IMAGE,
                .bufferFormat = NV_ENC_BUFFER_FORMAT_ARGB};
            CHECK(api.nvEncRegisterResource(encoder, &resource) == NV_ENC_SUCCESS);
            registered = resource.registeredResource;
            NV_ENC_CREATE_BITSTREAM_BUFFER buffer = {.version = NV_ENC_CREATE_BITSTREAM_BUFFER_VER};
            CHECK(api.nvEncCreateBitstreamBuffer(encoder, &buffer) == NV_ENC_SUCCESS);
            bitstream = buffer.bitstreamBuffer;
            initialization_us = monotonic_us() - frame_start;
        } else CHECK(memory_fd < 0 && message.width == first.width && message.height == first.height &&
            message.allocation_bytes == first.allocation_bytes && !memcmp(message.uuid, first.uuid, 16));
        double copy_start = monotonic_us();
        CHECK(SUCCEEDED(uurb_capture_texture_update(texture)));
        double encode_start = monotonic_us();
        NV_ENC_MAP_INPUT_RESOURCE mapped = {.version = NV_ENC_MAP_INPUT_RESOURCE_VER, .registeredResource = registered};
        CHECK(api.nvEncMapInputResource(encoder, &mapped) == NV_ENC_SUCCESS);
        NV_ENC_PIC_PARAMS picture = {.version = NV_ENC_PIC_PARAMS_VER,
            .inputWidth = message.width, .inputHeight = message.height, .inputBuffer = mapped.mappedResource,
            .bufferFmt = mapped.mappedBufferFmt, .outputBitstream = bitstream,
            .inputTimeStamp = message.timestamp, .inputDuration = 1, .frameIdx = frames,
            .pictureStruct = NV_ENC_PIC_STRUCT_FRAME};
        CHECK(api.nvEncEncodePicture(encoder, &picture) == NV_ENC_SUCCESS);
        NV_ENC_LOCK_BITSTREAM locked = {.version = NV_ENC_LOCK_BITSTREAM_VER, .outputBitstream = bitstream};
        CHECK(api.nvEncLockBitstream(encoder, &locked) == NV_ENC_SUCCESS);
        CHECK(locked.outputTimeStamp == message.timestamp && locked.frameIdx == frames && locked.bitstreamSizeInBytes &&
            locked.bitstreamSizeInBytes <= 64u * 1024u * 1024u - written);
        CHECK(!write_packet(output, locked.bitstreamBufferPtr, locked.bitstreamSizeInBytes));
        written += locked.bitstreamSizeInBytes;
        CHECK(api.nvEncUnlockBitstream(encoder, bitstream) == NV_ENC_SUCCESS);
        CHECK(api.nvEncUnmapInputResource(encoder, mapped.mappedResource) == NV_ENC_SUCCESS);
        double encode_end = monotonic_us();
        record_time(&copy_times, encode_start - copy_start);
        record_time(&encode_times, encode_end - encode_start);
        if (frames) record_time(&steady_times, encode_end - frame_start);
        struct uurb_gpu_message ack = {.magic = UURB_GPU_CHANNEL_MAGIC, .version = UURB_GPU_CHANNEL_VERSION,
            .kind = UURB_GPU_ACK, .sequence = message.sequence};
        CHECK(!uurb_gpu_channel_send(channel, &ack, -1, 2000));
        timestamp = message.timestamp; frames++;
    }
done:
    if (memory_fd >= 0) close(memory_fd);
    if (encoder && api.nvEncDestroyEncoder(encoder) != NV_ENC_SUCCESS) failed = 1;
    uurb_capture_texture_close(texture);
    if (context) ID3D11DeviceContext_Release(context);
    if (device) ID3D11Device_Release(device);
    if (adapter) IDXGIAdapter1_Release(adapter);
    if (factory) IDXGIFactory1_Release(factory);
    if (module) FreeLibrary(module);
    if (!failed) {
        printf("D3D11_RECEIVER_REPORT {\"frames\":%llu,\"width\":%u,\"height\":%u,\"codec\":\"%s\",\"encoded\":true,"
        "\"gpu_channel_received\":true,\"encoded_bytes\":%llu,\"d3d11_texture_materialized\":true,"
        "\"windows_nvenc_function_table\":true,\"windows_pe_client\":false,\"uu_session_tested\":false,"
        "\"capture_boundary_argument_checks\":true,\"initialization_us\":%.3f",
        (unsigned long long)frames, first.width, first.height, hevc ? "hevc" : "h264",
        (unsigned long long)written, initialization_us);
        /* Wall-clock CPU orchestration timings include GPU fence waits, not
         * GPU timestamp queries and not remote/network latency. */
        print_times("copy_wall_us", &copy_times);
        print_times("encode_wall_us", &encode_times);
        print_times("steady_frame_wall_us", &steady_times);
        puts("}");
    }
    return failed;
}
