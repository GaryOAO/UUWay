#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include "native_display_api.h"
static BOOL native_display_reconfigure_enabled = TRUE;
static char native_display_endpoint[108] = "/fixture/display.sock";
static SRWLOCK native_multi_lock = SRWLOCK_INIT;
static ULONGLONG native_multi_time;
static unsigned controls;
static DWORD WINAPI control(const char *path, DWORD code, const void *input, DWORD bytes)
{
    if (strcmp(path, native_display_endpoint)) return UURB_DISPLAY_FAILED;
    if (!code) return bytes ? UURB_DISPLAY_INVALID : 0;
    if (code != 0x30c004 || bytes != 24 || !input) return UURB_DISPLAY_INVALID;
    ++controls;
    return 0;
}
static DWORD (WINAPI *native_virtual_control_api)(const char *, DWORD, const void *, DWORD) = control;
static void write_log(const char *line) { (void)line; }
static void flush_log(void) {}
#include "uu_native_virtual_trace_win.h"
#define CHECK(value) do { if (!(value)) { fprintf(stderr, "failed line %d error %lu\n", __LINE__, GetLastError()); return 1; } } while (0)
int main(void)
{
    CHECK(LoadLibraryW(L"setupapi.dll"));
    native_virtual_trace_initialize();
    CHECK(native_virtual_hook_for((uintptr_t)virtual_ioctl) == (void *)&traced_virtual_ioctl);
    HDEVINFO set = traced_virtual_class_w(&uurb_idd_interface, NULL, NULL, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE);
    CHECK(set != INVALID_HANDLE_VALUE);
    for (unsigned retry = 0; retry < 100; ++retry)
        CHECK(virtual_track(virtual_sets, set, 1));
    SP_DEVICE_INTERFACE_DATA data = {.cbSize = sizeof(data)};
    CHECK(traced_virtual_enum(set, NULL, &uurb_idd_interface, 0, &data));
    DWORD required = 0;
    CHECK(!traced_virtual_detail_w(set, &data, NULL, 0, &required, NULL));
    CHECK(GetLastError() == ERROR_INSUFFICIENT_BUFFER && required > 8 && required < 128);
    BYTE storage[128] = {0};
    PSP_DEVICE_INTERFACE_DETAIL_DATA_W detail = (void *)storage;
    detail->cbSize = sizeof(*detail);
    CHECK(traced_virtual_detail_w(set, &data, detail, sizeof(storage), &required, NULL));
    CHECK(!wcscmp(detail->DevicePath, virtual_path_w));
    CHECK(!traced_virtual_enum(set, NULL, &uurb_idd_interface, 1, &data));
    CHECK(GetLastError() == ERROR_NO_MORE_ITEMS);
    HANDLE device = traced_virtual_create_w(detail->DevicePath, GENERIC_READ | GENERIC_WRITE, 0, NULL,
                                             OPEN_EXISTING, 0, NULL);
    CHECK(device != INVALID_HANDLE_VALUE);
    DWORD payload[6] = {0, 0, 0, 0, 0, 0}, bytes = 99;
    CHECK(traced_virtual_ioctl(device, 0x30c004, payload, sizeof(payload), NULL, 0, &bytes, NULL));
    CHECK(controls == 1 && bytes == 0);
    CHECK(!traced_virtual_ioctl(device, 0x30c004, (void *)1, sizeof(payload), NULL, 0, &bytes, NULL));
    CHECK(controls == 1);
    CHECK(!traced_virtual_ioctl(device, 0xffffffff, NULL, 0, NULL, 0, &bytes, NULL));
    native_display_reconfigure_enabled = FALSE;
    CHECK(!traced_virtual_ioctl(device, 0x30c004, payload, sizeof(payload), NULL, 0, &bytes, NULL));
    CHECK(controls == 1 && GetLastError() == ERROR_ACCESS_DENIED);
    CHECK(traced_virtual_close(device));
    CHECK(!virtual_track(virtual_handles, device, 0));
    CHECK(traced_virtual_destroy(set));
    CHECK(!virtual_track(virtual_sets, set, 0));
    HANDLE ordinary = CreateEventW(NULL, FALSE, FALSE, NULL);
    CHECK(ordinary && traced_virtual_close(ordinary));
    CHECK(traced_virtual_create_w(L"Z:\\uurb-file-does-not-exist", GENERIC_READ, 0, NULL,
                                 OPEN_EXISTING, 0, NULL) == INVALID_HANDLE_VALUE);
    puts("virtual discovery, real handles, control validation and passthrough passed");
    return 0;
}
