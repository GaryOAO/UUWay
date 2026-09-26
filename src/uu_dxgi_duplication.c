#define _GNU_SOURCE
#define COBJMACROS
#include "uu_dxgi_duplication.h"
#include "uu_d3d11_capture_texture.h"
#include "native_cursor_protocol.h"
#include <errno.h>
#include <limits.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

struct duplication {
    IDXGIOutputDuplication iface;
    LONG references;
    CRITICAL_SECTION lock;
    IUnknown *parent;
    struct uurb_capture_texture *texture;
    struct uurb_gpu_message first, frame;
    DXGI_OUTDUPL_DESC description;
    int channel, pending, acquired, lost;
    LARGE_INTEGER frequency, qpc_anchor;
    int64_t monotonic_anchor;
    LONGLONG last_qpc;
    /* Pointer reported like Windows: position/visibility per frame when they
     * change, shape through GetFramePointerShape. Source: the shared cursor
     * snapshot the capture producer writes (UURB_CURSOR_STATE_PATH). */
    HANDLE cursor_file;
    int cursor_reported, cursor_visible;
    POINT cursor_position;
    uint32_t cursor_generation, cursor_shape_serial, cursor_shape_delivered;
    UINT shape_width, shape_height;
    POINT shape_hotspot;
    uint8_t *shape;
};
static struct duplication *impl(IDXGIOutputDuplication *iface) { return (void *)iface; }
static int64_t monotonic_ns(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return -1;
    return (int64_t)now.tv_sec * 1000000000 + now.tv_nsec;
}
static HRESULT receive_frame(int fd, UINT timeout, struct uurb_gpu_message *message, int *memory)
{
    int64_t start = monotonic_ns();
    if (start < 0) return E_FAIL;
    for (;;) {
        int64_t now = monotonic_ns();
        if (now < 0) return E_FAIL;
        int64_t remaining = (int64_t)timeout - (now - start) / 1000000;
        int wait = timeout == INFINITE ? 1000 : remaining > 1000 ? 1000 : remaining > 0 ? remaining : 0;
        struct pollfd descriptor = {.fd = fd, .events = POLLIN};
        int result = poll(&descriptor, 1, wait);
        if (result < 0 && errno == EINTR) continue;
        if (result < 0) return DXGI_ERROR_ACCESS_LOST;
        if (result) {
            if (!(descriptor.revents & POLLIN) || uurb_gpu_channel_receive(fd, message, memory, 0))
                return DXGI_ERROR_ACCESS_LOST;
            return S_OK;
        }
        if (timeout != INFINITE && (monotonic_ns() - start) / 1000000 >= timeout)
            return DXGI_ERROR_WAIT_TIMEOUT;
    }
}
static HRESULT lose(struct duplication *s)
{
    s->lost = 1;
    if (s->channel >= 0) { close(s->channel); s->channel = -1; }
    return DXGI_ERROR_ACCESS_LOST;
}
static BOOL cursor_read(HANDLE file, void *data, DWORD size, LONGLONG offset)
{
    LARGE_INTEGER position = {.QuadPart = offset};
    DWORD received = 0;
    return SetFilePointerEx(file, position, NULL, FILE_BEGIN) &&
           ReadFile(file, data, size, &received, NULL) && received == size;
}

/* Take one consistent seqlock snapshot; refresh the stored shape (straight
 * alpha, as DXGI colour pointers are) when its serial changes. */
