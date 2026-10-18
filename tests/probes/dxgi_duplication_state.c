/* Real COM implementation + real Unix packets, deterministic texture double.
 * No GPU claims: the independent PE desktop probe supplies GPU acceptance. */
#define _GNU_SOURCE
#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include "uu_dxgi_duplication.h"
#include "uu_d3d11_capture_texture.h"
#include "native_cursor_protocol.h"
#include <assert.h>
#include <dirent.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#undef assert
#define assert(value) do { if (!(value)) { \
    fprintf(stderr, "check failed at line %d: %s\n", __LINE__, #value); \
    fflush(stderr); _exit(1); } } while (0)
struct uurb_capture_texture { ID3D11Texture2D iface; LONG refs; };
static int live, updates, fail_open, fail_update;
static IDXGIOutput *output_parent;
static HRESULT STDMETHODCALLTYPE texture_query(ID3D11Texture2D *iface, REFIID iid, void **out)
{
    assert(IsEqualGUID(iid, &IID_IDXGIResource)); *out = iface;
    InterlockedIncrement(&((struct uurb_capture_texture *)iface)->refs); return S_OK;
}
static ULONG STDMETHODCALLTYPE texture_addref(ID3D11Texture2D *iface)
{ return InterlockedIncrement(&((struct uurb_capture_texture *)iface)->refs); }
static ULONG STDMETHODCALLTYPE texture_release(ID3D11Texture2D *iface)
{
    LONG refs = InterlockedDecrement(&((struct uurb_capture_texture *)iface)->refs);
    if (!refs) { free(iface); live--; }
    return refs;
}
static ID3D11Texture2DVtbl texture_vtable = {
    .QueryInterface = texture_query, .AddRef = texture_addref, .Release = texture_release
};
HRESULT uurb_capture_texture_open(ID3D11Device *device, int fd, const struct uurb_gpu_message *frame,
                                  struct uurb_capture_texture **out)
{
    assert(device && uurb_gpu_message_valid(frame, 1) && fd >= 0); assert(!close(fd));
    if (fail_open) return E_FAIL;
    *out = calloc(1, sizeof(**out)); assert(*out);
    (*out)->iface.lpVtbl = &texture_vtable; (*out)->refs = 1; live++; return S_OK;
}
HRESULT uurb_capture_texture_update(struct uurb_capture_texture *texture)
{ assert(texture); updates++; return fail_update ? E_FAIL : S_OK; }
ID3D11Texture2D *uurb_capture_texture_get(struct uurb_capture_texture *texture)
{ return &texture->iface; }
void uurb_capture_texture_close(struct uurb_capture_texture *texture)
{ if (texture) texture_release(&texture->iface); }
static uint64_t now_ns(void)
{
    struct timespec now; assert(!clock_gettime(CLOCK_MONOTONIC, &now));
    return (uint64_t)now.tv_sec * 1000000000 + now.tv_nsec;
}
static int fd_count(void)
{
    DIR *directory = opendir("/proc/self/fd"); assert(directory);
    int count = 0;
    while (readdir(directory)) count++;
    closedir(directory); return count;
}
static struct uurb_gpu_message frame(uint64_t sequence, uint64_t timestamp)
{
    return (struct uurb_gpu_message){.magic = UURB_GPU_CHANNEL_MAGIC, .version = UURB_GPU_CHANNEL_VERSION,
        .kind = UURB_GPU_FRAME, .width = 640, .height = 360, .sequence = sequence,
        .timestamp = timestamp, .allocation_bytes = 1048576, .uuid = {1}, .ready_ns = now_ns() - 1000000};
}
static IDXGIOutputDuplication *create_pair(int *producer, int *consumed, struct uurb_gpu_message *first, const char *mode)
{
    int sockets[2]; assert(!socketpair(AF_UNIX, SOCK_SEQPACKET, 0, sockets));
    int allocation = open("/dev/null", O_RDONLY); assert(allocation >= 0);
    *first = frame(1, now_ns() - 1000000);
    if (!strcmp(mode, "future-pts")) first->timestamp += 1000000000;
    if (!strcmp(mode, "rate-17")) { first->rate_num = 17; first->rate_den = 1; }
    if (!strcmp(mode, "rate-fraction")) { first->rate_num = 60000; first->rate_den = 1001; }
    if (!strcmp(mode, "rate-variable")) {
        first->rate_den = 1; first->max_rate_num = 60000; first->max_rate_den = 1001;
    }
    if (!strcmp(mode, "rate-absent-max")) { first->max_rate_num = 120; first->max_rate_den = 1; }
    assert(!uurb_gpu_channel_send(sockets[0], first, allocation, 100)); close(allocation);
    IDXGIOutputDuplication *capture = NULL;
    HRESULT result = UurbCreateDuplication((void *)(uintptr_t)1, sockets[1], (IUnknown *)output_parent, &capture);
    if (fail_open) assert(FAILED(result) && !capture);
    else assert(result == S_OK && capture);
    *producer = sockets[0]; *consumed = sockets[1]; return capture;
}
static void ack(int producer, uint64_t sequence)
{
    struct uurb_gpu_message message; int fd = -1;
    assert(!uurb_gpu_channel_receive(producer, &message, &fd, 100));
    assert(fd < 0 && message.kind == UURB_GPU_ACK && message.sequence == sequence);
}
static void no_ack(int producer)
{
    struct pollfd descriptor = {.fd = producer, .events = POLLIN};
    assert(poll(&descriptor, 1, 0) == 0);
}
static void exercise(const char *mode)
{
    int baseline = fd_count(), producer, consumed;
    fail_open = !strcmp(mode, "open-failure"); fail_update = !strcmp(mode, "update-failure");
    struct uurb_gpu_message first;
    int future_pts = !strcmp(mode, "future-pts");
    IDXGIOutputDuplication *capture = create_pair(&producer, &consumed, &first, mode);
    if (fail_open) {
        assert(fcntl(consumed, F_GETFD) < 0); close(producer); assert(!live && fd_count() == baseline);
        printf("STATE %s passed\n", mode); return;
    }
    DXGI_OUTDUPL_DESC desc;
    IDXGIOutputDuplication_GetDesc(capture, &desc);
    assert(desc.ModeDesc.Width == 640 && desc.ModeDesc.Height == 360 && !desc.DesktopImageInSystemMemory);
    unsigned expected_num = !strcmp(mode, "rate-17") ? 17 :
        (!strcmp(mode, "rate-fraction") || !strcmp(mode, "rate-variable")) ? 60000 : 0;
    unsigned expected_den = expected_num == 60000 ? 1001 : 1;
    assert(desc.ModeDesc.RefreshRate.Numerator == expected_num && desc.ModeDesc.RefreshRate.Denominator == expected_den);
    assert(IDXGIOutputDuplication_SetPrivateData(capture, &IID_IUnknown, 0, NULL) == E_NOTIMPL);
    assert(IDXGIOutputDuplication_SetPrivateDataInterface(capture, &IID_IUnknown, NULL) == E_NOTIMPL);
    assert(IDXGIOutputDuplication_GetPrivateData(capture, &IID_IUnknown, NULL, NULL) == E_NOTIMPL);
    DXGI_OUTDUPL_FRAME_INFO info, untouched;
    memset(&info, 0x55, sizeof(info)); untouched = info;
    IDXGIResource *resource = NULL;
    UINT required = 123;
    RECT rect;
    assert(IDXGIOutputDuplication_GetFrameDirtyRects(capture, sizeof(rect), &rect, &required) == DXGI_ERROR_INVALID_CALL);
    assert(required == 123 && IDXGIOutputDuplication_ReleaseFrame(capture) == DXGI_ERROR_INVALID_CALL);
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, NULL, &resource) == E_INVALIDARG);
    no_ack(producer);
    if (!strcmp(mode, "pending-close")) goto done;
    HRESULT result = IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &info, &resource);
    if (fail_update) {
        assert(result == DXGI_ERROR_ACCESS_LOST && !resource && !memcmp(&info, &untouched, sizeof(info))); goto done;
    }
    assert(result == S_OK && resource && info.LastPresentTime.QuadPart > 0);
    LARGE_INTEGER qpc; assert(QueryPerformanceCounter(&qpc) && info.LastPresentTime.QuadPart <= qpc.QuadPart);
    no_ack(producer);
    IDXGIResource *unexpected = (void *)(uintptr_t)1;
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &info, &unexpected) == DXGI_ERROR_INVALID_CALL && !unexpected);
    assert(IDXGIOutputDuplication_GetFrameDirtyRects(capture, 0, NULL, &required) == DXGI_ERROR_MORE_DATA && required == sizeof(RECT));
    if (!strcmp(mode, "held-close")) goto done;
    assert(IDXGIOutputDuplication_ReleaseFrame(capture) == S_OK); ack(producer, 1);
    assert(IDXGIOutputDuplication_ReleaseFrame(capture) == DXGI_ERROR_INVALID_CALL);
    IDXGIResource_Release(resource); resource = NULL;
    untouched = info;
    uint64_t started = now_ns();
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 20, &info, &resource) == DXGI_ERROR_WAIT_TIMEOUT);
    assert(now_ns() - started >= 15000000 && now_ns() - started < 1000000000);
    assert(!resource && !memcmp(&info, &untouched, sizeof(info))); no_ack(producer);
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &info, &resource) == DXGI_ERROR_WAIT_TIMEOUT);
    struct uurb_gpu_message next = frame(2, now_ns() - 1000000);
    next.rate_num = first.rate_num; next.rate_den = first.rate_den;
    next.max_rate_num = first.max_rate_num; next.max_rate_den = first.max_rate_den;
    if (!strcmp(mode, "rate-change")) { next.rate_num = 17; next.rate_den = 1; }
    if (!strcmp(mode, "max-rate-change")) { next.rate_den = 1; next.max_rate_num = 17; next.max_rate_den = 1; }
    if (future_pts) next.timestamp += 1000000000;
    if (!strcmp(mode, "geometry")) next.width = 642;
    if (!strcmp(mode, "uuid")) next.uuid[0] = 2;
    if (!strcmp(mode, "allocation")) next.allocation_bytes *= 2;
    if (!strcmp(mode, "sequence")) next.sequence = 3;
    if (!strcmp(mode, "timestamp")) next.timestamp = first.timestamp;
    if (!strcmp(mode, "ready-time")) next.ready_ns = first.ready_ns;
    if (!strcmp(mode, "future-ready")) next.ready_ns = now_ns() + 1000000000;
    if (!strcmp(mode, "end")) next = (struct uurb_gpu_message){.magic = UURB_GPU_CHANNEL_MAGIC,
        .version = UURB_GPU_CHANNEL_VERSION, .kind = UURB_GPU_END, .sequence = 2};
    if (!strcmp(mode, "disconnect")) { close(producer); producer = -1; }
    else if (!strcmp(mode, "malformed")) assert(send(producer, "bad", 3, 0) == 3);
    else assert(!uurb_gpu_channel_send(producer, &next, -1, 100));
    result = IDXGIOutputDuplication_AcquireNextFrame(capture, 100, &info, &resource);
    if (!strcmp(mode, "success") || future_pts || !strcmp(mode, "rate-17") ||
        !strcmp(mode, "rate-fraction") || !strcmp(mode, "rate-variable") || !strcmp(mode, "rate-absent-max")) {
        assert(result == S_OK && resource && info.LastPresentTime.QuadPart > untouched.LastPresentTime.QuadPart);
        assert(QueryPerformanceCounter(&qpc) && info.LastPresentTime.QuadPart <= qpc.QuadPart);
        no_ack(producer); assert(IDXGIOutputDuplication_ReleaseFrame(capture) == S_OK); ack(producer, 2);
    } else {
        assert(result == DXGI_ERROR_ACCESS_LOST && !resource);
        assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &info, &resource) == DXGI_ERROR_ACCESS_LOST);
        assert(IDXGIOutputDuplication_ReleaseFrame(capture) == DXGI_ERROR_ACCESS_LOST);
        assert(IDXGIOutputDuplication_GetFrameDirtyRects(capture, 0, NULL, &required) == DXGI_ERROR_ACCESS_LOST);
    }
