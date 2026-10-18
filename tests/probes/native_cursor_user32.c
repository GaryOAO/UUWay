#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static void write_log(const char *line) { fputs(line, stdout); }
#include "uu_native_cursor_win.h"
#define CHECK(value) do { if (!(value)) { fprintf(stderr, "USER32 check failed: %s\n", #value); return 1; } } while (0)
int wmain(int argc, WCHAR **argv)
{
    (void)native_cursor_import_slots;
    CHECK(argc == 1 || argc == 4);
    if (argc == 4) CHECK(SetEnvironmentVariableW(L"UURB_CURSOR_STATE_PATH", argv[1]));
    CHECK(native_cursor_initialize() == 1);
    CURSORINFO info = {.cbSize = sizeof(info)};
    CHECK(native_cursor_info(&info));
    printf("USER32_CURSOR showing=%u x=%ld y=%ld\n", info.flags == CURSOR_SHOWING,
           info.ptScreenPos.x, info.ptScreenPos.y);
    if (argc == 1) return 0;
    CHECK(info.flags == CURSOR_SHOWING && info.hCursor);
    CHECK(info.ptScreenPos.x == wcstol(argv[2], NULL, 10) && info.ptScreenPos.y == wcstol(argv[3], NULL, 10));
    POINT position;
    CHECK(native_cursor_pos(&position) && position.x == info.ptScreenPos.x && position.y == info.ptScreenPos.y);
    ICONINFO icon;
    CHECK(GetIconInfo(info.hCursor, &icon));
    BITMAP bitmap;
    CHECK(GetObjectW(icon.hbmColor, sizeof(bitmap), &bitmap) == sizeof(bitmap));
    CHECK(bitmap.bmWidth == 2 && bitmap.bmHeight == 2 && icon.xHotspot == 1 && icon.yHotspot == 1);
    DeleteObject(icon.hbmColor); DeleteObject(icon.hbmMask);
    puts("USER32 real cursor handle and desktop position passed");
    return 0;
}
