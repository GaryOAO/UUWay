/* Runs only against uurb-nvenc-query.dll, never against a production DLL. */
#define _GNU_SOURCE
#define COBJMACROS
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <ffnvcodec/nvEncodeAPI.h>
#include <stdio.h>
#include <string.h>
#include <stdint.h>

typedef NVENCSTATUS (NVENCAPI *create_fn)(NV_ENCODE_API_FUNCTION_LIST *);
typedef NVENCSTATUS (NVENCAPI *version_fn)(uint32_t *);
#define CHECK(test) do { if (!(test)) { fprintf(stderr, "ABI check failed: %s\n", #test); goto done; } } while (0)
int main(int argc, char **argv)
{
    int missing_backend = argc == 3 && !strcmp(argv[2], "--missing-backend");
    if (argc != 2 && !missing_backend) { fprintf(stderr, "usage: uu-nvenc-query-probe.exe TEST_DLL [--missing-backend]\n"); return 2; }
    int result = 1;
    HMODULE library = LoadLibraryA(argv[1]);
    ID3D11Device *device = NULL;
    ID3D11DeviceContext *context = NULL;
    IDXGIFactory1 *factory = NULL;
    IDXGIAdapter1 *adapter = NULL;
    void *encoder = NULL;
    NV_ENCODE_API_FUNCTION_LIST api = {.version = NV_ENCODE_API_FUNCTION_LIST_VER};
    if (!library) fprintf(stderr, "LoadLibrary failed: %lu\n", (unsigned long)GetLastError());
    CHECK(library);
    create_fn create = (void *)GetProcAddress(library, "NvEncodeAPICreateInstance");
    version_fn version = (void *)GetProcAddress(library, "NvEncodeAPIGetMaxSupportedVersion");
    CHECK(create && version);
    if (missing_backend) {
        uint32_t unchanged = 0xdeadbeef;
        NV_ENCODE_API_FUNCTION_LIST saved = api;
        for (unsigned retry = 0; retry < 4; retry++) {
            CHECK(version(&unchanged) == NV_ENC_ERR_NO_ENCODE_DEVICE);
            CHECK(unchanged == 0xdeadbeef);
            CHECK(create(&api) == NV_ENC_ERR_NO_ENCODE_DEVICE);
            CHECK(!memcmp(&api, &saved, sizeof(api)));
        }
        puts("ABI missing backend checks passed; no capability advertised");
        result = 0;
        goto done;
    }
    CHECK(version(NULL) == NV_ENC_ERR_INVALID_PTR);
    uint32_t maximum = 0;
    CHECK(version(&maximum) == NV_ENC_SUCCESS && maximum == 0xd0);
    CHECK(create(NULL) == NV_ENC_ERR_INVALID_PTR);
    NV_ENCODE_API_FUNCTION_LIST invalid = {.version = 0x7002000c};
    NV_ENCODE_API_FUNCTION_LIST before = invalid;
    CHECK(create(&invalid) == NV_ENC_ERR_INVALID_VERSION);
    CHECK(!memcmp(&before, &invalid, sizeof(invalid)));
    CHECK(create(&api) == NV_ENC_SUCCESS);
    CHECK(api.nvEncOpenEncodeSessionEx && api.nvEncDestroyEncoder && api.nvEncInitializeEncoder);
    CHECK(!api.nvEncEncodePicture && !api.nvEncRegisterResource); /* explicitly query-only */
    CHECK(SUCCEEDED(CreateDXGIFactory1(&IID_IDXGIFactory1, (void **)&factory)));
    for (UINT i = 0; IDXGIFactory1_EnumAdapters1(factory, i, &adapter) == S_OK; ++i) {
        DXGI_ADAPTER_DESC1 desc;
        CHECK(SUCCEEDED(IDXGIAdapter1_GetDesc1(adapter, &desc)));
        if (desc.VendorId == 0x10de && !(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)) break;
        IDXGIAdapter1_Release(adapter); adapter = NULL;
    }
    CHECK(adapter);
    D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_11_0;
    CHECK(SUCCEEDED(D3D11CreateDevice((IDXGIAdapter *)adapter, D3D_DRIVER_TYPE_UNKNOWN, NULL,
        D3D11_CREATE_DEVICE_BGRA_SUPPORT, &level, 1, D3D11_SDK_VERSION, &device, NULL, &context)));
    NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS open = {
        .version = NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS_VER, .apiVersion = NVENCAPI_VERSION,
        .device = device, .deviceType = NV_ENC_DEVICE_TYPE_DIRECTX};
    for (unsigned cycle = 0; cycle < 4; ++cycle) {
        CHECK(api.nvEncOpenEncodeSessionEx(&open, &encoder) == NV_ENC_SUCCESS && encoder);
        uint32_t count = 0;
        CHECK(api.nvEncGetEncodeGUIDCount((void *)(uintptr_t)1, &count) == NV_ENC_ERR_INVALID_ENCODERDEVICE);
        CHECK(api.nvEncGetEncodeGUIDCount(encoder, NULL) == NV_ENC_ERR_INVALID_PTR);
        CHECK(api.nvEncGetEncodeGUIDCount(encoder, &count) == NV_ENC_SUCCESS && count && count <= 16);
        GUID codecs[16];
        CHECK(api.nvEncGetEncodeGUIDs(encoder, codecs, 16, &count) == NV_ENC_SUCCESS);
        unsigned found = 0;
        for (unsigned i = 0; i < count; ++i) {
            const char *codec = NULL;
            if (!memcmp(&codecs[i], &NV_ENC_CODEC_H264_GUID, sizeof(GUID))) { codec = "h264"; found |= 1; }
            if (!memcmp(&codecs[i], &NV_ENC_CODEC_HEVC_GUID, sizeof(GUID))) { codec = "hevc"; found |= 2; }
            if (!codec) continue;
            uint32_t nformats = 0, nprofiles = 0, npresets = 0, actual = 0;
            CHECK(api.nvEncGetInputFormatCount(encoder, codecs[i], &nformats) == NV_ENC_SUCCESS && nformats && nformats <= 128);
            NV_ENC_BUFFER_FORMAT formats[128];
            CHECK(api.nvEncGetInputFormats(encoder, codecs[i], formats, 128, &actual) == NV_ENC_SUCCESS && actual == nformats);
            CHECK(api.nvEncGetInputFormats(encoder, codecs[i], formats, 0, &actual) == NV_ENC_ERR_INVALID_PARAM);
            CHECK(api.nvEncGetEncodeProfileGUIDCount(encoder, codecs[i], &nprofiles) == NV_ENC_SUCCESS && nprofiles && nprofiles <= 128);
            GUID values[128];
            CHECK(api.nvEncGetEncodeProfileGUIDs(encoder, codecs[i], values, 128, &actual) == NV_ENC_SUCCESS && actual == nprofiles);
            CHECK(api.nvEncGetEncodePresetCount(encoder, codecs[i], &npresets) == NV_ENC_SUCCESS && npresets && npresets <= 128);
            CHECK(api.nvEncGetEncodePresetGUIDs(encoder, codecs[i], values, 128, &actual) == NV_ENC_SUCCESS && actual == npresets);
            CHECK(api.nvEncGetEncodePresetConfig && api.nvEncGetEncodePresetConfigEx);
            NV_ENC_PRESET_CONFIG preset = {.version = NV_ENC_PRESET_CONFIG_VER};
            preset.presetCfg.version = NV_ENC_CONFIG_VER;
            NV_ENC_PRESET_CONFIG unchanged = preset;
            CHECK(api.nvEncGetEncodePresetConfigEx(encoder, codecs[i], NV_ENC_PRESET_P1_GUID,
                NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY, NULL) == NV_ENC_ERR_INVALID_PTR);
            CHECK(api.nvEncGetEncodePresetConfigEx((void *)(uintptr_t)1, codecs[i], NV_ENC_PRESET_P1_GUID,
                NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY, &preset) == NV_ENC_ERR_INVALID_ENCODERDEVICE);
            CHECK(!memcmp(&preset, &unchanged, sizeof(preset)));
            preset.presetCfg.version = 0;
            CHECK(api.nvEncGetEncodePresetConfigEx(encoder, codecs[i], NV_ENC_PRESET_P1_GUID,
                NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY, &preset) == NV_ENC_ERR_INVALID_VERSION);
            preset = unchanged;
            CHECK(api.nvEncGetEncodePresetConfigEx(encoder, codecs[i], NV_ENC_PRESET_P1_GUID,
                NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY, &preset) == NV_ENC_SUCCESS);
            CHECK(preset.presetCfg.version == NV_ENC_CONFIG_VER);
            NV_ENC_CAPS_PARAM cap = {.version = NV_ENC_CAPS_PARAM_VER, .capsToQuery = NV_ENC_CAPS_WIDTH_MAX};
            int width = 0;
            CHECK(api.nvEncGetEncodeCaps(encoder, codecs[i], &cap, &width) == NV_ENC_SUCCESS && width >= 3840);
            printf("QUERY {\"cycle\":%u,\"codec\":\"%s\",\"formats\":[", cycle, codec);
            for (unsigned f = 0; f < nformats; ++f) printf("%s%u", f ? "," : "", formats[f]);
            printf("],\"profiles\":%u,\"presets\":%u,\"max_width\":%d,\"preset_config_ex\":true}\n", nprofiles, npresets, width);
        }
        CHECK(found == 3);
        NV_ENC_INITIALIZE_PARAMS init = {.version = NV_ENC_INITIALIZE_PARAMS_VER, .enableEncodeAsync = 1};
        NV_ENC_INITIALIZE_PARAMS saved = init;
        CHECK(api.nvEncInitializeEncoder(encoder, &init) == NV_ENC_ERR_UNIMPLEMENTED);
        CHECK(!memcmp(&saved, &init, sizeof(init))); /* no silent async flag rewrite */
        void *stale = encoder;
        CHECK(api.nvEncDestroyEncoder(encoder) == NV_ENC_SUCCESS);
        encoder = NULL;
        CHECK(api.nvEncDestroyEncoder(stale) == NV_ENC_ERR_INVALID_ENCODERDEVICE);
        CHECK(api.nvEncGetEncodeGUIDCount(stale, &count) == NV_ENC_ERR_INVALID_ENCODERDEVICE);
    }
    puts("ABI query checks passed; encoding_not_implemented=true; uu_session_tested=false");
    result = 0;
done:
    if (encoder && api.nvEncDestroyEncoder) api.nvEncDestroyEncoder(encoder);
    if (context) ID3D11DeviceContext_Release(context);
    if (device) ID3D11Device_Release(device);
    if (adapter) IDXGIAdapter1_Release(adapter);
    if (factory) IDXGIFactory1_Release(factory);
    if (library) FreeLibrary(library);
    return result;
}
