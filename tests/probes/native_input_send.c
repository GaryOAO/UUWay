#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>
#include <stdlib.h>
#include <stdio.h>

/* A real Windows consumer of the same native-only broker used by UU.
 * Sends only the explicitly selected test gesture. Not a remote session. */
struct request { DWORD magic, count, input_size; };
struct response { DWORD result, error; };
static BOOL transfer(HANDLE pipe, void *data, DWORD length, BOOL send)
{
    BYTE *position = data;
    while (length) {
        DWORD count = 0;
        BOOL ok = send ? WriteFile(pipe, position, length, &count, NULL)
                       : ReadFile(pipe, position, length, &count, NULL);
        if (!ok || !count) return FALSE;
        position += count; length -= count;
    }
    return TRUE;
}
int wmain(int argc, WCHAR **argv)
{
    INPUT events[256] = {0};
    DWORD count = 2;
    BOOL unsupported = FALSE;
    if (argc == 4 && !wcscmp(argv[1], L"move")) {
        WCHAR *end_x, *end_y;
        long x = wcstol(argv[2], &end_x, 10), y = wcstol(argv[3], &end_y, 10);
        if (!*argv[2] || !*argv[3] || *end_x || *end_y || x < 0 || x > 65535 || y < 0 || y > 65535) return 2;
        count = 1; events[0].type = INPUT_MOUSE;
        events[0].mi = (MOUSEINPUT){.dx = x, .dy = y, .dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE};
    } else if (argc == 2 && !wcscmp(argv[1], L"click")) {
        events[0].type = events[1].type = INPUT_MOUSE;
        events[0].mi.dwFlags = MOUSEEVENTF_LEFTDOWN; events[1].mi.dwFlags = MOUSEEVENTF_LEFTUP;
    } else if (argc == 2 && !wcscmp(argv[1], L"f8")) {
        events[0].type = events[1].type = INPUT_KEYBOARD;
        events[0].ki.wVk = events[1].ki.wVk = VK_F8; events[1].ki.dwFlags = KEYEVENTF_KEYUP;
    } else if (argc == 2 && (!wcscmp(argv[1], L"unicode-fixture") ||
                           !wcscmp(argv[1], L"revision-initial") ||
                           !wcscmp(argv[1], L"revision-update") ||
                           !wcscmp(argv[1], L"revision-excess") ||
                           !wcscmp(argv[1], L"revision-moved"))) {
        const WCHAR *text = L"UU 原生输入：中文🙂\n第二行\tABC";
        unsigned deletes = 0;
        if (!wcscmp(argv[1], L"revision-initial")) text = L"候选";
        if (!wcscmp(argv[1], L"revision-update")) { text = L"更新🙂"; deletes = 2; }
        if (!wcscmp(argv[1], L"revision-excess")) { text = L"拒绝"; deletes = 100; }
        if (!wcscmp(argv[1], L"revision-moved")) { text = L"拒绝"; deletes = 1; }
        count = 0;
        for (unsigned i = 0; i < deletes; ++i) {
            if (count + 2 > ARRAYSIZE(events)) return 2;
            events[count] = (INPUT){.type = INPUT_KEYBOARD, .ki = {.wVk = VK_BACK}};
            events[count + 1] = events[count];
            events[count + 1].ki.dwFlags = KEYEVENTF_KEYUP;
            count += 2;
        }
        for (unsigned i = 0; text[i]; ++i) {
            if (count + 2 > ARRAYSIZE(events)) return 2;
            events[count] = (INPUT){.type = INPUT_KEYBOARD,
                .ki = {.wScan = text[i], .dwFlags = KEYEVENTF_UNICODE}};
            events[count + 1] = events[count];
            events[count + 1].ki.dwFlags |= KEYEVENTF_KEYUP;
            count += 2;
        }
    } else if (argc == 2 && !wcscmp(argv[1], L"unsupported-text")) {
        unsupported = TRUE;
        events[0].type = events[1].type = INPUT_KEYBOARD;
        events[0].ki.wScan = events[1].ki.wScan = L'A';
        events[0].ki.dwFlags = KEYEVENTF_UNICODE; events[1].ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP;
    } else return 2;
    const WCHAR name[] = L"\\\\.\\pipe\\uurb-native-input-v1";
    if (!WaitNamedPipeW(name, 2000)) {
        printf("UURB_INPUT_PROBE stage=wait error=%lu\n", GetLastError()); return 1;
    }
    HANDLE pipe = CreateFileW(name, GENERIC_READ | GENERIC_WRITE, 0, NULL, OPEN_EXISTING, 0, NULL);
    if (pipe == INVALID_HANDLE_VALUE) {
        printf("UURB_INPUT_PROBE stage=open error=%lu\n", GetLastError()); return 1;
    }
    struct request request = {0x42525555, count, sizeof(INPUT)};
    struct response response = {0};
    BOOL ok = transfer(pipe, &request, sizeof(request), TRUE) &&
        transfer(pipe, events, count * sizeof(INPUT), TRUE) && transfer(pipe, &response, sizeof(response), FALSE);
    CloseHandle(pipe);
    printf("UURB_INPUT_PROBE stage=response transport=%u count=%lu error=%lu\n",
           (unsigned)ok, response.result, response.error);
    if (ok && argc == 2 && !wcsncmp(argv[1], L"revision-", 9) &&
        response.result == 0 && response.error != ERROR_SUCCESS) return 3;
    return !(ok && (unsupported ? response.result == 0 && response.error == ERROR_NOT_SUPPORTED
                               : response.result == count && response.error == ERROR_SUCCESS));
}
