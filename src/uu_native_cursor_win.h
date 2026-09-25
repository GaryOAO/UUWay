#ifndef UURB_NATIVE_CURSOR_WIN_H
#define UURB_NATIVE_CURSOR_WIN_H
#include "native_cursor_protocol.h"

/* USER32 handles are real cursors: GetIconInfo needs no fake handle or patch.
 * Deduplicated handles live for the process lifetime, just like shared system
 * cursors. A finite cache refuses new shapes on exhaustion instead of freeing
 * a handle that a caller may still be using. No per-poll GDI allocations. */
struct native_cursor_entry {
    HCURSOR handle;
    uint32_t width, height;
    int32_t hx, hy;
    uint8_t *pixels;
};
static struct native_cursor_entry native_cursors[128];
static unsigned native_cursor_count;
static HANDLE native_cursor_file = INVALID_HANDLE_VALUE;
static SRWLOCK native_cursor_lock = SRWLOCK_INIT;
static struct uurb_cursor_header native_cursor_cached;
static HCURSOR native_cursor_handle;
static uint8_t native_cursor_pixels[UURB_CURSOR_PIXEL_BYTES];
static uintptr_t native_get_cursor_info, native_get_cursor_pos;
static volatile LONG native_cursor_calls, native_cursor_errors;
static volatile LONG native_cursor_import_slots;
static BOOL native_cursor_video_embedded;
static BOOL native_cursor_video_composited;

static BOOL native_cursor_read(void *data, DWORD size, LONGLONG offset)
{
    LARGE_INTEGER position = {.QuadPart = offset};
    DWORD received;
    return SetFilePointerEx(native_cursor_file, position, NULL, FILE_BEGIN) &&
        ReadFile(native_cursor_file, data, size, &received, NULL) && received == size;
}

static HCURSOR native_cursor_create(const struct uurb_cursor_header *header)
{
    size_t bytes = (size_t)header->width * header->height * 4;
    for (unsigned i = 0; i < native_cursor_count; ++i) {
        struct native_cursor_entry *entry = &native_cursors[i];
        if (entry->width == header->width && entry->height == header->height &&
            entry->hx == header->hotspot_x && entry->hy == header->hotspot_y &&
            !memcmp(entry->pixels, native_cursor_pixels, bytes)) return entry->handle;
    }
    if (native_cursor_count == ARRAYSIZE(native_cursors)) return NULL;
    BITMAPINFO info = {0};
    info.bmiHeader.biSize = sizeof(info.bmiHeader);
    info.bmiHeader.biWidth = header->width;
    info.bmiHeader.biHeight = -(LONG)header->height;
    info.bmiHeader.biPlanes = 1;
    info.bmiHeader.biBitCount = 32;
    info.bmiHeader.biCompression = BI_RGB;
    void *bits = NULL;
    HBITMAP color = CreateDIBSection(NULL, &info, DIB_RGB_COLORS, &bits, NULL, 0);
    if (!color) return NULL;
    memcpy(bits, native_cursor_pixels, bytes);
    uint8_t mask_bits[((UURB_CURSOR_MAX_SIDE + 15) / 16) * 2 * UURB_CURSOR_MAX_SIDE] = {0};
    unsigned stride = ((header->width + 15) / 16) * 2;
    for (unsigned y = 0; y < header->height; ++y)
        for (unsigned x = 0; x < header->width; ++x)
            if (!native_cursor_pixels[((size_t)y * header->width + x) * 4 + 3])
                mask_bits[y * stride + x / 8] |= 0x80u >> (x % 8);
    HBITMAP mask = CreateBitmap(header->width, header->height, 1, 1, mask_bits);
    ICONINFO icon = {.fIcon = FALSE, .xHotspot = header->hotspot_x, .yHotspot = header->hotspot_y,
                     .hbmMask = mask, .hbmColor = color};
    HCURSOR handle = mask ? (HCURSOR)CreateIconIndirect(&icon) : NULL;
    if (mask) DeleteObject(mask);
    DeleteObject(color);
    if (!handle) return NULL;
    uint8_t *copy = HeapAlloc(GetProcessHeap(), 0, bytes);
    if (!copy) { DestroyCursor(handle); return NULL; }
    memcpy(copy, native_cursor_pixels, bytes);
    native_cursors[native_cursor_count++] = (struct native_cursor_entry){
        handle, header->width, header->height, header->hotspot_x, header->hotspot_y, copy};
    return handle;
}

/* Caller holds the lock. Read header/pixels/header and accept only one even
 * generation. Tiny position updates do not copy the sprite or recreate GDI. */
