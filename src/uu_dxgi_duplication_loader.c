/* PE loader: only the adjacent experimental Winelib backend is eligible. */
#include "uu_dxgi_duplication.h"
#include <wchar.h>
#include <string.h>
typedef HRESULT (WINAPI *create_fn)(ID3D11Device *, int, IUnknown *, IDXGIOutputDuplication **);
static INIT_ONCE once = INIT_ONCE_STATIC_INIT;
static create_fn create;
typedef HRESULT (WINAPI *endpoint_fn)(ID3D11Device *, const char *, IUnknown *, IDXGIOutputDuplication **);
static endpoint_fn create_endpoint;
static BOOL CALLBACK initialize(PINIT_ONCE init, PVOID arg, PVOID *context)
{
    (void)init; (void)arg; (void)context;
    WCHAR location[32768];
    const WCHAR filename[] = L"uurb-dxgi-capture.dll.so";
    HMODULE self = NULL;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        (LPCWSTR)(ULONG_PTR)&once, &self)) return FALSE;
    DWORD length = GetModuleFileNameW(self, location, sizeof(location) / sizeof(*location));
    if (!length || length >= sizeof(location) / sizeof(*location)) return FALSE;
    WCHAR *base = wcsrchr(location, L'\\');
    if (!base || (size_t)(++base - location) + sizeof(filename) / sizeof(*filename) > sizeof(location) / sizeof(*location))
        return FALSE;
    memcpy(base, filename, sizeof(filename));
    HMODULE backend = LoadLibraryW(location);
    if (!backend) return FALSE;
    create_fn fn = (void *)GetProcAddress(backend, "UurbCreateDuplication");
    endpoint_fn endpoint = (void *)GetProcAddress(backend, "UurbCreateDuplicationEndpoint");
    if (!fn || !endpoint || !GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                                 (LPCWSTR)(ULONG_PTR)&once, &self)) { FreeLibrary(backend); return FALSE; }
    /* COM vtables may outlive the caller's loader handle. Keep both loaded. */
    create = fn; create_endpoint = endpoint; return TRUE;
}
__declspec(dllexport) HRESULT WINAPI UurbCreateDuplication(ID3D11Device *device, int fd,
    IUnknown *parent, IDXGIOutputDuplication **out)
{
    if (!out || !device || fd < 3) return E_INVALIDARG;
    if (!InitOnceExecuteOnce(&once, initialize, NULL, NULL)) return DXGI_ERROR_UNSUPPORTED;
    return create(device, fd, parent, out);
}
__declspec(dllexport) HRESULT WINAPI UurbCreateDuplicationEndpoint(ID3D11Device *device, const char *path,
    IUnknown *parent, IDXGIOutputDuplication **out)
{
    if (!out || !device || !path) return E_INVALIDARG;
    *out = NULL;
    if (!InitOnceExecuteOnce(&once, initialize, NULL, NULL)) return DXGI_ERROR_UNSUPPORTED;
    return create_endpoint(device, path, parent, out);
}
