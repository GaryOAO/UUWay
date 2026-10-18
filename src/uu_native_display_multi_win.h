#ifndef UURB_NATIVE_DISPLAY_MULTI_WIN_H
#define UURB_NATIVE_DISPLAY_MULTI_WIN_H
/* Multi-output Win32 display view for UU.
 *
 * Included after uu_native_display_adapter_win.h.  When the native topology
 * has a topology API it synthesizes Windows
 * adapters, monitors and display-config paths from the same topology the
 * capture broker receives, so DXGI/DXVK enumerate one output per screen.
 *
 * Only public Windows interfaces are implemented.  The topology is fetched
 * from the same same-UID display socket as the single-output view; no UU
 * offsets, monitor serials or EDID data are used.
 */
#include <limits.h>
#include <wchar.h>

#define UURB_MULTI_LUID_LOW 0x55524242u
#define UURB_MULTI_LUID_HIGH 0x4d4du
#define UURB_MULTI_TARGET_BASE 0x1000u
#define UURB_MULTI_NOT_APPLICABLE LONG_MIN
#define UURB_MULTI_HANDLE_BASE 0x4d4d0000u
#define UURB_MULTI_HANDLE_STRIDE 0x40u
#define UURB_MULTI_HANDLE_MISSING ((HMONITOR)(uintptr_t)-1)

static struct uurb_display_topology native_multi_cache;
static SRWLOCK native_multi_lock = SRWLOCK_INIT;
static ULONGLONG native_multi_time;
static BOOL native_multi_valid;

struct uurb_native_dpi_get {
    DISPLAYCONFIG_DEVICE_INFO_HEADER header;
    int32_t min_scale_rel, cur_scale_rel, max_scale_rel;
};
struct uurb_native_dpi_set {
    DISPLAYCONFIG_DEVICE_INFO_HEADER header;
    int32_t scale_rel;
};
_Static_assert(sizeof(struct uurb_native_dpi_get) == 32, "native DPI get ABI");
_Static_assert(sizeof(struct uurb_native_dpi_set) == 24, "native DPI set ABI");

static BOOL native_multi_snapshot(struct uurb_display_topology *output, BOOL force)
{
    if (!native_display_topology_api || !native_display_endpoint[0]) return FALSE;
    AcquireSRWLockExclusive(&native_multi_lock);
    ULONGLONG now = GetTickCount64();
    if (force || !native_multi_time || now - native_multi_time >= 500) {
        struct uurb_display_topology value;
        DWORD status = native_display_topology_api(native_display_endpoint, &value, sizeof(value));
        if (status) {
            char message[160];
            snprintf(message, sizeof(message), "UURB_NATIVE_DISPLAY {\"event\":\"topology_query_failed\",\"status\":%lu}\r\n", (unsigned long)status);
            write_log(message); flush_log();
        }
        if (!status && value.version == UURB_DISPLAY_TOPOLOGY_VERSION &&
            value.monitor_count >= 1 && value.monitor_count <= UURB_DISPLAY_MAX_MONITORS &&
            value.mode_count <= UURB_DISPLAY_MAX_MONITORS * UURB_DISPLAY_MAX_MONITOR_MODES) {
            native_multi_cache = value;
            native_multi_time = now;
            native_multi_valid = TRUE;
        }
    }
    BOOL valid = native_multi_valid;
    if (valid && output) *output = native_multi_cache;
    ReleaseSRWLockExclusive(&native_multi_lock);
    return valid;
}

static BOOL native_multi_active(struct uurb_display_topology *output)
{
    struct uurb_display_topology value;
    if (!native_multi_snapshot(&value, FALSE)) return FALSE;
    if (output) *output = value;
    return TRUE;
}

static LUID native_multi_luid(void)
{
    LUID value = {.LowPart = UURB_MULTI_LUID_LOW, .HighPart = UURB_MULTI_LUID_HIGH};
    return value;
}

static BOOL native_multi_same_luid(LUID a, LUID b)
{
    return a.LowPart == b.LowPart && a.HighPart == b.HighPart;
}

static UINT native_multi_primary(const struct uurb_display_topology *topology)
{
    for (UINT i = 0; i < topology->monitor_count; ++i)
        if (topology->monitors[i].flags & UURB_DISPLAY_MONITOR_PRIMARY) return i;
    return 0;
}

/* Console-created screens and local GNOME changes do not originate in a
 * Win32 API call. Notify UU from its existing supervisor thread after the
 * native view changes, so it can refresh the remote screen list. */
static void native_multi_poll_changes(void)
{
    static struct uurb_display_topology previous;
    static BOOL observed;
    struct uurb_display_topology current;
    if (!native_multi_snapshot(&current, FALSE)) return;
    BOOL changed = observed && previous.monitor_count != current.monitor_count;
    for (UINT i = 0; observed && !changed && i < current.monitor_count; ++i) {
        const struct uurb_display_monitor *a = &previous.monitors[i], *b = &current.monitors[i];
        changed = a->flags != b->flags || a->x != b->x || a->y != b->y ||
                  a->width != b->width || a->height != b->height ||
                  a->refresh_millihz != b->refresh_millihz || a->scale_milli != b->scale_milli ||
                  strcmp(a->gdi_name, b->gdi_name) || strcmp(a->device_path, b->device_path);
    }
    previous = current;
    observed = TRUE;
    if (changed) {
        const struct uurb_display_monitor *primary = &current.monitors[native_multi_primary(&current)];
        SendNotifyMessageW(HWND_BROADCAST, WM_DISPLAYCHANGE, 32, MAKELPARAM(primary->width, primary->height));
    }
}

