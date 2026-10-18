/* One fixture is loaded as streamer.dll and ordinary.dll to exercise the
 * production module boundary through real imports and dynamic resolution. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
__declspec(dllexport) BOOL WINAPI ProbeCursorStatic(PCURSORINFO info)
{
    return GetCursorInfo(info);
}
__declspec(dllexport) BOOL WINAPI ProbeCursorDynamic(PCURSORINFO info)
{
    typedef BOOL (WINAPI *cursor_fn)(PCURSORINFO);
    cursor_fn query = (cursor_fn)(uintptr_t)GetProcAddress(GetModuleHandleW(L"user32.dll"), "GetCursorInfo");
    return query && query(info);
}
