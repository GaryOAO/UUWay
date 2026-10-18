#ifndef UURB_NATIVE_TERMINAL_TRACE_WIN_H
#define UURB_NATIVE_TERMINAL_TRACE_WIN_H
#include <tlhelp32.h>
#include <shellapi.h>
#include <lm.h>
/* Process diagnostics and the user-space IDD helper bridge.
 * Only DriverHelper startup is intercepted to load the owned display adapter.
 * Only a fixed executable category and numeric API result leave this code;
 * command lines, paths, tokens, environment, handles and pipe data do not. */
static BOOL (WINAPI *terminal_create_w)(LPCWSTR, LPWSTR, LPSECURITY_ATTRIBUTES,
    LPSECURITY_ATTRIBUTES, BOOL, DWORD, LPVOID, LPCWSTR, LPSTARTUPINFOW, LPPROCESS_INFORMATION);
static BOOL (WINAPI *terminal_create_a)(LPCSTR, LPSTR, LPSECURITY_ATTRIBUTES,
    LPSECURITY_ATTRIBUTES, BOOL, DWORD, LPVOID, LPCSTR, LPSTARTUPINFOA, LPPROCESS_INFORMATION);
static BOOL (WINAPI *terminal_create_user_w)(HANDLE, LPCWSTR, LPWSTR, LPSECURITY_ATTRIBUTES,
    LPSECURITY_ATTRIBUTES, BOOL, DWORD, LPVOID, LPCWSTR, LPSTARTUPINFOW, LPPROCESS_INFORMATION);
static HINSTANCE (WINAPI *terminal_shell_execute_w)(HWND, LPCWSTR, LPCWSTR, LPCWSTR, LPCWSTR, INT);
static BOOL (WINAPI *terminal_verify_version_w)(LPOSVERSIONINFOEXW, DWORD, DWORDLONG);
static DWORD (WINAPI *terminal_get_version)(void);
static BOOL (WINAPI *terminal_get_version_ex_w)(LPOSVERSIONINFOW);
typedef LONG (WINAPI *terminal_rtl_get_version_fn)(LPOSVERSIONINFOW);
static terminal_rtl_get_version_fn terminal_rtl_get_version;
static NET_API_STATUS (WINAPI *terminal_net_wksta_get_info)(LMSTR, DWORD, LPBYTE *);
static BOOL native_version_capability_enabled;
static volatile LONG terminal_trace_known, terminal_trace_other;
static volatile LONG native_terminal_import_slots;

static void terminal_trace_bridge(BOOL success)
{
    char line[256];
    snprintf(line, sizeof(line),
             "UURB_NATIVE_TERMINAL {\"event\":\"driver_helper_bridge\",\"success\":%s,\"error\":%lu}\r\n",
             success ? "true" : "false", (unsigned long)(success ? ERROR_SUCCESS : ERROR_GEN_FAILURE));
    write_log(line); flush_log();
}

static void terminal_trace_shell(unsigned kind, BOOL result, DWORD error)
{
    char line[256];
    snprintf(line, sizeof(line),
             "UURB_NATIVE_TERMINAL {\"event\":\"shell_execute\",\"kind\":%u,\"success\":%s,\"error\":%lu}\r\n",
             kind, result ? "true" : "false", (unsigned long)(result ? ERROR_SUCCESS : error));
    write_log(line); flush_log();
}

/* The vendor installs its IDD package in a short-lived DriverHelper.exe
 * child.  That child does not import SendInput, so the normal input bridge
 * initialization used to leave it completely uninstrumented.  Start that
 * helper suspended, load this same bridge into it, and then let it run.  The
 * bridge is still user-space only; it merely redirects the helper's existing
 * SetupAPI calls to the reviewed virtual-display endpoint. */
