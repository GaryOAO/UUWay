#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include "native_display_api.h"
DWORD WINAPI UurbDisplayDefault(const char *path, struct uurb_display_mode *output)
{
    return uurb_native_display_default(path, output);
}
DWORD WINAPI UurbDisplayQuery(const char *path, void *output)
{
    return uurb_native_display_query(path, output);
}
DWORD WINAPI UurbDisplayQueryV2(const char *path, struct uurb_display_snapshot *output, DWORD output_bytes)
{
    return uurb_native_display_query_v2(path, output, output_bytes);
}
DWORD WINAPI UurbDisplayChange(const char *path, const struct uurb_display_request *request, struct uurb_display_reply *output)
{
    return uurb_native_display_change(path, request, output);
}