static BOOL cursor_snapshot(struct duplication *s, struct uurb_cursor_header *out)
{
    if (!s->cursor_file) return FALSE;
    for (unsigned attempt = 0; attempt < 3; ++attempt) {
        struct uurb_cursor_header first, last;
        uint8_t *pixels = NULL;
        if (!cursor_read(s->cursor_file, &first, sizeof(first), 0)) return FALSE;
        if (first.sequence & 1) continue;
        if (first.magic != UURB_CURSOR_MAGIC || first.version != UURB_CURSOR_VERSION) return FALSE;
        BOOL shaped = first.active && first.visible && first.width && first.height &&
            first.width <= UURB_CURSOR_MAX_SIDE && first.height <= UURB_CURSOR_MAX_SIDE &&
            first.hotspot_x >= 0 && first.hotspot_y >= 0 &&
            first.hotspot_x < (int32_t)first.width && first.hotspot_y < (int32_t)first.height;
        BOOL new_shape = shaped && (first.shape_serial != s->cursor_shape_serial ||
                                    first.generation != s->cursor_generation || !s->shape);
        if (new_shape) {
            DWORD bytes = first.width * first.height * 4;
            pixels = malloc(bytes);
            if (!pixels || !cursor_read(s->cursor_file, pixels, bytes, 64)) { free(pixels); return FALSE; }
        }
        if (!cursor_read(s->cursor_file, &last, sizeof(last), 0) || memcmp(&first, &last, sizeof(first))) {
            free(pixels);
            continue;
        }
        if (new_shape) {
            for (size_t index = 0; index < (size_t)first.width * first.height * 4; index += 4) {
                uint8_t alpha = pixels[index + 3];
                for (int channel = 0; channel < 3 && alpha && alpha < 255; ++channel)
                    pixels[index + channel] = (uint8_t)(pixels[index + channel] * 255u / alpha);
            }
            free(s->shape);
            s->shape = pixels;
            s->shape_width = first.width;
            s->shape_height = first.height;
            s->shape_hotspot = (POINT){first.hotspot_x, first.hotspot_y};
            s->cursor_shape_serial = first.shape_serial;
            s->cursor_generation = first.generation;
        }
        *out = first;
        return TRUE;
    }
    return FALSE;
}

/* Fill the pointer fields of one frame's info, leaving them zero (unchanged)
 * when nothing moved, as DXGI does. */
static void report_pointer(struct duplication *s, DXGI_OUTDUPL_FRAME_INFO *info, LONGLONG qpc)
{
    struct uurb_cursor_header header;
    /* reserved[0] names the video policy; a pointer already drawn into the
     * video (embedded or composited) must not get a second, UU-drawn one. */
    if (!cursor_snapshot(s, &header) || header.reserved[0]) return;
    /* Apps hide the pointer while typing (Ghostty, GTK text fields), and a
     * phone viewer then loses it until it taps. Keep showing the last shape
     * wherever the desktop hides it; the video never contains a pointer. */
    int visible = header.active && s->shape;
    POINT position = {header.x - s->shape_hotspot.x, header.y - s->shape_hotspot.y};
    int new_shape = visible && s->cursor_shape_delivered != s->cursor_shape_serial;
    if (s->cursor_reported && visible == s->cursor_visible && !new_shape &&
        (!visible || (position.x == s->cursor_position.x && position.y == s->cursor_position.y)))
        return;
    info->LastMouseUpdateTime.QuadPart = qpc;
    info->PointerPosition.Position = position;
    info->PointerPosition.Visible = visible;
    if (new_shape) info->PointerShapeBufferSize = s->shape_width * s->shape_height * 4;
    s->cursor_reported = 1;
    s->cursor_visible = visible;
    s->cursor_position = position;
}

