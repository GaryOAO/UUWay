/* True MinGW PE consumer: real Wayland frames through standard DXGI COM slots
 * and Windows NVENC, not installed into official UU. No raw pixel readback. */
#define COBJMACROS
#include "uu_dxgi_duplication.h"
#include <dxgi1_5.h>
#include "native_nvenc_reference_config.h"
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef HRESULT (WINAPI *capture_fn)(ID3D11Device *, int, IUnknown *, IDXGIOutputDuplication **);
typedef NVENCSTATUS (NVENCAPI *encode_fn)(NV_ENCODE_API_FUNCTION_LIST *);
#define CHECK(test) do { if (!(test)) { fprintf(stderr, "DXGI PE failed: %s line %d\n", #test, __LINE__); goto done; } } while (0)
static int metadata_checks(IDXGIOutputDuplication *capture, const DXGI_OUTDUPL_DESC *desc,
                           const DXGI_OUTDUPL_FRAME_INFO *frame)
{
    RECT rect = {-1,-1,-1,-1}, before = rect;
    UINT size = 123;
    IDXGIResource *unexpected = (void *)(uintptr_t)1;
    DXGI_OUTDUPL_FRAME_INFO untouched, copy;
    memset(&untouched, 0x55, sizeof(untouched)); copy = untouched;
    if (IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &untouched, &unexpected) != DXGI_ERROR_INVALID_CALL ||
        unexpected || memcmp(&untouched, &copy, sizeof(copy))) return 1;
    if (frame->TotalMetadataBufferSize != sizeof(RECT) || frame->AccumulatedFrames != 1 ||
        frame->PointerPosition.Visible || frame->LastMouseUpdateTime.QuadPart || frame->PointerShapeBufferSize ||
        frame->ProtectedContentMaskedOut || frame->RectsCoalesced) return 1;
    if (IDXGIOutputDuplication_GetFrameDirtyRects(capture, 0, NULL, &size) != DXGI_ERROR_MORE_DATA ||
        size != sizeof(RECT)) return 1;
    if (IDXGIOutputDuplication_GetFrameDirtyRects(capture, sizeof(RECT)-1, &rect, &size) != DXGI_ERROR_MORE_DATA ||
        memcmp(&rect, &before, sizeof(rect))) return 1;
    if (IDXGIOutputDuplication_GetFrameDirtyRects(capture, sizeof(rect), &rect, &size) != S_OK ||
        rect.left || rect.top || rect.right != (LONG)desc->ModeDesc.Width || rect.bottom != (LONG)desc->ModeDesc.Height ||
        size != sizeof(rect)) return 1;
    if (IDXGIOutputDuplication_GetFrameDirtyRects(capture, sizeof(rect), NULL, &size) != E_INVALIDARG ||
        IDXGIOutputDuplication_GetFrameDirtyRects(capture, sizeof(rect), &rect, NULL) != E_INVALIDARG) return 1;
    if (IDXGIOutputDuplication_GetFrameMoveRects(capture, 0, NULL, &size) != S_OK || size) return 1;
    DXGI_OUTDUPL_POINTER_SHAPE_INFO pointer;
    if (IDXGIOutputDuplication_GetFramePointerShape(capture, 0, NULL, &size, &pointer) != S_OK || size ||
        pointer.Width || pointer.Height || pointer.Pitch || pointer.Type) return 1;
    DXGI_MAPPED_RECT mapped;
    if (IDXGIOutputDuplication_MapDesktopSurface(capture, &mapped) != DXGI_ERROR_UNSUPPORTED ||
        IDXGIOutputDuplication_UnMapDesktopSurface(capture) != DXGI_ERROR_INVALID_CALL) return 1;
    return 0;
}
static int hook_checks(IDXGIOutput5 *output, ID3D11Device *device, IUnknown *not_device)
{
    if (GetModuleHandleW(L"uurb-dxgi-capture-loader.dll") || GetModuleHandleW(L"uurb-dxgi-capture.dll.so")) return 1;
    DXGI_FORMAT bgra = DXGI_FORMAT_B8G8R8A8_UNORM, hdr = DXGI_FORMAT_R16G16B16A16_FLOAT;
    IDXGIOutputDuplication *capture = NULL;
    if (IDXGIOutput5_DuplicateOutput1(output, (IUnknown *)device, 0, 1, &bgra, NULL) != E_INVALIDARG ||
        IDXGIOutput5_DuplicateOutput1(output, NULL, 0, 1, &bgra, &capture) != E_INVALIDARG || capture ||
        IDXGIOutput5_DuplicateOutput1(output, (IUnknown *)device, 1, 1, &bgra, &capture) != DXGI_ERROR_UNSUPPORTED ||
        IDXGIOutput5_DuplicateOutput1(output, (IUnknown *)device, 0, 1, &hdr, &capture) != DXGI_ERROR_UNSUPPORTED ||
        IDXGIOutput5_DuplicateOutput1(output, (IUnknown *)device, 0, 1, NULL, &capture) != E_INVALIDARG ||
        IDXGIOutput5_DuplicateOutput1(output, (IUnknown *)device, 0, 65, &bgra, &capture) != E_INVALIDARG ||
        IDXGIOutput5_DuplicateOutput1(output, not_device, 0, 1, &bgra, &capture) != E_INVALIDARG || capture) return 1;
    WCHAR fd[16] = {0}, target[CCHDEVICENAME], endpoint[108] = {0};
    DWORD endpoint_length = GetEnvironmentVariableW(L"UURB_DXGI_CAPTURE_SOCKET", endpoint, 108);
    if ((!GetEnvironmentVariableW(L"UURB_DXGI_CAPTURE_FD", fd, 16) && !endpoint_length) ||
        !GetEnvironmentVariableW(L"UURB_DXGI_CAPTURE_OUTPUT", target, CCHDEVICENAME)) return 1;
    SetEnvironmentVariableW(L"UURB_DXGI_CAPTURE_FD", NULL);
    SetEnvironmentVariableW(L"UURB_DXGI_CAPTURE_SOCKET", NULL);
    HRESULT disabled = IDXGIOutput5_DuplicateOutput1(output, (IUnknown *)device, 0, 1, &bgra, &capture);
    SetEnvironmentVariableW(L"UURB_DXGI_CAPTURE_FD", L"2147483648");
    HRESULT malformed = IDXGIOutput5_DuplicateOutput1(output, (IUnknown *)device, 0, 1, &bgra, &capture);
    SetEnvironmentVariableW(L"UURB_DXGI_CAPTURE_FD", fd[0] ? fd : NULL);
    SetEnvironmentVariableW(L"UURB_DXGI_CAPTURE_SOCKET", endpoint_length ? endpoint : NULL);
    SetEnvironmentVariableW(L"UURB_DXGI_CAPTURE_OUTPUT", L"not-the-selected-output");
    HRESULT wrong_output = IDXGIOutput5_DuplicateOutput1(output, (IUnknown *)device, 0, 1, &bgra, &capture);
    SetEnvironmentVariableW(L"UURB_DXGI_CAPTURE_OUTPUT", target);
    return disabled != DXGI_ERROR_UNSUPPORTED || malformed != E_INVALIDARG ||
           wrong_output != DXGI_ERROR_UNSUPPORTED || capture ||
           GetModuleHandleW(L"uurb-dxgi-capture-loader.dll") || GetModuleHandleW(L"uurb-dxgi-capture.dll.so");
}
int main(int argc, char **argv)
{
    int hooked = argc == 5, legacy = hooked && !strcmp(argv[4], "legacy");
    if ((argc != 4 && argc != 5) || (hooked && !legacy && strcmp(argv[4], "duplicate1")) ||
        (strcmp(argv[3], "h264") && strcmp(argv[3], "hevc"))) return 2;
    char *end;
    long descriptor = strtol(argv[1], &end, 10);
    if (*end || descriptor < 3 || descriptor > INT_MAX) return 2;
    int hevc = !strcmp(argv[3], "hevc"), failed = 1;
    HMODULE capture_module = hooked ? NULL : LoadLibraryA("uurb-dxgi-capture-loader.dll");
    HMODULE encoder_module = LoadLibraryA("uurb-nvenc-encode-loader.dll");
    IDXGIFactory1 *factory = NULL;
    IDXGIAdapter1 *adapter = NULL;
    IDXGIOutput *output_base = NULL;
    IDXGIOutput5 *output5 = NULL;
    DXGI_OUTPUT_DESC output_desc = {0};
    ID3D11Device *device = NULL;
    ID3D11DeviceContext *context = NULL;
    IDXGIOutputDuplication *capture = NULL;
    IDXGIResource *resource = NULL;
    ID3D11Texture2D *texture = NULL;
    IUnknown *identity = NULL;
    HANDLE output = INVALID_HANDLE_VALUE;
    void *encoder = NULL;
    unsigned frames = 0;
    uint64_t written = 0, last_timestamp = 0;
    DXGI_OUTDUPL_DESC desc = {0};
    NV_ENCODE_API_FUNCTION_LIST api = {.version = NV_ENCODE_API_FUNCTION_LIST_VER};
    NV_ENC_REGISTERED_PTR registered = NULL;
    NV_ENC_OUTPUT_PTR bitstream = NULL;
    CHECK((hooked || capture_module) && encoder_module);
    capture_fn create_capture = hooked ? NULL : (void *)GetProcAddress(capture_module, "UurbCreateDuplication");
    encode_fn create_encoder = (void *)GetProcAddress(encoder_module, "NvEncodeAPICreateInstance");
    CHECK((hooked || create_capture) && create_encoder && create_encoder(&api) == NV_ENC_SUCCESS);
    CHECK(SUCCEEDED(CreateDXGIFactory1(&IID_IDXGIFactory1, (void **)&factory)));
    for (UINT i = 0; IDXGIFactory1_EnumAdapters1(factory, i, &adapter) == S_OK; ++i) {
        DXGI_ADAPTER_DESC1 description;
        CHECK(SUCCEEDED(IDXGIAdapter1_GetDesc1(adapter, &description)));
        if (description.VendorId == 0x10de && !(description.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)) break;
        IDXGIAdapter1_Release(adapter); adapter = NULL;
    }
    CHECK(adapter);
    D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_11_0;
    CHECK(SUCCEEDED(D3D11CreateDevice((IDXGIAdapter *)adapter, D3D_DRIVER_TYPE_UNKNOWN, NULL,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT, &level, 1, D3D11_SDK_VERSION, &device, NULL, &context)));
    if (hooked) {
        CHECK(IDXGIAdapter1_EnumOutputs(adapter, 0, &output_base) == S_OK);
        CHECK(IDXGIOutput_QueryInterface(output_base, &IID_IDXGIOutput5, (void **)&output5) == S_OK);
        CHECK(IDXGIOutput_GetDesc(output_base, &output_desc) == S_OK);
        CHECK(!hook_checks(output5, device, (IUnknown *)factory));
    }
    output = CreateFileA(argv[2], GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_TEMPORARY, NULL);
    CHECK(output != INVALID_HANDLE_VALUE);
    puts("D3D11_RECEIVER_READY"); fflush(stdout);
    fprintf(stderr, "PE: invoking capture constructor\n");
    DXGI_FORMAT format = DXGI_FORMAT_B8G8R8A8_UNORM;
    HRESULT creation = hooked ? (legacy ? IDXGIOutput5_DuplicateOutput(output5, (IUnknown *)device, &capture) :
        IDXGIOutput5_DuplicateOutput1(output5, (IUnknown *)device, 0, 1, &format, &capture)) :
        create_capture(device, descriptor, (IUnknown *)adapter, &capture);
    fprintf(stderr, "PE: capture constructor returned 0x%08lx\n", (unsigned long)creation);
    CHECK(creation == S_OK && capture);
    if (hooked) {
        IDXGIOutputDuplication *second = NULL;
        CHECK(IDXGIOutput5_DuplicateOutput1(output5, (IUnknown *)device, 0, 1, &format, &second) ==
              DXGI_ERROR_NOT_CURRENTLY_AVAILABLE && !second);
    }
    CHECK(IDXGIOutputDuplication_QueryInterface(capture, &IID_IUnknown, (void **)&identity) == S_OK &&
        identity == (IUnknown *)capture);
    IUnknown_Release(identity); identity = NULL;
    CHECK(IDXGIOutputDuplication_QueryInterface(capture, &IID_IDXGIObject, (void **)&identity) == S_OK &&
        identity == (IUnknown *)capture);
    IUnknown_Release(identity); identity = NULL;
    CHECK(IDXGIOutputDuplication_QueryInterface(capture, &IID_ID3D11Device, (void **)&identity) == E_NOINTERFACE && !identity);
    CHECK(IDXGIOutputDuplication_GetParent(capture, hooked ? &IID_IDXGIOutput5 : &IID_IDXGIAdapter1,
        (void **)&identity) == S_OK && identity == (hooked ? (IUnknown *)output5 : (IUnknown *)adapter));
    IUnknown_Release(identity); identity = NULL;
    IDXGIOutputDuplication_GetDesc(capture, &desc);
    CHECK(desc.ModeDesc.Width && desc.ModeDesc.Height && desc.ModeDesc.Format == DXGI_FORMAT_B8G8R8A8_UNORM &&
        !desc.DesktopImageInSystemMemory && desc.Rotation == DXGI_MODE_ROTATION_IDENTITY);
    if (hooked) CHECK(output_desc.DesktopCoordinates.right - output_desc.DesktopCoordinates.left == (LONG)desc.ModeDesc.Width &&
        output_desc.DesktopCoordinates.bottom - output_desc.DesktopCoordinates.top == (LONG)desc.ModeDesc.Height);
    UINT required;
    CHECK(IDXGIOutputDuplication_GetFrameDirtyRects(capture, 0, NULL, &required) == DXGI_ERROR_INVALID_CALL);
    CHECK(IDXGIOutputDuplication_ReleaseFrame(capture) == DXGI_ERROR_INVALID_CALL);
    DXGI_OUTDUPL_FRAME_INFO frame;
    CHECK(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, NULL, &resource) == E_INVALIDARG);
    CHECK(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &frame, NULL) == E_INVALIDARG);
    for (;;) {
        HRESULT result = IDXGIOutputDuplication_AcquireNextFrame(capture, 1000, &frame, &resource);
        if (result == DXGI_ERROR_WAIT_TIMEOUT) continue;
        if (result == DXGI_ERROR_ACCESS_LOST) {
            CHECK(frames && !resource);
            CHECK(IDXGIOutputDuplication_AcquireNextFrame(capture, 0, &frame, &resource) == DXGI_ERROR_ACCESS_LOST);
            CHECK(IDXGIOutputDuplication_ReleaseFrame(capture) == DXGI_ERROR_ACCESS_LOST);
            failed = 0; break;
        }
        CHECK(result == S_OK && resource && frames < 4096);
        LARGE_INTEGER now;
        QueryPerformanceCounter(&now);
        if (frame.LastPresentTime.QuadPart <= 0 || (uint64_t)frame.LastPresentTime.QuadPart <= last_timestamp ||
            frame.LastPresentTime.QuadPart > now.QuadPart)
            fprintf(stderr, "PE QPC mismatch frame=%u present=%lld last=%llu now=%lld\n", frames,
                (long long)frame.LastPresentTime.QuadPart, (unsigned long long)last_timestamp, (long long)now.QuadPart);
        CHECK(QueryPerformanceCounter(&now) && frame.LastPresentTime.QuadPart > 0 &&
            (uint64_t)frame.LastPresentTime.QuadPart > last_timestamp && frame.LastPresentTime.QuadPart <= now.QuadPart);
        CHECK(!metadata_checks(capture, &desc, &frame));
        CHECK(IDXGIResource_QueryInterface(resource, &IID_ID3D11Texture2D, (void **)&texture) == S_OK);
        if (!frames) {
            NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS open = {.version = NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS_VER,
                .deviceType = NV_ENC_DEVICE_TYPE_DIRECTX, .device = device, .apiVersion = NVENCAPI_VERSION};
            CHECK(api.nvEncOpenEncodeSessionEx(&open, &encoder) == NV_ENC_SUCCESS);
            NV_ENC_PRESET_CONFIG preset = {.version = NV_ENC_PRESET_CONFIG_VER};
            preset.presetCfg.version = NV_ENC_CONFIG_VER;
            CHECK(api.nvEncGetEncodePresetConfigEx(encoder, hevc ? NV_ENC_CODEC_HEVC_GUID : NV_ENC_CODEC_H264_GUID,
                NV_ENC_PRESET_P1_GUID, NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY, &preset) == NV_ENC_SUCCESS);
            NV_ENC_CONFIG config;
            NV_ENC_INITIALIZE_PARAMS init;
            uurb_nvenc_reference_config(&preset.presetCfg, desc.ModeDesc.Width, desc.ModeDesc.Height,
                                       hevc, 20000000, &config, &init);
            CHECK(api.nvEncInitializeEncoder(encoder, &init) == NV_ENC_SUCCESS);
            NV_ENC_REGISTER_RESOURCE input = {.version = NV_ENC_REGISTER_RESOURCE_VER,
                .resourceType = NV_ENC_INPUT_RESOURCE_TYPE_DIRECTX, .width = desc.ModeDesc.Width,
                .height = desc.ModeDesc.Height, .resourceToRegister = texture, .bufferUsage = NV_ENC_INPUT_IMAGE,
                .bufferFormat = NV_ENC_BUFFER_FORMAT_ARGB};
            CHECK(api.nvEncRegisterResource(encoder, &input) == NV_ENC_SUCCESS);
            registered = input.registeredResource;
            NV_ENC_CREATE_BITSTREAM_BUFFER buffer = {.version = NV_ENC_CREATE_BITSTREAM_BUFFER_VER};
            CHECK(api.nvEncCreateBitstreamBuffer(encoder, &buffer) == NV_ENC_SUCCESS);
            bitstream = buffer.bitstreamBuffer;
        }
        NV_ENC_MAP_INPUT_RESOURCE mapped = {.version = NV_ENC_MAP_INPUT_RESOURCE_VER, .registeredResource = registered};
        CHECK(api.nvEncMapInputResource(encoder, &mapped) == NV_ENC_SUCCESS);
        NV_ENC_PIC_PARAMS picture = {.version = NV_ENC_PIC_PARAMS_VER,
            .inputWidth = desc.ModeDesc.Width, .inputHeight = desc.ModeDesc.Height, .inputBuffer = mapped.mappedResource,
            .bufferFmt = mapped.mappedBufferFmt, .outputBitstream = bitstream,
            .inputTimeStamp = frame.LastPresentTime.QuadPart, .inputDuration = 1, .frameIdx = frames,
            .pictureStruct = NV_ENC_PIC_STRUCT_FRAME};
        CHECK(api.nvEncEncodePicture(encoder, &picture) == NV_ENC_SUCCESS);
        NV_ENC_LOCK_BITSTREAM locked = {.version = NV_ENC_LOCK_BITSTREAM_VER, .outputBitstream = bitstream};
        CHECK(api.nvEncLockBitstream(encoder, &locked) == NV_ENC_SUCCESS);
        CHECK(locked.outputTimeStamp == (uint64_t)frame.LastPresentTime.QuadPart && locked.frameIdx == frames &&
            locked.bitstreamSizeInBytes && locked.bitstreamSizeInBytes <= 64u * 1024u * 1024u - written);
        DWORD bytes;
        CHECK(WriteFile(output, locked.bitstreamBufferPtr, locked.bitstreamSizeInBytes, &bytes, NULL) &&
            bytes == locked.bitstreamSizeInBytes);
        written += bytes;
        CHECK(api.nvEncUnlockBitstream(encoder, bitstream) == NV_ENC_SUCCESS);
        CHECK(api.nvEncUnmapInputResource(encoder, mapped.mappedResource) == NV_ENC_SUCCESS);
        ID3D11Texture2D_Release(texture); texture = NULL;
        IDXGIResource_Release(resource); resource = NULL;
        CHECK(IDXGIOutputDuplication_ReleaseFrame(capture) == S_OK);
        CHECK(IDXGIOutputDuplication_ReleaseFrame(capture) == DXGI_ERROR_INVALID_CALL);
        last_timestamp = frame.LastPresentTime.QuadPart; frames++;
    }
