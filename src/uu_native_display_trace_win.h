#ifndef UURB_NATIVE_DISPLAY_TRACE_WIN_H
#define UURB_NATIVE_DISPLAY_TRACE_WIN_H
#include "uu_native_virtual_trace_win.h"
#include "uu_native_terminal_trace_win.h"
/* Observe standard display boundaries without changing their behavior.
 * Bounded numeric diagnostics only: never log device names/serials or pointers.
 * Originals, return values, output buffers and thread last-error are preserved.
 */
static BOOL (WINAPI *display_enum)(LPCWSTR, DWORD, DEVMODEW *);
static BOOL (WINAPI *display_enum_ex)(LPCWSTR, DWORD, DEVMODEW *, DWORD);
static LONG (WINAPI *display_change)(LPCWSTR, DEVMODEW *, HWND, DWORD, LPVOID);
static LONG (WINAPI *display_query)(UINT32, UINT32 *, DISPLAYCONFIG_PATH_INFO *, UINT32 *, DISPLAYCONFIG_MODE_INFO *, DISPLAYCONFIG_TOPOLOGY_ID *);
static LONG (WINAPI *display_set)(UINT32, DISPLAYCONFIG_PATH_INFO *, UINT32, DISPLAYCONFIG_MODE_INFO *, UINT32);
static FARPROC (WINAPI *display_getproc)(HMODULE, LPCSTR);
static volatile LONG display_trace_calls[5], native_display_import_slots;