static RECT native_multi_rect(const struct uurb_display_monitor *monitor)
{
    LONG x = (LONG)(int32_t)monitor->x, y = (LONG)(int32_t)monitor->y;
    return (RECT){x, y, x + (LONG)monitor->width, y + (LONG)monitor->height};
}

static void native_multi_union(const struct uurb_display_topology *topology, RECT *result)
{
    RECT value = native_multi_rect(&topology->monitors[0]);
    for (UINT i = 1; i < topology->monitor_count; ++i) {
        RECT item = native_multi_rect(&topology->monitors[i]);
        if (item.left < value.left) value.left = item.left;
        if (item.top < value.top) value.top = item.top;
        if (item.right > value.right) value.right = item.right;
        if (item.bottom > value.bottom) value.bottom = item.bottom;
    }
    *result = value;
}

static void native_multi_utf8(const char *source, WCHAR *output, size_t count)
{
    if (!output || !count) return;
    output[0] = 0;
    if (source && count > 1)
        MultiByteToWideChar(CP_UTF8, 0, source, -1, output, (int)count);
}

static int native_multi_device_id(LPCWSTR device)
{
    if (!device) return 0;
    static const WCHAR prefix[] = L"\\\\.\\DISPLAY";
    WCHAR buffer[64];
    size_t length = 0;
    for (; length < ARRAYSIZE(buffer) - 1; ++length) {
        SIZE_T bytes = 0;
        if (!ReadProcessMemory(GetCurrentProcess(), device + length, &buffer[length],
                               sizeof(buffer[length]), &bytes) || bytes != sizeof(buffer[length])) return -1;
        if (!buffer[length]) break;
        if (length < ARRAYSIZE(prefix) - 1 && buffer[length] != prefix[length]) return -1;
    }
    if (length == ARRAYSIZE(buffer) - 1) return -1;
    buffer[length] = 0;
    if (length <= ARRAYSIZE(prefix) - 2) return -1;
    WCHAR *end = NULL;
    unsigned long number = wcstoul(buffer + ARRAYSIZE(prefix) - 1, &end, 10);
    if (!end || end == buffer + ARRAYSIZE(prefix) - 1 || *end || number < 1 ||
        number > UURB_DISPLAY_MAX_MONITORS) return -1;
    return (int)number - 1;
}

static UINT native_multi_id(const struct uurb_display_monitor *monitor)
{
    WCHAR name[16];
    native_multi_utf8(monitor->gdi_name, name, ARRAYSIZE(name));
    int id = native_multi_device_id(name);
    return id < 0 ? monitor->index : (UINT)id;
}

static int native_multi_index_for_id(const struct uurb_display_topology *topology, UINT id)
{
    for (UINT i = 0; i < topology->monitor_count; ++i)
        if (native_multi_id(&topology->monitors[i]) == id) return (int)i;
    return -1;
}

static int native_multi_device_index(LPCWSTR device)
{
    if (!device) return 0;
    int id = native_multi_device_id(device);
    struct uurb_display_topology topology;
    if (id < 0 || !native_multi_snapshot(&topology, FALSE)) return -1;
    return native_multi_index_for_id(&topology, (UINT)id);
}

static HMONITOR native_multi_handle(UINT index)
{
    return (HMONITOR)(uintptr_t)(UURB_MULTI_HANDLE_BASE + index * UURB_MULTI_HANDLE_STRIDE);
}

static int native_multi_handle_index(HMONITOR handle)
{
    uintptr_t value = (uintptr_t)handle;
    if (value < UURB_MULTI_HANDLE_BASE) return -1;
    value -= UURB_MULTI_HANDLE_BASE;
    if (value % UURB_MULTI_HANDLE_STRIDE) return -1;
    value /= UURB_MULTI_HANDLE_STRIDE;
    return value < UURB_DISPLAY_MAX_MONITORS ? (int)value : -1;
}

static void native_multi_device_string(const struct uurb_display_monitor *monitor,
                                       DISPLAY_DEVICEW *output, DWORD flags, BOOL primary)
{
    ZeroMemory(output, sizeof(*output));
    output->cb = sizeof(*output);
    native_multi_utf8(monitor->gdi_name, output->DeviceName, ARRAYSIZE(output->DeviceName));
    native_multi_utf8(monitor->friendly, output->DeviceString, ARRAYSIZE(output->DeviceString));
    output->StateFlags = DISPLAY_DEVICE_ATTACHED_TO_DESKTOP |
                         (primary ? DISPLAY_DEVICE_PRIMARY_DEVICE : 0);
    if (flags & EDD_GET_DEVICE_INTERFACE_NAME) {
        native_multi_utf8(monitor->device_path, output->DeviceID, ARRAYSIZE(output->DeviceID));
    } else {
        native_multi_utf8("UURB\\NativeDisplay", output->DeviceID, ARRAYSIZE(output->DeviceID));
    }
    native_multi_utf8("\\Registry\\Machine\\System\\CurrentControlSet\\Control\\Video\\UURB",
                      output->DeviceKey, ARRAYSIZE(output->DeviceKey));
}

