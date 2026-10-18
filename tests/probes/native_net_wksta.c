/* Reproduce UU's actual Super Screen OS gate with Wine's real netapi32,
 * then check that remote queries, failures and buffer ownership survive. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <lm.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>
static void write_log(const char *line) { (void)line; }
static void flush_log(void) {}
#include "uu_native_terminal_trace_win.h"
#define CHECK(value) do { if (!(value)) { fprintf(stderr, "line %d\n", __LINE__); return 1; } } while (0)
static WKSTA_INFO_100 fixture;
static NET_API_STATUS WINAPI fake_query(LMSTR server, DWORD level, LPBYTE *buffer)
{
    (void)server;
    SetLastError(1234);
    if (level == 999) return ERROR_INVALID_LEVEL;
    *buffer = (LPBYTE)&fixture;
    return NERR_Success;
}
static BOOL super_screen_gate(NET_API_STATUS (WINAPI *query)(LMSTR, DWORD, LPBYTE *))
{
    LPBYTE buffer = NULL;
    NET_API_STATUS status = query(NULL, 100, &buffer);
    BOOL result = !status && buffer && ((WKSTA_INFO_100 *)buffer)->wki100_ver_major >= 10;
    if (buffer) NetApiBufferFree(buffer);
    return result;
}
int main(void)
{
    CHECK(terminal_driver_helper(L"\"C:\\Program Files\\UU\\DriverHelper.exe\" --install", TRUE, TRUE));
    CHECK(terminal_driver_helper(L"C:\\Program Files\\UU\\DriverHelper.exe", TRUE, FALSE));
    CHECK(!terminal_driver_helper(L"\"C:\\Program Files\\UU\\Other.exe\" DriverHelper.exe", TRUE, TRUE));
    CHECK(LoadLibraryW(L"netapi32.dll"));
    native_terminal_trace_initialize();
    CHECK(terminal_net_wksta_get_info);
    CHECK(native_terminal_hook_for((uintptr_t)terminal_net_wksta_get_info) ==
          (void *)&traced_terminal_net_wksta_get_info);
    native_version_capability_enabled = FALSE;
    CHECK(super_screen_gate(terminal_net_wksta_get_info) ==
          super_screen_gate(traced_terminal_net_wksta_get_info));
    native_version_capability_enabled = TRUE;
    CHECK(super_screen_gate(traced_terminal_net_wksta_get_info));

    terminal_net_wksta_get_info = fake_query;
    WCHAR computer[] = L"fixture-computer", group[] = L"fixture-group", remote[] = L"remote";
    fixture.wki100_computername = computer;
    fixture.wki100_langroup = group;
    for (DWORD level = 100; level <= 102; ++level) {
        fixture.wki100_ver_major = 6; fixture.wki100_ver_minor = 2;
        LPBYTE output = NULL;
        CHECK(!traced_terminal_net_wksta_get_info(NULL, level, &output));
        CHECK(GetLastError() == 1234 && output == (LPBYTE)&fixture);
        CHECK(fixture.wki100_ver_major == 10 && !fixture.wki100_ver_minor);
        CHECK(fixture.wki100_computername == computer && fixture.wki100_langroup == group);
    }
    LPBYTE output = NULL;
    fixture.wki100_ver_major = 6; fixture.wki100_ver_minor = 2;
    CHECK(!traced_terminal_net_wksta_get_info(remote, 100, &output));
    CHECK(fixture.wki100_ver_major == 6 && fixture.wki100_ver_minor == 2);
    CHECK(traced_terminal_net_wksta_get_info(NULL, 999, &output) == ERROR_INVALID_LEVEL);
    CHECK(GetLastError() == 1234 && fixture.wki100_ver_major == 6);
    native_version_capability_enabled = FALSE;
    CHECK(!traced_terminal_net_wksta_get_info(NULL, 100, &output));
    CHECK(fixture.wki100_ver_major == 6);
    native_version_capability_enabled = TRUE;
    fixture.wki100_ver_major = 11; fixture.wki100_ver_minor = 1;
    CHECK(!traced_terminal_net_wksta_get_info(NULL, 100, &output));
    CHECK(fixture.wki100_ver_major == 11 && fixture.wki100_ver_minor == 1);
    puts("UU Super Screen OS gate and NetAPI ownership passed");
    return 0;
}