done:
    assert(IDXGIOutputDuplication_Release(capture) == 0);
    if (resource) { assert(live == 1); IDXGIResource_Release(resource); }
    if (producer >= 0) {
        char remaining[128];
        assert(recv(producer, remaining, sizeof(remaining), MSG_DONTWAIT) == 0);
        close(producer);
    }
    assert(!live && fcntl(consumed, F_GETFD) < 0 && fd_count() == baseline);
    printf("STATE %s passed\n", mode);
}
struct test_output { IDXGIOutput iface; LONG refs; unsigned index; };
static HRESULT STDMETHODCALLTYPE output_query(IDXGIOutput *iface, REFIID iid, void **out)
{
    assert(IsEqualGUID(iid, &IID_IDXGIOutput));
    *out = iface; ++((struct test_output *)iface)->refs; return S_OK;
}
static ULONG STDMETHODCALLTYPE output_addref(IDXGIOutput *iface)
{ return ++((struct test_output *)iface)->refs; }
static ULONG STDMETHODCALLTYPE output_release(IDXGIOutput *iface)
{ return --((struct test_output *)iface)->refs; }
static HRESULT STDMETHODCALLTYPE output_desc(IDXGIOutput *iface, DXGI_OUTPUT_DESC *out)
{
    unsigned index = ((struct test_output *)iface)->index;
    memset(out, 0, sizeof(*out));
    static const WCHAR name[] = L"\\\\.\\DISPLAY1";
    memcpy(out->DeviceName, name, sizeof(name));
    out->DeviceName[ARRAYSIZE(name) - 2] += index;
    out->DesktopCoordinates = (RECT){index * 640, 0, (index + 1) * 640, 360};
    return S_OK;
}
static IDXGIOutputVtbl output_vtable = {
    .QueryInterface = output_query, .AddRef = output_addref, .Release = output_release, .GetDesc = output_desc
};
static void write_cursor_snapshot(HANDLE file, unsigned serial, unsigned generation,
                                  unsigned shape_serial, int x, int visible, uint8_t color)
{
    struct uurb_cursor_header header = {.magic = UURB_CURSOR_MAGIC, .version = UURB_CURSOR_VERSION,
        .sequence = serial * 2 + 2, .generation = generation, .active = 1, .visible = visible,
        .shape_serial = shape_serial, .width = 2, .height = 2, .hotspot_x = 1, .hotspot_y = 1, .x = x, .y = 99};
    uint8_t pixels[16]; memset(pixels, 255, sizeof(pixels));
    for (unsigned i = 0; i < sizeof(pixels); i += 4) pixels[i] = color;
    DWORD written;
    LARGE_INTEGER start = {.QuadPart = 0};
    assert(SetFilePointerEx(file, start, NULL, FILE_BEGIN));
    assert(WriteFile(file, &header, sizeof(header), &written, NULL) && written == sizeof(header));
    assert(WriteFile(file, pixels, sizeof(pixels), &written, NULL) && written == sizeof(pixels));
}
static void write_cursor(HANDLE file, unsigned serial, int x, int visible)
{
    write_cursor_snapshot(file, serial, 1, 1, x, visible, 255);
}
static void exercise_cursor(unsigned index)
{
    int producer, consumed;
    struct test_output output = {{&output_vtable}, 1, index};
    output_parent = &output.iface;
    fail_open = fail_update = 0;
    HANDLE files[3];
    const WCHAR *names[] = {L"cursor.state", L"cursor.0.state", L"cursor.1.state"};
    for (unsigned i = 0; i < 3; ++i) {
        files[i] = CreateFileW(names[i], GENERIC_READ | GENERIC_WRITE,
                              FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                              NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        assert(files[i] != INVALID_HANDLE_VALUE);
        write_cursor(files[i], 1, i == index + 1 ? 124 : 42, 1);
    }
    assert(SetEnvironmentVariableW(L"UURB_CURSOR_STATE_PATH", L"cursor.state"));
    assert(SetEnvironmentVariableW(L"UURB_CURSOR_STATE_DIR", L"."));
    struct uurb_gpu_message first;
    IDXGIOutputDuplication *capture = create_pair(&producer, &consumed, &first, "success");
    DXGI_OUTDUPL_FRAME_INFO info;
    IDXGIResource *resource = NULL;
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &info, &resource) == S_OK);
    assert(info.PointerPosition.Visible && info.PointerPosition.Position.x == 123 &&
           info.PointerPosition.Position.y == 98 && info.PointerShapeBufferSize == 16);
    DXGI_OUTDUPL_POINTER_SHAPE_INFO shape;
    UINT required = 0;
    uint8_t pixels[16];
    assert(IDXGIOutputDuplication_GetFramePointerShape(capture, 0, NULL, &required, &shape) == DXGI_ERROR_MORE_DATA);
    assert(required == sizeof(pixels));
    assert(IDXGIOutputDuplication_GetFramePointerShape(capture, sizeof(pixels), pixels, &required, &shape) == S_OK);
    assert(shape.Width == 2 && shape.Height == 2 && shape.HotSpot.x == 1 && pixels[3] == 255);
    IDXGIResource_Release(resource);
    assert(IDXGIOutputDuplication_ReleaseFrame(capture) == S_OK); ack(producer, 1);
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &info, &resource) == DXGI_ERROR_WAIT_TIMEOUT);
    int before = updates;
    write_cursor(files[index + 1], 2, 125, 1);
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &info, &resource) == S_OK);
    assert(info.LastMouseUpdateTime.QuadPart && !info.LastPresentTime.QuadPart && !info.AccumulatedFrames);
    assert(!info.TotalMetadataBufferSize && !info.PointerShapeBufferSize && info.PointerPosition.Position.x == 124);
    assert(updates == before);
    IDXGIResource_Release(resource);
    assert(IDXGIOutputDuplication_ReleaseFrame(capture) == S_OK); no_ack(producer);

    /* A pointer leaving this monitor must hide its old sprite. */
    write_cursor(files[index + 1], 3, 125, 0);
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &info, &resource) == S_OK);
    assert(info.LastMouseUpdateTime.QuadPart && !info.PointerPosition.Visible);
    IDXGIResource_Release(resource);
    assert(IDXGIOutputDuplication_ReleaseFrame(capture) == S_OK); no_ack(producer);
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &info, &resource) == DXGI_ERROR_WAIT_TIMEOUT);

    /* A restarted/hotplugged producer can reuse shape serial 1. Both the
     * physical and secondary/virtual output must publish its new image. */
    write_cursor_snapshot(files[index + 1], 4, 2, 1, 125, 1, 128);
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &info, &resource) == S_OK);
    assert(info.PointerPosition.Visible && info.PointerShapeBufferSize == sizeof(pixels));
    assert(!info.LastPresentTime.QuadPart && info.LastMouseUpdateTime.QuadPart);
    assert(IDXGIOutputDuplication_GetFramePointerShape(capture, sizeof(pixels), pixels, &required, &shape) == S_OK);
    assert(pixels[0] == 128 && shape.HotSpot.x == 1);
    IDXGIResource_Release(resource);
    assert(IDXGIOutputDuplication_ReleaseFrame(capture) == S_OK); no_ack(producer);
    /* A shape-only change on a static desktop must also reach UU. */
    write_cursor_snapshot(files[index + 1], 5, 2, 2, 125, 1, 64);
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &info, &resource) == S_OK);
    assert(info.PointerPosition.Visible && info.PointerShapeBufferSize == sizeof(pixels));
    assert(IDXGIOutputDuplication_GetFramePointerShape(capture, sizeof(pixels), pixels, &required, &shape) == S_OK);
    assert(pixels[0] == 64);
    IDXGIResource_Release(resource);
    assert(IDXGIOutputDuplication_ReleaseFrame(capture) == S_OK); no_ack(producer);
    assert(updates == before);

    struct uurb_gpu_message next = frame(2, now_ns());
    assert(!uurb_gpu_channel_send(producer, &next, -1, 100));
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 100, &info, &resource) == S_OK);
    assert(info.LastPresentTime.QuadPart && info.AccumulatedFrames == 1);
    IDXGIResource_Release(resource);
    assert(IDXGIOutputDuplication_ReleaseFrame(capture) == S_OK); ack(producer, 2);
    assert(IDXGIOutputDuplication_Release(capture) == 0 && output.refs == 1 && !live);
    close(producer);
    for (unsigned i = 0; i < 3; ++i) { CloseHandle(files[i]); assert(DeleteFileW(names[i])); }
    SetEnvironmentVariableW(L"UURB_CURSOR_STATE_DIR", NULL);
    SetEnvironmentVariableW(L"UURB_CURSOR_STATE_PATH", NULL);
    output_parent = NULL;
    printf("STATE cursor-output-%u passed\n", index);
}