/* 1 = handled, 0 = handled failure, -1 = not applicable. */
static int native_multi_enum_devices_w(LPCWSTR device, DWORD index, DISPLAY_DEVICEW *output, DWORD flags)
{
    struct uurb_display_topology topology;
    if (!output || output->cb != sizeof(*output) || (flags & ~EDD_GET_DEVICE_INTERFACE_NAME))
        return -1;
    if (!native_multi_active(&topology)) return -1;
    if (!device) {
        if (index >= topology.monitor_count) {
            SetLastError(ERROR_NO_MORE_ITEMS);
            return 0;
        }
        native_multi_device_string(&topology.monitors[index], output, flags, index == native_multi_primary(&topology));
        return 1;
    }
    int monitor_index = native_multi_device_index(device);
    if (monitor_index < 0 || (UINT)monitor_index >= topology.monitor_count) return -1;
    if (index) {
        SetLastError(ERROR_NO_MORE_ITEMS);
        return 0;
    }
    native_multi_device_string(&topology.monitors[monitor_index], output, flags,
                               (UINT)monitor_index == native_multi_primary(&topology));
    WCHAR suffix[] = L"\\Monitor0";
    size_t length = wcslen(output->DeviceName);
    if (length + ARRAYSIZE(suffix) <= ARRAYSIZE(output->DeviceName))
        memcpy(output->DeviceName + length, suffix, sizeof(suffix));
    return 1;
}

static int native_multi_enum_mode(LPCWSTR device, DWORD index, DEVMODEW *output, DWORD flags)
{
    if (!output || output->dmSize != sizeof(DEVMODEW) || output->dmDriverExtra ||
        (flags & ~(EDS_RAWMODE | EDS_ROTATEDMODE))) return -1;
    struct uurb_display_topology topology;
    if (!native_multi_active(&topology)) return -1;
    int monitor_index = native_multi_device_index(device);
    if (monitor_index < 0 || (UINT)monitor_index >= topology.monitor_count) return -1;
    const struct uurb_display_monitor *monitor = &topology.monitors[monitor_index];
    uint32_t width, height, refresh;
    if (index == ENUM_CURRENT_SETTINGS || index == ENUM_REGISTRY_SETTINGS) {
        width = monitor->width;
        height = monitor->height;
        refresh = monitor->refresh_millihz;
    } else {
        if (index >= monitor->mode_count) {
            SetLastError(ERROR_NO_MORE_ITEMS);
            return 0;
        }
        const struct uurb_display_mode *mode = &topology.modes[monitor->mode_offset + index];
        width = mode->width;
        height = mode->height;
        refresh = mode->refresh_millihz;
    }
    DEVMODEW value;
    ZeroMemory(&value, sizeof(value));
    value.dmSize = sizeof(value);
    value.dmSpecVersion = DM_SPECVERSION;
    value.dmFields = DM_BITSPERPEL | DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY |
                     DM_DISPLAYFLAGS | DM_POSITION | DM_DISPLAYORIENTATION | DM_DISPLAYFIXEDOUTPUT;
    value.dmBitsPerPel = 32;
    value.dmPelsWidth = width;
    value.dmPelsHeight = height;
    value.dmDisplayFrequency = (refresh + 500) / 1000;
    value.dmPosition.x = (LONG)(int32_t)monitor->x;
    value.dmPosition.y = (LONG)(int32_t)monitor->y;
    value.dmDisplayOrientation = DMDO_DEFAULT;
    value.dmDisplayFixedOutput = DMDFO_DEFAULT;
    *output = value;
    return 1;
}

static BOOL native_multi_mode_matches(const struct uurb_display_topology *topology,
                                      const struct uurb_display_monitor *monitor,
                                      const DEVMODEW *mode)
{
    uint32_t width = monitor->width, height = monitor->height, refresh = mode->dmDisplayFrequency;
    if (mode->dmFields & DM_PELSWIDTH) width = mode->dmPelsWidth;
    if (mode->dmFields & DM_PELSHEIGHT) height = mode->dmPelsHeight;
    BOOL rate_specified = (mode->dmFields & DM_DISPLAYFREQUENCY) && mode->dmDisplayFrequency;
    for (uint32_t i = 0; i < monitor->mode_count; ++i) {
        const struct uurb_display_mode *candidate = &topology->modes[monitor->mode_offset + i];
        if (candidate->width != width || candidate->height != height) continue;
        if (!rate_specified || candidate->refresh_millihz / 1000 == refresh) return TRUE;
    }
    return FALSE;
}