static HRESULT STDMETHODCALLTYPE query(IDXGIOutputDuplication *iface, REFIID iid, void **out)
{
    if (!out) return E_POINTER;
    *out = NULL;
    if (!iid) return E_INVALIDARG;
    if (!IsEqualGUID(iid, &IID_IUnknown) && !IsEqualGUID(iid, &IID_IDXGIObject) &&
        !IsEqualGUID(iid, &IID_IDXGIOutputDuplication)) return E_NOINTERFACE;
    *out = iface; InterlockedIncrement(&impl(iface)->references); return S_OK;
}
static ULONG STDMETHODCALLTYPE addref(IDXGIOutputDuplication *iface)
{ return InterlockedIncrement(&impl(iface)->references); }
static ULONG STDMETHODCALLTYPE release(IDXGIOutputDuplication *iface)
{
    struct duplication *s = impl(iface);
    LONG count = InterlockedDecrement(&s->references);
    if (count) return count;
    /* Closing a held/pending lease disconnects without an ACK. The publisher
     * must stop, not reuse a source whose consumer completion is unknown. */
    if (s->channel >= 0) close(s->channel);
    if (s->cursor_file) CloseHandle(s->cursor_file);
    free(s->shape);
    uurb_capture_texture_close(s->texture);
    if (s->parent) IUnknown_Release(s->parent);
    DeleteCriticalSection(&s->lock); free(s); return 0;
}
static HRESULT STDMETHODCALLTYPE set_private(IDXGIOutputDuplication *iface, REFGUID guid, UINT bytes, const void *data)
{ (void)iface; (void)guid; (void)bytes; (void)data; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE set_interface(IDXGIOutputDuplication *iface, REFGUID guid, const IUnknown *object)
{ (void)iface; (void)guid; (void)object; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE get_private(IDXGIOutputDuplication *iface, REFGUID guid, UINT *bytes, void *data)
{ (void)iface; (void)guid; (void)bytes; (void)data; return E_NOTIMPL; }
static HRESULT STDMETHODCALLTYPE get_parent(IDXGIOutputDuplication *iface, REFIID iid, void **out)
{
    if (!out) return E_POINTER;
    *out = NULL;
    if (!iid) return E_INVALIDARG;
    return impl(iface)->parent ? IUnknown_QueryInterface(impl(iface)->parent, iid, out) : E_NOINTERFACE;
}
static void STDMETHODCALLTYPE get_desc(IDXGIOutputDuplication *iface, DXGI_OUTDUPL_DESC *out)
{ if (out) *out = impl(iface)->description; }
static HRESULT STDMETHODCALLTYPE acquire(IDXGIOutputDuplication *iface, UINT timeout,
    DXGI_OUTDUPL_FRAME_INFO *info, IDXGIResource **resource)
{
    struct duplication *s = impl(iface);
    if (!info || !resource) return E_INVALIDARG;
    *resource = NULL;
    EnterCriticalSection(&s->lock);
    HRESULT result = DXGI_ERROR_ACCESS_LOST;
    if (s->lost) goto done;
    result = DXGI_ERROR_INVALID_CALL;
    if (s->acquired) goto done;
    if (!s->pending) {
        int memory = -1;
        struct uurb_gpu_message frame;
        result = receive_frame(s->channel, timeout, &frame, &memory);
        if (result == DXGI_ERROR_WAIT_TIMEOUT) goto done;
        if (FAILED(result)) { result = lose(s); goto done; }
        int unexpected_fd = memory >= 0;
        if (unexpected_fd) close(memory);
        if (unexpected_fd || s->frame.sequence == UINT64_MAX || frame.sequence != s->frame.sequence + 1 ||
            frame.kind != UURB_GPU_FRAME || frame.width != s->first.width || frame.height != s->first.height ||
            frame.allocation_bytes != s->first.allocation_bytes || memcmp(frame.uuid, s->first.uuid, 16) ||
            frame.rate_num != s->first.rate_num || frame.rate_den != s->first.rate_den ||
            frame.max_rate_num != s->first.max_rate_num || frame.max_rate_den != s->first.max_rate_den ||
            frame.timestamp <= s->frame.timestamp || frame.ready_ns <= s->frame.ready_ns) {
            result = lose(s); goto done;
        }
        s->frame = frame;
    }
    s->pending = 0;
    /* Mutter PTS can be a FUTURE target presentation time, not when an image
     * became available. Convert the producer's GPU-ready CLOCK_MONOTONIC
     * timestamp to QPC; preserve original PTS separately for ordering. */
    int64_t available = monotonic_ns();
    if (available < 0 || s->frame.ready_ns > (uint64_t)available) { result = lose(s); goto done; }
    __int128 delta = (__int128)s->frame.ready_ns - s->monotonic_anchor;
    __int128 qpc = s->qpc_anchor.QuadPart + delta * s->frequency.QuadPart / 1000000000;
    if (qpc <= 0 || qpc > LLONG_MAX || (s->last_qpc && qpc <= s->last_qpc)) { result = lose(s); goto done; }
    result = uurb_capture_texture_update(s->texture);
    if (FAILED(result)) { result = lose(s); goto done; }
    result = ID3D11Texture2D_QueryInterface(uurb_capture_texture_get(s->texture), &IID_IDXGIResource, (void **)resource);
    if (FAILED(result)) { result = lose(s); goto done; }
    DXGI_OUTDUPL_FRAME_INFO value = {0};
    value.LastPresentTime.QuadPart = qpc;
    value.AccumulatedFrames = 1;
    value.TotalMetadataBufferSize = sizeof(RECT);
    /* With the pointer excluded from the video (metadata policy), UU draws
     * it from this pointer data, as on Windows. Full-frame dirty metadata is
     * conservative. */
    report_pointer(s, &value, qpc);
    *info = value; s->last_qpc = qpc; s->acquired = 1; result = S_OK;
done:
    LeaveCriticalSection(&s->lock); return result;
}
static HRESULT frame_state(struct duplication *s)
{ return s->lost ? DXGI_ERROR_ACCESS_LOST : s->acquired ? S_OK : DXGI_ERROR_INVALID_CALL; }
static HRESULT STDMETHODCALLTYPE dirty(IDXGIOutputDuplication *iface, UINT bytes, RECT *rects, UINT *required)
{
    struct duplication *s = impl(iface);
    if (!required || (bytes && !rects)) return E_INVALIDARG;
    EnterCriticalSection(&s->lock);
    HRESULT result = frame_state(s);
    if (SUCCEEDED(result)) {
        *required = sizeof(RECT);
        if (bytes < sizeof(RECT)) result = DXGI_ERROR_MORE_DATA;
        else *rects = (RECT){0, 0, s->first.width, s->first.height};
    }
    LeaveCriticalSection(&s->lock); return result;
}
static HRESULT STDMETHODCALLTYPE moves(IDXGIOutputDuplication *iface, UINT bytes,
    DXGI_OUTDUPL_MOVE_RECT *rects, UINT *required)
{
    struct duplication *s = impl(iface);
    if (!required || (bytes && !rects)) return E_INVALIDARG;
    EnterCriticalSection(&s->lock);
    HRESULT result = frame_state(s);
    if (SUCCEEDED(result)) *required = 0;
    LeaveCriticalSection(&s->lock); return result;
}
static HRESULT STDMETHODCALLTYPE pointer(IDXGIOutputDuplication *iface, UINT bytes, void *buffer,
    UINT *required, DXGI_OUTDUPL_POINTER_SHAPE_INFO *info)
{
    struct duplication *s = impl(iface);
    if (!required || !info || (bytes && !buffer)) return E_INVALIDARG;
    EnterCriticalSection(&s->lock);
    HRESULT result = frame_state(s);
    if (SUCCEEDED(result)) {
        memset(info, 0, sizeof(*info));
        *required = s->shape ? s->shape_width * s->shape_height * 4 : 0;
        if (s->shape && bytes < *required) result = DXGI_ERROR_MORE_DATA;
        else if (s->shape) {
            memcpy(buffer, s->shape, *required);
            info->Type = DXGI_OUTDUPL_POINTER_SHAPE_TYPE_COLOR;
            info->Width = s->shape_width;
            info->Height = s->shape_height;
            info->Pitch = s->shape_width * 4;
            info->HotSpot = s->shape_hotspot;
            s->cursor_shape_delivered = s->cursor_shape_serial;
        }
    }
    LeaveCriticalSection(&s->lock); return result;
}
static HRESULT STDMETHODCALLTYPE map_surface(IDXGIOutputDuplication *iface, DXGI_MAPPED_RECT *out)
{
    if (!out) return E_INVALIDARG;
    struct duplication *s = impl(iface);
    EnterCriticalSection(&s->lock);
    HRESULT result = frame_state(s);
    if (SUCCEEDED(result)) result = DXGI_ERROR_UNSUPPORTED;
    LeaveCriticalSection(&s->lock); return result;
}
static HRESULT STDMETHODCALLTYPE unmap(IDXGIOutputDuplication *iface)
{
    struct duplication *s = impl(iface);
    EnterCriticalSection(&s->lock);
    HRESULT result = s->lost ? DXGI_ERROR_ACCESS_LOST : DXGI_ERROR_INVALID_CALL;
    LeaveCriticalSection(&s->lock); return result;
}
static HRESULT STDMETHODCALLTYPE release_frame(IDXGIOutputDuplication *iface)
{
    struct duplication *s = impl(iface);
    EnterCriticalSection(&s->lock);
    HRESULT result = frame_state(s);
    if (SUCCEEDED(result)) {
        struct uurb_gpu_message ack = {.magic = UURB_GPU_CHANNEL_MAGIC, .version = UURB_GPU_CHANNEL_VERSION,
            .kind = UURB_GPU_ACK, .sequence = s->frame.sequence};
        /* The source copy finished synchronously in Acquire. The next update
         * fences prior D3D11 commands before writing the local texture again. */
        if (uurb_gpu_channel_send(s->channel, &ack, -1, 2000)) result = lose(s);
        s->acquired = 0;
    }
    LeaveCriticalSection(&s->lock); return result;
}
static IDXGIOutputDuplicationVtbl vtable = {
    query, addref, release, set_private, set_interface, get_private, get_parent,
    get_desc, acquire, dirty, moves, pointer, map_surface, unmap, release_frame
};
HRESULT WINAPI UurbCreateDuplication(ID3D11Device *device, int channel, IUnknown *parent, IDXGIOutputDuplication **out)
{
    HRESULT result = E_INVALIDARG;
    struct duplication *s = NULL;
    int memory = -1;
    if (!device || !out || channel < 3 || uurb_gpu_channel_check(channel)) goto done;
    fprintf(stderr, "DXGI backend: private channel validated\n");
    s = calloc(1, sizeof(*s));
    if (!s) { result = E_OUTOFMEMORY; goto done; }
    s->iface.lpVtbl = &vtable; s->references = 1; s->channel = channel; channel = -1;
    InitializeCriticalSection(&s->lock);
    {
        WCHAR path[MAX_PATH];
        DWORD length = GetEnvironmentVariableW(L"UURB_CURSOR_STATE_PATH", path, MAX_PATH);
        if (length && length < MAX_PATH) {
            HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                                      NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
            if (file != INVALID_HANDLE_VALUE) s->cursor_file = file;
        }
    }
    result = receive_frame(s->channel, 10000, &s->first, &memory);
    if (FAILED(result)) goto done;
    fprintf(stderr, "DXGI backend: initial GPU descriptor received\n");
    if (s->first.kind != UURB_GPU_FRAME || s->first.sequence != 1 || memory < 0) { result = E_INVALIDARG; goto done; }
    s->monotonic_anchor = monotonic_ns();
    if (s->monotonic_anchor < 0 || !QueryPerformanceCounter(&s->qpc_anchor) ||
        !QueryPerformanceFrequency(&s->frequency) || s->frequency.QuadPart <= 0) { result = E_FAIL; goto done; }
    int owned = memory; memory = -1;
    result = uurb_capture_texture_open(device, owned, &s->first, &s->texture);
    if (FAILED(result)) goto done;
    fprintf(stderr, "DXGI backend: D3D11 materializer ready\n");
    if (parent) { s->parent = parent; IUnknown_AddRef(parent); }
    s->frame = s->first; s->pending = 1;
    s->description.ModeDesc = (DXGI_MODE_DESC){.Width = s->first.width, .Height = s->first.height,
        .RefreshRate = {s->first.rate_num, s->first.rate_den ? s->first.rate_den : 1},
        .Format = DXGI_FORMAT_B8G8R8A8_UNORM,
        .ScanlineOrdering = DXGI_MODE_SCANLINE_ORDER_PROGRESSIVE, .Scaling = DXGI_MODE_SCALING_UNSPECIFIED};
    /* A variable-rate capture exposes its negotiated ceiling as the nominal
     * duplication mode. It does not imply this many frames were delivered.
     * Missing metadata remains unknown (0/1), never a fabricated 60 Hz. */
    if (!s->first.rate_num && s->first.rate_den == 1 && s->first.max_rate_num)
        s->description.ModeDesc.RefreshRate = (DXGI_RATIONAL){s->first.max_rate_num, s->first.max_rate_den};
    s->description.Rotation = DXGI_MODE_ROTATION_IDENTITY;
    *out = &s->iface; s = NULL; result = S_OK;
done:
    if (channel >= 3) close(channel);
    if (memory >= 0) close(memory);
    if (s) release(&s->iface);
    if (FAILED(result)) fprintf(stderr, "DXGI backend constructor: HRESULT 0x%08lx\n", (unsigned long)result);
    return result;
}

HRESULT WINAPI UurbCreateDuplicationEndpoint(ID3D11Device *device, const char *path,
    IUnknown *parent, IDXGIOutputDuplication **out)
{
    if (!out || !device) return E_INVALIDARG;
    *out = NULL;
    int channel = uurb_gpu_channel_connect(path);
    if (channel < 0) return DXGI_ERROR_NOT_CURRENTLY_AVAILABLE;
    return UurbCreateDuplication(device, channel, parent, out);
}