static BOOL native_cursor_snapshot(struct uurb_cursor_header *header, HCURSOR *handle)
{
    for (unsigned attempt = 0; attempt < 3; ++attempt) {
        struct uurb_cursor_header first, last;
        if (!native_cursor_read(&first, sizeof(first), 0)) return FALSE;
        if (first.sequence & 1) continue;
        if (first.magic != UURB_CURSOR_MAGIC || first.version != UURB_CURSOR_VERSION ||
            first.active > 1 || first.visible > 1) return FALSE;
        BOOL changed = first.generation != native_cursor_cached.generation ||
            first.shape_serial != native_cursor_cached.shape_serial || !native_cursor_handle;
        if (first.active && first.visible) {
            if (!first.generation || !first.shape_serial || !first.width || !first.height ||
                first.width > UURB_CURSOR_MAX_SIDE || first.height > UURB_CURSOR_MAX_SIDE ||
                first.hotspot_x < 0 || first.hotspot_y < 0 ||
                first.hotspot_x >= (int32_t)first.width || first.hotspot_y >= (int32_t)first.height) return FALSE;
            if (!native_cursor_video_composited && changed &&
                !native_cursor_read(native_cursor_pixels, first.width * first.height * 4, 64)) return FALSE;
        }
        if (!native_cursor_read(&last, sizeof(last), 0)) return FALSE;
        if (memcmp(&first, &last, sizeof(first))) continue;
        *header = first;
        *handle = NULL;
        if (first.active && first.visible && !native_cursor_video_composited) {
            if (changed) {
                HCURSOR next = native_cursor_create(&first);
                if (!next) return FALSE;
                native_cursor_handle = next;
                native_cursor_cached = first;
            }
            *handle = native_cursor_handle;
        }
        return TRUE;
    }
    return FALSE;
}

static BOOL WINAPI native_cursor_info(PCURSORINFO info)
{
    if (!info || info->cbSize != sizeof(*info)) { SetLastError(ERROR_INVALID_PARAMETER); return FALSE; }
    if (native_cursor_video_embedded) {
        /* Hiding a second sprite must not fabricate a different position from
         * the still-unmodified GetCursorPos API. This is the USER32 position,
         * not a claim that Wine mirrors the compositor's native pointer. */
        POINT position;
        typedef BOOL (WINAPI *cursor_position_fn)(LPPOINT);
        if (!native_get_cursor_pos || !((cursor_position_fn)native_get_cursor_pos)(&position))
            return FALSE;
        info->flags = 0;
        info->hCursor = NULL;
        info->ptScreenPos = position;
        if (InterlockedIncrement(&native_cursor_calls) == 1)
            write_log("UURB_NATIVE_CURSOR {\"video_embedded\":true,\"separate_sprite_hidden\":true}\r\n");
        return TRUE;
    }
    struct uurb_cursor_header header;
    HCURSOR handle;
    AcquireSRWLockExclusive(&native_cursor_lock);
    BOOL ok = native_cursor_snapshot(&header, &handle);
    if (ok) {
        info->flags = handle ? CURSOR_SHOWING : 0;
        info->hCursor = handle;
        info->ptScreenPos = (POINT){header.x, header.y};
    }
    ReleaseSRWLockExclusive(&native_cursor_lock);
    if (InterlockedIncrement(&native_cursor_calls) == 1)
        write_log("UURB_NATIVE_CURSOR {\"get_cursor_info_called\":true}\r\n");
    if (!ok) {
        SetLastError(ERROR_NOT_READY);
        if (InterlockedIncrement(&native_cursor_errors) <= 4)
            write_log("UURB_NATIVE_CURSOR {\"snapshot_unavailable\":true}\r\n");
    }
    return ok;
}

static BOOL WINAPI native_cursor_pos(LPPOINT point)
{
    if (!point) { SetLastError(ERROR_INVALID_PARAMETER); return FALSE; }
    struct uurb_cursor_header header;
    HCURSOR handle;
    AcquireSRWLockExclusive(&native_cursor_lock);
    BOOL ok = native_cursor_snapshot(&header, &handle) && header.active;
    if (ok) *point = (POINT){header.x, header.y};
    ReleaseSRWLockExclusive(&native_cursor_lock);
    if (!ok) SetLastError(ERROR_NOT_READY);
    return ok;
}

static int native_cursor_initialize(void)
{
    WCHAR path[32768];
    DWORD length = GetEnvironmentVariableW(L"UURB_CURSOR_STATE_PATH", path, ARRAYSIZE(path));
    if (!length) return 0;
    if (length >= ARRAYSIZE(path)) return -1;
    native_cursor_file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                                    NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (native_cursor_file == INVALID_HANDLE_VALUE) return -1;
    LARGE_INTEGER size;
    struct uurb_cursor_header header;
    if (!GetFileSizeEx(native_cursor_file, &size) || size.QuadPart != sizeof(struct uurb_cursor_snapshot) ||
        !native_cursor_read(&header, sizeof(header), 0) || header.magic != UURB_CURSOR_MAGIC ||
        header.version != UURB_CURSOR_VERSION) return -1;
    native_get_cursor_info = (uintptr_t)GetProcAddress(GetModuleHandleW(L"user32.dll"), "GetCursorInfo");
    native_get_cursor_pos = (uintptr_t)GetProcAddress(GetModuleHandleW(L"user32.dll"), "GetCursorPos");
    if (header.reserved[0] > UURB_CURSOR_VIDEO_COMPOSITED || header.reserved[1] || header.reserved[2]) return -1;
    native_cursor_video_embedded = header.reserved[0] == UURB_CURSOR_VIDEO_EMBEDDED;
    native_cursor_video_composited = header.reserved[0] == UURB_CURSOR_VIDEO_COMPOSITED;
    return native_get_cursor_info && native_get_cursor_pos ? 1 : -1;
}
#endif