static int native_multi_change_mode(LPCWSTR device, DEVMODEW *mode, HWND window, DWORD flags, LPVOID extra)
{
    struct uurb_display_topology topology;
    if (window || extra || !native_multi_active(&topology)) return UURB_MULTI_NOT_APPLICABLE;
    int monitor_index = native_multi_device_index(device);
    if (monitor_index < 0 || (UINT)monitor_index >= topology.monitor_count)
        return UURB_MULTI_NOT_APPLICABLE;
    if (flags & ~(CDS_TEST | CDS_FULLSCREEN | CDS_RESET | CDS_UPDATEREGISTRY | CDS_GLOBAL))
        return DISP_CHANGE_BADFLAGS;
    if (flags & CDS_NORESET) return DISP_CHANGE_NOTUPDATED;
    if ((flags & CDS_GLOBAL) && !(flags & CDS_UPDATEREGISTRY)) return DISP_CHANGE_BADFLAGS;
    if (flags & CDS_TEST) {
        if (!mode) return DISP_CHANGE_SUCCESSFUL;
        DEVMODEW copied;
        SIZE_T bytes = 0;
        if (!ReadProcessMemory(GetCurrentProcess(), mode, &copied, sizeof(copied), &bytes) ||
            bytes != sizeof(copied) || copied.dmSize != sizeof(copied) || copied.dmDriverExtra)
            return DISP_CHANGE_BADPARAM;
        if ((copied.dmFields & DM_BITSPERPEL) && copied.dmBitsPerPel != 32) return DISP_CHANGE_BADMODE;
        if (native_multi_mode_matches(&topology, &topology.monitors[monitor_index], &copied))
            return DISP_CHANGE_SUCCESSFUL;
        return DISP_CHANGE_BADMODE;
    }
    if (!native_display_reconfigure_enabled) return DISP_CHANGE_NOTUPDATED;
    if (!mode) return DISP_CHANGE_SUCCESSFUL; /* UU's refresh must not replay an old default. */
    DEVMODEW copied;
    SIZE_T bytes = 0;
    if (!ReadProcessMemory(GetCurrentProcess(), mode, &copied, sizeof(copied), &bytes) ||
        bytes != sizeof(copied) || copied.dmSize != sizeof(copied) || copied.dmDriverExtra)
        return DISP_CHANGE_BADPARAM;
    DWORD allowed = DM_BITSPERPEL | DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY |
                    DM_DISPLAYFLAGS | DM_POSITION | DM_DISPLAYORIENTATION | DM_DISPLAYFIXEDOUTPUT;
    const struct uurb_display_monitor *monitor = &topology.monitors[monitor_index];
    if (copied.dmFields & ~allowed) return DISP_CHANGE_BADPARAM;
    if (((copied.dmFields & DM_BITSPERPEL) && copied.dmBitsPerPel != 32) ||
        ((copied.dmFields & DM_DISPLAYFLAGS) && copied.dmDisplayFlags) ||
        ((copied.dmFields & DM_DISPLAYORIENTATION) && copied.dmDisplayOrientation != DMDO_DEFAULT) ||
        ((copied.dmFields & DM_DISPLAYFIXEDOUTPUT) && copied.dmDisplayFixedOutput != DMDFO_DEFAULT) ||
        ((copied.dmFields & DM_POSITION) &&
         (copied.dmPosition.x != (LONG)(int32_t)monitor->x || copied.dmPosition.y != (LONG)(int32_t)monitor->y)))
        return DISP_CHANGE_BADMODE;
    struct uurb_display_request request = {.version = 1,
        .operation = !(flags & CDS_UPDATEREGISTRY) ? UURB_DISPLAY_APPLY :
            flags & CDS_GLOBAL ? UURB_DISPLAY_APPLY_GLOBAL_DEFAULT : UURB_DISPLAY_APPLY_USER_DEFAULT,
        .serial = topology.serial, .width = monitor->width, .height = monitor->height,
        .refresh_millihz = monitor->refresh_millihz,
        .reserved = UURB_DISPLAY_OUTPUT_SELECTED | ((unsigned)monitor_index << UURB_DISPLAY_OUTPUT_SHIFT) |
                    ((flags & CDS_RESET) ? UURB_DISPLAY_FORCE_RESET : 0)};
    if (copied.dmFields & (DM_PELSWIDTH | DM_PELSHEIGHT)) request.refresh_millihz = 0;
    if (copied.dmFields & DM_PELSWIDTH) request.width = copied.dmPelsWidth;
    if (copied.dmFields & DM_PELSHEIGHT) request.height = copied.dmPelsHeight;
    if (copied.dmFields & DM_DISPLAYFREQUENCY) {
        if (copied.dmDisplayFrequency > UURB_DISPLAY_MAX_REFRESH_HZ) return DISP_CHANGE_BADMODE;
        request.refresh_millihz = copied.dmDisplayFrequency <= 1 ? 0 : copied.dmDisplayFrequency * 1000;
    }
    struct uurb_display_reply reply;
    DWORD status = native_display_change_api(native_display_endpoint, &request, &reply);
    if (status == UURB_DISPLAY_BAD_MODE || status == UURB_DISPLAY_INVALID) return DISP_CHANGE_BADMODE;
    if (status) return status == UURB_DISPLAY_DEFAULT_FAILED ? DISP_CHANGE_NOTUPDATED : DISP_CHANGE_FAILED;
    if (reply.changed) {
        AcquireSRWLockExclusive(&native_multi_lock);
        native_multi_time = 0;
        ReleaseSRWLockExclusive(&native_multi_lock);
        SendNotifyMessageW(HWND_BROADCAST, WM_DISPLAYCHANGE, 32, MAKELPARAM(request.width, request.height));
    }
    return DISP_CHANGE_SUCCESSFUL;
}