done:
    if (identity) IUnknown_Release(identity);
    if (encoder && api.nvEncDestroyEncoder(encoder) != NV_ENC_SUCCESS) failed = 1;
    if (texture) ID3D11Texture2D_Release(texture);
    if (resource) IDXGIResource_Release(resource);
    if (capture) IDXGIOutputDuplication_Release(capture);
    if (context) ID3D11DeviceContext_Release(context);
    if (device) ID3D11Device_Release(device);
    if (output5) IDXGIOutput5_Release(output5);
    if (output_base) IDXGIOutput_Release(output_base);
    if (adapter) IDXGIAdapter1_Release(adapter);
    if (factory) IDXGIFactory1_Release(factory);
    if (capture_module) FreeLibrary(capture_module);
    if (encoder_module) FreeLibrary(encoder_module);
    if (output != INVALID_HANDLE_VALUE && !CloseHandle(output)) failed = 1;
    if (!failed) printf("D3D11_RECEIVER_REPORT {\"frames\":%u,\"width\":%u,\"height\":%u,\"codec\":\"%s\","
        "\"encoded\":true,\"encoded_bytes\":%llu,\"gpu_channel_received\":true,\"d3d11_texture_materialized\":true,"
        "\"windows_nvenc_function_table\":true,\"windows_pe_client\":true,\"dxgi_duplication_interface\":true,"
        "\"metadata_and_lease_checks\":true,\"qpc_timestamp_checks\":true,\"terminal_access_lost_checked\":true,"
        "\"encode_timestamp_source\":\"gpu-ready-qpc\",\"dxvk_creation_hook\":%s,\"capture_constructor\":\"%s\","
        "\"output_geometry_matches_capture\":%s,\"hook_negative_checks\":%s,"
        "\"capture_nominal_rate\":[%u,%u],\"uu_session_tested\":false}\n",
        frames, desc.ModeDesc.Width, desc.ModeDesc.Height, hevc ? "hevc" : "h264", (unsigned long long)written,
        hooked ? "true" : "false", hooked ? (legacy ? "legacy" : "duplicate1") : "explicit-test",
        hooked ? "true" : "false", hooked ? "true" : "false",
        desc.ModeDesc.RefreshRate.Numerator, desc.ModeDesc.RefreshRate.Denominator);
    return failed;
}
