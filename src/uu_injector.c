#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <stdio.h>
#include <wchar.h>
#include <stdlib.h>

static DWORD find_process(const wchar_t *name)
{
    PROCESSENTRY32W entry;
    HANDLE snapshot;
    DWORD process_id = 0;

    snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE)
        return 0;

    ZeroMemory(&entry, sizeof(entry));
    entry.dwSize = sizeof(entry);
    if (Process32FirstW(snapshot, &entry)) {
        do {
            if (_wcsicmp(entry.szExeFile, name) == 0) {
                process_id = entry.th32ProcessID;
                break;
            }
        } while (Process32NextW(snapshot, &entry));
    }

    CloseHandle(snapshot);
    return process_id;
}

static int inject_library(DWORD process_id, const wchar_t *dll_path)
{
    SIZE_T path_bytes = (wcslen(dll_path) + 1) * sizeof(wchar_t);
    LPTHREAD_START_ROUTINE load_library;
    HANDLE process;
    HANDLE thread;
    void *remote_path;
    DWORD remote_result = 0;
    DWORD wait_result;

    process = OpenProcess(PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION |
                              PROCESS_VM_OPERATION | PROCESS_VM_WRITE |
                              PROCESS_VM_READ,
                          FALSE, process_id);
    if (process == NULL) {
        fwprintf(stderr, L"OpenProcess failed: %lu\n", GetLastError());
        return 1;
    }

    remote_path = VirtualAllocEx(process, NULL, path_bytes,
                                 MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (remote_path == NULL) {
        fwprintf(stderr, L"VirtualAllocEx failed: %lu\n", GetLastError());
        CloseHandle(process);
        return 1;
    }

    if (!WriteProcessMemory(process, remote_path, dll_path, path_bytes, NULL)) {
        fwprintf(stderr, L"WriteProcessMemory failed: %lu\n", GetLastError());
        VirtualFreeEx(process, remote_path, 0, MEM_RELEASE);
        CloseHandle(process);
        return 1;
    }

    load_library = (LPTHREAD_START_ROUTINE)(uintptr_t)GetProcAddress(
        GetModuleHandleW(L"kernel32.dll"), "LoadLibraryW");
    if (load_library == NULL) {
        fwprintf(stderr, L"LoadLibraryW lookup failed: %lu\n", GetLastError());
        VirtualFreeEx(process, remote_path, 0, MEM_RELEASE);
        CloseHandle(process);
        return 1;
    }

    thread = CreateRemoteThread(process, NULL, 0, load_library, remote_path, 0,
                                NULL);
    if (thread == NULL) {
        fwprintf(stderr, L"CreateRemoteThread failed: %lu\n", GetLastError());
        VirtualFreeEx(process, remote_path, 0, MEM_RELEASE);
        CloseHandle(process);
        return 1;
    }

    wait_result = WaitForSingleObject(thread, 10000);
    if (wait_result != WAIT_OBJECT_0) {
        fwprintf(stderr, L"LoadLibraryW thread wait failed: %lu\n",
                 wait_result == WAIT_FAILED ? GetLastError() : wait_result);
        /* The unfinished remote thread may still be reading remote_path. */
        CloseHandle(thread);
        CloseHandle(process);
        return 1;
    }
    if (!GetExitCodeThread(thread, &remote_result)) {
        fwprintf(stderr, L"GetExitCodeThread failed: %lu\n", GetLastError());
        CloseHandle(thread);
        VirtualFreeEx(process, remote_path, 0, MEM_RELEASE);
        CloseHandle(process);
        return 1;
    }
    CloseHandle(thread);
    VirtualFreeEx(process, remote_path, 0, MEM_RELEASE);
    CloseHandle(process);

    if (remote_result == 0) {
        fwprintf(stderr, L"LoadLibraryW failed in the target process\n");
        return 1;
    }

    return 0;
}

static int native_ready(DWORD pid, const WCHAR *path)
{
    HMODULE local = LoadLibraryExW(path, NULL, DONT_RESOLVE_DLL_REFERENCES);
    if (!local) return 1;
    FARPROC ready = GetProcAddress(local, "UurbNativeInputReady");
    uintptr_t offset = ready ? (uintptr_t)ready - (uintptr_t)local : 0;
    FreeLibrary(local);
    if (!offset) return 1;
    MODULEENTRY32W entry = {.dwSize = sizeof(entry)};
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid);
    if (snapshot == INVALID_HANDLE_VALUE) return 1;
    uintptr_t address = 0;
    if (Module32FirstW(snapshot, &entry)) do {
        if (!_wcsicmp(entry.szExePath, path) && offset < entry.modBaseSize) {
            address = (uintptr_t)entry.modBaseAddr + offset; break;
        }
    } while (Module32NextW(snapshot, &entry));
    CloseHandle(snapshot);
    if (!address) return 1;
    HANDLE process = OpenProcess(PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION | PROCESS_VM_OPERATION |
                                  PROCESS_VM_READ | PROCESS_VM_WRITE, FALSE, pid);
    if (!process) return 1;
    int result = 1;
    ULONGLONG deadline = GetTickCount64() + 5000;
    while (GetTickCount64() < deadline) {
        HANDLE thread = CreateRemoteThread(process, NULL, 0, (LPTHREAD_START_ROUTINE)address, NULL, 0, NULL);
        if (!thread) break;
        DWORD value = 0;
        DWORD waited = WaitForSingleObject(thread, 1000);
        BOOL got = waited == WAIT_OBJECT_0 && GetExitCodeThread(thread, &value);
        CloseHandle(thread);
        if (!got || value == 2) break;
        if (value == 1) { result = 0; break; }
        Sleep(50);
    }
    CloseHandle(process);
    return result;
}

int wmain(int argc, wchar_t **argv)
{
    wchar_t full_path[MAX_PATH];
    const wchar_t *process_name;
    DWORD process_id;
    DWORD path_length;

    BOOL verify_native = argc == 4 && !wcscmp(argv[2], L"--native-pid");
    if (argc < 2 || (argc > 3 && !verify_native)) {
        fwprintf(stderr,
                 L"Usage: uu-injector.exe DLL_PATH [PROCESS_NAME]\n");
        return 2;
    }

    path_length = GetFullPathNameW(argv[1], MAX_PATH, full_path, NULL);
    if (path_length == 0) {
        fwprintf(stderr, L"GetFullPathNameW failed: %lu\n", GetLastError());
        return 1;
    }
    if (path_length >= MAX_PATH) {
        fwprintf(stderr, L"DLL path exceeds MAX_PATH\n");
        return 1;
    }

    process_name = argc == 3 ? argv[2] : L"GameViewerServer.exe";
    if (verify_native) {
        WCHAR *end;
        process_id = wcstoul(argv[3], &end, 10);
        if (!*argv[3] || *end || !process_id) return 2;
    } else process_id = find_process(process_name);
    if (process_id == 0) {
        fwprintf(stderr, L"Process not found: %ls\n", process_name);
        return 1;
    }

    int result = inject_library(process_id, full_path);
    return result || (verify_native && native_ready(process_id, full_path));
}