static int native_multi_enum_monitors(HDC dc, LPCRECT clip, MONITORENUMPROC callback, LPARAM data)
{
    struct uurb_display_topology topology;
    if (dc || !callback || !native_multi_active(&topology)) return -1;
    RECT clip_value;
    if (clip) {
        SIZE_T bytes = 0;
        if (!ReadProcessMemory(GetCurrentProcess(), clip, &clip_value, sizeof(clip_value), &bytes) ||
            bytes != sizeof(clip_value)) {
            SetLastError(ERROR_INVALID_PARAMETER);
            return 0;
        }
    }
    for (UINT i = 0; i < topology.monitor_count; ++i) {
        RECT rectangle = native_multi_rect(&topology.monitors[i]);
        RECT selected = rectangle;
        if (clip && !IntersectRect(&selected, &rectangle, &clip_value)) continue;
        if (!callback(native_multi_handle(native_multi_id(&topology.monitors[i])), NULL, &selected, data)) break;
    }
    return 1;
}

static int native_multi_monitor_info(HMONITOR monitor, LPMONITORINFO output, BOOL ex, BOOL ansi)
{
    struct uurb_display_topology topology;
    int id = native_multi_handle_index(monitor);
    int index = -1;
    if (id >= 0 && native_multi_active(&topology)) index = native_multi_index_for_id(&topology, (UINT)id);
    if (index < 0 || !output || output->cbSize < sizeof(MONITORINFO) ||
        !native_multi_active(&topology) || (UINT)index >= topology.monitor_count) return -1;
    RECT rectangle = native_multi_rect(&topology.monitors[index]);
    output->rcMonitor = rectangle;
    output->rcWork = rectangle;
    output->dwFlags = (UINT)index == native_multi_primary(&topology) ? MONITORINFOF_PRIMARY : 0;
    if (ex) {
        if (ansi) {
            MONITORINFOEXA *value = (MONITORINFOEXA *)output;
            const char *name = topology.monitors[index].gdi_name;
            size_t length = strlen(name);
            if (length >= ARRAYSIZE(value->szDevice)) length = ARRAYSIZE(value->szDevice) - 1;
            memcpy(value->szDevice, name, length); value->szDevice[length] = 0;
        } else {
            MONITORINFOEXW *value = (MONITORINFOEXW *)output;
            native_multi_utf8(topology.monitors[index].gdi_name, value->szDevice, ARRAYSIZE(value->szDevice));
        }
    }
    return 1;
}

static HMONITOR native_multi_pick_rect(const struct uurb_display_topology *topology,
                                       const RECT *rectangle, DWORD flags)
{
    UINT primary = native_multi_primary(topology);
    HMONITOR best = NULL;
    LONG best_distance = LONG_MAX;
    LONG center_x = rectangle->left + (rectangle->right - rectangle->left) / 2;
    LONG center_y = rectangle->top + (rectangle->bottom - rectangle->top) / 2;
    for (UINT i = 0; i < topology->monitor_count; ++i) {
        RECT item = native_multi_rect(&topology->monitors[i]);
        RECT intersection;
        if (IntersectRect(&intersection, &item, rectangle)) {
            LONG area = (intersection.right - intersection.left) *
                        (intersection.bottom - intersection.top);
            if (best == NULL || area > best_distance) {
                best = native_multi_handle(native_multi_id(&topology->monitors[i]));
                best_distance = area;
            }
        }
    }
    if (best) return best;
    if (flags == MONITOR_DEFAULTTONULL) return NULL;
    if (flags == MONITOR_DEFAULTTOPRIMARY) return native_multi_handle(native_multi_id(&topology->monitors[primary]));
    for (UINT i = 0; i < topology->monitor_count; ++i) {
        RECT item = native_multi_rect(&topology->monitors[i]);
        LONG x = item.left + (item.right - item.left) / 2;
        LONG y = item.top + (item.bottom - item.top) / 2;
        LONG distance = (x - center_x) * (x - center_x) + (y - center_y) * (y - center_y);
        if (distance < best_distance) {
            best_distance = distance;
            best = native_multi_handle(native_multi_id(&topology->monitors[i]));
        }
    }
    return best ? best : native_multi_handle(native_multi_id(&topology->monitors[primary]));
}

static int native_multi_monitor_from_point(POINT point, DWORD flags, HMONITOR *output)
{
    struct uurb_display_topology topology;
    if (!native_multi_active(&topology)) return -1;
    RECT rectangle = {point.x, point.y, point.x + 1, point.y + 1};
    *output = native_multi_pick_rect(&topology, &rectangle, flags);
    return 1;
}

