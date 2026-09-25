#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <d3dcompiler.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "native_frame_protocol.h"

static ID3D11Device *device;
static ID3D11DeviceContext *context;
static IDXGISwapChain *swapchain;
static ID3D11RenderTargetView *target;
static ID3D11Texture2D *texture;
static ID3D11ShaderResourceView *source_view;
static ID3D11VertexShader *vertex_shader;
static ID3D11PixelShader *pixel_shader;
static ID3D11SamplerState *sampler;
static HCURSOR native_cursor;
static UINT texture_width, texture_height;
static LONG canvas_width, canvas_height;
/* DXVK 3.1 public interop IID; requiring this prevents accidental use of
 * Wine's builtin D3D11/OpenGL path, whose adapter identity can be spoofed.
 * https://github.com/doitsujin/dxvk/blob/v3.1/src/dxgi/dxgi_interfaces.h */
static const GUID uurb_vk_interop = {0xe2ef5fa5, 0xdc21, 0x4af7,
    {0x90, 0xc4, 0xf6, 0x7e, 0xf6, 0xa0, 0x93, 0x23}};

static uint32_t acquire(const uint32_t *word)
{
    /* The mapping is read-only: do not use interlocked RMW on it. x86-64
     * aligned loads are atomic; MemoryBarrier orders the following reads. */
    uint32_t value = *(volatile const uint32_t *)word;
    MemoryBarrier();
    return value;
}

static LRESULT CALLBACK window_proc(HWND window, UINT message, WPARAM wp, LPARAM lp)
{
    if (message == WM_DESTROY) { PostQuitMessage(0); return 0; }
    if (message == WM_SETCURSOR) { SetCursor(native_cursor); return TRUE; }
    if (message == WM_ERASEBKGND) return 1;
    return DefWindowProcW(window, message, wp, lp);
}