static BOOL terminal_driver_helper(const void *text, BOOL wide, BOOL command)
{
    WCHAR basename[64];
    unsigned length = 0;
    BOOL quoted = FALSE;
    if (!text) return FALSE;
    for (unsigned i = 0; i < 1024; ++i) {
        WCHAR ch = 0; SIZE_T bytes = 0;
        if (!ReadProcessMemory(GetCurrentProcess(), (const BYTE *)text + i * (wide ? 2 : 1),
                               &ch, wide ? 2 : 1, &bytes) || bytes != (SIZE_T)(wide ? 2 : 1)) return FALSE;
        if (i == 0 && ch == L'"') { quoted = TRUE; continue; }
        if (!ch || (!quoted && command && (ch == L' ' || ch == L'\t')) || ch == L'"') {
            basename[length] = 0;
            return !lstrcmpiW(basename, L"DriverHelper.exe");
        }
        if (ch == L'\\' || ch == L'/') length = 0;
        else if (length + 1 < ARRAYSIZE(basename)) basename[length++] = ch;
        else return FALSE;
    }
    return FALSE;
}

static BOOL terminal_inject_bridge(PROCESS_INFORMATION *process)
{
    if (!process || !process->hProcess) return FALSE;
    HMODULE bridge = GetModuleHandleW(L"native-input-bridge.dll");
    HMODULE kernel = GetModuleHandleW(L"kernel32.dll");
    if (!bridge || !kernel) return FALSE;
    WCHAR path[32768];
    DWORD length = GetModuleFileNameW(bridge, path, ARRAYSIZE(path));
    if (!length || length >= ARRAYSIZE(path)) return FALSE;
    SIZE_T bytes = ((SIZE_T)length + 1) * sizeof(WCHAR);
    LPVOID remote = VirtualAllocEx(process->hProcess, NULL, bytes, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
    if (!remote) return FALSE;
    SIZE_T written = 0;
    BOOL result = WriteProcessMemory(process->hProcess, remote, path, bytes, &written) && written == bytes;
    HANDLE thread = NULL;
    if (result) {
        FARPROC load_raw = GetProcAddress(kernel, "LoadLibraryW");
        LPTHREAD_START_ROUTINE load = NULL;
        if (load_raw) memcpy(&load, &load_raw, sizeof(load));
        thread = load ? CreateRemoteThread(process->hProcess, NULL, 0, load, remote, 0, NULL) : NULL;
        result = thread != NULL;
    }
    BOOL completed = thread == NULL;
    if (thread) {
        completed = WaitForSingleObject(thread, 5000) == WAIT_OBJECT_0;
        if (!completed) result = FALSE;
        DWORD status = 0;
        if (!GetExitCodeThread(thread, &status) || !status) result = FALSE;
        CloseHandle(thread);
    }
    /* A timed-out loader may still read its argument. Its process owns that
     * allocation until exit; freeing it early could crash the helper. */
    if (completed) VirtualFreeEx(process->hProcess, remote, 0, MEM_RELEASE);
    return result;
}

static BOOL terminal_wait_bridge(PROCESS_INFORMATION *process)
{
    HMODULE local = GetModuleHandleW(L"native-input-bridge.dll");
    FARPROC ready_raw = local ? GetProcAddress(local, "UurbNativeInputReady") : NULL;
    if (!process || !process->dwProcessId || !local || !ready_raw) return FALSE;
    uintptr_t offset = (uintptr_t)ready_raw - (uintptr_t)local;
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32,
                                               process->dwProcessId);
    if (snapshot == INVALID_HANDLE_VALUE) return FALSE;
    MODULEENTRY32W item = {.dwSize = sizeof(item)};
    BYTE *remote_base = NULL;
    if (Module32FirstW(snapshot, &item)) do {
        if (!lstrcmpiW(item.szModule, L"native-input-bridge.dll")) {
            remote_base = item.modBaseAddr;
            break;
        }
    } while (Module32NextW(snapshot, &item));
    CloseHandle(snapshot);
    if (!remote_base) return FALSE;
    LPTHREAD_START_ROUTINE ready = NULL;
    uintptr_t remote_address = (uintptr_t)remote_base + offset;
    memcpy(&ready, &remote_address, sizeof(ready));
    for (unsigned attempt = 0; attempt < 100; ++attempt) {
        HANDLE thread = CreateRemoteThread(process->hProcess, NULL, 0, ready, NULL, 0, NULL);
        if (thread) {
            DWORD status = 0;
            BOOL done = WaitForSingleObject(thread, 100) == WAIT_OBJECT_0 &&
                        GetExitCodeThread(thread, &status) && status != 0;
            CloseHandle(thread);
            if (done) return TRUE;
        }
        Sleep(10);
    }
    return FALSE;
}