static int native_multi_monitor_from_rect(const RECT *rectangle, DWORD flags, HMONITOR *output)
{
    struct uurb_display_topology topology;
    if (!rectangle || !native_multi_active(&topology)) return -1;
    *output = native_multi_pick_rect(&topology, rectangle, flags);
    return 1;
}

static int native_multi_monitor_from_window(HWND window, DWORD flags, HMONITOR *output)
{
    struct uurb_display_topology topology;
    if (!native_multi_active(&topology)) return -1;
    RECT rectangle = {0, 0, 0, 0};
    if (!window || !GetWindowRect(window, &rectangle) ||
        rectangle.right <= rectangle.left || rectangle.bottom <= rectangle.top)
        rectangle = (RECT){0, 0, 1, 1};
    *output = native_multi_pick_rect(&topology, &rectangle, flags);
    return 1;
}

static int native_multi_metrics(int index, int *value)
{
    struct uurb_display_topology topology;
    if (!native_multi_active(&topology)) return -1;
    UINT primary = native_multi_primary(&topology);
    RECT union_rect;
    native_multi_union(&topology, &union_rect);
    switch (index) {
    case SM_CXSCREEN: case SM_CYSCREEN:
        *value = index == SM_CXSCREEN ? (int)topology.monitors[primary].width
                                      : (int)topology.monitors[primary].height;
        return 1;
    case SM_XVIRTUALSCREEN: *value = union_rect.left; return 1;
    case SM_YVIRTUALSCREEN: *value = union_rect.top; return 1;
    case SM_CXVIRTUALSCREEN: *value = union_rect.right - union_rect.left; return 1;
    case SM_CYVIRTUALSCREEN: *value = union_rect.bottom - union_rect.top; return 1;
    case SM_CMONITORS: *value = (int)topology.monitor_count; return 1;
    default: return -1;
    }
}

static int native_multi_work_area(RECT *output)
{
    struct uurb_display_topology topology;
    if (!output || !native_multi_active(&topology)) return -1;
    *output = native_multi_rect(&topology.monitors[native_multi_primary(&topology)]);
    return 1;
}

static int native_multi_buffer_sizes(UINT32 flags, UINT32 *paths, UINT32 *modes, LONG *result)
{
    struct uurb_display_topology topology;
    if (!paths || !modes || (flags & ~(QDC_ALL_PATHS | QDC_ONLY_ACTIVE_PATHS | QDC_VIRTUAL_MODE_AWARE)) ||
        !native_multi_active(&topology)) return -1;
    *paths = topology.monitor_count;
    *modes = topology.monitor_count * 2;
    *result = ERROR_SUCCESS;
    return 1;
}

static void native_multi_signal(const struct uurb_display_monitor *monitor,
                                DISPLAYCONFIG_VIDEO_SIGNAL_INFO *signal)
{
    ZeroMemory(signal, sizeof(*signal));
    signal->activeSize.cx = signal->totalSize.cx = monitor->width;
    signal->activeSize.cy = signal->totalSize.cy = monitor->height;
    signal->vSyncFreq.Numerator = monitor->refresh_millihz;
    signal->vSyncFreq.Denominator = 1000;
    signal->hSyncFreq.Numerator = monitor->height * monitor->refresh_millihz;
    signal->hSyncFreq.Denominator = 1000;
    signal->pixelRate = (UINT64)monitor->width * monitor->height * monitor->refresh_millihz / 1000;
    signal->videoStandard = 255;
    signal->scanLineOrdering = DISPLAYCONFIG_SCANLINE_ORDERING_PROGRESSIVE;
}