static void display_trace(unsigned api, DWORD flags, LONG result, DWORD width, DWORD height, DWORD hz,
                          DWORD fields, BOOL primary)
{
    DWORD saved = GetLastError();
    LONG sequence = InterlockedIncrement(&display_trace_calls[api]);
    if (sequence <= ((api == 2 || api == 4) ? 64 : 16)) {
        static const char *names[] = {"EnumDisplaySettingsW", "EnumDisplaySettingsExW", "ChangeDisplaySettingsExW",
                                     "QueryDisplayConfig", "SetDisplayConfig"};
        char line[384];
        snprintf(line, sizeof(line), "UURB_NATIVE_DISPLAY {\"api\":\"%s\",\"call\":%ld,\"flags\":%lu,"
            "\"result\":%ld,\"width\":%lu,\"height\":%lu,\"refresh_hz\":%lu,\"fields\":%lu,\"primary_device\":%s}\r\n",
            names[api], sequence, flags, result, width, height, hz, fields, primary ? "true" : "false");
        write_log(line); flush_log();
    }
    SetLastError(saved);
}
static BOOL display_primary(LPCWSTR device)
{
    if (!device) return TRUE;
    const WCHAR primary[] = L"\\\\.\\DISPLAY1";
    WCHAR copy[ARRAYSIZE(primary)];
    SIZE_T bytes = 0;
    return ReadProcessMemory(GetCurrentProcess(), device, copy, sizeof(copy), &bytes) &&
           bytes == sizeof(copy) && copy[ARRAYSIZE(copy) - 1] == 0 && !_wcsicmp(copy, primary);
}
#include "uu_native_display_adapter_win.h"
#include "uu_native_display_geometry_win.h"
#include "uu_native_display_set_win.h"
static void display_mode_trace(unsigned api, DWORD flags, LONG result, const DEVMODEW *mode, LPCWSTR device)
{
    DWORD saved = GetLastError();
    DEVMODEW copy;
    SIZE_T bytes = 0;
    BOOL readable = mode && ReadProcessMemory(GetCurrentProcess(), mode, &copy, sizeof(copy), &bytes) && bytes == sizeof(copy);
    DWORD fields = readable && copy.dmSize >= sizeof(DEVMODEW) ? copy.dmFields : 0;
    display_trace(api, flags, result,
        fields & DM_PELSWIDTH ? copy.dmPelsWidth : 0,
        fields & DM_PELSHEIGHT ? copy.dmPelsHeight : 0,
        fields & DM_DISPLAYFREQUENCY ? copy.dmDisplayFrequency : 0, fields, display_primary(device));
    SetLastError(saved);
}
static BOOL WINAPI traced_display_enum(LPCWSTR name, DWORD index, DEVMODEW *mode)
{
    BOOL result = native_display_adapter_enabled ? native_display_enum_mode(name, index, mode, 0) : display_enum(name, index, mode);
    display_mode_trace(0, index, result, result ? mode : NULL, name);
    return result;
}
static BOOL WINAPI traced_display_enum_ex(LPCWSTR name, DWORD index, DEVMODEW *mode, DWORD flags)
{
    BOOL result = native_display_adapter_enabled ? native_display_enum_mode(name, index, mode, flags) : display_enum_ex(name, index, mode, flags);
    display_mode_trace(1, flags, result, result ? mode : NULL, name);
    return result;
}
static LONG WINAPI traced_display_change(LPCWSTR name, DEVMODEW *mode, HWND window, DWORD flags, LPVOID extra)
{
    LONG result = native_display_adapter_enabled ? native_display_change_mode(name, mode, window, flags, extra) : display_change(name, mode, window, flags, extra);
    display_mode_trace(2, flags, result, mode, name);
    return result;
}
static LONG WINAPI traced_display_query(UINT32 flags, UINT32 *paths, DISPLAYCONFIG_PATH_INFO *path,
                                        UINT32 *modes, DISPLAYCONFIG_MODE_INFO *mode, DISPLAYCONFIG_TOPOLOGY_ID *topology)
{
    LONG result = display_query(flags, paths, path, modes, mode, topology);
    DWORD saved = GetLastError();
    if (result == ERROR_SUCCESS && native_display_adapter_enabled)
        result = native_display_query_geometry(flags, *paths, path, *modes, mode);
    SetLastError(saved);
    display_trace(3, flags, result, 0, 0, 0, 0, FALSE);
    return result;
}
static LONG WINAPI traced_display_set(UINT32 paths, DISPLAYCONFIG_PATH_INFO *path,
                                     UINT32 modes, DISPLAYCONFIG_MODE_INFO *mode, UINT32 flags)
{
    LONG result = native_display_adapter_enabled ? native_display_set_config(paths, path, modes, mode, flags) : display_set(paths, path, modes, mode, flags);
    display_trace(4, flags, result, 0, 0, 0, 0, FALSE);
    return result;
}
static void *native_display_hook_for(uintptr_t original)
{
    if (!original) return NULL;
#define MAP(original_fn, hook_fn) if (original == (uintptr_t)original_fn) return (void *)&hook_fn
    MAP(display_enum, traced_display_enum);
    MAP(display_enum_ex, traced_display_enum_ex);
    MAP(display_change, traced_display_change);
    MAP(display_query, traced_display_query);
    MAP(display_set, traced_display_set);
#undef MAP
    void *geometry = native_geometry_hook_for(original);
    if (geometry) return geometry;
    void *terminal = native_terminal_hook_for(original);
    if (terminal) return terminal;
    return native_virtual_hook_for(original);
}
static FARPROC WINAPI traced_display_getproc(HMODULE module, LPCSTR name)
{
    FARPROC original = display_getproc(module, name);
    DWORD saved = GetLastError();
    void *replacement = native_display_hook_for((uintptr_t)original);
    SetLastError(saved);
    return replacement ? (FARPROC)replacement : original;
}
static void native_display_trace_initialize(void)
{
    native_virtual_trace_initialize();
    native_terminal_trace_initialize();
    native_geometry_initialize();
    HMODULE module = GetModuleHandleW(L"user32.dll");
    display_getproc = (void *)GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "GetProcAddress");
#define GET(variable, name) variable = (void *)GetProcAddress(module, name)
    GET(display_enum, "EnumDisplaySettingsW");
    GET(display_enum_ex, "EnumDisplaySettingsExW");
    GET(display_change, "ChangeDisplaySettingsExW");
    GET(display_query, "QueryDisplayConfig");
    GET(display_set, "SetDisplayConfig");
#undef GET
}
#endif