static unsigned terminal_executable_kind(const void *text, BOOL wide, BOOL command)
{
    WCHAR basename[64];
    unsigned length=0;
    BOOL quoted=FALSE;
    if (!text) return 0;
    /* Inspect at most the executable token, not its arguments. Overlong or
     * inaccessible names are simply "other". ReadProcessMemory avoids a
     * diagnostic dereference changing the original API's error behavior. */
    for (unsigned i=0; i<1024; ++i) {
        WCHAR ch=0; SIZE_T bytes=0;
        if (!ReadProcessMemory(GetCurrentProcess(), (const BYTE *)text+i*(wide?2:1),
                               &ch, wide?2:1, &bytes) || bytes!=(SIZE_T)(wide?2:1)) return 0;
        if (i==0 && ch==L'"') { quoted=TRUE; continue; }
        if (!ch || (quoted && ch==L'"') || (!quoted && command && (ch==L' ' || ch==L'\t'))) {
            basename[length]=0;
            static const WCHAR *names[]={L"conpty_bridge.exe",L"uuyc-mux.exe",L"powershell.exe",
                                         L"cmd.exe",L"OpenConsole.exe",L"uuyc-cli.exe"};
            for (unsigned n=0; n<ARRAYSIZE(names); ++n)
                if (!lstrcmpiW(basename,names[n])) return n+1;
            return 0;
        }
        if (ch==L'\\' || ch==L'/') length=0;
        else if (length+1<ARRAYSIZE(basename)) basename[length++]=ch;
        else return 0;
    }
    return 0;
}
static LONG terminal_trace_begin(unsigned api, unsigned kind, DWORD flags, BOOL inherit)
{
    LONG sequence=InterlockedIncrement(kind?&terminal_trace_known:&terminal_trace_other);
    if (sequence>(kind?64:16)) return 0;
    char line[320];
    snprintf(line,sizeof(line),"UURB_NATIVE_TERMINAL {\"event\":\"process_start\",\"api\":%u,"
        "\"kind\":%u,\"sequence\":%ld,\"flags\":%lu,\"inherit\":%s,\"tick_ms\":%llu}\r\n",
        api,kind,sequence,(unsigned long)flags,inherit?"true":"false",(unsigned long long)GetTickCount64());
    write_log(line);flush_log();return sequence;
}
static void terminal_trace_end(unsigned api, unsigned kind, LONG sequence, BOOL result, DWORD error)
{
    if (!sequence) return;
    char line[256];
    snprintf(line,sizeof(line),"UURB_NATIVE_TERMINAL {\"event\":\"process_result\",\"api\":%u,"
        "\"kind\":%u,\"sequence\":%ld,\"success\":%s,\"error\":%lu}\r\n",
        api,kind,sequence,result?"true":"false",(unsigned long)(result?0:error));
    write_log(line);flush_log();
}
static BOOL WINAPI traced_terminal_create_w(LPCWSTR app, LPWSTR cmd, LPSECURITY_ATTRIBUTES process_attr,
    LPSECURITY_ATTRIBUTES thread_attr, BOOL inherit, DWORD flags, LPVOID env, LPCWSTR cwd,
    LPSTARTUPINFOW startup, LPPROCESS_INFORMATION output)
{
    DWORD saved=GetLastError();
    BOOL helper = terminal_driver_helper(app ? app : cmd, TRUE, app == NULL);
    unsigned kind=terminal_executable_kind(app?app:cmd,TRUE,app==NULL);
    LONG sequence=terminal_trace_begin(0,kind,flags,inherit);
    SetLastError(saved);
    BOOL caller_suspended = (flags & CREATE_SUSPENDED) != 0;
    DWORD child_flags = helper ? flags | CREATE_SUSPENDED : flags;
    BOOL result=terminal_create_w(app,cmd,process_attr,thread_attr,inherit,child_flags,env,cwd,startup,output);
    saved=GetLastError();
    if (result && helper) {
        BOOL injected = terminal_inject_bridge(output);
        if (injected) injected = terminal_wait_bridge(output);
        terminal_trace_bridge(injected);
        if (!caller_suspended && output && output->hThread) ResumeThread(output->hThread);
    }
    terminal_trace_end(0,kind,sequence,result,saved);SetLastError(saved);return result;
}