static int native_multi_query_config(UINT32 flags, UINT32 *path_count, DISPLAYCONFIG_PATH_INFO *paths,
                                     UINT32 *mode_count, DISPLAYCONFIG_MODE_INFO *modes,
                                     DISPLAYCONFIG_TOPOLOGY_ID *topology_id, LONG *result)
{
    struct uurb_display_topology topology;
    if (!path_count || !mode_count || !paths || !modes ||
        (flags & ~(QDC_ALL_PATHS | QDC_ONLY_ACTIVE_PATHS | QDC_VIRTUAL_MODE_AWARE)) ||
        !native_multi_active(&topology)) return -1;
    UINT32 required_paths = topology.monitor_count, required_modes = topology.monitor_count * 2;
    if (*path_count < required_paths || *mode_count < required_modes) {
        *path_count = required_paths;
        *mode_count = required_modes;
        *result = ERROR_INSUFFICIENT_BUFFER;
        return 1;
    }
    LUID luid = native_multi_luid();
    for (UINT32 i = 0; i < topology.monitor_count; ++i) {
        const struct uurb_display_monitor *monitor = &topology.monitors[i];
        DISPLAYCONFIG_PATH_INFO path;
        ZeroMemory(&path, sizeof(path));
        path.flags = DISPLAYCONFIG_PATH_ACTIVE;
        path.sourceInfo.adapterId = luid;
        path.sourceInfo.id = native_multi_id(monitor) + 1;
        path.sourceInfo.modeInfoIdx = i * 2;
        path.targetInfo.adapterId = luid;
        path.targetInfo.id = UURB_MULTI_TARGET_BASE + native_multi_id(monitor);
        path.targetInfo.modeInfoIdx = i * 2 + 1;
        path.targetInfo.outputTechnology = DISPLAYCONFIG_OUTPUT_TECHNOLOGY_DISPLAYPORT_EXTERNAL;
        path.targetInfo.rotation = DISPLAYCONFIG_ROTATION_IDENTITY;
        path.targetInfo.scaling = DISPLAYCONFIG_SCALING_IDENTITY;
        path.targetInfo.refreshRate.Numerator = monitor->refresh_millihz;
        path.targetInfo.refreshRate.Denominator = 1000;
        path.targetInfo.scanLineOrdering = DISPLAYCONFIG_SCANLINE_ORDERING_PROGRESSIVE;
        path.targetInfo.targetAvailable = TRUE;
        path.targetInfo.statusFlags = DISPLAYCONFIG_TARGET_IN_USE;
        paths[i] = path;

        DISPLAYCONFIG_MODE_INFO source, target;
        ZeroMemory(&source, sizeof(source));
        ZeroMemory(&target, sizeof(target));
        source.infoType = DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE;
        source.id = path.sourceInfo.id;
        source.adapterId = luid;
        source.sourceMode.width = monitor->width;
        source.sourceMode.height = monitor->height;
        source.sourceMode.pixelFormat = DISPLAYCONFIG_PIXELFORMAT_32BPP;
        source.sourceMode.position.x = (LONG)(int32_t)monitor->x;
        source.sourceMode.position.y = (LONG)(int32_t)monitor->y;
        target.infoType = DISPLAYCONFIG_MODE_INFO_TYPE_TARGET;
        target.id = path.targetInfo.id;
        target.adapterId = luid;
        native_multi_signal(monitor, &target.targetMode.targetVideoSignalInfo);
        modes[i * 2] = source;
        modes[i * 2 + 1] = target;
    }
    *path_count = required_paths;
    *mode_count = required_modes;
    if (topology_id) *topology_id = DISPLAYCONFIG_TOPOLOGY_EXTEND;
    *result = ERROR_SUCCESS;
    return 1;
}

