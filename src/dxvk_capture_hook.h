#ifndef UURB_DXVK_CAPTURE_HOOK_H
#define UURB_DXVK_CAPTURE_HOOK_H
/* Included only by the isolated, pinned DXVK 3.1 build. No vtable patching,
 * app DLL modification or implicit capture. Legacy inherited-FD mode is
 * one-shot. Socket mode opens a fresh close-on-exec channel per duplication.
 * After dispatch an inherited FD may be recycled: never retry that FD. */
#include <d3d11.h>
#include <dxgi1_5.h>
#include <climits>
#include <cwchar>
#include <cstring>
namespace uurb_capture {
using Create = HRESULT (WINAPI *)(ID3D11Device *, int, IUnknown *, IDXGIOutputDuplication **);
using Endpoint = HRESULT (WINAPI *)(ID3D11Device *, const char *, IUnknown *, IDXGIOutputDuplication **);
static INIT_ONCE loader_once = INIT_ONCE_STATIC_INIT;
static Create create = nullptr;
static Endpoint create_endpoint = nullptr;
static LONG claimed = 0;
static BOOL CALLBACK load(PINIT_ONCE, PVOID, PVOID *) {
  HMODULE self = nullptr;
  WCHAR location[32768];
  const WCHAR name[] = L"uurb-dxgi-capture-loader.dll";
  if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
      reinterpret_cast<LPCWSTR>(&loader_once), &self)) return FALSE;
  DWORD length = GetModuleFileNameW(self, location, sizeof(location) / sizeof(*location));
  if (!length || length >= sizeof(location) / sizeof(*location)) return FALSE;
  WCHAR *base = std::wcsrchr(location, L'\\');
  if (!base || size_t(++base - location) + sizeof(name) / sizeof(*name) > sizeof(location) / sizeof(*location))
    return FALSE;
  std::memcpy(base, name, sizeof(name));
  HMODULE loader = LoadLibraryW(location);
  if (!loader) return FALSE;
  Create function = reinterpret_cast<Create>(GetProcAddress(loader, "UurbCreateDuplication"));
  Endpoint endpoint = reinterpret_cast<Endpoint>(GetProcAddress(loader, "UurbCreateDuplicationEndpoint"));
  if (!function || !endpoint) { FreeLibrary(loader); return FALSE; }
  // Both references are pinned: the returned COM object retains backend code.
  if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
      reinterpret_cast<LPCWSTR>(&loader_once), &self)) { FreeLibrary(loader); return FALSE; }
  create = function;
  create_endpoint = endpoint;
  return TRUE;
}
static HRESULT duplicate(IDXGIOutput *output, IUnknown *device, UINT flags, UINT count,
    const DXGI_FORMAT *formats, IDXGIOutputDuplication **out) {
  if (!out) return E_INVALIDARG;
  *out = nullptr;
  if (!output || !device || (count && !formats) || count > 64) return E_INVALIDARG;
  if (flags) return DXGI_ERROR_UNSUPPORTED;
  bool bgra = count == 0; // Legacy DuplicateOutput forwards a zero-length list.
  for (UINT i = 0; i < count; ++i) bgra |= formats[i] == DXGI_FORMAT_B8G8R8A8_UNORM;
  if (!bgra) return DXGI_ERROR_UNSUPPORTED;

  WCHAR descriptor[16], target[CCHDEVICENAME], endpoint_w[108];
  char endpoint[108] = {};
  DWORD endpoint_length = GetEnvironmentVariableW(L"UURB_DXGI_CAPTURE_SOCKET", endpoint_w, 108);
  if (endpoint_length >= 108) return E_INVALIDARG;
  for (DWORD i = 0; i < endpoint_length; ++i) {
    if (endpoint_w[i] < 0x20 || endpoint_w[i] > 0x7e) return E_INVALIDARG;
    endpoint[i] = char(endpoint_w[i]);
  }
  DWORD length = GetEnvironmentVariableW(L"UURB_DXGI_CAPTURE_FD", descriptor, 16);
  if (!length && !endpoint_length) return DXGI_ERROR_UNSUPPORTED;
  if (length >= 16 || (length && endpoint_length) || (endpoint_length && endpoint[0] != '/')) return E_INVALIDARG;
  int fd = 0;
  for (DWORD i = 0; i < length; ++i) {
    if (descriptor[i] < L'0' || descriptor[i] > L'9') return E_INVALIDARG;
    int digit = descriptor[i] - L'0';
    if (fd > (INT_MAX - digit) / 10) return E_INVALIDARG;
    fd = fd * 10 + digit;
  }
  if (!endpoint_length && fd < 3) return E_INVALIDARG;
  length = GetEnvironmentVariableW(L"UURB_DXGI_CAPTURE_OUTPUT", target, CCHDEVICENAME);
  if (!length || length >= CCHDEVICENAME) return E_INVALIDARG;
  DXGI_OUTPUT_DESC description = {};
  if (FAILED(output->GetDesc(&description)) || !description.AttachedToDesktop ||
      std::wcscmp(description.DeviceName, target)) return DXGI_ERROR_UNSUPPORTED;

  ID3D11Device *d3d = nullptr;
  IDXGIDevice *dxgi = nullptr;
  IDXGIAdapter *device_adapter = nullptr, *output_adapter = nullptr;
  HRESULT result = device->QueryInterface(__uuidof(ID3D11Device), reinterpret_cast<void **>(&d3d));
  if (FAILED(result)) return E_INVALIDARG;
  result = d3d->QueryInterface(__uuidof(IDXGIDevice), reinterpret_cast<void **>(&dxgi));
  if (SUCCEEDED(result)) result = dxgi->GetAdapter(&device_adapter);
  if (SUCCEEDED(result)) result = output->GetParent(__uuidof(IDXGIAdapter), reinterpret_cast<void **>(&output_adapter));
  DXGI_ADAPTER_DESC a = {}, b = {};
  if (SUCCEEDED(result)) result = device_adapter->GetDesc(&a);
  if (SUCCEEDED(result)) result = output_adapter->GetDesc(&b);
  if (SUCCEEDED(result) && (a.VendorId != 0x10de || a.AdapterLuid.LowPart != b.AdapterLuid.LowPart ||
      a.AdapterLuid.HighPart != b.AdapterLuid.HighPart)) result = E_INVALIDARG;
  if (output_adapter) output_adapter->Release();
  if (device_adapter) device_adapter->Release();
  if (dxgi) dxgi->Release();
  if (SUCCEEDED(result) && !InitOnceExecuteOnce(&loader_once, load, nullptr, nullptr)) result = DXGI_ERROR_UNSUPPORTED;
  if (SUCCEEDED(result)) {
    if (endpoint_length) result = create_endpoint(d3d, endpoint, output, out);
    else if (InterlockedCompareExchange(&claimed, 1, 0)) result = DXGI_ERROR_NOT_CURRENTLY_AVAILABLE;
    else result = create(d3d, fd, output, out);
  }
  d3d->Release();
  return result;
}
}
#endif