static HINSTANCE WINAPI traced_terminal_shell_execute_w(HWND window, LPCWSTR operation, LPCWSTR file,
                                                        LPCWSTR parameters, LPCWSTR directory, INT show)
{
    BOOL helper = terminal_driver_helper(file, TRUE, FALSE);
    if (!helper) {
        HINSTANCE result = terminal_shell_execute_w(window, operation, file, parameters, directory, show);
        terminal_trace_shell(terminal_executable_kind(file, TRUE, FALSE), (INT_PTR)result > 32,
                             (INT_PTR)result > 32 ? ERROR_SUCCESS : ERROR_GEN_FAILURE);
        return result;
    }
    WCHAR command[32768];
    size_t file_length = file ? wcslen(file) : 0;
    size_t parameter_length = parameters ? wcslen(parameters) : 0;
    if (!file_length || file_length + parameter_length + 4 >= ARRAYSIZE(command)) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return (HINSTANCE)(INT_PTR)SE_ERR_ACCESSDENIED;
    }
    command[0] = L'"';
    memcpy(command + 1, file, file_length * sizeof(WCHAR));
    command[file_length + 1] = L'"';
    if (parameter_length) {
        command[file_length + 2] = L' ';
        memcpy(command + file_length + 3, parameters, parameter_length * sizeof(WCHAR));
    }
    command[file_length + parameter_length + 2 + (parameter_length ? 1 : 0)] = 0;
    STARTUPINFOW startup = {.cb = sizeof(startup)};
    PROCESS_INFORMATION process = {0};
    BOOL result = terminal_create_w(NULL, command, NULL, NULL, FALSE, CREATE_SUSPENDED,
                                    NULL, directory, &startup, &process);
    DWORD saved = GetLastError();
    if (result) {
        BOOL injected = terminal_inject_bridge(&process);
        if (injected) injected = terminal_wait_bridge(&process);
        terminal_trace_bridge(injected);
        if (process.hThread) ResumeThread(process.hThread);
        if (process.hThread) CloseHandle(process.hThread);
        if (process.hProcess) CloseHandle(process.hProcess);
        if (!injected) {
            terminal_trace_shell(7, FALSE, ERROR_GEN_FAILURE);
            SetLastError(ERROR_GEN_FAILURE);
            return (HINSTANCE)(INT_PTR)SE_ERR_ACCESSDENIED;
        }
        terminal_trace_shell(7, TRUE, ERROR_SUCCESS);
        SetLastError(ERROR_SUCCESS);
        return (HINSTANCE)(INT_PTR)32;
    }
    SetLastError(saved);
    terminal_trace_shell(7, FALSE, saved);
    return (HINSTANCE)(INT_PTR)SE_ERR_ACCESSDENIED;
}

static BOOL WINAPI traced_terminal_verify_version_w(LPOSVERSIONINFOEXW version, DWORD type, DWORDLONG mask)
{
    BOOL result = terminal_verify_version_w(version, type, mask);
    DWORD saved = GetLastError();
    DWORD major = version ? version->dwMajorVersion : 0;
    DWORD minor = version ? version->dwMinorVersion : 0;
    DWORD build = version ? version->dwBuildNumber : 0;
    char line[320];
    snprintf(line, sizeof(line),
             "UURB_NATIVE_TERMINAL {\"event\":\"verify_version\",\"type\":%lu,\"mask\":%llu,\"major\":%lu,\"minor\":%lu,\"build\":%lu,\"result\":%s,\"error\":%lu}\r\n",
             (unsigned long)type, (unsigned long long)mask, (unsigned long)major,
             (unsigned long)minor, (unsigned long)build, result ? "true" : "false",
             (unsigned long)saved);
    write_log(line); flush_log();
    /* Wine's compatibility version can lag the Windows build required by
     * UU's IDD feature.  Accept a bounded Windows 10/11 workstation check so
     * the user-space IDD adapter reaches the actual SetupAPI path. */
    if (!result && version && (type & (VER_MAJORVERSION | VER_MINORVERSION | VER_BUILDNUMBER)) &&
        major <= 10 && minor <= 1 && build <= 22621) {
        SetLastError(ERROR_SUCCESS);
        return TRUE;
    }
    SetLastError(saved);
    return result;
}

