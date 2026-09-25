#define _GNU_SOURCE
#define COBJMACROS
#include "uu_d3d11_frame_adapter.h"
#include <d3dcompiler.h>
#include <stdlib.h>
#include <string.h>

struct uurb_d3d11_frame_adapter {
    ID3D11Device *device;
    ID3D11CommandList *commands;
};

static int same_device(ID3D11Device *a, ID3D11Device *b)
{
    IUnknown *ia = NULL, *ib = NULL;
    int same = 0;
    if (SUCCEEDED(ID3D11Device_QueryInterface(a, &IID_IUnknown, (void **)&ia)) &&
        SUCCEEDED(ID3D11Device_QueryInterface(b, &IID_IUnknown, (void **)&ib))) same = ia == ib;
    if (ia) IUnknown_Release(ia);
    if (ib) IUnknown_Release(ib);
    return same;
}

static int supported_texture(const D3D11_TEXTURE2D_DESC *d)
{
    return d->Width >= 2 && d->Height >= 2 && d->Width <= 4096 && d->Height <= 4096 &&
        !(d->Width & 1) && !(d->Height & 1) && d->MipLevels == 1 && d->ArraySize == 1 &&
        d->SampleDesc.Count == 1 && !d->SampleDesc.Quality &&
        d->Usage == D3D11_USAGE_DEFAULT && !d->CPUAccessFlags;
}

void uurb_d3d11_frame_adapter_destroy(struct uurb_d3d11_frame_adapter *s)
{
    if (!s) return;
    if (s->commands) ID3D11CommandList_Release(s->commands);
    if (s->device) ID3D11Device_Release(s->device);
    free(s);
}

#define CHECK(call) do { result = (call); if (FAILED(result)) goto done; } while (0)
HRESULT uurb_d3d11_frame_adapter_create(ID3D11Device *device,
    ID3D11Texture2D *source, ID3D11Texture2D *output,
    struct uurb_d3d11_frame_adapter **out)
{
    if (!device || !source || !output || !out || source == output) return E_INVALIDARG;
    D3D11_TEXTURE2D_DESC src, dst;
    ID3D11Texture2D_GetDesc(source, &src);
    ID3D11Texture2D_GetDesc(output, &dst);
    if (!supported_texture(&src) || !supported_texture(&dst) ||
        src.Width != dst.Width || src.Height != dst.Height ||
        (src.Format != DXGI_FORMAT_NV12 && src.Format != DXGI_FORMAT_B8G8R8A8_UNORM) ||
        dst.Format != DXGI_FORMAT_B8G8R8A8_UNORM ||
        !(dst.BindFlags & D3D11_BIND_RENDER_TARGET))
        return E_INVALIDARG;
    ID3D11Device *source_device = NULL, *output_device = NULL;
    ID3D11Texture2D_GetDevice(source, &source_device);
    ID3D11Texture2D_GetDevice(output, &output_device);
    int matched = same_device(device, source_device) && same_device(device, output_device);
    ID3D11Device_Release(source_device);
    ID3D11Device_Release(output_device);
    if (!matched) return E_INVALIDARG;