static void exercise_legacy_cursor_policy(void)
{
    int producer, consumed;
    struct uurb_gpu_message first;
    HANDLE file = CreateFileW(L"client-cursor.state", GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    assert(file != INVALID_HANDLE_VALUE);
    write_cursor(file, 1, 124, 1);
    assert(SetEnvironmentVariableW(L"UURB_CURSOR_STATE_PATH", L"client-cursor.state"));
    assert(SetEnvironmentVariableW(L"UURB_CURSOR_PRESENTATION", L"client"));
    IDXGIOutputDuplication *capture = create_pair(&producer, &consumed, &first, "success");
    DXGI_OUTDUPL_FRAME_INFO info;
    IDXGIResource *resource = NULL;
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &info, &resource) == S_OK);
    assert(info.LastMouseUpdateTime.QuadPart && info.PointerPosition.Visible && info.PointerShapeBufferSize == 16);
    UINT required = 99;
    DXGI_OUTDUPL_POINTER_SHAPE_INFO shape;
    uint8_t pixels[16]; memset(pixels, 255, sizeof(pixels));
    assert(IDXGIOutputDuplication_GetFramePointerShape(capture, 0, NULL, &required, &shape) == DXGI_ERROR_MORE_DATA);
    assert(required == sizeof(pixels));
    assert(IDXGIOutputDuplication_GetFramePointerShape(capture, sizeof(pixels), pixels, &required, &shape) == S_OK);
    assert(shape.Type == DXGI_OUTDUPL_POINTER_SHAPE_TYPE_COLOR && shape.Width == 2 && shape.Height == 2);
    assert(shape.HotSpot.x == 1 && shape.HotSpot.y == 1 && shape.Pitch == 8);
    for (unsigned i = 0; i < sizeof(pixels); ++i) assert(pixels[i] == 255);
    /* Legacy overrides must not hide either public cursor API. */
    DWORD bytes; LARGE_INTEGER start = {.QuadPart = 64};
    assert(SetFilePointerEx(file, start, NULL, FILE_BEGIN));
    assert(ReadFile(file, pixels, sizeof(pixels), &bytes, NULL) && bytes == sizeof(pixels));
    for (unsigned i = 0; i < sizeof(pixels); ++i) assert(pixels[i] == 255);
    IDXGIResource_Release(resource);
    assert(IDXGIOutputDuplication_ReleaseFrame(capture) == S_OK); ack(producer, 1);
    write_cursor(file, 2, 125, 1);
    /* Static desktop movement retains actual visibility and coordinates,
     * without a new desktop texture or a false GPU producer ACK. */
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &info, &resource) == S_OK);
    assert(info.LastMouseUpdateTime.QuadPart && !info.LastPresentTime.QuadPart && !info.AccumulatedFrames);
    assert(info.PointerPosition.Visible && !info.PointerShapeBufferSize && !info.TotalMetadataBufferSize);
    assert(info.PointerPosition.Position.x == 124);
    IDXGIResource_Release(resource);
    assert(IDXGIOutputDuplication_ReleaseFrame(capture) == S_OK);
    no_ack(producer);
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &info, &resource) == DXGI_ERROR_WAIT_TIMEOUT);
    write_cursor(file, 3, 125, 0);
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &info, &resource) == S_OK);
    assert(info.LastMouseUpdateTime.QuadPart && !info.PointerPosition.Visible);
    IDXGIResource_Release(resource);
    assert(IDXGIOutputDuplication_ReleaseFrame(capture) == S_OK); no_ack(producer);
    write_cursor_snapshot(file, 4, 2, 1, 125, 1, 128);
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &info, &resource) == S_OK);
    assert(info.PointerPosition.Visible && info.PointerShapeBufferSize == sizeof(pixels));
    assert(IDXGIOutputDuplication_GetFramePointerShape(capture, sizeof(pixels), pixels, &required, &shape) == S_OK);
    for (unsigned i = 0; i < sizeof(pixels); ++i) assert(pixels[i] == (i % 4 ? 255 : 128));
    IDXGIResource_Release(resource);
    assert(IDXGIOutputDuplication_ReleaseFrame(capture) == S_OK); no_ack(producer);
    assert(IDXGIOutputDuplication_Release(capture) == 0 && !live);
    close(producer); CloseHandle(file); assert(DeleteFileW(L"client-cursor.state"));
    SetEnvironmentVariableW(L"UURB_CURSOR_PRESENTATION", NULL);
    SetEnvironmentVariableW(L"UURB_CURSOR_STATE_PATH", NULL);
    puts("STATE legacy-cursor-policy-ignored passed");
}
struct late_source { int channel, allocation; struct uurb_gpu_message first; };
static DWORD WINAPI publish_late_cursor(void *argument)
{
    struct late_source *source = argument;
    Sleep(100);
    HANDLE file = CreateFileW(L"cursor.2.state", GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    assert(file != INVALID_HANDLE_VALUE);
    write_cursor(file, 1, 124, 1);
    CloseHandle(file);
    assert(!uurb_gpu_channel_send(source->channel, &source->first, source->allocation, 1000));
    close(source->allocation);
    return 0;
}
static void exercise_hotplug_cursor(void)
{
    struct test_output output = {{&output_vtable}, 1, 2};
    int sockets[2]; assert(!socketpair(AF_UNIX, SOCK_SEQPACKET, 0, sockets));
    DeleteFileW(L"cursor.2.state");
    assert(SetEnvironmentVariableW(L"UURB_CURSOR_STATE_DIR", L"."));
    struct late_source source = {.channel = sockets[0], .allocation = open("/dev/null", O_RDONLY),
                                 .first = frame(1, now_ns())};
    assert(source.allocation >= 0);
    HANDLE worker = CreateThread(NULL, 0, publish_late_cursor, &source, 0, NULL);
    assert(worker);
    IDXGIOutputDuplication *capture = NULL;
    assert(UurbCreateDuplication((void *)(uintptr_t)1, sockets[1], (IUnknown *)&output.iface, &capture) == S_OK);
    assert(WaitForSingleObject(worker, 2000) == WAIT_OBJECT_0); CloseHandle(worker);
    DXGI_OUTDUPL_FRAME_INFO info; IDXGIResource *resource = NULL;
    assert(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &info, &resource) == S_OK);
    assert(info.PointerPosition.Visible && info.PointerShapeBufferSize == 16 && info.PointerPosition.Position.x == 123);
    IDXGIResource_Release(resource);
    assert(IDXGIOutputDuplication_ReleaseFrame(capture) == S_OK); ack(sockets[0], 1);
    assert(IDXGIOutputDuplication_Release(capture) == 0 && output.refs == 1 && !live);
    close(sockets[0]); assert(DeleteFileW(L"cursor.2.state"));
    SetEnvironmentVariableW(L"UURB_CURSOR_STATE_DIR", NULL);
    puts("STATE hotplug-cursor passed");
}
int main(void)
{
    const char *cases[] = {"success", "future-pts", "pending-close", "held-close", "open-failure", "update-failure",
        "rate-17", "rate-fraction", "rate-variable", "rate-absent-max", "rate-change", "max-rate-change",
        "geometry", "uuid", "allocation", "sequence", "timestamp", "ready-time", "future-ready", "end", "disconnect", "malformed"};
    for (unsigned repeat = 0; repeat < 3; ++repeat)
        for (unsigned i = 0; i < sizeof(cases) / sizeof(*cases); ++i) exercise(cases[i]);
    exercise_cursor(0);
    exercise_cursor(1);
    exercise_legacy_cursor_policy();
    exercise_hotplug_cursor();
    int regular = open("/dev/null", O_RDONLY);
    IDXGIOutputDuplication *out = (void *)(uintptr_t)1;
    assert(UurbCreateDuplication((void *)(uintptr_t)1, regular, NULL, &out) == E_INVALIDARG &&
        out == (void *)(uintptr_t)1 && fcntl(regular, F_GETFD) < 0);
    puts("DXGI deterministic state checks passed; no physical GPU claims");
    return 0;
}
