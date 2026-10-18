/* Explicit live test of the standard Win32 IDD boundary. Run only in a
 * disposable Wine prefix with the UUWay display service endpoint passed in.
 * The caller must reserve an unused virtual slot and clean it up on failure.
 * No account files or input events are read/sent. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <setupapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>
#define CHECK(value) do { if (!(value)) { fprintf(stderr, "live virtual check failed line %d error %lu\n", __LINE__, GetLastError()); return 1; } } while (0)
static const GUID interface_id = {0xc5710012,0xf569,0x4079,{0x84,0x14,0x56,0x4a,0xb6,0x32,0x83,0x47}};
static BOOL find_virtual(WCHAR *name, DWORD width, DWORD height)
{
    for (DWORD i = 0; i < 8; ++i) {
        DISPLAY_DEVICEW device = {.cb = sizeof(device)};
        if (!EnumDisplayDevicesW(NULL, i, &device, 0)) break;
        if (wcscmp(device.DeviceString, L"GameViewer Virtual Display Adapter")) continue;
        DEVMODEW mode = {.dmSize = sizeof(mode)};
        if (!EnumDisplaySettingsW(device.DeviceName, ENUM_CURRENT_SETTINGS, &mode)) continue;
        if (mode.dmPelsWidth == width && mode.dmPelsHeight == height) {
            wcscpy(name, device.DeviceName); return TRUE;
        }
    }
    return FALSE;
}
static BOOL wait_virtual(WCHAR *name, DWORD width, DWORD height)
{
    for (unsigned i = 0; i < 30; ++i) {
        if (find_virtual(name, width, height)) return TRUE;
        Sleep(100);
    }
    return FALSE;
}
int wmain(int argc, WCHAR **argv)
{
    if (argc != 3) return 2;
    DWORD slot = wcstoul(argv[2], NULL, 10);
    if (slot >= 8) return 2;
    SetEnvironmentVariableW(L"UURB_DISPLAY_SOCKET", argv[1]);
    SetEnvironmentVariableW(L"UURB_DISPLAY_ALLOW_CHANGES", L"1");
    SendInput(0, NULL, 0); /* Import slot only; no input is emitted. */
    CHECK(LoadLibraryW(L"uu-native-input-bridge.dll"));
    Sleep(1500);
    int before = GetSystemMetrics(SM_CMONITORS);
    UINT32 original_paths = 8, original_modes = 24;
    DISPLAYCONFIG_PATH_INFO saved_paths[8]; DISPLAYCONFIG_MODE_INFO saved_modes[24];
    CHECK(QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, &original_paths, saved_paths,
                             &original_modes, saved_modes, NULL) == ERROR_SUCCESS);
    HDEVINFO set = SetupDiGetClassDevsW(&interface_id, NULL, NULL, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE);
    CHECK(set != INVALID_HANDLE_VALUE);
    SP_DEVICE_INTERFACE_DATA data = {.cbSize = sizeof(data)};
    CHECK(SetupDiEnumDeviceInterfaces(set, NULL, &interface_id, 0, &data));
    BYTE storage[256] = {0}; PSP_DEVICE_INTERFACE_DETAIL_DATA_W detail = (void *)storage;
    detail->cbSize = sizeof(*detail);
    CHECK(SetupDiGetDeviceInterfaceDetailW(set, &data, detail, sizeof(storage), NULL, NULL));
    HANDLE handle = CreateFileW(detail->DevicePath, GENERIC_READ | GENERIC_WRITE, 0, NULL, OPEN_EXISTING, 0, NULL);
    CHECK(handle != INVALID_HANDLE_VALUE);
    DWORD returned = 0;
    DWORD modes[] = {slot, 2, 1280, 720, 60, 1600, 900, 60};
    CHECK(DeviceIoControl(handle, 0x30c010, modes, sizeof(modes), NULL, 0, &returned, NULL));
    DWORD plug[] = {slot, 0, 0, 0, 0, 0};
    CHECK(DeviceIoControl(handle, 0x30c004, plug, sizeof(plug), NULL, 0, &returned, NULL));
    WCHAR name[32];
    CHECK(wait_virtual(name, 1280, 720));
    CHECK(GetSystemMetrics(SM_CMONITORS) == before + 1);
    DWORD resized[] = {slot, 1, 1600, 900, 60};
    CHECK(DeviceIoControl(handle, 0x30c00c, resized, sizeof(resized), NULL, 0, &returned, NULL));
    CHECK(wait_virtual(name, 1600, 900));
    UINT32 path_count = 8, mode_count = 24;
    DISPLAYCONFIG_PATH_INFO paths[8]; DISPLAYCONFIG_MODE_INFO configuration[24];
    CHECK(QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, &path_count, paths, &mode_count, configuration, NULL) == ERROR_SUCCESS);
    DISPLAYCONFIG_PATH_INFO selected = {0};
    for (UINT32 i = 0; i < path_count; ++i) {
        DISPLAYCONFIG_SOURCE_DEVICE_NAME source = {.header = {.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME,
            .size = sizeof(source), .adapterId = paths[i].sourceInfo.adapterId, .id = paths[i].sourceInfo.id}};
        CHECK(DisplayConfigGetDeviceInfo(&source.header) == ERROR_SUCCESS);
        if (!wcscmp(name, source.viewGdiDeviceName)) selected = paths[i];
    }
    CHECK(selected.flags & DISPLAYCONFIG_PATH_ACTIVE);
    configuration[selected.sourceInfo.modeInfoIdx].sourceMode.position = (POINTL){0, 0};
    UINT32 flags = SDC_USE_SUPPLIED_DISPLAY_CONFIG | SDC_APPLY | SDC_SAVE_TO_DATABASE |
                   SDC_ALLOW_CHANGES | SDC_FORCE_MODE_ENUMERATION;
    CHECK(SetDisplayConfig(1, &selected, mode_count, configuration, flags) == ERROR_SUCCESS);
    Sleep(650);
    CHECK(GetSystemMetrics(SM_CMONITORS) == 1);
    WCHAR exclusive_name[32];
    CHECK(wait_virtual(exclusive_name, 1600, 900) && !wcscmp(name, exclusive_name));
    CHECK(SetDisplayConfig(original_paths, saved_paths, original_modes, saved_modes, flags) == ERROR_SUCCESS);
    Sleep(650);
    CHECK(GetSystemMetrics(SM_CMONITORS) == before + 1);
    CHECK(DeviceIoControl(handle, 0x30c008, &slot, sizeof(slot), NULL, 0, &returned, NULL));
    Sleep(600);
    CHECK(GetSystemMetrics(SM_CMONITORS) == before);
    CHECK(CloseHandle(handle)); CHECK(SetupDiDestroyDeviceInfoList(set));
    puts("LIVE Win32 IDD create, resize, exclusive CCD enter/restore, stable identity and remove passed");
    return 0;
}
