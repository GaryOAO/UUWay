/* Controlled native trial in an existing UU prefix. Never installs services,
 * edits account files or starts the retired RDP bridge. Registry writes are
 * absent-only; cleanup removes only values still matching this invocation. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <wchar.h>
#include <tlhelp32.h>

struct setting { HKEY hive; const WCHAR *key, *name, *value; };
static struct setting settings[16];
static unsigned count;
static const WCHAR env_key[] = L"SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment";
static const WCHAR *override_keys[] = {
    L"Software\\Wine\\AppDefaults\\GameViewerServer.exe\\DllOverrides",
    L"Software\\Wine\\AppDefaults\\StreamerCodecDetector.exe\\DllOverrides",
    L"Software\\Wine\\AppDefaults\\GameViewer.exe\\DllOverrides"};

static void event(const char *name, DWORD code)
{
    printf("UURB_NATIVE_BOOT {\"event\":\"%s\",\"code\":%lu}\n", name, code);
    fflush(stdout);
}

static DWORD preflight(void)
{
    for (unsigned i = 0; i < count; ++i) {
        HKEY key;
        LSTATUS status = RegOpenKeyExW(settings[i].hive, settings[i].key, 0, KEY_QUERY_VALUE, &key);
        if (status == ERROR_FILE_NOT_FOUND) continue;
        if (status) return status;
        DWORD bytes = 0;
        status = RegQueryValueExW(key, settings[i].name, NULL, NULL, NULL, &bytes);
        RegCloseKey(key);
        if (status != ERROR_FILE_NOT_FOUND) return ERROR_ALREADY_EXISTS;
    }
    return 0;
}

static DWORD apply_settings(void)
{
    for (unsigned i = 0; i < count; ++i) {
        HKEY key;
        LSTATUS status = RegCreateKeyExW(settings[i].hive, settings[i].key, 0, NULL, 0,
                                       KEY_SET_VALUE, NULL, &key, NULL);
        if (status) return status;
        status = RegSetValueExW(key, settings[i].name, 0, REG_SZ, (const BYTE *)settings[i].value,
                               (wcslen(settings[i].value) + 1) * sizeof(WCHAR));
        RegCloseKey(key);
        if (status) return status;
        if (settings[i].hive == HKEY_LOCAL_MACHINE &&
            !SetEnvironmentVariableW(settings[i].name, settings[i].value)) return GetLastError();
    }
    return 0;
}

static DWORD cleanup_settings(void)
{
    DWORD failure = 0;
    for (unsigned i = 0; i < count; ++i) {
        HKEY key;
        LSTATUS status = RegOpenKeyExW(settings[i].hive, settings[i].key, 0, KEY_QUERY_VALUE | KEY_SET_VALUE, &key);
        if (status == ERROR_FILE_NOT_FOUND) continue;
        if (status) { failure = status; continue; }
        WCHAR buffer[32768];
        DWORD bytes = sizeof(buffer), type = 0;
        status = RegQueryValueExW(key, settings[i].name, NULL, &type, (BYTE *)buffer, &bytes);
        if (!status && type == REG_SZ && bytes == (wcslen(settings[i].value) + 1) * sizeof(WCHAR) &&
            !memcmp(buffer, settings[i].value, bytes)) status = RegDeleteValueW(key, settings[i].name);
        else if (status != ERROR_FILE_NOT_FOUND) status = ERROR_INVALID_DATA;
        if (status && status != ERROR_FILE_NOT_FOUND) failure = status;
        SecureZeroMemory(buffer, sizeof(buffer));
        RegCloseKey(key);
    }
    return failure;
}

static DWORD launch(const WCHAR *executable, PROCESS_INFORMATION *child)
{
    STARTUPINFOW startup = {.cb = sizeof(startup)};
    return CreateProcessW(executable, NULL, NULL, NULL, FALSE, 0, NULL, NULL, &startup, child) ? 0 : GetLastError();
}

static DWORD server_pid(void)
{
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) return 0;
    PROCESSENTRY32W entry = {.dwSize = sizeof(entry)};
    DWORD pid = 0;
    if (Process32FirstW(snapshot, &entry)) do {
        if (!_wcsicmp(entry.szExeFile, L"GameViewerServer.exe")) {
            if (pid) { pid = 0; break; } /* ambiguous target is not safe */
            pid = entry.th32ProcessID;
        }
    } while (Process32NextW(snapshot, &entry));
    CloseHandle(snapshot);
    return pid;
}