    struct uurb_d3d11_frame_adapter *s = calloc(1, sizeof(*s));
    if (!s) return E_OUTOFMEMORY;
    HRESULT result = E_FAIL;
    ID3D11DeviceContext *deferred = NULL;
    ID3D11RenderTargetView *target = NULL;
    ID3D11ShaderResourceView *inputs[2] = {NULL, NULL};
    ID3D11Texture2D *sampled = NULL;
    ID3D11VertexShader *vertex = NULL;
    ID3D11PixelShader *pixel = NULL;
    ID3DBlob *vs = NULL, *ps = NULL, *errors = NULL;
    const char *vertex_code =
        "float4 main(uint id:SV_VertexID):SV_Position {float2 p=float2((id<<1)&2,id&2);"
        "return float4(p*float2(2,-2)+float2(-1,1),0,1);}";
    /* Integer loads explicitly align each UV sample to its 2x2 luma block.
     * No filtered chroma reconstruction, rotation, HDR or matrix negotiation. */
    const char *pixel_code = src.Format == DXGI_FORMAT_NV12 ?
        "Texture2D<float> yp:register(t0); Texture2D<float2> uvp:register(t1);"
        "float4 main(float4 p:SV_Position):SV_Target {int2 pos=int2(p.xy);"
        "float y=(yp.Load(int3(pos,0))*255-16)/219;"
        "float2 uv=(uvp.Load(int3(pos/2,0))*255-128)/224;"
        "return float4(y+1.5748*uv.y,y-0.187324*uv.x-0.468124*uv.y,y+1.8556*uv.x,1);}" :
        "Texture2D<float4> img:register(t0);"
        "float4 main(float4 p:SV_Position):SV_Target {return img.Load(int3(int2(p.xy),0));}";
    CHECK(ID3D11Device_CreateDeferredContext(device, 0, &deferred));
    if (!(src.BindFlags & D3D11_BIND_SHADER_RESOURCE)) {
        /* Official UU uses UAV-only or RTV-only NV12 encoder inputs. Preserve
         * their resources; copy into one retained sampleable GPU texture per
         * registration. The recorded command list owns its lifetime. */
        D3D11_TEXTURE2D_DESC copy = src;
        copy.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        copy.MiscFlags = 0;
        CHECK(ID3D11Device_CreateTexture2D(device, &copy, NULL, &sampled));
        ID3D11DeviceContext_CopyResource(deferred, (ID3D11Resource *)sampled, (ID3D11Resource *)source);
    } else {
        sampled = source;
        ID3D11Texture2D_AddRef(sampled);
    }
    CHECK(ID3D11Device_CreateRenderTargetView(device, (ID3D11Resource *)output, NULL, &target));
    D3D11_SHADER_RESOURCE_VIEW_DESC view = {.Format = src.Format == DXGI_FORMAT_NV12 ?
        DXGI_FORMAT_R8_UNORM : DXGI_FORMAT_B8G8R8A8_UNORM, .ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D};
    view.Texture2D.MipLevels = 1;
    CHECK(ID3D11Device_CreateShaderResourceView(device, (ID3D11Resource *)sampled, &view, &inputs[0]));
    if (src.Format == DXGI_FORMAT_NV12) {
        view.Format = DXGI_FORMAT_R8G8_UNORM;
        CHECK(ID3D11Device_CreateShaderResourceView(device, (ID3D11Resource *)sampled, &view, &inputs[1]));
    }
    CHECK(D3DCompile(vertex_code, strlen(vertex_code), NULL, NULL, NULL, "main", "vs_4_0", 0, 0, &vs, &errors));
    if (errors) { ID3D10Blob_Release(errors); errors = NULL; }
    CHECK(D3DCompile(pixel_code, strlen(pixel_code), NULL, NULL, NULL, "main", "ps_4_0", 0, 0, &ps, &errors));
    CHECK(ID3D11Device_CreateVertexShader(device, ID3D10Blob_GetBufferPointer(vs), ID3D10Blob_GetBufferSize(vs), NULL, &vertex));
    CHECK(ID3D11Device_CreatePixelShader(device, ID3D10Blob_GetBufferPointer(ps), ID3D10Blob_GetBufferSize(ps), NULL, &pixel));
    /* A fresh deferred context has default state, independent of UU's pipeline. */
    D3D11_VIEWPORT viewport = {0, 0, src.Width, src.Height, 0, 1};
    ID3D11DeviceContext_RSSetViewports(deferred, 1, &viewport);
    ID3D11DeviceContext_OMSetRenderTargets(deferred, 1, &target, NULL);
    ID3D11DeviceContext_IASetPrimitiveTopology(deferred, D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ID3D11DeviceContext_VSSetShader(deferred, vertex, NULL, 0);
    ID3D11DeviceContext_PSSetShader(deferred, pixel, NULL, 0);
    ID3D11DeviceContext_PSSetShaderResources(deferred, 0, 2, inputs);
    ID3D11DeviceContext_Draw(deferred, 3, 0);
    CHECK(ID3D11DeviceContext_FinishCommandList(deferred, FALSE, &s->commands));
    s->device = device;
    ID3D11Device_AddRef(device);
    *out = s;
    s = NULL;
    result = S_OK;
done:
    if (deferred) ID3D11DeviceContext_Release(deferred);
    if (target) ID3D11RenderTargetView_Release(target);
    for (unsigned i = 0; i < 2; ++i) if (inputs[i]) ID3D11ShaderResourceView_Release(inputs[i]);
    if (sampled) ID3D11Texture2D_Release(sampled);
    if (vertex) ID3D11VertexShader_Release(vertex);
    if (pixel) ID3D11PixelShader_Release(pixel);
    if (vs) ID3D10Blob_Release(vs);
    if (ps) ID3D10Blob_Release(ps);
    if (errors) ID3D10Blob_Release(errors);
    uurb_d3d11_frame_adapter_destroy(s);
    return result;
}

HRESULT uurb_d3d11_frame_adapter_submit(struct uurb_d3d11_frame_adapter *s,
    ID3D11DeviceContext *immediate)
{
    if (!s || !immediate || ID3D11DeviceContext_GetType(immediate) != D3D11_DEVICE_CONTEXT_IMMEDIATE)
        return E_INVALIDARG;
    ID3D11Device *device = NULL;
    ID3D11DeviceContext_GetDevice(immediate, &device);
    int matched = same_device(s->device, device);
    ID3D11Device_Release(device);
    if (!matched) return E_INVALIDARG;
    HRESULT result = ID3D11Device_GetDeviceRemovedReason(s->device);
    if (FAILED(result)) return result;
    ID3D11DeviceContext_ExecuteCommandList(immediate, s->commands, TRUE);
    return ID3D11Device_GetDeviceRemovedReason(s->device);
}
