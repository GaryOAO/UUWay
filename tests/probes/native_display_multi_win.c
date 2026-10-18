/* Offline probe for the multi-output Win32 display shim.
 *
 * Run it with the injected bridge and a display endpoint (the real service or
 * a synthetic fixture).  It never changes a display mode; it only enumerates.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

static void print_settings(LPCWSTR device)
{
    DEVMODEW mode;
    ZeroMemory(&mode, sizeof(mode));
    mode.dmSize = sizeof(mode);
    BOOL ok = EnumDisplaySettingsW(device, ENUM_CURRENT_SETTINGS, &mode);
    wprintf(L"  settings(%ls) ok=%d %lux%lu@%lu pos=%ld,%ld fields=0x%lx\n",
            device ? device : L"<null>", ok, mode.dmPelsWidth, mode.dmPelsHeight,
            mode.dmDisplayFrequency, mode.dmPosition.x, mode.dmPosition.y, mode.dmFields);
    ZeroMemory(&mode, sizeof(mode));
    mode.dmSize = sizeof(mode);
    ok = EnumDisplaySettingsW(device, 1, &mode);
    wprintf(L"  settings-mode1(%ls) ok=%d %lux%lu@%lu pos=%ld,%ld\n",
            device ? device : L"<null>", ok, mode.dmPelsWidth, mode.dmPelsHeight,
            mode.dmDisplayFrequency, mode.dmPosition.x, mode.dmPosition.y);
}

static void print_devices(LPCWSTR parent)
{
    for (DWORD i = 0; i < 4; ++i) {
        DISPLAY_DEVICEW device;
        ZeroMemory(&device, sizeof(device));
        device.cb = sizeof(device);
        BOOL ok = EnumDisplayDevicesW(parent, i, &device, 0);
        wprintf(L"device(%ls,%lu) ok=%d name=%ls string=%ls flags=0x%lx\n",
                parent ? parent : L"<null>", i, ok, device.DeviceName, device.DeviceString,
                device.StateFlags);
        if (ok && !parent) print_settings(device.DeviceName);
    }
}

static BOOL CALLBACK monitor_callback(HMONITOR monitor, HDC dc, LPRECT rectangle, LPARAM data)
{
    (void)dc; (void)data;
    MONITORINFOEXW info;
    ZeroMemory(&info, sizeof(info));
    info.cbSize = sizeof(info);
    BOOL ok = GetMonitorInfoW(monitor, (LPMONITORINFO)&info);
    wprintf(L"monitor h=%p ok=%d name=%ls rect=%ld,%ld,%ld,%ld work=%ld,%ld,%ld,%ld flags=0x%lx\n",
            monitor, ok, info.szDevice, info.rcMonitor.left, info.rcMonitor.top,
            info.rcMonitor.right, info.rcMonitor.bottom, info.rcWork.left, info.rcWork.top,
            info.rcWork.right, info.rcWork.bottom, info.dwFlags);
    wprintf(L"  callback-rect=%ld,%ld,%ld,%ld\n", rectangle->left, rectangle->top,
            rectangle->right, rectangle->bottom);
    return TRUE;
}

int wmain(int argc, wchar_t **argv)
{
    if (argc != 2) {
        fwprintf(stderr, L"Usage: %ls <display-socket>\n", argv[0]);
        return 2;
    }
    WCHAR bridge[32768];
    DWORD length = GetModuleFileNameW(NULL, bridge, ARRAYSIZE(bridge));
    if (!length || length >= ARRAYSIZE(bridge)) return 2;
    WCHAR *slash = wcsrchr(bridge, L'\\');
    if (!slash || (size_t)(slash + 1 - bridge) + ARRAYSIZE(L"uu-native-input-bridge.dll") > ARRAYSIZE(bridge)) return 2;
    wcscpy(slash + 1, L"uu-native-input-bridge.dll");
    LPCWSTR endpoint = argv[1];
    SetEnvironmentVariableW(L"UURB_DISPLAY_SOCKET", endpoint);
    /* The bridge only patches vendor-module display imports after it has a
     * real SendInput slot in the main executable. */
    SendInput(0, NULL, 0);
    HMODULE module = LoadLibraryW(bridge);
    wprintf(L"bridge=%p error=%lu endpoint=%ls\n", module, GetLastError(), endpoint);
    if (!module) return 3;
    Sleep(1500);

    wprintf(L"--- adapters\n");
    print_devices(NULL);
    wprintf(L"--- DISPLAY1 monitors\n");
    print_devices(L"\\\\.\\DISPLAY1");
    wprintf(L"--- DISPLAY2 monitors\n");
    print_devices(L"\\\\.\\DISPLAY2");
    wprintf(L"--- monitors\n");
    EnumDisplayMonitors(NULL, NULL, monitor_callback, 0);

    wprintf(L"metrics cx=%d cy=%d vx=%d vy=%d vcx=%d vcy=%d count=%d\n",
            GetSystemMetrics(SM_CXSCREEN), GetSystemMetrics(SM_CYSCREEN),
            GetSystemMetrics(SM_XVIRTUALSCREEN), GetSystemMetrics(SM_YVIRTUALSCREEN),
            GetSystemMetrics(SM_CXVIRTUALSCREEN), GetSystemMetrics(SM_CYVIRTUALSCREEN),
            GetSystemMetrics(SM_CMONITORS));
    RECT work = {0};
    BOOL spi = SystemParametersInfoW(SPI_GETWORKAREA, 0, &work, 0);
    wprintf(L"workarea ok=%d %ld,%ld,%ld,%ld\n", spi, work.left, work.top, work.right, work.bottom);

    POINT point = {2000, 10};
    HMONITOR nearest = MonitorFromPoint(point, MONITOR_DEFAULTTONEAREST);
    MONITORINFOEXW info;
    ZeroMemory(&info, sizeof(info));
    info.cbSize = sizeof(info);
    BOOL nearest_ok = nearest && GetMonitorInfoW(nearest, (LPMONITORINFO)&info);
    wprintf(L"nearest(2000,10)=%p ok=%d name=%ls\n", nearest, nearest_ok, info.szDevice);

    UINT32 paths = 0, modes = 0;
    LONG sizes = GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, &paths, &modes);
    wprintf(L"buffers result=%ld paths=%lu modes=%lu\n", sizes, paths, modes);
    if (!sizes && paths && modes) {
        DISPLAYCONFIG_PATH_INFO *path = calloc(paths, sizeof(*path));
        DISPLAYCONFIG_MODE_INFO *mode = calloc(modes, sizeof(*mode));
        if (path && mode) {
            LONG query = QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, &paths, path, &modes, mode, NULL);
            wprintf(L"query result=%ld paths=%lu modes=%lu\n", query, paths, modes);
            for (UINT32 i = 0; i < paths; ++i) {
                DISPLAYCONFIG_SOURCE_DEVICE_NAME source;
                ZeroMemory(&source, sizeof(source));
                source.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME;
                source.header.size = sizeof(source);
                source.header.adapterId = path[i].sourceInfo.adapterId;
                source.header.id = path[i].sourceInfo.id;
                LONG source_result = DisplayConfigGetDeviceInfo(&source.header);
                DISPLAYCONFIG_TARGET_DEVICE_NAME target;
                ZeroMemory(&target, sizeof(target));
                target.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME;
                target.header.size = sizeof(target);
                target.header.adapterId = path[i].targetInfo.adapterId;
                target.header.id = path[i].targetInfo.id;
                LONG target_result = DisplayConfigGetDeviceInfo(&target.header);
                wprintf(L"  path %lu source=%ld/%ls target=%ld/%ls/%ls\n", i, source_result,
                        source.viewGdiDeviceName, target_result, target.monitorDevicePath,
                        target.monitorFriendlyDeviceName);
            }
        } else {
            wprintf(L"allocation failed\n");
        }
        free(path); free(mode);
    }
    return 0;
}
