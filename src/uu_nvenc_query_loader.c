/* Test-only PE loader for the Winelib Windows->Linux query adapter.
 * No production nvEncodeAPI64.dll is built or installed. Load only the adjacent
 * backend by absolute path, never a backend from CWD/PATH. Not an encoder. */
#include <windows.h>
#include <ffnvcodec/nvEncodeAPI.h>
#include <string.h>
#include <wchar.h>

#ifndef UURB_NVENC_BACKEND_FILENAME
#define UURB_NVENC_BACKEND_FILENAME L"uurb-nvenc-query.dll.so"
#endif

typedef NVENCSTATUS (NVENCAPI *create_fn)(NV_ENCODE_API_FUNCTION_LIST *);
typedef NVENCSTATUS (NVENCAPI *version_fn)(uint32_t *);
static INIT_ONCE initialization = INIT_ONCE_STATIC_INIT;
static create_fn create_api;
static version_fn version_api;

static BOOL CALLBACK initialize(PINIT_ONCE once, PVOID parameter, PVOID *context)
{
    (void)once; (void)parameter; (void)context;
    HMODULE self = NULL, backend = NULL;
    WCHAR location[32768];
    const WCHAR filename[] = UURB_NVENC_BACKEND_FILENAME;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                           (LPCWSTR)(ULONG_PTR)&initialization, &self)) return FALSE;
    DWORD length = GetModuleFileNameW(self, location, sizeof(location) / sizeof(*location));
    if (!length || length >= sizeof(location) / sizeof(*location)) return FALSE;
    WCHAR *base = wcsrchr(location, L'\\');
    if (!base || (size_t)(++base - location) + sizeof(filename) / sizeof(*filename) > sizeof(location) / sizeof(*location))
        return FALSE;
    memcpy(base, filename, sizeof(filename));
    backend = LoadLibraryW(location);
    if (!backend) return FALSE;
    create_fn create = (void *)GetProcAddress(backend, "NvEncodeAPICreateInstance");
    version_fn version = (void *)GetProcAddress(backend, "NvEncodeAPIGetMaxSupportedVersion");
    if (!create || !version) { FreeLibrary(backend); return FALSE; }
    /* Function-table pointers can outlive the caller's HMODULE. Keep the loader
     * and its single backend reference alive until process exit. No DllMain work. */
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                           (LPCWSTR)(ULONG_PTR)&initialization, &self)) {
        FreeLibrary(backend); return FALSE;
    }
    create_api = create;
    version_api = version;
    return TRUE;
}

__declspec(dllexport) NVENCSTATUS NVENCAPI NvEncodeAPICreateInstance(NV_ENCODE_API_FUNCTION_LIST *api)
{
    if (!api) return NV_ENC_ERR_INVALID_PTR;
    if (!InitOnceExecuteOnce(&initialization, initialize, NULL, NULL)) return NV_ENC_ERR_NO_ENCODE_DEVICE;
    return create_api(api);
}

__declspec(dllexport) NVENCSTATUS NVENCAPI NvEncodeAPIGetMaxSupportedVersion(uint32_t *version)
{
    if (!version) return NV_ENC_ERR_INVALID_PTR;
    if (!InitOnceExecuteOnce(&initialization, initialize, NULL, NULL)) return NV_ENC_ERR_NO_ENCODE_DEVICE;
    return version_api(version);
}
