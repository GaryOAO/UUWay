#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>
#include <string.h>
#include "native_display_api.h"
typedef DWORD (WINAPI *query_fn)(const char *, struct uurb_display_snapshot *, DWORD);
typedef DWORD (WINAPI *change_fn)(const char *, const struct uurb_display_request *, struct uurb_display_reply *);
typedef DWORD (WINAPI *default_fn)(const char *, struct uurb_display_mode *);
typedef DWORD (WINAPI *topology_fn)(const char *, struct uurb_display_topology *, DWORD);
typedef DWORD (WINAPI *virtual_fn)(const char *, DWORD, const void *, DWORD);
static query_fn query;
static change_fn change;
static default_fn defaults;
static topology_fn topology;
static virtual_fn virtual_control;
static INIT_ONCE once = INIT_ONCE_STATIC_INIT;
static BOOL CALLBACK initialize(PINIT_ONCE unused, PVOID value, PVOID *context)
{
    (void)unused; (void)value; (void)context;
    HMODULE self, backend;
    WCHAR location[32768], filename[] = L"uurb-native-display.dll.so";
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        (LPCWSTR)(uintptr_t)&once, &self)) return FALSE;
    DWORD length = GetModuleFileNameW(self, location, ARRAYSIZE(location));
    if (!length || length >= ARRAYSIZE(location)) return FALSE;
    WCHAR *base = wcsrchr(location, L'\\');
    if (!base || (size_t)(++base - location) + ARRAYSIZE(filename) > ARRAYSIZE(location)) return FALSE;
    memcpy(base, filename, sizeof(filename));
    backend = LoadLibraryW(location);
    if (!backend) return FALSE;
    query_fn q = (void *)GetProcAddress(backend, "UurbDisplayQueryV2");
    change_fn c = (void *)GetProcAddress(backend, "UurbDisplayChange");
    default_fn d = (void *)GetProcAddress(backend, "UurbDisplayDefault");
    /* Optional during rollback: an older backend has no topology export and
     * the adapter then keeps its single-output path. */
    topology_fn t = (void *)GetProcAddress(backend, "UurbDisplayTopology");
    if (!q || !c || !d || !GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
        (LPCWSTR)(uintptr_t)&once, &self)) { FreeLibrary(backend); return FALSE; }
    query = q; change = c; defaults = d; topology = t;
    virtual_control = (void *)GetProcAddress(backend, "UurbVirtualControl");
    return TRUE;
}
__declspec(dllexport) DWORD WINAPI UurbVirtualControl(const char *path, DWORD code, const void *input, DWORD bytes)
{
    if (!InitOnceExecuteOnce(&once, initialize, NULL, NULL) || !virtual_control) return UURB_DISPLAY_FAILED;
    return virtual_control(path, code, input, bytes);
}
__declspec(dllexport) DWORD WINAPI UurbDisplayDefault(const char *path, struct uurb_display_mode *output)
{
    if (!InitOnceExecuteOnce(&once, initialize, NULL, NULL)) return UURB_DISPLAY_FAILED;
    return defaults(path, output);
}
__declspec(dllexport) DWORD WINAPI UurbDisplayQuery(const char *path, void *output)
{
    (void)path; (void)output;
    return UURB_DISPLAY_INVALID;
}
__declspec(dllexport) DWORD WINAPI UurbDisplayQueryV2(const char *path, struct uurb_display_snapshot *output, DWORD output_bytes)
{
    if (!output || output_bytes != sizeof(*output)) return UURB_DISPLAY_INVALID;
    if (!InitOnceExecuteOnce(&once, initialize, NULL, NULL)) return UURB_DISPLAY_FAILED;
    return query(path, output, output_bytes);
}
__declspec(dllexport) DWORD WINAPI UurbDisplayChange(const char *path, const struct uurb_display_request *request, struct uurb_display_reply *output)
{
    if (!InitOnceExecuteOnce(&once, initialize, NULL, NULL)) return UURB_DISPLAY_FAILED;
    return change(path, request, output);
}
__declspec(dllexport) DWORD WINAPI UurbDisplayTopology(const char *path, struct uurb_display_topology *output, DWORD output_bytes)
{
    if (!output || output_bytes != sizeof(*output)) return UURB_DISPLAY_INVALID;
    if (!InitOnceExecuteOnce(&once, initialize, NULL, NULL) || !topology) return UURB_DISPLAY_FAILED;
    return topology(path, output, output_bytes);
}