static int native_multi_device_info(LONG (WINAPI *original)(DISPLAYCONFIG_DEVICE_INFO_HEADER *),
                                    DISPLAYCONFIG_DEVICE_INFO_HEADER *packet, BOOL is_set, LONG *result)
{
    (void)original;
    struct uurb_display_topology topology;
    if (!packet || !native_multi_active(&topology)) return -1;
    DISPLAYCONFIG_DEVICE_INFO_HEADER header;
    SIZE_T bytes = 0;
    if (!ReadProcessMemory(GetCurrentProcess(), packet, &header, sizeof(header), &bytes) ||
        bytes != sizeof(header)) {
        *result = ERROR_INVALID_PARAMETER;
        return 1;
    }
    LUID luid = native_multi_luid();
    if (!native_multi_same_luid(header.adapterId, luid)) return -1;
    int32_t type = (int32_t)header.type;
    UINT source_id = header.id ? header.id - 1 : UINT_MAX;
    UINT target_id = header.id >= UURB_MULTI_TARGET_BASE ? header.id - UURB_MULTI_TARGET_BASE : UINT_MAX;
    int source_index = native_multi_index_for_id(&topology, source_id);
    int target_index = native_multi_index_for_id(&topology, target_id);
    if (type == DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME) {
        if (is_set || header.size != sizeof(DISPLAYCONFIG_SOURCE_DEVICE_NAME) ||
            source_index < 0) {
            *result = ERROR_INVALID_PARAMETER; return 1;
        }
        DISPLAYCONFIG_SOURCE_DEVICE_NAME value;
        ZeroMemory(&value, sizeof(value));
        value.header = header;
        value.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME;
        value.header.size = sizeof(value);
        native_multi_utf8(topology.monitors[source_index].gdi_name,
                          value.viewGdiDeviceName, ARRAYSIZE(value.viewGdiDeviceName));
        if (!WriteProcessMemory(GetCurrentProcess(), packet, &value, sizeof(value), &bytes) ||
            bytes != sizeof(value)) { *result = ERROR_INVALID_PARAMETER; return 1; }
        *result = ERROR_SUCCESS; return 1;
    }
    if (type == DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME) {
        if (is_set || header.size != sizeof(DISPLAYCONFIG_TARGET_DEVICE_NAME) ||
            target_index < 0) {
            *result = ERROR_INVALID_PARAMETER; return 1;
        }
        const struct uurb_display_monitor *monitor =
            &topology.monitors[target_index];
        DISPLAYCONFIG_TARGET_DEVICE_NAME value;
        ZeroMemory(&value, sizeof(value));
        value.header = header;
        value.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME;
        value.header.size = sizeof(value);
        native_multi_utf8(monitor->device_path, value.monitorDevicePath,
                          ARRAYSIZE(value.monitorDevicePath));
        native_multi_utf8(monitor->friendly, value.monitorFriendlyDeviceName,
                          ARRAYSIZE(value.monitorFriendlyDeviceName));
        if (!WriteProcessMemory(GetCurrentProcess(), packet, &value, sizeof(value), &bytes) ||
            bytes != sizeof(value)) { *result = ERROR_INVALID_PARAMETER; return 1; }
        *result = ERROR_SUCCESS; return 1;
    }
    if (type == DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO) {
        if (is_set || header.size != sizeof(DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO)) {
            *result = ERROR_INVALID_PARAMETER; return 1;
        }
        DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO value;
        ZeroMemory(&value, sizeof(value));
        value.header = header;
        value.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO;
        value.header.size = sizeof(value);
        value.advancedColorSupported = 0;
        value.advancedColorEnabled = 0;
        value.colorEncoding = DISPLAYCONFIG_COLOR_ENCODING_RGB;
        value.bitsPerColorChannel = 8;
        if (!WriteProcessMemory(GetCurrentProcess(), packet, &value, sizeof(value), &bytes) ||
            bytes != sizeof(value)) { *result = ERROR_INVALID_PARAMETER; return 1; }
        *result = ERROR_SUCCESS; return 1;
    }
    if (type == -3 || type == -4) {
        if ((is_set ? type != -4 : type != -3) || source_index < 0) {
            *result = ERROR_NOT_SUPPORTED; return 1;
        }
        const struct uurb_display_monitor *monitor = &topology.monitors[source_index];
        if (is_set) {
            if (!native_display_reconfigure_enabled || header.size != sizeof(struct uurb_native_dpi_set)) {
                *result = ERROR_NOT_SUPPORTED; return 1;
            }
            struct uurb_native_dpi_set value;
            if (!ReadProcessMemory(GetCurrentProcess(), packet, &value, sizeof(value), &bytes) ||
                bytes != sizeof(value)) { *result = ERROR_INVALID_PARAMETER; return 1; }
            int target = value.scale_rel;
            if (target < 0 || target >= (int)UURB_DISPLAY_SCALE_COUNT ||
                !(monitor->scale_mask & (1u << (unsigned)target))) {
                *result = ERROR_BAD_CONFIGURATION; return 1;
            }
            uint32_t scale = uurb_display_scale_milli((unsigned)target);
            if (scale == monitor->scale_milli) { *result = ERROR_SUCCESS; return 1; }
            struct uurb_display_request request = {.version = 1, .operation = UURB_DISPLAY_APPLY,
                .serial = topology.serial, .width = monitor->width, .height = monitor->height,
                .refresh_millihz = monitor->refresh_millihz, .scale_milli = scale,
                .reserved = UURB_DISPLAY_OUTPUT_SELECTED | ((UINT)source_index << UURB_DISPLAY_OUTPUT_SHIFT)};
            struct uurb_display_reply reply;
            DWORD status = native_display_change_api(native_display_endpoint, &request, &reply);
            *result = !status ? ERROR_SUCCESS : status == UURB_DISPLAY_BAD_MODE ? ERROR_BAD_CONFIGURATION : ERROR_GEN_FAILURE;
            if (!status) {
                AcquireSRWLockExclusive(&native_multi_lock); native_multi_time = 0; ReleaseSRWLockExclusive(&native_multi_lock);
                SendNotifyMessageW(HWND_BROADCAST, WM_DISPLAYCHANGE, 32, MAKELPARAM(monitor->width, monitor->height));
            }
            return 1;
        }
        if (header.size != sizeof(struct uurb_native_dpi_get)) {
            *result = ERROR_NOT_SUPPORTED; return 1;
        }
        unsigned minimum = 0, maximum = UURB_DISPLAY_SCALE_COUNT - 1;
        while (minimum < UURB_DISPLAY_SCALE_COUNT && !(monitor->scale_mask & (1u << minimum))) ++minimum;
        while (maximum > 0 && !(monitor->scale_mask & (1u << maximum))) --maximum;
        uint32_t interval = minimum <= maximum ? (((1u << (maximum - minimum + 1)) - 1u) << minimum) : 0;
        if (!interval || monitor->scale_mask != interval || minimum != 0) {
            *result = ERROR_NOT_SUPPORTED; return 1;
        }
        int current = uurb_display_scale_index(monitor->scale_milli);
        if (current < 0 || !(monitor->scale_mask & (1u << current))) {
            *result = ERROR_NOT_SUPPORTED; return 1;
        }
        struct uurb_native_dpi_get value;
        if (!ReadProcessMemory(GetCurrentProcess(), packet, &value, sizeof(value), &bytes) ||
            bytes != sizeof(value)) { *result = ERROR_INVALID_PARAMETER; return 1; }
        value.min_scale_rel = (int)minimum;
        value.cur_scale_rel = current;
        value.max_scale_rel = (int)maximum;
        if (!WriteProcessMemory(GetCurrentProcess(), packet, &value, sizeof(value), &bytes) ||
            bytes != sizeof(value)) { *result = ERROR_INVALID_PARAMETER; return 1; }
        *result = ERROR_SUCCESS; return 1;
    }
    return -1;
}

#endif