static BOOL init_renderer(HWND window)
{
    IDXGIFactory1 *factory = NULL;
    IDXGIAdapter1 *adapter = NULL;
    DXGI_ADAPTER_DESC1 desc;
    DXGI_SWAP_CHAIN_DESC chain;
    ID3D11Texture2D *backbuffer = NULL;
    ID3DBlob *vs = NULL, *ps = NULL, *errors = NULL;
    D3D11_SAMPLER_DESC sampling;
    D3D11_VIEWPORT viewport;
    HRESULT status;
    const char shader[] =
        "Texture2D img:register(t0); SamplerState sam:register(s0);"
        "struct V{float4 pos:SV_Position;float2 uv:TEXCOORD0;};"
        "V vs(uint id:SV_VertexID){V o;float2 p=float2((id<<1)&2,id&2);"
        "o.uv=p;o.pos=float4(p*float2(2,-2)+float2(-1,1),0,1);return o;}"
        "float4 ps(V i):SV_Target{return float4(img.Sample(sam,i.uv).rgb,1);}"
        ;
    if (FAILED(CreateDXGIFactory1(&IID_IDXGIFactory1, (void **)&factory))) return FALSE;
    /* Prefer NVIDIA; permit an actual Intel/AMD adapter, never WARP/llvmpipe. */
    for (UINT pass = 0; pass < 2 && device == NULL; ++pass) {
        for (UINT index = 0; IDXGIFactory1_EnumAdapters1(factory, index, &adapter) == S_OK; ++index) {
            IDXGIAdapter1_GetDesc1(adapter, &desc);
            BOOL allowed = !(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) &&
                (desc.VendorId == 0x10de || desc.VendorId == 0x8086 || desc.VendorId == 0x1002);
            if (!allowed || (pass == 0 && desc.VendorId != 0x10de)) {
                IDXGIAdapter1_Release(adapter); adapter = NULL; continue;
            }
            memset(&chain, 0, sizeof(chain));
            chain.BufferDesc.Width = canvas_width;
            chain.BufferDesc.Height = canvas_height;
            chain.BufferDesc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
            chain.SampleDesc.Count = 1;
            chain.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
            chain.BufferCount = 2;
            chain.OutputWindow = window;
            chain.Windowed = TRUE;
            chain.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;
            D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_11_0;
            status = D3D11CreateDeviceAndSwapChain((IDXGIAdapter *)adapter,
                D3D_DRIVER_TYPE_UNKNOWN, NULL, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                &level, 1, D3D11_SDK_VERSION, &chain, &swapchain, &device, NULL, &context);
            IDXGIAdapter1_Release(adapter); adapter = NULL;
            if (SUCCEEDED(status)) {
                fprintf(stderr, "Native D3D11 adapter: %ls vendor=%04x device=%04x\n",
                    desc.Description, desc.VendorId, desc.DeviceId);
                break;
            }
            fprintf(stderr, "D3D11 adapter initialization failed: 0x%08lx\n", (unsigned long)status);
        }
    }
    IDXGIFactory1_Release(factory);
    if (device == NULL) {
        fprintf(stderr, "No usable hardware D3D11 adapter. Software fallback is disabled.\n");
        return FALSE;
    }
    IUnknown *interop = NULL;
    if (FAILED(ID3D11Device_QueryInterface(device, &uurb_vk_interop, (void **)&interop))) {
        fprintf(stderr, "DXVK Vulkan interop required; refusing unverified Wine builtin renderer.\n");
        return FALSE;
    }
    IUnknown_Release(interop);
    fprintf(stderr, "DXVK Vulkan interop verified; capture/upload still use CPU memory.\n");
    if (FAILED(IDXGISwapChain_GetBuffer(swapchain, 0, &IID_ID3D11Texture2D, (void **)&backbuffer))) return FALSE;
    status = ID3D11Device_CreateRenderTargetView(device, (ID3D11Resource *)backbuffer, NULL, &target);
    ID3D11Texture2D_Release(backbuffer);
    if (FAILED(status)) return FALSE;
    status = D3DCompile(shader, strlen(shader), NULL, NULL, NULL, "vs", "vs_4_0", 0, 0, &vs, &errors);
    if (FAILED(status)) goto shader_error;
    status = D3DCompile(shader, strlen(shader), NULL, NULL, NULL, "ps", "ps_4_0", 0, 0, &ps, &errors);
    if (FAILED(status)) goto shader_error;
    status = ID3D11Device_CreateVertexShader(device, ID3D10Blob_GetBufferPointer(vs), ID3D10Blob_GetBufferSize(vs), NULL, &vertex_shader);
    if (SUCCEEDED(status)) status = ID3D11Device_CreatePixelShader(device, ID3D10Blob_GetBufferPointer(ps), ID3D10Blob_GetBufferSize(ps), NULL, &pixel_shader);
    ID3D10Blob_Release(vs); ID3D10Blob_Release(ps);
    if (FAILED(status)) return FALSE;
    memset(&sampling, 0, sizeof(sampling));
    sampling.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sampling.AddressU = sampling.AddressV = sampling.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampling.MaxLOD = D3D11_FLOAT32_MAX;
    if (FAILED(ID3D11Device_CreateSamplerState(device, &sampling, &sampler))) return FALSE;
    viewport = (D3D11_VIEWPORT){0, 0, (FLOAT)canvas_width, (FLOAT)canvas_height, 0, 1};
    ID3D11DeviceContext_RSSetViewports(context, 1, &viewport);
    ID3D11DeviceContext_OMSetRenderTargets(context, 1, &target, NULL);
    ID3D11DeviceContext_IASetPrimitiveTopology(context, D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ID3D11DeviceContext_VSSetShader(context, vertex_shader, NULL, 0);
    ID3D11DeviceContext_PSSetShader(context, pixel_shader, NULL, 0);
    ID3D11DeviceContext_PSSetSamplers(context, 0, 1, &sampler);
    return TRUE;
shader_error:
    if (errors) fprintf(stderr, "Shader compilation: %s\n", (char *)ID3D10Blob_GetBufferPointer(errors));
    if (errors) ID3D10Blob_Release(errors);
    if (vs) ID3D10Blob_Release(vs);
    return FALSE;
}

static BOOL prepare_texture(UINT width, UINT height)
{
    if (width == texture_width && height == texture_height) return TRUE;
    ID3D11ShaderResourceView *none = NULL;
    ID3D11DeviceContext_PSSetShaderResources(context, 0, 1, &none);
    if (source_view) { ID3D11ShaderResourceView_Release(source_view); source_view = NULL; }
    if (texture) { ID3D11Texture2D_Release(texture); texture = NULL; }
    D3D11_TEXTURE2D_DESC desc;
    memset(&desc, 0, sizeof(desc));
    desc.Width = width; desc.Height = height; desc.MipLevels = desc.ArraySize = 1;
    desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM; desc.SampleDesc.Count = 1;
    desc.Usage = D3D11_USAGE_DEFAULT; desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    if (FAILED(ID3D11Device_CreateTexture2D(device, &desc, NULL, &texture))) return FALSE;
    if (FAILED(ID3D11Device_CreateShaderResourceView(device, (ID3D11Resource *)texture, NULL, &source_view))) return FALSE;
    texture_width = width; texture_height = height;
    fprintf(stderr, "Native frame texture: %ux%u; GPU output: %ldx%ld\n", width, height, canvas_width, canvas_height);
    return TRUE;
}

static void update_cursor(const uint32_t *header, HWND window, int origin_x, int origin_y)
{
    static uint32_t previous_serial;
    static int previous_x = -1, previous_y = -1;
    uint32_t seq = acquire(header + UURB_CURSOR_META);
    if (seq & 1) return;
    int x = (int32_t)header[33], y = (int32_t)header[34];
    UINT width = header[35], height = header[36], hx = header[37], hy = header[38];
    uint32_t serial = header[39];
    if (texture_width == 0 || width == 0 || height == 0 ||
        width > 256 || height > 256 || width * height * 4 > UURB_CURSOR_BYTES || hx >= width || hy >= height) return;
    if (serial != previous_serial) {
        BITMAPINFO bitmap;
        memset(&bitmap, 0, sizeof(bitmap));
        bitmap.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        bitmap.bmiHeader.biWidth = width; bitmap.bmiHeader.biHeight = -(LONG)height;
        bitmap.bmiHeader.biPlanes = 1; bitmap.bmiHeader.biBitCount = 32;
        void *pixels = NULL;
        HBITMAP color = CreateDIBSection(NULL, &bitmap, DIB_RGB_COLORS, &pixels, NULL, 0);
        if (!color) return;
        memcpy(pixels, (const BYTE *)header + UURB_FRAME_HEADER_BYTES, width * height * 4);
        MemoryBarrier();
        if (acquire(header + UURB_CURSOR_META) != seq) { DeleteObject(color); return; }
        BYTE mask_bits[8192] = {0};
        HBITMAP mask = CreateBitmap(width, height, 1, 1, mask_bits);
        ICONINFO info = {FALSE, hx, hy, mask, color};
        HCURSOR created = CreateIconIndirect(&info);
        DeleteObject(mask); DeleteObject(color);
        if (created) {
            HCURSOR old = native_cursor;
            native_cursor = created; SetCursor(created);
            if (previous_serial && old) DestroyCursor(old);
            previous_serial = serial;
        }
    }
    MemoryBarrier();
    if (acquire(header + UURB_CURSOR_META) != seq) return;
    x = MulDiv(x - origin_x, canvas_width, texture_width);
    y = MulDiv(y - origin_y, canvas_height, texture_height);
    if (x >= 0 && y >= 0 && x < canvas_width && y < canvas_height && (x != previous_x || y != previous_y)) {
        POINT point = {x, y}; ClientToScreen(window, &point);
        SetCursorPos(point.x, point.y); previous_x = x; previous_y = y;
    }
}

int wmain(int argc, wchar_t **argv)
{
    if (argc != 2) { fprintf(stderr, "usage: uu-native-presenter.exe SHARED_FRAME_FILE\n"); return 2; }
    HANDLE file = CreateFileW(argv[1], GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
        NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    LARGE_INTEGER size;
    if (file == INVALID_HANDLE_VALUE || !GetFileSizeEx(file, &size) || size.QuadPart != UURB_FRAME_FILE_BYTES) return 2;
    HANDLE mapping = CreateFileMappingW(file, NULL, PAGE_READONLY, 0, 0, NULL);
    const uint32_t *header = mapping ? MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, UURB_FRAME_FILE_BYTES) : NULL;
    if (!header || acquire(header) != UURB_FRAME_MAGIC || header[1] != UURB_FRAME_VERSION ||
        header[2] != UURB_FRAME_HEADER_BYTES || header[3] != UURB_FRAME_SLOT_BYTES) return 2;
    SetProcessDPIAware();
    canvas_width = GetSystemMetrics(SM_CXSCREEN); canvas_height = GetSystemMetrics(SM_CYSCREEN);
    native_cursor = LoadCursorW(NULL, (LPCWSTR)IDC_ARROW);
    WNDCLASSW wc = {0}; wc.lpfnWndProc = window_proc; wc.hInstance = GetModuleHandleW(NULL);
    wc.lpszClassName = L"UURBNativePresenter"; wc.hCursor = native_cursor;
    if (!RegisterClassW(&wc)) return 1;
    HWND window = CreateWindowW(wc.lpszClassName, L"Ubuntu-Desktop-Relay", WS_POPUP | WS_VISIBLE,
        0, 0, canvas_width, canvas_height, NULL, NULL, wc.hInstance, NULL);
    if (!window || !init_renderer(window)) return 1;
    uint32_t last_frame = 0, heartbeat = acquire(header + UURB_FRAME_HEARTBEAT);
    ULONGLONG heartbeat_at = GetTickCount64(), reported_at = heartbeat_at;
    unsigned presented = 0;
    int origin_x = 0, origin_y = 0;
    for (;;) {
        MSG msg;
        while (PeekMessageW(&msg, NULL, 0, 0, PM_REMOVE)) {
            if (msg.message == WM_QUIT) return 0;
            TranslateMessage(&msg); DispatchMessageW(&msg);
        }
        ULONGLONG now = GetTickCount64();
        uint32_t current_heartbeat = acquire(header + UURB_FRAME_HEARTBEAT);
        if (current_heartbeat != heartbeat) {
            heartbeat = current_heartbeat; heartbeat_at = now;
        } else if (now - heartbeat_at > 5000) {
            fprintf(stderr, "Native producer heartbeat stalled for 5 seconds; exiting.\n");
            return 3;
        }
        if (now - reported_at >= 5000) {
            fprintf(stderr, "Native presenter: %.1f changed frames/s\n",
                presented * 1000.0 / (double)(now - reported_at));
            presented = 0; reported_at = now;
        }
        UINT slot = acquire(header + UURB_FRAME_ACTIVE);
        if (slot > 1) return 2;
        const uint32_t *meta = header + UURB_FRAME_META(slot);
        uint32_t seq = acquire(meta + 3), frame = meta[4];
        UINT width = meta[0], height = meta[1], stride = meta[2];
        int next_origin_x = (int32_t)meta[5], next_origin_y = (int32_t)meta[6];
        MemoryBarrier();
        if ((seq & 1) || acquire(meta + 3) != seq) {
            MsgWaitForMultipleObjects(0, NULL, FALSE, 1, QS_ALLINPUT);
            continue;
        }
        if (!(seq & 1) && frame && frame != last_frame) {
            if (!uurb_frame_geometry_valid(width, height, stride)) return 2;
            if (!prepare_texture(width, height)) return 1;
            const BYTE *pixels = (const BYTE *)header + UURB_FRAME_DATA_OFFSET + slot * UURB_FRAME_SLOT_BYTES;
            ID3D11DeviceContext_UpdateSubresource(context, (ID3D11Resource *)texture, 0, NULL, pixels, stride, 0);
            MemoryBarrier();
            if (acquire(meta + 3) == seq) {
                origin_x = next_origin_x; origin_y = next_origin_y;
                ID3D11DeviceContext_PSSetShaderResources(context, 0, 1, &source_view);
                ID3D11DeviceContext_Draw(context, 3, 0);
                if (FAILED(IDXGISwapChain_Present(swapchain, 1, 0))) return 1;
                last_frame = frame;
                ++presented;
            }
        }
        update_cursor(header, window, origin_x, origin_y);
        MsgWaitForMultipleObjects(0, NULL, FALSE, 8, QS_ALLINPUT);
    }
}
