/* Real PE CreateProcess boundary. Launch only the adjacent capture probe;
 * this does not execute UU, authenticate an account or install services. */
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>
int wmain(int argc, wchar_t **argv)
{
    if (argc != 5 || (wcscmp(argv[3], L"h264") && wcscmp(argv[3], L"hevc")) ||
        (wcscmp(argv[4], L"duplicate1") && wcscmp(argv[4], L"legacy")) ||
        wcschr(argv[2], L'"') || wcslen(argv[2]) > 4096) return 2;
    long descriptor = 0;
    for (const wchar_t *p = argv[1]; *p; ++p) {
        if (*p < L'0' || *p > L'9' || descriptor > (0x7fffffff - (*p - L'0')) / 10) return 2;
        descriptor = descriptor * 10 + (*p - L'0');
    }
    if (descriptor < 3) return 2;
    WCHAR executable[32768], command[32768];
    DWORD size = GetModuleFileNameW(NULL, executable, 32768);
    if (!size || size >= 32768) return 1;
    WCHAR *base = wcsrchr(executable, L'\\');
    const WCHAR name[] = L"uu-dxgi-pe-capture-probe.exe";
    if (!base || ++base - executable + sizeof(name) / sizeof(*name) >= 32768) return 1;
    memcpy(base, name, sizeof(name));
    int count = _snwprintf(command, 32768, L"\"%ls\" %ld \"%ls\" %ls %ls",
                          executable, descriptor, argv[2], argv[3], argv[4]);
    if (count < 0 || count >= 32768) return 1;
    STARTUPINFOW startup = {.cb = sizeof(startup), .dwFlags = STARTF_USESTDHANDLES,
        .hStdInput = GetStdHandle(STD_INPUT_HANDLE), .hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE),
        .hStdError = GetStdHandle(STD_ERROR_HANDLE)};
    PROCESS_INFORMATION child = {0};
    if (!CreateProcessW(executable, command, NULL, NULL, TRUE, 0, NULL, NULL, &startup, &child)) {
        fprintf(stderr, "PE child creation failed: %lu\n", GetLastError()); return 1;
    }
    DWORD code = 4;
    if (WaitForSingleObject(child.hProcess, 45000) == WAIT_OBJECT_0)
        GetExitCodeProcess(child.hProcess, &code);
    else {
        TerminateProcess(child.hProcess, 4);
        WaitForSingleObject(child.hProcess, 5000);
    }
    CloseHandle(child.hThread); CloseHandle(child.hProcess);
    printf("UURB_PE_CHILD_COMPLETE %lu\n", code);
    return code;
}
