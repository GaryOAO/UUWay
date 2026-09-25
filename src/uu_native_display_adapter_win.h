#ifndef UURB_NATIVE_DISPLAY_ADAPTER_WIN_H
#define UURB_NATIVE_DISPLAY_ADAPTER_WIN_H
#include "native_display_api.h"
typedef DWORD (WINAPI *native_display_query_fn)(const char *, struct uurb_display_snapshot *, DWORD);
typedef DWORD (WINAPI *native_display_change_fn)(const char *, const struct uurb_display_request *, struct uurb_display_reply *);
typedef DWORD (WINAPI *native_display_default_fn)(const char *, struct uurb_display_mode *);
static native_display_query_fn native_display_query_api;
static native_display_change_fn native_display_change_api;
static native_display_default_fn native_display_default_api;
static char native_display_endpoint[108];
static struct uurb_display_snapshot native_display_cache;
static struct uurb_display_mode native_display_initial;
static SRWLOCK native_display_cache_lock = SRWLOCK_INIT;
static ULONGLONG native_display_cache_time;
static BOOL native_display_adapter_enabled;

static int native_display_adapter_initialize(void)
{
    WCHAR endpoint[108], location[32768];
    DWORD size = GetEnvironmentVariableW(L"UURB_DISPLAY_SOCKET", endpoint, ARRAYSIZE(endpoint));
    if (!size) return 0;  /* Passthrough remains the explicit default. */
    if (size >= ARRAYSIZE(endpoint) || endpoint[0] != L'/' ||
        !WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, endpoint, -1, native_display_endpoint,
                             sizeof(native_display_endpoint), NULL, NULL)) return -1;
    HMODULE self, loader;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        (LPCWSTR)(uintptr_t)&native_display_adapter_enabled, &self)) return -1;
    size = GetModuleFileNameW(self, location, ARRAYSIZE(location));
    if (!size || size >= ARRAYSIZE(location)) return -1;
    WCHAR *base = wcsrchr(location, L'\\');
    const WCHAR filename[] = L"uurb-native-display-loader.dll";
    if (!base || (size_t)(++base - location) + ARRAYSIZE(filename) > ARRAYSIZE(location)) return -1;
    memcpy(base, filename, sizeof(filename));
    loader = LoadLibraryW(location);
    if (!loader) return -1;
    native_display_query_api = (void *)GetProcAddress(loader, "UurbDisplayQueryV2");
    native_display_change_api = (void *)GetProcAddress(loader, "UurbDisplayChange");
    native_display_default_api = (void *)GetProcAddress(loader, "UurbDisplayDefault");
    if (!native_display_query_api || !native_display_change_api || !native_display_default_api ||
        native_display_query_api(native_display_endpoint, &native_display_cache, sizeof(native_display_cache)) ||
        native_display_cache.version != UURB_DISPLAY_SNAPSHOT_VERSION || !native_display_cache.count ||
        native_display_cache.count > UURB_DISPLAY_MAX_MODES) return -1;
    native_display_initial = native_display_cache.current;
    native_display_cache_time = GetTickCount64();
    native_display_adapter_enabled = TRUE;
    return 1;
}
static BOOL native_display_refresh(BOOL force)
{
    ULONGLONG now = GetTickCount64();
    if (!force && now - native_display_cache_time < 250) return TRUE;
    struct uurb_display_snapshot value;
    if (!native_display_query_api || native_display_query_api(native_display_endpoint, &value, sizeof(value)) ||
        value.version != UURB_DISPLAY_SNAPSHOT_VERSION ||
        !value.count || value.count > UURB_DISPLAY_MAX_MODES) return FALSE;
    native_display_cache = value; native_display_cache_time = now;
    return TRUE;
}
static BOOL native_display_enum_mode(LPCWSTR device, DWORD index, DEVMODEW *output, DWORD flags)
{
    if (!display_primary(device) || !output || output->dmSize != sizeof(DEVMODEW) || output->dmDriverExtra ||
        (flags & ~(EDS_RAWMODE | EDS_ROTATEDMODE))) { SetLastError(ERROR_INVALID_PARAMETER); return FALSE; }
    BOOL result = FALSE;
    AcquireSRWLockExclusive(&native_display_cache_lock);
    if ((index == 0 || index == ENUM_CURRENT_SETTINGS) && !native_display_refresh(index == 0)) goto done;
    struct uurb_display_mode selected;
    if (index == ENUM_CURRENT_SETTINGS) selected = native_display_cache.current;
    else if (index == ENUM_REGISTRY_SETTINGS) {
        if (native_display_default_api(native_display_endpoint, &selected)) goto done;
    }
    else {
        if (index >= native_display_cache.count) goto done;
        selected = native_display_cache.modes[index];
    }
    DEVMODEW mode = {0};
    mode.dmSize = sizeof(mode); mode.dmSpecVersion = DM_SPECVERSION;
    wcscpy(mode.dmDeviceName, L"UURB Native Display");
    mode.dmFields = DM_BITSPERPEL | DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY | DM_DISPLAYFLAGS |
                    DM_POSITION | DM_DISPLAYORIENTATION | DM_DISPLAYFIXEDOUTPUT;
    mode.dmBitsPerPel = 32; mode.dmPelsWidth = selected.width; mode.dmPelsHeight = selected.height;
    mode.dmDisplayFrequency = (selected.refresh_millihz + 500) / 1000;
    mode.dmDisplayOrientation = DMDO_DEFAULT; mode.dmDisplayFixedOutput = DMDFO_DEFAULT;
    *output = mode; result = TRUE;
done:
    ReleaseSRWLockExclusive(&native_display_cache_lock);
    if (!result) SetLastError(ERROR_NO_MORE_ITEMS);
    return result;
}
static LONG native_display_change_mode(LPCWSTR device, DEVMODEW *mode, HWND window, DWORD flags, LPVOID extra)
{
    if (!display_primary(device) || window || extra) return DISP_CHANGE_BADPARAM;
    if (flags & CDS_NORESET) return DISP_CHANGE_NOTUPDATED;
    if ((flags & CDS_GLOBAL) && !(flags & CDS_UPDATEREGISTRY)) return DISP_CHANGE_BADFLAGS;
    if (flags & ~(CDS_TEST | CDS_FULLSCREEN | CDS_RESET | CDS_UPDATEREGISTRY | CDS_GLOBAL)) return DISP_CHANGE_BADFLAGS;
    DEVMODEW copied;
    if (mode) {
        SIZE_T bytes;
        if (!ReadProcessMemory(GetCurrentProcess(), mode, &copied, sizeof(copied), &bytes) || bytes != sizeof(copied) ||
            copied.dmSize != sizeof(copied) || copied.dmDriverExtra) return DISP_CHANGE_BADPARAM;
        mode = &copied;
        DWORD allowed = DM_BITSPERPEL | DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY | DM_DISPLAYFLAGS |
                        DM_POSITION | DM_DISPLAYORIENTATION | DM_DISPLAYFIXEDOUTPUT;
        if (mode->dmFields & ~allowed) return DISP_CHANGE_BADPARAM;
        if (((mode->dmFields & DM_BITSPERPEL) && mode->dmBitsPerPel != 32) ||
            ((mode->dmFields & DM_DISPLAYFLAGS) && mode->dmDisplayFlags) ||
            ((mode->dmFields & DM_POSITION) && (mode->dmPosition.x || mode->dmPosition.y)) ||
            ((mode->dmFields & DM_DISPLAYORIENTATION) && mode->dmDisplayOrientation != DMDO_DEFAULT) ||
            ((mode->dmFields & DM_DISPLAYFIXEDOUTPUT) && mode->dmDisplayFixedOutput != DMDFO_DEFAULT)) return DISP_CHANGE_BADMODE;
    } else if (flags) return DISP_CHANGE_BADPARAM;
    LONG result = DISP_CHANGE_FAILED;
    BOOL notify_change = FALSE;
    DWORD changed_width = 0, changed_height = 0;
    AcquireSRWLockExclusive(&native_display_cache_lock);
    if (!native_display_refresh(TRUE)) goto done;
    struct uurb_display_mode current = native_display_cache.current;
    if (!mode && native_display_default_api(native_display_endpoint, &current)) goto done;
    struct uurb_display_request request = {.version = 1,
        .operation = flags & CDS_TEST ? UURB_DISPLAY_VERIFY : !(flags & CDS_UPDATEREGISTRY) ? UURB_DISPLAY_APPLY :
            flags & CDS_GLOBAL ? UURB_DISPLAY_APPLY_GLOBAL_DEFAULT : UURB_DISPLAY_APPLY_USER_DEFAULT,
        .serial = native_display_cache.serial, .width = current.width, .height = current.height,
        .refresh_millihz = current.refresh_millihz,
        .reserved = !(flags & CDS_TEST) && (flags & CDS_RESET) ? UURB_DISPLAY_FORCE_RESET : 0};
    if (mode) {
        if (mode->dmFields & (DM_PELSWIDTH | DM_PELSHEIGHT)) request.refresh_millihz = 0;
        if (mode->dmFields & DM_PELSWIDTH) request.width = mode->dmPelsWidth;
        if (mode->dmFields & DM_PELSHEIGHT) request.height = mode->dmPelsHeight;
        if (mode->dmFields & DM_DISPLAYFREQUENCY) {
            if (mode->dmDisplayFrequency > 120) { result = DISP_CHANGE_BADMODE; goto done; }
            request.refresh_millihz = mode->dmDisplayFrequency <= 1 ? 0 : mode->dmDisplayFrequency * 1000;
        }
    }
    struct uurb_display_reply reply;
    DWORD status = native_display_change_api(native_display_endpoint, &request, &reply);
    if (status == UURB_DISPLAY_BAD_MODE || status == UURB_DISPLAY_INVALID) result = DISP_CHANGE_BADMODE;
    else if (status == UURB_DISPLAY_DEFAULT_FAILED) result = DISP_CHANGE_NOTUPDATED;
    else if (!status) {
        result = DISP_CHANGE_SUCCESSFUL;
        if (reply.changed) {
            native_display_cache_time = 0;
            notify_change = TRUE;
            changed_width = request.width; changed_height = request.height;
        }
    }
done:
    ReleaseSRWLockExclusive(&native_display_cache_lock);
    /* Same-thread window procedures can run during SendNotifyMessage. They
     * may query geometry immediately; never notify while holding its lock. */
    if (notify_change)
        SendNotifyMessageW(HWND_BROADCAST, WM_DISPLAYCHANGE, 32, MAKELPARAM(changed_width, changed_height));
    return result;
}
#endif
