/* Real production import routing, in an isolated Wine prefix without UU. */
#define UURB_NATIVE_INPUT_ONLY
#include "../../src/uu_input_bridge.c"
#define CHECK(value) do { if (!(value)) { fprintf(stderr, "CURSOR channel check failed line %d: %s\n", __LINE__, #value); return 1; } } while (0)
typedef BOOL (WINAPI *cursor_fn)(PCURSORINFO);
static int check_view(cursor_fn query, BOOL visible, LONG x, LONG y)
{
    CURSORINFO info = {.cbSize = sizeof(info)};
    CHECK(query && query(&info));
    CHECK(info.flags == (visible ? CURSOR_SHOWING : 0));
    CHECK(visible ? info.hCursor != NULL : info.hCursor == NULL);
    CHECK(info.ptScreenPos.x == x && info.ptScreenPos.y == y);
    return 0;
}
int wmain(int argc, WCHAR **argv)
{
    CHECK(argc == 5);
    CHECK(SetEnvironmentVariableW(L"UURB_CURSOR_STATE_PATH", argv[1]));
    native_display_trace_initialize();
    CHECK(native_cursor_initialize() == 1);
    patch_native_module(GetModuleHandleW(NULL));
    LONG x = wcstol(argv[2], NULL, 10), y = wcstol(argv[3], NULL, 10);
    POINT position;
    (void)argv[4];
    CHECK(!check_view(GetCursorInfo, TRUE, x, y));
    cursor_fn client = (cursor_fn)(uintptr_t)GetProcAddress(GetModuleHandleW(L"user32.dll"), "GetCursorInfo");
    CHECK(!check_view(client, TRUE, x, y));
    HMODULE capture = LoadLibraryW(L"streamer.dll"), ordinary = LoadLibraryW(L"ordinary.dll");
    CHECK(capture && ordinary);
    patch_native_module(capture); patch_native_module(ordinary);
    cursor_fn capture_static = (cursor_fn)(uintptr_t)GetProcAddress(capture, "ProbeCursorStatic");
    cursor_fn capture_dynamic = (cursor_fn)(uintptr_t)GetProcAddress(capture, "ProbeCursorDynamic");
    cursor_fn ordinary_static = (cursor_fn)(uintptr_t)GetProcAddress(ordinary, "ProbeCursorStatic");
    cursor_fn ordinary_dynamic = (cursor_fn)(uintptr_t)GetProcAddress(ordinary, "ProbeCursorDynamic");
    CHECK(!check_view(capture_static, TRUE, x, y));
    CHECK(!check_view(capture_dynamic, TRUE, x, y));
    CHECK(!check_view(ordinary_static, TRUE, x, y));
    CHECK(!check_view(ordinary_dynamic, TRUE, x, y));

    /* Hide with no bitmap, as Mutter does while typing. Preserve coordinates
     * and the private cached shape; do not resurrect it as a visible cursor. */
    HANDLE state = CreateFileW(argv[1], GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING, 0, NULL);
    CHECK(state != INVALID_HANDLE_VALUE);
    struct uurb_cursor_header header, original; DWORD bytes;
    CHECK(ReadFile(state, &header, sizeof(header), &bytes, NULL) && bytes == sizeof(header));
    original = header;
    LARGE_INTEGER start = {.QuadPart = 0};
    header.sequence |= 1;
    CHECK(SetFilePointerEx(state, start, NULL, FILE_BEGIN));
    CHECK(WriteFile(state, &header, sizeof(header), &bytes, NULL) && bytes == sizeof(header));
    CHECK(!check_view(GetCursorInfo, TRUE, x, y));
    CHECK(!check_view(capture_dynamic, TRUE, x, y));
    CHECK(GetCursorPos(&position) && position.x == x && position.y == y);
    header = original; header.visible = 0; header.width = header.height = 0;

    CHECK(SetFilePointerEx(state, start, NULL, FILE_BEGIN));
    CHECK(WriteFile(state, &header, sizeof(header), &bytes, NULL) && bytes == sizeof(header));
    CHECK(!check_view(GetCursorInfo, FALSE, x, y));
    CHECK(!check_view(client, FALSE, x, y));
    CHECK(!check_view(ordinary_dynamic, FALSE, x, y));
    CHECK(!check_view(capture_dynamic, FALSE, x, y));
    CHECK(GetCursorPos(&position) && position.x == x && position.y == y);
    CHECK(SetFilePointerEx(state, start, NULL, FILE_BEGIN));
    CHECK(WriteFile(state, &original, sizeof(original), &bytes, NULL) && bytes == sizeof(original));
    CHECK(!check_view(GetCursorInfo, TRUE, x, y));
    CHECK(!check_view(capture_static, TRUE, x, y));
    header = original; header.sequence |= 1;
    CHECK(SetFilePointerEx(state, start, NULL, FILE_BEGIN));
    CHECK(WriteFile(state, &header, sizeof(header), &bytes, NULL) && bytes == sizeof(header));
    Sleep(120); /* An abandoned write cannot retain a cursor indefinitely. */
    CURSORINFO expired = {.cbSize = sizeof(expired)};
    CHECK(!GetCursorInfo(&expired) && GetLastError() == ERROR_NOT_READY);
    CHECK(SetFilePointerEx(state, start, NULL, FILE_BEGIN));
    CHECK(WriteFile(state, &original, sizeof(original), &bytes, NULL) && bytes == sizeof(original));
    CHECK(!check_view(GetCursorInfo, TRUE, x, y));
    CloseHandle(state); FreeLibrary(ordinary); FreeLibrary(capture);
    puts("USER32 client/capture imports, dynamic queries and hide/restore passed");
    return 0;
}
