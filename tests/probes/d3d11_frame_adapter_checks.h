/* GPU integration checks only; deliberately hostile caller state. Not a hot path. */
static int adapter_submit_checked(struct uurb_d3d11_frame_adapter *adapter,
    ID3D11Device *device, ID3D11DeviceContext *context, ID3D11RenderTargetView *target,
    ID3D11ShaderResourceView *input)
{
    int result = 1;
    ID3D11RasterizerState *raster = NULL, *got_raster = NULL;
    ID3D11BlendState *blend = NULL, *got_blend = NULL;
    ID3D11VertexShader *vertex = NULL, *got_vertex = NULL;
    ID3D11PixelShader *pixel = NULL, *got_pixel = NULL;
    ID3D11RenderTargetView *got_target = NULL;
    ID3D11ShaderResourceView *got_input = NULL;
    ID3D11DeviceContext_VSGetShader(context, &vertex, NULL, NULL);
    ID3D11DeviceContext_PSGetShader(context, &pixel, NULL, NULL);
    D3D11_RASTERIZER_DESC rd = {.FillMode = D3D11_FILL_WIREFRAME, .CullMode = D3D11_CULL_FRONT,
        .DepthClipEnable = TRUE, .ScissorEnable = TRUE};
    /* No color writes, empty scissor and line topology would spoil conversion
     * if the command list inherited any of these caller states. */
    D3D11_BLEND_DESC bd = {0};
    bd.RenderTarget[0].SrcBlend = bd.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ONE;
    bd.RenderTarget[0].DestBlend = bd.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_ZERO;
    bd.RenderTarget[0].BlendOp = bd.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
    HR(ID3D11Device_CreateRasterizerState(device, &rd, &raster));
    HR(ID3D11Device_CreateBlendState(device, &bd, &blend));
    D3D11_VIEWPORT viewport = {3, 5, 7, 9, 0.25f, 0.75f}, got_viewport = {0};
    D3D11_RECT rect = {1, 1, 1, 1}, got_rect = {0};
    const float factors[4] = {0.25f, 0.5f, 0.75f, 1};
    float got_factors[4] = {0};
    UINT got_mask = 0, count = 1;
    D3D11_PRIMITIVE_TOPOLOGY topology;
    ID3D11DeviceContext_RSSetState(context, raster);
    ID3D11DeviceContext_RSSetViewports(context, 1, &viewport);
    ID3D11DeviceContext_RSSetScissorRects(context, 1, &rect);
    ID3D11DeviceContext_OMSetRenderTargets(context, 1, &target, NULL);
    ID3D11DeviceContext_OMSetBlendState(context, blend, factors, 0x12345678);
    ID3D11DeviceContext_IASetPrimitiveTopology(context, D3D11_PRIMITIVE_TOPOLOGY_LINELIST);
    ID3D11DeviceContext_PSSetShaderResources(context, 0, 1, &input);
    HR(uurb_d3d11_frame_adapter_submit(adapter, context));
    ID3D11DeviceContext_RSGetState(context, &got_raster);
    ID3D11DeviceContext_RSGetViewports(context, &count, &got_viewport);
    if (count != 1) goto done;
    ID3D11DeviceContext_RSGetScissorRects(context, &count, &got_rect);
    if (count != 1) goto done;
    ID3D11DeviceContext_OMGetRenderTargets(context, 1, &got_target, NULL);
    ID3D11DeviceContext_OMGetBlendState(context, &got_blend, got_factors, &got_mask);
    ID3D11DeviceContext_IAGetPrimitiveTopology(context, &topology);
    ID3D11DeviceContext_PSGetShaderResources(context, 0, 1, &got_input);
    ID3D11DeviceContext_VSGetShader(context, &got_vertex, NULL, NULL);
    ID3D11DeviceContext_PSGetShader(context, &got_pixel, NULL, NULL);
    if (got_raster != raster || got_blend != blend || got_vertex != vertex || got_pixel != pixel ||
        got_input != input || got_target != target || got_mask != 0x12345678 ||
        topology != D3D11_PRIMITIVE_TOPOLOGY_LINELIST || memcmp(&viewport, &got_viewport, sizeof(viewport)) ||
        memcmp(&rect, &got_rect, sizeof(rect)) || memcmp(factors, got_factors, sizeof(factors))) goto done;
    result = 0;
done:
    if (result) fprintf(stderr, "Adapter caller-state isolation/restoration failed\n");
    ID3D11DeviceContext_ClearState(context);
    if (raster) ID3D11RasterizerState_Release(raster);
    if (got_raster) ID3D11RasterizerState_Release(got_raster);
    if (blend) ID3D11BlendState_Release(blend);
    if (got_blend) ID3D11BlendState_Release(got_blend);
    if (vertex) ID3D11VertexShader_Release(vertex);
    if (got_vertex) ID3D11VertexShader_Release(got_vertex);
    if (pixel) ID3D11PixelShader_Release(pixel);
    if (got_pixel) ID3D11PixelShader_Release(got_pixel);
    if (got_target) ID3D11RenderTargetView_Release(got_target);
    if (got_input) ID3D11ShaderResourceView_Release(got_input);
    return result;
}

