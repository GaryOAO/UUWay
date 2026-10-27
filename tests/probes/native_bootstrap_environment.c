/* Real bootstrap registry/environment/cleanup; only child launch is doubled.
 * The test runs in its own Wine prefix and never starts vendor software. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
static BOOL WINAPI probe_create_process(LPCWSTR, LPWSTR, LPSECURITY_ATTRIBUTES, LPSECURITY_ATTRIBUTES,
                                       BOOL, DWORD, LPVOID, LPCWSTR, LPSTARTUPINFOW, LPPROCESS_INFORMATION);
#define CreateProcessW probe_create_process
#define wmain bootstrap_main
#include "../../src/uu_native_wine_bootstrap.c"
#undef wmain
#undef CreateProcessW
#include <userenv.h>
#define check(value) do { if (!(value)) { \
    fprintf(stderr, "check failed at line %d: %s\n", __LINE__, #value); \
    fflush(stderr); ExitProcess(1); } } while (0)

static unsigned checked;
static BOOL change_owner;
static const WCHAR other[] = L"Z:\\another-owner";
static DWORD WINAPI request_stop(void *stop)
{
    Sleep(30);
    SetEvent(stop);
    return 0;
}
static void check_server_settle(void)
{
    HANDLE stop = CreateEventW(NULL, TRUE, FALSE, NULL);
    check(stop != NULL);
    HANDLE process = OpenProcess(SYNCHRONIZE, FALSE, GetCurrentProcessId());
    check(process != NULL);
    ULONGLONG before = GetTickCount64();
    check(settle_server(process, stop, 100) == ERROR_SUCCESS);
    check(GetTickCount64() - before >= 90);
    HANDLE worker = CreateThread(NULL, 0, request_stop, stop, 0, NULL);
    check(worker != NULL);
    before = GetTickCount64();
    check(settle_server(process, stop, 8000) == ERROR_CANCELLED);
    check(GetTickCount64() - before < 4000);
    check(WaitForSingleObject(worker, 1000) == WAIT_OBJECT_0);
    CloseHandle(worker);
    check(ResetEvent(stop));

    WCHAR executable[32768];
    check(GetModuleFileNameW(NULL, executable, ARRAYSIZE(executable)) > 0);
    STARTUPINFOW startup = {.cb = sizeof(startup)};
    PROCESS_INFORMATION child = {0};
    check(CreateProcessW(executable, NULL, NULL, NULL, FALSE, CREATE_SUSPENDED,
                         NULL, NULL, &startup, &child));
    check(TerminateProcess(child.hProcess, 0));
    check(WaitForSingleObject(child.hProcess, 5000) == WAIT_OBJECT_0);
    check(settle_server(child.hProcess, stop, 8000) == ERROR_PROCESS_ABORTED);
    check(SetEvent(stop));
    check(settle_server(child.hProcess, stop, 8000) == ERROR_CANCELLED);
    CloseHandle(child.hThread);
    CloseHandle(child.hProcess);
    check(ResetEvent(stop));
    check(settle_server(NULL, stop, 100) == ERROR_INVALID_HANDLE);
    CloseHandle(process);
    CloseHandle(stop);
}
static const WCHAR *environment_value(const WCHAR *block, const WCHAR *name)
{
    size_t length = wcslen(name);
    for (const WCHAR *entry = block; *entry; entry += wcslen(entry) + 1)
        if (!wcsncmp(entry, name, length) && entry[length] == L'=') return entry + length + 1;
    return NULL;
}
static BOOL WINAPI probe_create_process(LPCWSTR app, LPWSTR command, LPSECURITY_ATTRIBUTES process,
    LPSECURITY_ATTRIBUTES thread, BOOL inherit, DWORD flags, LPVOID env, LPCWSTR cwd,
    LPSTARTUPINFOW startup, LPPROCESS_INFORMATION child)
{
    (void)app; (void)command; (void)process; (void)thread; (void)inherit; (void)flags;
    (void)env; (void)cwd; (void)startup; (void)child;
    check(count <= sizeof(settings) / sizeof(settings[0]));
    /* A service creates a new Windows environment without Unix inheritance. */
    void *environment = NULL;
    check(CreateEnvironmentBlock(&environment, NULL, FALSE));
    const WCHAR *path = environment_value(environment, L"UURB_CURSOR_STATE_PATH");
    const WCHAR *directory = environment_value(environment, L"UURB_CURSOR_STATE_DIR");
    check(path && !wcscmp(path, L"Z:\\owned\\cursor.state"));
    check(directory && !wcscmp(directory, L"Z:\\owned"));
    const WCHAR *presentation = environment_value(environment, L"UURB_CURSOR_PRESENTATION");
    check(presentation && !wcscmp(presentation, L"client"));
    check(DestroyEnvironmentBlock(environment));
    ++checked;
    if (change_owner) {
        HKEY key;
        check(RegOpenKeyExW(HKEY_LOCAL_MACHINE, env_key, 0, KEY_SET_VALUE, &key) == 0);
        check(RegSetValueExW(key, L"UURB_CURSOR_STATE_DIR", 0, REG_SZ, (const BYTE *)other, sizeof(other)) == 0);
        RegCloseKey(key);
    }
    SetLastError(ERROR_FILE_NOT_FOUND);
    return FALSE; /* Exercise production cleanup after a launch failure. */
}
int wmain(void)
{
    check_server_settle();
    check(SetEnvironmentVariableW(L"UURB_NATIVE_BOOTSTRAP", L"1"));
    check(SetEnvironmentVariableW(L"UURB_CURSOR_STATE_PATH", L"Z:\\owned\\cursor.state"));
    check(SetEnvironmentVariableW(L"UURB_CURSOR_STATE_DIR", L"Z:\\owned"));
    check(SetEnvironmentVariableW(L"UURB_CURSOR_PRESENTATION", L"client"));
    WCHAR *arguments[] = {L"bootstrap.exe", L"--run", L"/owned/capture.sock",
                          L"Z:\\owned\\dxvk.conf", L"Z:\\owned\\gui.exe", L"UURB-NATIVE-env-test"};
    check(bootstrap_main(6, arguments) == 1 && checked == 1);
    HKEY key;
    check(RegOpenKeyExW(HKEY_LOCAL_MACHINE, env_key, 0, KEY_QUERY_VALUE | KEY_SET_VALUE, &key) == 0);
    DWORD bytes = 0;
    check(RegQueryValueExW(key, L"UURB_CURSOR_STATE_DIR", NULL, NULL, NULL, &bytes) == ERROR_FILE_NOT_FOUND);
    check(RegQueryValueExW(key, L"UURB_CURSOR_PRESENTATION", NULL, NULL, NULL, &bytes) == ERROR_FILE_NOT_FOUND);

    change_owner = TRUE; count = 0;
    check(bootstrap_main(6, arguments) == 1 && checked == 2);
    WCHAR value[64]; bytes = sizeof(value);
    check(RegQueryValueExW(key, L"UURB_CURSOR_STATE_DIR", NULL, NULL, (BYTE *)value, &bytes) == 0);
    check(bytes == sizeof(other) && !wcscmp(value, other));
    count = 0;
    check(bootstrap_main(6, arguments) == 1 && checked == 2); /* Existing value blocks startup. */
    check(RegDeleteValueW(key, L"UURB_CURSOR_STATE_DIR") == 0);

    /* Cold recovery removes the matching directory written by this trial. */
    const WCHAR owned[] = L"Z:\\owned";
    check(RegSetValueExW(key, L"UURB_CURSOR_STATE_DIR", 0, REG_SZ, (const BYTE *)owned, sizeof(owned)) == 0);
    arguments[1] = L"--recover"; count = 0;
    check(bootstrap_main(6, arguments) == 0);
    check(RegQueryValueExW(key, L"UURB_CURSOR_STATE_DIR", NULL, NULL, NULL, &bytes) == ERROR_FILE_NOT_FOUND);
    RegCloseKey(key);
    puts("BOOTSTRAP fresh environment and owned cleanup passed");
    return 0;
}