static DWORD WINAPI traced_terminal_get_version(void)
{
    DWORD original = terminal_get_version();
    char line[256];
    snprintf(line, sizeof(line),
             "UURB_NATIVE_TERMINAL {\"event\":\"get_version\",\"original\":%lu,\"reported\":%lu}\r\n",
             (unsigned long)original, (unsigned long)((22621u << 16) | 10u));
    write_log(line); flush_log();
    return (22621u << 16) | 10u;
}

static BOOL WINAPI traced_terminal_get_version_ex_w(LPOSVERSIONINFOW version)
{
    BOOL result = terminal_get_version_ex_w(version);
    DWORD saved = GetLastError();
    if (result && version) {
        version->dwMajorVersion = 10;
        version->dwMinorVersion = 0;
        version->dwBuildNumber = 22621;
        version->dwPlatformId = VER_PLATFORM_WIN32_NT;
    }
    char line[256];
    snprintf(line, sizeof(line),
             "UURB_NATIVE_TERMINAL {\"event\":\"get_version_ex\",\"success\":%s,\"reported\":\"10.0.22621\",\"error\":%lu}\r\n",
             result ? "true" : "false", (unsigned long)saved);
    write_log(line); flush_log();
    SetLastError(saved);
    return result;
}

static LONG WINAPI traced_terminal_rtl_get_version(LPOSVERSIONINFOW version)
{
    LONG result = terminal_rtl_get_version(version);
    DWORD saved = GetLastError();
    if (version) {
        version->dwMajorVersion = 10;
        version->dwMinorVersion = 0;
        version->dwBuildNumber = 22621;
        version->dwPlatformId = VER_PLATFORM_WIN32_NT;
        result = 0;
    }
    char line[256];
    snprintf(line, sizeof(line),
             "UURB_NATIVE_TERMINAL {\"event\":\"rtl_get_version\",\"reported\":\"10.0.22621\",\"error\":%lu}\r\n",
             (unsigned long)saved);
    write_log(line); flush_log();
    SetLastError(saved);
    return result;
}
/* UU 4.42's Super Screen entry returns 501 when NetWkstaGetInfo(100)
 * reports a major version below 10. Wine's netapi32 returns 6.2 independently
 * of RtlGetVersion. Keep its allocation/ownership and all identity fields;
 * only local version fields follow the enabled native display capability.
 * Remote queries, failures, and unsupported levels are exact passthrough. */
