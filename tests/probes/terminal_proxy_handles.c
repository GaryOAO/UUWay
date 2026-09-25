/* Isolated regression: reproduce UU's inherited, bare-hex pipe handles.
 * No UU account or production process is used. Never dump shell output. */
#define main terminal_proxy_main
#include "../../src/uu_terminal_proxy.c"
#undef main
#include <assert.h>

static int await_marker(HANDLE pipe, HANDLE child, const char *marker)
{
    char data[65536] = {0};
    DWORD start = GetTickCount(), size = 0;
    while (GetTickCount() - start < 8000) {
        DWORD available = 0, count = 0;
        if (!PeekNamedPipe(pipe, NULL, 0, NULL, &available, NULL)) {
            printf("fixture_output_pipe_error=%lu\n", GetLastError());
            return 0;
        }
        if (available) {
            DWORD room = (DWORD)sizeof(data) - 1 - size;
            if (!room || !ReadFile(pipe, data + size,
                                   available < room ? available : room, &count, NULL)) return 0;
            size += count;
            data[size] = 0;
            if (strstr(data, marker)) return 1;
        }
        if (WaitForSingleObject(child, 0) != WAIT_TIMEOUT) {
            puts("fixture_child_exited");
            return 0;
        }
        Sleep(10);
    }
    printf("fixture_output_timeout bytes=%lu initial_size_seen=%d resized_size_seen=%d\n",
           size, strstr(data, "31 93") != NULL, strstr(data, "30 92") != NULL);
    return 0;
}

static int put(HANDLE pipe, const void *data, DWORD length)
{
    DWORD count = 0;
    return WriteFile(pipe, data, length, &count, NULL) && count == length;
}

int main(int argc, char **argv)
{
    HANDLE parsed = NULL;
    char *args[] = {"proxy", "--cols", "93", "--rows=31",
                    "--stdin-handle", "104", "--stdout-handle=2ac"};
    assert(parse_handle_value("104", &parsed) && (uintptr_t)parsed == 0x104);
    assert(parse_handle_value("2ac", &parsed) && (uintptr_t)parsed == 0x2ac);
    assert(parse_handle_value("0x2ac", &parsed) && (uintptr_t)parsed == 0x2ac);
    assert(!parse_handle_value("garbage", &parsed));
    assert(!parse_handle_value("0", &parsed));
    assert(!parse_handle_value("-1", &parsed));
    assert(!parse_handle_value(" 104", &parsed));
    assert(!parse_handle_value("ffffffffffffffff", &parsed));
    parse_bridge_arguments(sizeof(args) / sizeof(args[0]), args);
    assert(requested_columns == 93 && requested_rows == 31);
    assert((uintptr_t)proxy_input == 0x104 && (uintptr_t)proxy_output == 0x2ac);
    if (argc != 2) return 2;

    SECURITY_ATTRIBUTES sa = {sizeof(sa), NULL, TRUE};
    HANDLE in_read, in_write, out_read, out_write, ctl_read, ctl_write;
    assert(CreatePipe(&in_read, &in_write, &sa, 4096));
    assert(CreatePipe(&out_read, &out_write, &sa, 65536));
    assert(CreatePipe(&ctl_read, &ctl_write, &sa, 4096));
    assert(SetHandleInformation(in_write, HANDLE_FLAG_INHERIT, 0));
    assert(SetHandleInformation(out_read, HANDLE_FLAG_INHERIT, 0));
    assert(SetHandleInformation(ctl_write, HANDLE_FLAG_INHERIT, 0));
    char command[4096];
    snprintf(command, sizeof(command),
        "\"%s\" --shell powershell --cols 93 --rows 31 --stdin-handle %llx "
        "--stdout-handle %llx --ctl-handle %llx --uuyc-mux-session fixture-session",
        argv[1], (unsigned long long)(uintptr_t)in_read,
        (unsigned long long)(uintptr_t)out_write, (unsigned long long)(uintptr_t)ctl_read);
    STARTUPINFOA si = {0};
    PROCESS_INFORMATION pi = {0};
    si.cb = sizeof(si);
    /* Deliberately no usable stdio: only explicit handles can pass this test. */
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = si.hStdOutput = si.hStdError = INVALID_HANDLE_VALUE;
    assert(CreateProcessA(NULL, command, NULL, NULL, TRUE, CREATE_NO_WINDOW,
                          NULL, NULL, &si, &pi));
    CloseHandle(pi.hThread);
    CloseHandle(in_read); CloseHandle(out_write); CloseHandle(ctl_read);
    int result = 1;
    unsigned stage = 1;
    const char check[] = "printf '\\125URB_HANDLE_IO_OK\\n'; stty size\n";
    if (!put(in_write, check, sizeof(check) - 1) ||
        !await_marker(out_read, pi.hProcess, "31 93")) goto done;
    stage = 2;

    /* A byte-stream control message may be split across multiple writes. */
    const unsigned char resize[] = {1, 92, 0, 30, 0};
    if (!put(ctl_write, resize, 2)) goto done;
    Sleep(40);
    if (!put(ctl_write, resize + 2, 3)) goto done;
    Sleep(300);
    if (!put(in_write, "stty size\n", 10) ||
        !await_marker(out_read, pi.hProcess, "30 92")) goto done;
    stage = 3;
    const unsigned char close_request = 2;
    if (!put(ctl_write, &close_request, 1) ||
        WaitForSingleObject(pi.hProcess, 4000) != WAIT_OBJECT_0) goto done;
    DWORD status = 99;
    if (!GetExitCodeProcess(pi.hProcess, &status) || status != 0) goto done;
    result = 0;
done:
    if (result) printf("fixture_failed_stage=%u\n", stage);
    if (WaitForSingleObject(pi.hProcess, 0) == WAIT_TIMEOUT) {
        TerminateProcess(pi.hProcess, 99);
        WaitForSingleObject(pi.hProcess, 2000);
    }
    CloseHandle(pi.hProcess);
    CloseHandle(in_write); CloseHandle(out_read); CloseHandle(ctl_write);
    puts(result ? "TERMINAL_HANDLES_FAIL" : "TERMINAL_HANDLES_PASS");
    return result;
}