static int adapter_rejection_checks(ID3D11Device *device, ID3D11Texture2D *source,
    ID3D11Texture2D *output, struct uurb_d3d11_frame_adapter *valid)
{
    int result = 1;
    struct uurb_d3d11_frame_adapter *candidate = valid;
    ID3D11Texture2D *bad = NULL;
    ID3D11DeviceContext *deferred = NULL, *other_context = NULL;
    ID3D11Device *other = NULL;
    D3D11_TEXTURE2D_DESC desc;
    ID3D11Texture2D_GetDesc(output, &desc);
    if (uurb_d3d11_frame_adapter_create(NULL, source, output, &candidate) != E_INVALIDARG ||
        candidate != valid || uurb_d3d11_frame_adapter_create(device, source, source, &candidate) != E_INVALIDARG ||
        candidate != valid || uurb_d3d11_frame_adapter_create(device, source, output, NULL) != E_INVALIDARG ||
        uurb_d3d11_frame_adapter_submit(NULL, NULL) != E_INVALIDARG) goto done;
    HR(ID3D11Device_CreateDeferredContext(device, 0, &deferred));
    if (uurb_d3d11_frame_adapter_submit(valid, deferred) != E_INVALIDARG) goto done;
    /* Real invalid textures, not mocked descriptors. */
    for (unsigned i = 0; i < 7; ++i) {
        D3D11_TEXTURE2D_DESC d = desc;
        d.MiscFlags = 0;
        if (i == 0) d.Width += 2;
        if (i == 1) d.Width -= 1;
        if (i == 2) d.ArraySize = 2;
        if (i == 3) d.MipLevels = 2;
        if (i == 4) d.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        if (i == 5) d.SampleDesc.Count = 2;
        if (i == 6) d.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        HR(ID3D11Device_CreateTexture2D(device, &d, NULL, &bad));
        HRESULT rejected = i == 6 ? uurb_d3d11_frame_adapter_create(device, source, bad, &candidate) :
            uurb_d3d11_frame_adapter_create(device, bad, output, &candidate);
        if (rejected != E_INVALIDARG || candidate != valid) goto done;
        ID3D11Texture2D_Release(bad); bad = NULL;
    }
    HR(D3D11CreateDevice(NULL, D3D_DRIVER_TYPE_HARDWARE, NULL, 0, NULL, 0,
        D3D11_SDK_VERSION, &other, NULL, &other_context));
    if (uurb_d3d11_frame_adapter_create(other, source, output, &candidate) != E_INVALIDARG ||
        candidate != valid || uurb_d3d11_frame_adapter_submit(valid, other_context) != E_INVALIDARG) goto done;
    result = 0;
done:
    if (candidate != valid) uurb_d3d11_frame_adapter_destroy(candidate);
    if (bad) ID3D11Texture2D_Release(bad);
    if (deferred) ID3D11DeviceContext_Release(deferred);
    if (other_context) ID3D11DeviceContext_Release(other_context);
    if (other) ID3D11Device_Release(other);
    fprintf(stderr, "Adapter invalid-resource/context checks: %s\n", result ? "FAILED" : "passed");
    return result;
}