static NET_API_STATUS WINAPI traced_terminal_net_wksta_get_info(LMSTR server, DWORD level, LPBYTE *buffer)
{
    NET_API_STATUS status = terminal_net_wksta_get_info(server, level, buffer);
    DWORD saved = GetLastError(), major = 0, minor = 0;
    BOOL changed = FALSE;
    if (status == NERR_Success && !server && buffer && *buffer &&
        (level == 100 || level == 101 || level == 102)) {
        /* The initial version fields share the WKSTA_INFO_100 layout. */
        WKSTA_INFO_100 *info = (WKSTA_INFO_100 *)*buffer;
        major = info->wki100_ver_major; minor = info->wki100_ver_minor;
        if (native_version_capability_enabled && major < 10) {
            info->wki100_ver_major = 10;
            info->wki100_ver_minor = 0;
            changed = TRUE;
        }
    }
    char line[384];
    snprintf(line, sizeof(line),
        "UURB_NATIVE_TERMINAL {\"event\":\"net_wksta_version\",\"pid\":%lu,\"level\":%lu,\"local\":%s,\"status\":%lu,\"original_major\":%lu,\"original_minor\":%lu,\"overridden\":%s,\"reported_major\":%lu}\r\n",
        (unsigned long)GetCurrentProcessId(), (unsigned long)level, server ? "false" : "true",
        (unsigned long)status, (unsigned long)major, (unsigned long)minor,
        changed ? "true" : "false", (unsigned long)(changed ? 10 : major));
    write_log(line); flush_log();
    SetLastError(saved);
    return status;
}
static BOOL WINAPI traced_terminal_create_a(LPCSTR app, LPSTR cmd, LPSECURITY_ATTRIBUTES process_attr,
    LPSECURITY_ATTRIBUTES thread_attr, BOOL inherit, DWORD flags, LPVOID env, LPCSTR cwd,
    LPSTARTUPINFOA startup, LPPROCESS_INFORMATION output)
{
    DWORD saved=GetLastError();
    unsigned kind=terminal_executable_kind(app?app:cmd,FALSE,app==NULL);
    LONG sequence=terminal_trace_begin(1,kind,flags,inherit);
    SetLastError(saved);
    BOOL result=terminal_create_a(app,cmd,process_attr,thread_attr,inherit,flags,env,cwd,startup,output);
    saved=GetLastError();terminal_trace_end(1,kind,sequence,result,saved);SetLastError(saved);return result;
}
static BOOL WINAPI traced_terminal_create_user_w(HANDLE token, LPCWSTR app, LPWSTR cmd,
    LPSECURITY_ATTRIBUTES process_attr, LPSECURITY_ATTRIBUTES thread_attr, BOOL inherit, DWORD flags,
    LPVOID env, LPCWSTR cwd, LPSTARTUPINFOW startup, LPPROCESS_INFORMATION output)
{
    DWORD saved=GetLastError();
    unsigned kind=terminal_executable_kind(app?app:cmd,TRUE,app==NULL);
    LONG sequence=terminal_trace_begin(2,kind,flags,inherit);
    SetLastError(saved);
    BOOL result=terminal_create_user_w(token,app,cmd,process_attr,thread_attr,inherit,flags,env,cwd,startup,output);
    saved=GetLastError();terminal_trace_end(2,kind,sequence,result,saved);SetLastError(saved);return result;
}
static void *native_terminal_hook_for(uintptr_t original)
{
    if (!original) return NULL;
    if (original==(uintptr_t)terminal_create_w) return (void *)&traced_terminal_create_w;
    if (original==(uintptr_t)terminal_create_a) return (void *)&traced_terminal_create_a;
    if (original==(uintptr_t)terminal_create_user_w) return (void *)&traced_terminal_create_user_w;
    if (original==(uintptr_t)terminal_shell_execute_w) return (void *)&traced_terminal_shell_execute_w;
    if (original==(uintptr_t)terminal_verify_version_w) return (void *)&traced_terminal_verify_version_w;
    if (original==(uintptr_t)terminal_get_version) return (void *)&traced_terminal_get_version;
    if (original==(uintptr_t)terminal_get_version_ex_w) return (void *)&traced_terminal_get_version_ex_w;
    if (original==(uintptr_t)terminal_rtl_get_version) return (void *)&traced_terminal_rtl_get_version;
    if (original==(uintptr_t)terminal_net_wksta_get_info) return (void *)&traced_terminal_net_wksta_get_info;
    return NULL;
}
static void native_terminal_trace_initialize(void)
{
    HMODULE kernel=GetModuleHandleW(L"kernel32.dll"), advapi=GetModuleHandleW(L"advapi32.dll"), shell=GetModuleHandleW(L"shell32.dll");
    terminal_create_w=(void *)GetProcAddress(kernel,"CreateProcessW");
    terminal_create_a=(void *)GetProcAddress(kernel,"CreateProcessA");
    if (advapi) terminal_create_user_w=(void *)GetProcAddress(advapi,"CreateProcessAsUserW");
    if (shell) terminal_shell_execute_w=(void *)GetProcAddress(shell,"ShellExecuteW");
    terminal_verify_version_w=(void *)GetProcAddress(kernel,"VerifyVersionInfoW");
    terminal_get_version=(void *)GetProcAddress(kernel,"GetVersion");
    terminal_get_version_ex_w=(void *)GetProcAddress(kernel,"GetVersionExW");
    HMODULE netapi = GetModuleHandleW(L"netapi32.dll");
    if (netapi) terminal_net_wksta_get_info=(void *)GetProcAddress(netapi,"NetWkstaGetInfo");
    HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    if (ntdll) {
        FARPROC rtl = GetProcAddress(ntdll, "RtlGetVersion");
        if (rtl) memcpy(&terminal_rtl_get_version, &rtl, sizeof(terminal_rtl_get_version));
    }
}
#endif