static DWORD attach_input(const WCHAR *folder, DWORD pid)
{
    WCHAR executable[32768], command[32768];
    if (wcslen(folder) > 15000) return ERROR_INVALID_PARAMETER;
    swprintf(executable, 32768, L"%lsnative-input-injector.exe", folder);
    swprintf(command, 32768, L"\"%ls\" \"%lsnative-input-bridge.dll\" --native-pid %lu", executable, folder, pid);
    STARTUPINFOW startup = {.cb = sizeof(startup)};
    PROCESS_INFORMATION child = {0};
    if (!CreateProcessW(executable, command, NULL, NULL, FALSE, 0, NULL, NULL, &startup, &child))
        return GetLastError();
    DWORD code = ERROR_TIMEOUT;
    if (WaitForSingleObject(child.hProcess, 18000) == WAIT_OBJECT_0) GetExitCodeProcess(child.hProcess, &code);
    else TerminateProcess(child.hProcess, ERROR_TIMEOUT);
    CloseHandle(child.hThread); CloseHandle(child.hProcess);
    return code;
}

int wmain(int argc, WCHAR **argv)
{
    WCHAR guard[16];
    if (GetEnvironmentVariableW(L"UURB_NATIVE_BOOTSTRAP", guard, 16) != 1 || wcscmp(guard, L"1")) return 2;
    if (argc == 3 && !wcscmp(argv[1], L"--stop")) {
        if (wcsncmp(argv[2], L"UURB-NATIVE-", 12)) return 2;
        HANDLE stop = OpenEventW(EVENT_MODIFY_STATE, FALSE, argv[2]);
        if (!stop) return 1;
        BOOL result = SetEvent(stop); CloseHandle(stop); return result ? 0 : 1;
    }
    if (argc != 6 || (wcscmp(argv[1], L"--run") && wcscmp(argv[1], L"--recover")) ||
        argv[2][0] != L'/' || wcslen(argv[2]) >= 108 || wcslen(argv[3]) >= 32000 ||
        wcslen(argv[4]) >= 32000 || wcsncmp(argv[5], L"UURB-NATIVE-", 12)) return 2;
    settings[count++] = (struct setting){HKEY_LOCAL_MACHINE, env_key, L"UURB_DXGI_CAPTURE_SOCKET", argv[2]};
    settings[count++] = (struct setting){HKEY_LOCAL_MACHINE, env_key, L"UURB_DXGI_CAPTURE_OUTPUT", L"\\\\.\\DISPLAY1"};
    settings[count++] = (struct setting){HKEY_LOCAL_MACHINE, env_key, L"DXVK_CONFIG_FILE", argv[3]};
    settings[count++] = (struct setting){HKEY_LOCAL_MACHINE, env_key, L"WINEDLLOVERRIDES", L"mscoree,mshtml=;d3d11,dxgi,nvEncodeAPI64=n"};
    /* UU creates its Server with a fresh Windows service/user environment.
     * Unix inheritance alone is not enough. Use the same absent-only,
     * invocation-owned registry transport as the GPU endpoint, including
     * exact-value cleanup/recovery; never touch account configuration. */
    WCHAR cursor_path[32768];
    DWORD cursor_length = GetEnvironmentVariableW(L"UURB_CURSOR_STATE_PATH", cursor_path, 32768);
    if (cursor_length >= 32768) return 2;
    if (cursor_length)
        settings[count++] = (struct setting){HKEY_LOCAL_MACHINE, env_key, L"UURB_CURSOR_STATE_PATH", cursor_path};
    WCHAR display_socket[108];
    DWORD display_length = GetEnvironmentVariableW(L"UURB_DISPLAY_SOCKET", display_socket, ARRAYSIZE(display_socket));
    if (display_length >= ARRAYSIZE(display_socket) || (display_length && display_socket[0] != L'/')) return 2;
    if (display_length)
        settings[count++] = (struct setting){HKEY_LOCAL_MACHINE, env_key, L"UURB_DISPLAY_SOCKET", display_socket};
    for (unsigned i = 0; i < 3; ++i) {
        settings[count++] = (struct setting){HKEY_CURRENT_USER, override_keys[i], L"dxgi", L"native"};
        settings[count++] = (struct setting){HKEY_CURRENT_USER, override_keys[i], L"d3d11", L"native"};
        settings[count++] = (struct setting){HKEY_CURRENT_USER, override_keys[i], L"nvEncodeAPI64", L"native"};
    }
    if (!wcscmp(argv[1], L"--recover")) {
        DWORD status = cleanup_settings(); event("recovery_finished", status); return status ? 1 : 0;
    }
    DWORD status = preflight();
    if (status) { event("configuration_conflict", status); return 1; }
    HANDLE stop = CreateEventW(NULL, TRUE, FALSE, argv[5]);
    if (!stop || GetLastError() == ERROR_ALREADY_EXISTS) { if (stop) CloseHandle(stop); return 1; }
    /* The journal recognizes this flushed marker before any registry write. */
    event("preflight_passed", 0);
    status = apply_settings();
    event("configuration_applied", status);
    PROCESS_INFORMATION winlogon = {0}, gui = {0}, input = {0};
    SC_HANDLE manager = NULL, service = NULL;
    if (status) goto done;
    WCHAR standin[32768];
    DWORD length = GetModuleFileNameW(NULL, standin, 32768);
    WCHAR *base = length && length < 32768 ? wcsrchr(standin, L'\\') : NULL;
    if (!base || ++base - standin + 32 >= 32768) { status = ERROR_INVALID_PARAMETER; goto done; }
    WCHAR folder[32768];
    *base = 0; wcscpy(folder, standin);
    WCHAR enabled[2];
    BOOL native_input = GetEnvironmentVariableW(L"UURB_NATIVE_INPUT_ENABLED", enabled, 2) == 1 && enabled[0] == L'1';
    if (native_input) {
        wcscpy(base, L"native-input-broker.exe");
        status = launch(standin, &input);
        if (status) goto done;
        ULONGLONG deadline = GetTickCount64() + 5000;
        while (!WaitNamedPipeW(L"\\\\.\\pipe\\uurb-native-input-v1", 100)) {
            if (GetTickCount64() >= deadline || WaitForSingleObject(input.hProcess, 0) == WAIT_OBJECT_0) {
                status = ERROR_TIMEOUT; goto done;
            }
            Sleep(50);
        }
        event("native_input_broker_ready", 0);
    }
    wcscpy(base, L"winlogon.exe");
    status = launch(standin, &winlogon);
    event("winlogon_started", status);
    if (status) goto done;
    manager = OpenSCManagerW(NULL, NULL, SC_MANAGER_CONNECT);
    if (!manager) { status = GetLastError(); goto done; }
    service = OpenServiceW(manager, L"GameViewerService", SERVICE_START | SERVICE_USER_DEFINED_CONTROL);
    if (!service) { status = GetLastError(); goto done; }
    if (!StartServiceW(service, 0, NULL) && GetLastError() != ERROR_SERVICE_ALREADY_RUNNING) {
        status = GetLastError(); goto done;
    }
    SERVICE_STATUS service_status = {0};
    if (!ControlService(service, 133, &service_status)) { status = GetLastError(); goto done; }
    event("service_bootstrap_accepted", 0);
    status = launch(argv[4], &gui);
    event("gui_started", status);
    if (!status && !native_input) WaitForSingleObject(stop, INFINITE);
    if (!status && native_input) {
        DWORD attached = 0;
        ULONGLONG missing_since = GetTickCount64();
        while (WaitForSingleObject(stop, 250) == WAIT_TIMEOUT) {
            if (WaitForSingleObject(input.hProcess, 0) == WAIT_OBJECT_0) { status = ERROR_BROKEN_PIPE; break; }
            DWORD target = server_pid();
            if (!target) {
                if (GetTickCount64() - missing_since > 15000) { status = ERROR_PROCESS_ABORTED; break; }
                continue;
            }
            missing_since = GetTickCount64();
            if (target == attached) continue;
            status = attach_input(folder, target);
            event("native_input_hook_ready", status);
            if (status) break;
            attached = target;
        }
    }
done:
    if (service) CloseServiceHandle(service);
    if (manager) CloseServiceHandle(manager);
    if (gui.hThread) CloseHandle(gui.hThread);
    if (gui.hProcess) CloseHandle(gui.hProcess);
    /* Keep the broker usable until Python stops this whole owned prefix;
     * never leave a running hooked server with a deliberately dead broker. */
    if (input.hThread) CloseHandle(input.hThread);
    if (input.hProcess) CloseHandle(input.hProcess);
    if (winlogon.hProcess) {
        TerminateProcess(winlogon.hProcess, 0); WaitForSingleObject(winlogon.hProcess, 5000);
        CloseHandle(winlogon.hProcess); CloseHandle(winlogon.hThread);
    }
    DWORD cleaned = cleanup_settings();
    event("configuration_removed", cleaned);
    event("bootstrap_exit", status);
    CloseHandle(stop);
    return status || cleaned ? 1 : 0;
}
