#ifndef UURB_NATIVE_TERMINAL_TRACE_WIN_H
#define UURB_NATIVE_TERMINAL_TRACE_WIN_H
/* Passive process-start diagnostics at reviewed vendor imports only.
 * Never replace the child, change its arguments/handles, or launch a shell.
 * Only a fixed executable category and numeric API result leave this code;
 * command lines, paths, tokens, environment, handles and pipe data do not. */
static BOOL (WINAPI *terminal_create_w)(LPCWSTR, LPWSTR, LPSECURITY_ATTRIBUTES,
    LPSECURITY_ATTRIBUTES, BOOL, DWORD, LPVOID, LPCWSTR, LPSTARTUPINFOW, LPPROCESS_INFORMATION);
static BOOL (WINAPI *terminal_create_a)(LPCSTR, LPSTR, LPSECURITY_ATTRIBUTES,
    LPSECURITY_ATTRIBUTES, BOOL, DWORD, LPVOID, LPCSTR, LPSTARTUPINFOA, LPPROCESS_INFORMATION);
static BOOL (WINAPI *terminal_create_user_w)(HANDLE, LPCWSTR, LPWSTR, LPSECURITY_ATTRIBUTES,
    LPSECURITY_ATTRIBUTES, BOOL, DWORD, LPVOID, LPCWSTR, LPSTARTUPINFOW, LPPROCESS_INFORMATION);
static volatile LONG terminal_trace_known, terminal_trace_other;
static volatile LONG native_terminal_import_slots;

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
    unsigned kind=terminal_executable_kind(app?app:cmd,TRUE,app==NULL);
    LONG sequence=terminal_trace_begin(0,kind,flags,inherit);
    SetLastError(saved);
    BOOL result=terminal_create_w(app,cmd,process_attr,thread_attr,inherit,flags,env,cwd,startup,output);
    saved=GetLastError();terminal_trace_end(0,kind,sequence,result,saved);SetLastError(saved);return result;
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
    return NULL;
}
static void native_terminal_trace_initialize(void)
{
    HMODULE kernel=GetModuleHandleW(L"kernel32.dll"), advapi=GetModuleHandleW(L"advapi32.dll");
    terminal_create_w=(void *)GetProcAddress(kernel,"CreateProcessW");
    terminal_create_a=(void *)GetProcAddress(kernel,"CreateProcessA");
    if (advapi) terminal_create_user_w=(void *)GetProcAddress(advapi,"CreateProcessAsUserW");
}
#endif
