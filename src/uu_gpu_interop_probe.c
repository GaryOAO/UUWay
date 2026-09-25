/* Experimental Winelib probe. No UU hooks, capture, input, or installed changes.
 * Interop ABI: DXVK v3.1 src/dxgi/dxgi_interfaces.h.
 * Wine handle ABI: Wine 11.0 include/wine/server.h. */
#define _GNU_SOURCE
#define COBJMACROS
#include <windows.h>
#include <winternl.h>
#include <winioctl.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <dxgi1_2.h>
#include <vulkan/vulkan.h>
#include <dlfcn.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include "native_cuda_encode_session.h"
#include "uu_d3d11_frame_adapter.h"
#include "uu_d3d11_encode_session.h"

int uurb_wine11_gpu_fd(void *shared, void **opened);
int uurb_encoder_probe_lifecycle(int fd, uint64_t bytes, const unsigned char uuid[16],
    unsigned width, unsigned height);

#include "uu_dxvk_gpu_interop.h"

#define HR(call) do { HRESULT hr_ = (call); if (FAILED(hr_)) { \
    fprintf(stderr, "%s: HRESULT=%08x\n", #call, (unsigned)hr_); goto done; } } while (0)

#include "../tests/probes/d3d11_frame_adapter_checks.h"

struct draw_state {
    ID3D11VertexShader *vertex;
    ID3D11PixelShader *pixel;
    ID3D11SamplerState *sampler;
};
static void draw_destroy(struct draw_state *s)
{
    if (s->vertex) ID3D11VertexShader_Release(s->vertex);
    if (s->pixel) ID3D11PixelShader_Release(s->pixel);
    if (s->sampler) ID3D11SamplerState_Release(s->sampler);
}
static int draw_pattern(struct draw_state *s, ID3D11Device *device, ID3D11DeviceContext *context,
    ID3D11RenderTargetView *target, unsigned width, unsigned height, ID3D11ShaderResourceView *input,
    ID3D11ShaderResourceView *input_uv, int show_planes)
{
    int result = 1;
    ID3DBlob *vs = NULL, *ps = NULL, *errors = NULL;
    if (s->pixel) goto draw;
    char shader[1024];
    snprintf(shader, sizeof(shader),
        "float4 vs(uint id:SV_VertexID):SV_Position {float2 p=float2((id<<1)&2,id&2);"
        "return float4(p*float2(2,-2)+float2(-1,1),0,1);}"
        "float4 ps(float4 p:SV_Position):SV_Target {"
        "return p.x<%u ? (p.y<%u?float4(1,0,0,1):float4(0,0,1,1)):"
        "(p.y<%u?float4(0,1,0,1):float4(1,1,1,1));}", width/2, height/2, height/2);
    if (input) snprintf(shader, sizeof(shader),
        "Texture2D img:register(t0); SamplerState sm:register(s0);"
        "float4 vs(uint id:SV_VertexID):SV_Position {float2 p=float2((id<<1)&2,id&2);"
        "return float4(p*float2(2,-2)+float2(-1,1),0,1);}"
        "float4 ps(float4 p:SV_Position):SV_Target {return img.Sample(sm,p.xy/float2(%u,%u));}", width, height);
    if (input_uv) snprintf(shader, sizeof(shader),
        "Texture2D<float> yp:register(t0); Texture2D<float2> uvp:register(t1); SamplerState sm:register(s0);"
        "float4 vs(uint id:SV_VertexID):SV_Position {float2 p=float2((id<<1)&2,id&2);"
        "return float4(p*float2(2,-2)+float2(-1,1),0,1);}"
        "float4 ps(float4 p:SV_Position):SV_Target {float2 pos=p.xy/float2(%u,%u);"
        "float y=(yp.Sample(sm,pos)*255-16)/219; float2 uv=(uvp.Sample(sm,pos)*255-128)/224;"
        "return float4(%s,1);}", width, height, show_planes ? "float3(yp.Sample(sm,pos),uvp.Sample(sm,pos))" :
        "float3(y+1.5748*uv.y,y-0.187324*uv.x-0.468124*uv.y,y+1.8556*uv.x)");
    HR(D3DCompile(shader, strlen(shader), NULL, NULL, NULL, "vs", "vs_4_0", 0, 0, &vs, &errors));
    if (errors) { ID3D10Blob_Release(errors); errors = NULL; }
    HR(D3DCompile(shader, strlen(shader), NULL, NULL, NULL, "ps", "ps_4_0", 0, 0, &ps, &errors));
    HR(ID3D11Device_CreateVertexShader(device, ID3D10Blob_GetBufferPointer(vs), ID3D10Blob_GetBufferSize(vs), NULL, &s->vertex));
    HR(ID3D11Device_CreatePixelShader(device, ID3D10Blob_GetBufferPointer(ps), ID3D10Blob_GetBufferSize(ps), NULL, &s->pixel));
    if (input) {
        D3D11_SAMPLER_DESC sampling = {0};
        sampling.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
        sampling.AddressU = sampling.AddressV = sampling.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
        sampling.MaxLOD = D3D11_FLOAT32_MAX;
        HR(ID3D11Device_CreateSamplerState(device, &sampling, &s->sampler));
    }
draw:;
    D3D11_VIEWPORT viewport = {0, 0, width, height, 0, 1};
    ID3D11DeviceContext_RSSetViewports(context, 1, &viewport);
    ID3D11DeviceContext_OMSetRenderTargets(context, 1, &target, NULL);
    ID3D11DeviceContext_IASetPrimitiveTopology(context, D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ID3D11DeviceContext_VSSetShader(context, s->vertex, NULL, 0);
    ID3D11DeviceContext_PSSetShader(context, s->pixel, NULL, 0);
    if (input) {
        ID3D11DeviceContext_PSSetSamplers(context, 0, 1, &s->sampler);
        ID3D11ShaderResourceView *inputs[] = {input, input_uv};
        ID3D11DeviceContext_PSSetShaderResources(context, 0, 2, inputs);
    }
    ID3D11DeviceContext_Draw(context, 3, 0);
    ID3D11ShaderResourceView *no_input[] = {NULL, NULL};
    ID3D11DeviceContext_PSSetShaderResources(context, 0, 2, no_input);
    ID3D11RenderTargetView *none = NULL;
    ID3D11DeviceContext_OMSetRenderTargets(context, 1, &none, NULL);
    result = 0;
done:
    if (errors) ID3D10Blob_Release(errors);
    if (vs) ID3D10Blob_Release(vs);
    if (ps) ID3D10Blob_Release(ps);
    return result;
}

/* Generate each NV12 plane through ordinary GPU draws, avoiding fast-clear
 * metadata as a confounder. Only a 16-byte shader constant is uploaded; all
 * NV12 pixel values are generated on the GPU. This is a fixture, not a hot path. */
static int draw_nv12_plane(struct draw_state *s, ID3D11Device *device, ID3D11DeviceContext *context,
    ID3D11RenderTargetView *target, unsigned width, unsigned height, unsigned frame, int chroma, int spatial)
{
    int result = 1;
    ID3DBlob *vs = NULL, *ps = NULL, *errors = NULL;
    ID3D11Buffer *constants = NULL;
    if (!s->pixel) {
        char shader[1600];
        snprintf(shader, sizeof(shader),
            "cbuffer Frame:register(b0){uint frame_index;uint width;uint height;uint spatial;};"
            "struct O {float4 p:SV_Position; nointerpolation uint n:TEXCOORD0;};"
            "O vs(uint id:SV_VertexID){O o;float2 p=float2((id<<1)&2,id&2);"
            "o.p=float4(p*float2(2,-2)+float2(-1,1),0,1);o.n=frame_index;return o;}"
            "float4 ps(O o):SV_Target {float3 c=float3(o.n&3,(o.n>>2)&3,(o.n>>4)&3)/3.0;"
            "if(spatial) c=o.p.x<width/2 ? (o.p.y<height/2?float3(1,0,0):float3(0,0,1)):"
            "(o.p.y<height/2?float3(0,1,0):float3(1,1,1));"
            "return %s;}", chroma ?
            "float4((128+224*dot(c,float3(-0.114572,-0.385428,0.5)))/255,(128+224*dot(c,float3(0.5,-0.454153,-0.045847)))/255,0,1)" :
            "float4((16+219*dot(c,float3(0.2126,0.7152,0.0722)))/255,0,0,1)");
        HR(D3DCompile(shader, strlen(shader), NULL, NULL, NULL, "vs", "vs_4_0", 0, 0, &vs, &errors));
        if (errors) { ID3D10Blob_Release(errors); errors = NULL; }
        HR(D3DCompile(shader, strlen(shader), NULL, NULL, NULL, "ps", "ps_4_0", 0, 0, &ps, &errors));
        HR(ID3D11Device_CreateVertexShader(device, ID3D10Blob_GetBufferPointer(vs), ID3D10Blob_GetBufferSize(vs), NULL, &s->vertex));
        HR(ID3D11Device_CreatePixelShader(device, ID3D10Blob_GetBufferPointer(ps), ID3D10Blob_GetBufferSize(ps), NULL, &s->pixel));
    }
    D3D11_VIEWPORT viewport = {0, 0, width, height, 0, 1};
    const unsigned indices[4] = {frame, width, height, spatial};
    D3D11_BUFFER_DESC constant_desc = {.ByteWidth = sizeof(indices), .Usage = D3D11_USAGE_IMMUTABLE,
        .BindFlags = D3D11_BIND_CONSTANT_BUFFER};
    D3D11_SUBRESOURCE_DATA initial = {.pSysMem = indices};
    HR(ID3D11Device_CreateBuffer(device, &constant_desc, &initial, &constants));
    ID3D11DeviceContext_VSSetConstantBuffers(context, 0, 1, &constants);
    ID3D11DeviceContext_PSSetConstantBuffers(context, 0, 1, &constants);
    ID3D11DeviceContext_RSSetViewports(context, 1, &viewport);
    ID3D11DeviceContext_OMSetRenderTargets(context, 1, &target, NULL);
    ID3D11DeviceContext_IASetPrimitiveTopology(context, D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ID3D11DeviceContext_VSSetShader(context, s->vertex, NULL, 0);
    ID3D11DeviceContext_PSSetShader(context, s->pixel, NULL, 0);
    ID3D11DeviceContext_Draw(context, 3, 0);
    ID3D11RenderTargetView *none = NULL;
    ID3D11DeviceContext_OMSetRenderTargets(context, 1, &none, NULL);
    ID3D11Buffer *no_constants = NULL;
    ID3D11DeviceContext_VSSetConstantBuffers(context, 0, 1, &no_constants);
    ID3D11DeviceContext_PSSetConstantBuffers(context, 0, 1, &no_constants);
    result = 0;
done:
    if (constants) ID3D11Buffer_Release(constants);
    if (errors) ID3D10Blob_Release(errors);
    if (vs) ID3D10Blob_Release(vs);
    if (ps) ID3D10Blob_Release(ps);
    return result;
}


int main(int argc, char **argv)
{
    if ((argc != 5 && argc != 6) || (strcmp(argv[2], "pattern") && strcmp(argv[2], "clear") && strcmp(argv[2], "sequence") && strcmp(argv[2], "nv12") && strcmp(argv[2], "nv12_planes") && strcmp(argv[2], "nv12_pattern"))) {
        fprintf(stderr, "usage: uu-gpu-interop-probe.exe OUTPUT_DIRECTORY pattern|clear|sequence|nv12|nv12_planes|nv12_pattern WIDTH HEIGHT [FRAMES]\n"); return 2;
    }
    int show_planes = !strcmp(argv[2], "nv12_planes");
    int bridge_session = getenv("UURB_D3D11_ENCODE_SESSION") != NULL;
    if (bridge_session && show_planes) return 2;
    int spatial = !strcmp(argv[2], "nv12_pattern");
    int nv12 = !strcmp(argv[2], "nv12") || show_planes || spatial;
    unsigned frames = 1;
    if (argc == 6) {
        char *end = NULL;
        errno = 0;
        unsigned long value = strtoul(argv[5], &end, 10);
        if (errno || !*argv[5] || *end || !value || value > 600) return 2;
        frames = value;
    }
    unsigned dimensions[2];
    for (int i = 0; i < 2; ++i) {
        char *end = NULL;
        errno = 0;
        unsigned long value = strtoul(argv[3 + i], &end, 10);
        if (errno || !*argv[3 + i] || *end || value < 2 || value > 8192 || value % 2) {
            fprintf(stderr, "Dimensions must be even values from 2 to 8192\n"); return 2;
        }
        dimensions[i] = value;
    }
    if ((uint64_t)dimensions[0] * dimensions[1] > 16777216) {
        fprintf(stderr, "Synthetic texture exceeds 64 MiB bound\n"); return 2;
    }
    const char *(CDECL *wine_version)(void) =
        (void *)GetProcAddress(GetModuleHandleA("ntdll.dll"), "wine_get_version");
    if (!wine_version || strcmp(wine_version(), "11.0")) {
        fprintf(stderr, "Private interop probe requires exactly Wine 11.0\n"); return 2;
    }
    int result = 1, fd = -1;
    IDXGIFactory1 *factory = NULL;
    IDXGIAdapter1 *adapter = NULL;
    ID3D11Device *device = NULL;
    ID3D11DeviceContext *context = NULL;
    ID3D11Texture2D *texture = NULL;
    ID3D11Texture2D *source = NULL;
    ID3D11Texture2D *second_source = NULL;
    ID3D11RenderTargetView *source_target = NULL;
    ID3D11ShaderResourceView *source_view = NULL;
    ID3D11ShaderResourceView *uv_view = NULL;
    ID3D11RenderTargetView *uv_target = NULL;
    ID3D11RenderTargetView *target = NULL;
    IDXGIResource1 *resource = NULL;
    InteropDevice *interop = NULL;
    InteropSurface *surface = NULL;
    HANDLE shared = NULL;
    HMODULE vulkan = NULL;
    struct draw_state pattern_draw = {0}, sampling_draw = {0};
    struct draw_state nv12_draw[2] = {0};
    struct uurb_d3d11_frame_adapter *frame_adapter = NULL;
    struct transfer_state transfer = {0};
    struct uurb_encoder *encoders[2] = {NULL, NULL};
    struct uurb_d3d11_encoder *bridges[2] = {NULL, NULL};
    uint64_t registered[2][2] = {{0}};
    FILE *outputs[2] = {NULL, NULL}, *metadata = NULL;
    ID3D11Query *completed = NULL;
    int external_owned = 0;
    VkDevice vkdevice = VK_NULL_HANDLE;
    VkImage image = VK_NULL_HANDLE;
    HR(CreateDXGIFactory1(&IID_IDXGIFactory1, (void **)&factory));
    for (UINT index = 0; IDXGIFactory1_EnumAdapters1(factory, index, &adapter) == S_OK; ++index) {
        DXGI_ADAPTER_DESC1 desc;
        HR(IDXGIAdapter1_GetDesc1(adapter, &desc));
        if (desc.VendorId == 0x10de && !(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)) break;
        IDXGIAdapter1_Release(adapter); adapter = NULL;
    }
    if (!adapter) { fprintf(stderr, "No exposed NVIDIA hardware adapter\n"); goto done; }
    D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_11_0;
    HR(D3D11CreateDevice((IDXGIAdapter *)adapter, D3D_DRIVER_TYPE_UNKNOWN, NULL,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT, &level, 1, D3D11_SDK_VERSION, &device, NULL, &context));
    HR(ID3D11Device_QueryInterface(device, &interop_device_iid, (void **)&interop));
    D3D11_TEXTURE2D_DESC desc = {0};
    desc.Width = dimensions[0]; desc.Height = dimensions[1]; desc.MipLevels = 1; desc.ArraySize = 1;
    desc.Format = nv12 ? DXGI_FORMAT_NV12 : DXGI_FORMAT_B8G8R8A8_UNORM; desc.SampleDesc.Count = 1;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    HR(ID3D11Device_CreateTexture2D(device, &desc, NULL, &source));
    if (bridge_session) {
        HR(ID3D11Device_CreateTexture2D(device, &desc, NULL, &second_source));
        for (unsigned codec = 0; codec < 2; ++codec) {
            HR(uurb_d3d11_encoder_open(device, desc.Width, desc.Height, codec, 40000000, &bridges[codec]));
            HR(uurb_d3d11_encoder_register(bridges[codec], source, &registered[codec][0]));
            HR(uurb_d3d11_encoder_register(bridges[codec], second_source, &registered[codec][1]));
            if (SUCCEEDED(uurb_d3d11_encoder_release(bridges[codec]))) goto done;
            uint64_t extra[14], unchanged = UINT64_MAX;
            for (unsigned i = 0; i < 14; ++i) HR(uurb_d3d11_encoder_register(bridges[codec], source, &extra[i]));
            if (uurb_d3d11_encoder_register(bridges[codec], source, &unchanged) != E_OUTOFMEMORY ||
                unchanged != UINT64_MAX) goto done;
            for (unsigned i = 0; i < 14; ++i) HR(uurb_d3d11_encoder_unregister(bridges[codec], extra[i]));
        }
        if (SUCCEEDED(uurb_d3d11_encoder_unregister(bridges[0], registered[1][0]))) goto done;
    }
    if (nv12) {
        D3D11_RENDER_TARGET_VIEW_DESC rtv = {.Format = DXGI_FORMAT_R8_UNORM, .ViewDimension = D3D11_RTV_DIMENSION_TEXTURE2D};
        D3D11_SHADER_RESOURCE_VIEW_DESC srv = {.Format = DXGI_FORMAT_R8_UNORM, .ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D};
        srv.Texture2D.MipLevels = 1;
        HR(ID3D11Device_CreateRenderTargetView(device, (ID3D11Resource *)source, &rtv, &source_target));
        HR(ID3D11Device_CreateShaderResourceView(device, (ID3D11Resource *)source, &srv, &source_view));
        rtv.Format = srv.Format = DXGI_FORMAT_R8G8_UNORM;
        HR(ID3D11Device_CreateRenderTargetView(device, (ID3D11Resource *)source, &rtv, &uv_target));
        HR(ID3D11Device_CreateShaderResourceView(device, (ID3D11Resource *)source, &srv, &uv_view));
    } else {
        HR(ID3D11Device_CreateRenderTargetView(device, (ID3D11Resource *)source, NULL, &source_target));
        HR(ID3D11Device_CreateShaderResourceView(device, (ID3D11Resource *)source, NULL, &source_view));
    }
    desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    desc.MiscFlags = D3D11_RESOURCE_MISC_SHARED | D3D11_RESOURCE_MISC_SHARED_NTHANDLE;
    HR(ID3D11Device_CreateTexture2D(device, &desc, NULL, &texture));
    HR(ID3D11Device_CreateRenderTargetView(device, (ID3D11Resource *)texture, NULL, &target));
    if (!show_planes) HR(uurb_d3d11_frame_adapter_create(device, source, texture, &frame_adapter));
    if (frame_adapter && adapter_rejection_checks(device, source, texture, frame_adapter)) goto done;
    /* A sampling pass materializes fast clears and provides the GPU-only
     * conversion boundary for ordinary, non-shareable UU textures. */
    fprintf(stderr, "GPU source %s -> sampling pass -> shared texture\n", argv[2]);
    D3D11_QUERY_DESC query_desc = {.Query = D3D11_QUERY_EVENT};
    HR(ID3D11Device_CreateQuery(device, &query_desc, &completed));
    interop->lpVtbl->FlushRenderingCommands(interop);
    HR(ID3D11Texture2D_QueryInterface(texture, &interop_surface_iid, (void **)&surface));
    HR(ID3D11Texture2D_QueryInterface(texture, &IID_IDXGIResource1, (void **)&resource));
    HR(IDXGIResource1_CreateSharedHandle(resource, NULL, DXGI_SHARED_RESOURCE_READ, NULL, &shared));
    VkInstance instance;
    VkPhysicalDevice physical;
    VkImageLayout layout;
    VkImageCreateInfo image_info = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    interop->lpVtbl->GetVulkanHandles(interop, &instance, &physical, &vkdevice);
    HR(surface->lpVtbl->GetVulkanImageInfo(surface, &image, &layout, &image_info));
    vulkan = LoadLibraryA("vulkan-1.dll");
    PFN_vkGetImageMemoryRequirements requirements_fn =
        (void *)GetProcAddress(vulkan, "vkGetImageMemoryRequirements");
    PFN_vkGetPhysicalDeviceProperties2 properties_fn =
        (void *)GetProcAddress(vulkan, "vkGetPhysicalDeviceProperties2");
    if (!requirements_fn || !properties_fn) goto done;
    VkMemoryRequirements requirements;
    requirements_fn(vkdevice, image, &requirements);
    VkPhysicalDeviceIDProperties id = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ID_PROPERTIES};
    VkPhysicalDeviceProperties2 properties = {.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2, .pNext = &id};
    properties_fn(physical, &properties);
    fprintf(stderr, "Vulkan texture: device=%s size=%llu layout=%u format=%u tiling=%u\n",
        properties.properties.deviceName, (unsigned long long)requirements.size,
        layout, image_info.format, image_info.tiling);
    fd = shared_fd(shared);
    if (fd < 0) goto done;
    fprintf(stderr, "Shared D3D11 texture exported to native FD (no pixel readback)\n");
    if (layout != VK_IMAGE_LAYOUT_GENERAL || image_info.format != VK_FORMAT_B8G8R8A8_UNORM ||
        image_info.tiling != VK_IMAGE_TILING_OPTIMAL) {
        fprintf(stderr, "Unsupported shared image layout, format or tiling\n"); goto done;
    }
    for (unsigned i = 0; i < 3; ++i) {
        char path[4096];
        const char *name = i == 0 ? "h264.bin" : i == 1 ? "hevc.bin" : "frames.jsonl";
        int length = snprintf(path, sizeof(path), "%s/%s", argv[1], name);
        if (length < 0 || (size_t)length >= sizeof(path)) goto done;
        int output_fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
        if (output_fd < 0) goto done;
        FILE *file = fdopen(output_fd, "wb");
        if (!file) { close(output_fd); goto done; }
        if (i < 2) outputs[i] = file; else metadata = file;
    }
    for (unsigned frame = 0; frame < frames; ++frame) {
        if (!strcmp(argv[2], "sequence") || !strcmp(argv[2], "clear") || nv12) {
            /* 64 distinct colors identify ordering, stale frames and lost updates.
             * Repeated after 64 frames; quadrant regressions are separate cases. */
            unsigned n = !strcmp(argv[2], "sequence") || nv12 ? frame : 3;
            const float color[4] = {(n & 3) / 3.0f, ((n >> 2) & 3) / 3.0f, ((n >> 4) & 3) / 3.0f, 1};
            if (nv12) {
                if (draw_nv12_plane(&nv12_draw[0], device, context, source_target, desc.Width, desc.Height, frame, 0, spatial) ||
                    draw_nv12_plane(&nv12_draw[1], device, context, uv_target, desc.Width/2, desc.Height/2, frame, 1, spatial)) goto done;
            } else ID3D11DeviceContext_ClearRenderTargetView(context, source_target, color);
        } else if (draw_pattern(&pattern_draw, device, context, source_target, desc.Width, desc.Height, NULL, NULL, 0)) goto done;
        if (bridge_session) ID3D11DeviceContext_CopyResource(context, (ID3D11Resource *)second_source, (ID3D11Resource *)source);
        if (frame_adapter) {
            if (adapter_submit_checked(frame_adapter, device, context, target, source_view)) goto done;
        } else if (draw_pattern(&sampling_draw, device, context, target, desc.Width, desc.Height, source_view, uv_view, show_planes)) goto done;
        ID3D11DeviceContext_End(context, (ID3D11Asynchronous *)completed);
        ID3D11DeviceContext_Flush(context);
        ULONGLONG deadline = GetTickCount64() + 5000;
        HRESULT completion;
        while ((completion = ID3D11DeviceContext_GetData(context,
            (ID3D11Asynchronous *)completed, NULL, 0, 0)) == S_FALSE && GetTickCount64() < deadline) Sleep(1);
        if (completion != S_OK) { fprintf(stderr, "D3D11 completion fence failed\n"); goto done; }
        if (transfer_ownership(&transfer, interop, vkdevice, image, 0)) goto done;
        external_owned = 1;
        if (!bridge_session && !frame && frames > 1 && uurb_encoder_probe_lifecycle(fd, requirements.size,
            id.deviceUUID, desc.Width, desc.Height)) goto done;
        for (unsigned codec = 0; codec < 2; ++codec) {
            if (!bridge_session && !encoders[codec]) {
                int imported_fd = fcntl(fd, F_DUPFD_CLOEXEC, 0);
                encoders[codec] = uurb_encoder_open(imported_fd, requirements.size, id.deviceUUID,
                    desc.Width, desc.Height, codec, 40000000);
                if (!encoders[codec]) goto done;
            }
            int reconfigure = frames > 1 && frame == frames / 2;
            if (reconfigure) {
                if (bridge_session) {
                    uint64_t stale = registered[codec][1];
                    HR(uurb_d3d11_encoder_unregister(bridges[codec], stale));
                    HR(uurb_d3d11_encoder_register(bridges[codec], second_source, &registered[codec][1]));
                    if (registered[codec][1] == stale || SUCCEEDED(uurb_d3d11_encoder_unregister(bridges[codec], stale))) goto done;
                    HR(uurb_d3d11_encoder_reconfigure(bridges[codec], 20000000));
                } else if (uurb_encoder_reconfigure(encoders[codec], 20000000)) goto done;
            }
            struct uurb_packet packet;
            if (bridge_session) {
                HR(uurb_d3d11_encoder_encode(bridges[codec], registered[codec][frame % 2],
                    1000 + frame, frame == frames / 3, &packet));
                struct uurb_packet rejected;
                if (SUCCEEDED(uurb_d3d11_encoder_encode(bridges[codec], registered[codec][0], 9000, 0, &rejected)) ||
                    SUCCEEDED(uurb_d3d11_encoder_unregister(bridges[codec], registered[codec][0])) ||
                    SUCCEEDED(uurb_d3d11_encoder_reconfigure(bridges[codec], 10000000))) goto done;
            } else if (uurb_encoder_encode(encoders[codec], 1000 + frame, frame == frames / 3, &packet)) goto done;
            if (fwrite(packet.data, 1, packet.size, outputs[codec]) != packet.size) goto done;
            if (fprintf(metadata, "{\"codec\":\"%s\",\"frame\":%u,\"timestamp\":%llu,\"idr\":%s,\"reconfigured\":%s,\"bytes\":%u}\n",
                codec ? "hevc" : "h264", frame, (unsigned long long)packet.timestamp,
                packet.idr ? "true" : "false", reconfigure ? "true" : "false", packet.size) < 0) goto done;
            if (bridge_session) {
                if (frame + 1 == frames) {
                    /* Consumer can abandon a locked packet during teardown. */
                    HR(uurb_d3d11_encoder_close(bridges[codec]));
                    bridges[codec] = NULL;
                } else {
                    HR(uurb_d3d11_encoder_release(bridges[codec]));
                    struct uurb_packet rejected;
                    if (SUCCEEDED(uurb_d3d11_encoder_encode(bridges[codec], registered[codec][0], 1000 + frame, 0, &rejected))) goto done;
                }
            } else if (uurb_encoder_release_packet(encoders[codec])) goto done;
        }
        if (transfer_ownership(&transfer, interop, vkdevice, image, 1)) goto done;
        external_owned = 0;
    }
    fprintf(stderr, "Native NVENC: %u changing frames per codec, persistent H264/HEVC sessions; no raw CPU readback\n", frames);
    if (frame_adapter) fprintf(stderr, "Adapter cached submissions: %u; caller-state checks: passed\n", frames);
    if (bridge_session) fprintf(stderr, "Bridge session: two source textures, one encoder per codec; stale/cross-session/busy/timestamp checks passed\n");
    result = 0;
done:
    for (unsigned codec = 0; codec < 2; ++codec) if (FAILED(uurb_d3d11_encoder_close(bridges[codec]))) _Exit(4);
    if (encoders[0] || encoders[1] || external_owned) {
        if (!external_owned && transfer_ownership(&transfer, interop, vkdevice, image, 0)) _Exit(4);
        for (unsigned codec = 0; codec < 2; ++codec) {
            if (uurb_encoder_close(encoders[codec])) _Exit(4);
        }
        if (transfer_ownership(&transfer, interop, vkdevice, image, 1)) result = 1;
    }
    for (unsigned codec = 0; codec < 2; ++codec) if (outputs[codec] && fclose(outputs[codec])) result = 1;
    if (metadata && fclose(metadata)) result = 1;
    if (completed) ID3D11Query_Release(completed);
    uurb_d3d11_frame_adapter_destroy(frame_adapter);
    draw_destroy(&sampling_draw);
    draw_destroy(&pattern_draw);
    transfer_destroy(&transfer, vkdevice);
    if (fd >= 0) close(fd);
    if (shared) CloseHandle(shared);
    if (surface) surface->lpVtbl->Release(surface);
    if (resource) IDXGIResource1_Release(resource);
    if (target) ID3D11RenderTargetView_Release(target);
    if (texture) ID3D11Texture2D_Release(texture);
    if (source_view) ID3D11ShaderResourceView_Release(source_view);
    if (second_source) ID3D11Texture2D_Release(second_source);
    if (uv_view) ID3D11ShaderResourceView_Release(uv_view);
    if (uv_target) ID3D11RenderTargetView_Release(uv_target);
    draw_destroy(&nv12_draw[0]);
    draw_destroy(&nv12_draw[1]);
    if (source_target) ID3D11RenderTargetView_Release(source_target);
    if (source) ID3D11Texture2D_Release(source);
    if (interop) interop->lpVtbl->Release(interop);
    if (context) ID3D11DeviceContext_Release(context);
    if (device) ID3D11Device_Release(device);
    if (adapter) IDXGIAdapter1_Release(adapter);
    if (factory) IDXGIFactory1_Release(factory);
    if (vulkan) FreeLibrary(vulkan);
    return result;
}
