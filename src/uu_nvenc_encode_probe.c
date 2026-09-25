/* Real Windows PE client exercising standard NVENC ABI, not internal calls.
 * Synthetic D3D11 textures only; run under a disposable Wine/Xvfb prefix. */
#define COBJMACROS
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "native_nvenc_reference_config.h"
#include "../tests/probes/d3d11_nv12_fixture.h"

typedef NVENCSTATUS (NVENCAPI *create_fn)(NV_ENCODE_API_FUNCTION_LIST *);
#define CHECK(test) do { if (!(test)) { fprintf(stderr, "ENCODE ABI check failed: %s line %d\n", #test, __LINE__); goto done; } } while (0)
int main(int argc, char **argv)
{
    if (argc != 4 || (strcmp(argv[2], "bgra") && strcmp(argv[2], "nv12")) ||
        (strcmp(argv[3], "sampled") && strcmp(argv[3], "rtv") && strcmp(argv[3], "uav"))) return 2;
    int nv12 = !strcmp(argv[2], "nv12"), result = 1;
    char negotiation_flag[2];
    int negotiated = GetEnvironmentVariableA("UURB_NVENC_NEGOTIATION_PROBE", negotiation_flag, 2) == 1 && negotiation_flag[0] == '1';
    HMODULE library = LoadLibraryA("uurb-nvenc-encode-loader.dll");
    IDXGIFactory1 *factory = NULL;
    IDXGIAdapter1 *adapter = NULL;
    ID3D11Device *device = NULL;
    ID3D11DeviceContext *context = NULL;
    ID3D11Texture2D *textures[2] = {NULL};
    ID3D11Texture2D *render_textures[2] = {NULL};
    ID3D11RenderTargetView *targets[2][2] = {{NULL}};
    struct nv12_fixture fixture = {0};
    HANDLE output = INVALID_HANDLE_VALUE;
    void *encoder = NULL, *other = NULL;
    NV_ENCODE_API_FUNCTION_LIST api = {.version = NV_ENCODE_API_FUNCTION_LIST_VER};
    CHECK(library);
    create_fn create = (void *)GetProcAddress(library, "NvEncodeAPICreateInstance");
    CHECK(create && create(&api) == NV_ENC_SUCCESS);
    CHECK(api.nvEncInitializeEncoder && api.nvEncRegisterResource && api.nvEncMapInputResource &&
          api.nvEncEncodePicture && api.nvEncLockBitstream && api.nvEncUnlockBitstream);
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
    for (unsigned i = 0; i < 2; ++i) {
        D3D11_TEXTURE2D_DESC desc = {.Width = 640, .Height = 360, .MipLevels = 1, .ArraySize = 1,
            .Format = nv12 ? DXGI_FORMAT_NV12 : DXGI_FORMAT_B8G8R8A8_UNORM, .SampleDesc = {1, 0},
            .Usage = D3D11_USAGE_DEFAULT, .BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET};
        if (!strcmp(argv[3], "rtv")) desc.BindFlags = D3D11_BIND_RENDER_TARGET;
        if (!strcmp(argv[3], "uav")) desc.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
        CHECK(SUCCEEDED(ID3D11Device_CreateTexture2D(device, &desc, NULL, &textures[i])));
        if (!strcmp(argv[3], "uav")) {
            desc.BindFlags = D3D11_BIND_RENDER_TARGET;
            CHECK(SUCCEEDED(ID3D11Device_CreateTexture2D(device, &desc, NULL, &render_textures[i])));
        } else {
            render_textures[i] = textures[i];
            ID3D11Texture2D_AddRef(render_textures[i]);
        }
        D3D11_RENDER_TARGET_VIEW_DESC view = {.Format = nv12 ? DXGI_FORMAT_R8_UNORM : desc.Format,
            .ViewDimension = D3D11_RTV_DIMENSION_TEXTURE2D};
        CHECK(SUCCEEDED(ID3D11Device_CreateRenderTargetView(device, (ID3D11Resource *)render_textures[i], &view, &targets[i][0])));
        if (nv12) {
            view.Format = DXGI_FORMAT_R8G8_UNORM;
            CHECK(SUCCEEDED(ID3D11Device_CreateRenderTargetView(device, (ID3D11Resource *)render_textures[i], &view, &targets[i][1])));
        }
    }
    for (unsigned codec = 0; codec < 2; ++codec) {
        GUID guid = codec ? NV_ENC_CODEC_HEVC_GUID : NV_ENC_CODEC_H264_GUID;
        const char *name = codec ? "hevc" : "h264";
        char filename[4096];
        CHECK(snprintf(filename, sizeof(filename), "%s/%s.bin", argv[1], name) < (int)sizeof(filename));
        output = CreateFileA(filename, GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_TEMPORARY, NULL);
        CHECK(output != INVALID_HANDLE_VALUE);
        NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS open = {.version = NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS_VER,
            .deviceType = NV_ENC_DEVICE_TYPE_DIRECTX, .device = device, .apiVersion = NVENCAPI_VERSION};
        CHECK(api.nvEncOpenEncodeSessionEx(&open, &encoder) == NV_ENC_SUCCESS);
        /* Exercise every unsupported public slot through the real PE ABI. */
#define REJECT_STRUCT(name, type) do { \
    type params, before; memset(&params, 0xa5, sizeof(params)); before = params; \
    CHECK(api.name && api.name(encoder, &params) == NV_ENC_ERR_UNIMPLEMENTED && \
          !memcmp(&params, &before, sizeof(params))); \
} while (0)
        REJECT_STRUCT(nvEncCreateInputBuffer, NV_ENC_CREATE_INPUT_BUFFER);
        REJECT_STRUCT(nvEncLockInputBuffer, NV_ENC_LOCK_INPUT_BUFFER);
        REJECT_STRUCT(nvEncGetEncodeStats, NV_ENC_STAT);
        REJECT_STRUCT(nvEncRegisterAsyncEvent, NV_ENC_EVENT_PARAMS);
        REJECT_STRUCT(nvEncUnregisterAsyncEvent, NV_ENC_EVENT_PARAMS);
        REJECT_STRUCT(nvEncCreateMVBuffer, NV_ENC_CREATE_MV_BUFFER);
        REJECT_STRUCT(nvEncRunMotionEstimationOnly, NV_ENC_MEONLY_PARAMS);
        REJECT_STRUCT(nvEncRestoreEncoderState, NV_ENC_RESTORE_ENCODER_STATE_PARAMS);
        REJECT_STRUCT(nvEncLookaheadPicture, NV_ENC_LOOKAHEAD_PIC_PARAMS);
#undef REJECT_STRUCT
        CHECK(api.nvEncDestroyInputBuffer && api.nvEncDestroyInputBuffer(encoder, NULL) == NV_ENC_ERR_UNIMPLEMENTED);
        CHECK(api.nvEncUnlockInputBuffer && api.nvEncUnlockInputBuffer(encoder, NULL) == NV_ENC_ERR_UNIMPLEMENTED);
        CHECK(api.nvEncDestroyMVBuffer && api.nvEncDestroyMVBuffer(encoder, NULL) == NV_ENC_ERR_UNIMPLEMENTED);
        CHECK(api.nvEncInvalidateRefFrames && api.nvEncInvalidateRefFrames(encoder, UINT64_MAX) == NV_ENC_ERR_UNIMPLEMENTED);
        CHECK(api.nvEncSetIOCudaStreams && api.nvEncSetIOCudaStreams(encoder, NULL, NULL) == NV_ENC_ERR_UNIMPLEMENTED);
        NV_ENC_SEQUENCE_PARAM_PAYLOAD sequence = {.version = NV_ENC_SEQUENCE_PARAM_PAYLOAD_VER};
        NV_ENC_SEQUENCE_PARAM_PAYLOAD before_sequence = sequence;
        CHECK(api.nvEncGetSequenceParamEx && api.nvEncGetSequenceParamEx(encoder, NULL, &sequence) == NV_ENC_ERR_INVALID_PTR &&
              !memcmp(&sequence, &before_sequence, sizeof(sequence)));
        unsigned char header_bytes[4096];
        memset(header_bytes, 0xa5, sizeof(header_bytes));
        uint32_t header_size = 12345;
        sequence.spsppsBuffer = header_bytes; sequence.inBufferSize = sizeof(header_bytes);
        sequence.outSPSPPSPayloadSize = &header_size;
        CHECK(api.nvEncGetSequenceParams(encoder, &sequence) == NV_ENC_ERR_ENCODER_NOT_INITIALIZED);
        CHECK(header_size == 12345 && header_bytes[0] == 0xa5);
        void *legacy = (void *)(uintptr_t)0x1234;
        CHECK(api.nvEncOpenEncodeSession && api.nvEncOpenEncodeSession(device, 0, &legacy) == NV_ENC_ERR_UNIMPLEMENTED &&
              legacy == (void *)(uintptr_t)0x1234);
        CHECK(api.nvEncGetLastErrorString && api.nvEncGetLastErrorString(encoder));
        uint32_t count;
        CHECK(api.nvEncGetInputFormatCount(encoder, guid, &count) == NV_ENC_SUCCESS && count == 2);
        NV_ENC_BUFFER_FORMAT formats[2];
        CHECK(api.nvEncGetInputFormats(encoder, guid, formats, 2, &count) == NV_ENC_SUCCESS);
        CHECK((formats[0] == NV_ENC_BUFFER_FORMAT_NV12 && formats[1] == NV_ENC_BUFFER_FORMAT_ARGB) ||
              (formats[1] == NV_ENC_BUFFER_FORMAT_NV12 && formats[0] == NV_ENC_BUFFER_FORMAT_ARGB));
        CHECK(api.nvEncGetEncodeProfileGUIDCount(encoder, guid, &count) == NV_ENC_SUCCESS && count == 1);
        GUID profile;
        CHECK(api.nvEncGetEncodeProfileGUIDs(encoder, guid, &profile, 1, &count) == NV_ENC_SUCCESS && count == 1);
        CHECK(!memcmp(&profile, &NV_ENC_CODEC_PROFILE_AUTOSELECT_GUID, sizeof(profile)));
        CHECK(api.nvEncGetEncodePresetCount(encoder, guid, &count) == NV_ENC_SUCCESS && count == 1);
        CHECK(api.nvEncGetEncodePresetGUIDs(encoder, guid, &profile, 1, &count) == NV_ENC_SUCCESS && count == 1);
        CHECK(!memcmp(&profile, &NV_ENC_PRESET_P1_GUID, sizeof(profile)));
        count = 12345;
        NV_ENC_BUFFER_FORMAT unchanged_formats[2];
        memcpy(unchanged_formats, formats, sizeof(formats));
        CHECK(api.nvEncGetInputFormats(encoder, guid, formats, 1, &count) == NV_ENC_ERR_NOT_ENOUGH_BUFFER);
        CHECK(count == 12345 && !memcmp(formats, unchanged_formats, sizeof(formats)));
        NV_ENC_CAPS_PARAM caps = {.version = NV_ENC_CAPS_PARAM_VER, .capsToQuery = NV_ENC_CAPS_SUPPORT_10BIT_ENCODE};
        int supported = -1;
        CHECK(api.nvEncGetEncodeCaps(encoder, guid, &caps, &supported) == NV_ENC_SUCCESS && supported == 0);
        caps.capsToQuery = NV_ENC_CAPS_LEVEL_MAX; supported = -12345;
        CHECK(api.nvEncGetEncodeCaps(encoder, guid, &caps, &supported) == NV_ENC_ERR_UNIMPLEMENTED && supported == -12345);
        NV_ENC_PRESET_CONFIG preset = {.version = NV_ENC_PRESET_CONFIG_VER};
        preset.presetCfg.version = NV_ENC_CONFIG_VER;
        NV_ENC_PRESET_CONFIG before_preset = preset;
        CHECK(api.nvEncGetEncodePresetConfig(encoder, guid, NV_ENC_PRESET_P1_GUID, &preset) == NV_ENC_ERR_UNIMPLEMENTED &&
              !memcmp(&preset, &before_preset, sizeof(preset)));
        CHECK(api.nvEncGetEncodePresetConfigEx(encoder, guid, NV_ENC_PRESET_P1_GUID,
            NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY, &preset) == NV_ENC_SUCCESS);
        NV_ENC_CONFIG config;
        NV_ENC_INITIALIZE_PARAMS init;
        uurb_nvenc_reference_config(&preset.presetCfg, 640, 360, codec, 40000000, &config, &init);
        init.enableEncodeAsync = 1;
        CHECK(api.nvEncInitializeEncoder(encoder, &init) == NV_ENC_ERR_UNSUPPORTED_PARAM && init.enableEncodeAsync == 1);
        init.enableEncodeAsync = 0; init.frameRateNum = 241;
        CHECK(api.nvEncInitializeEncoder(encoder, &init) == NV_ENC_ERR_UNSUPPORTED_PARAM && init.frameRateNum == 241);
        init.frameRateNum = 60;
        if (negotiated) {
            init.frameRateNum = codec ? 120 : 30000;
            init.frameRateDen = codec ? 1 : 1001;
            config.gopLength = NVENC_INFINITE_GOPLENGTH;
            config.rcParams.rateControlMode = NV_ENC_PARAMS_RC_VBR;
            config.rcParams.averageBitRate = 12000000;
            config.rcParams.maxBitRate = 24000000;
            config.rcParams.vbvBufferSize = 300000;
            config.rcParams.vbvInitialDelay = 150000;
            config.rcParams.enableAQ = 1;
            config.rcParams.aqStrength = 5;
        }
        NV_ENC_CONFIG original_config = config;
        NV_ENC_INITIALIZE_PARAMS original_init = init;
        CHECK(api.nvEncGetSequenceParamEx(NULL, &init, &sequence) == NV_ENC_ERR_INVALID_ENCODERDEVICE);
        CHECK(api.nvEncGetSequenceParamEx(encoder, NULL, &sequence) == NV_ENC_ERR_INVALID_PTR);
        init.enableEncodeAsync = 1;
        CHECK(api.nvEncGetSequenceParamEx(encoder, &init, &sequence) == NV_ENC_ERR_UNSUPPORTED_PARAM);
        init.enableEncodeAsync = 0;
        sequence.inBufferSize = 1;
        CHECK(api.nvEncGetSequenceParamEx(encoder, &init, &sequence) == NV_ENC_ERR_NOT_ENOUGH_BUFFER);
        CHECK(header_size == 12345 && header_bytes[0] == 0xa5);
        sequence.inBufferSize = sizeof(header_bytes);
        CHECK(api.nvEncGetSequenceParamEx(encoder, &init, &sequence) == NV_ENC_SUCCESS);
        CHECK(header_size > 8 && header_size < sizeof(header_bytes));
        unsigned char before_init_headers[4096];
        uint32_t before_init_size = header_size;
        memcpy(before_init_headers, header_bytes, header_size);
        CHECK(!memcmp(&config, &original_config, sizeof(config)) && !memcmp(&init, &original_init, sizeof(init)));
        /* Out-of-band query must not initialize or allocate frame resources. */
        CHECK(api.nvEncGetSequenceParams(encoder, &sequence) == NV_ENC_ERR_ENCODER_NOT_INITIALIZED);
        memset(header_bytes, 0xa5, sizeof(header_bytes));
        header_size = 12345;
        NVENCSTATUS initialized = api.nvEncInitializeEncoder(encoder, &init);
        if (initialized) fprintf(stderr, "Initialize status: %d\n", initialized);
        CHECK(initialized == NV_ENC_SUCCESS);
        CHECK(!memcmp(&config, &original_config, sizeof(config)) && !memcmp(&init, &original_init, sizeof(init)));
        sequence.version = 0;
        CHECK(api.nvEncGetSequenceParams(encoder, &sequence) == NV_ENC_ERR_INVALID_VERSION);
        sequence.version = NV_ENC_SEQUENCE_PARAM_PAYLOAD_VER;
        sequence.reserved[0] = 1;
        CHECK(api.nvEncGetSequenceParams(encoder, &sequence) == NV_ENC_ERR_UNSUPPORTED_PARAM);
        sequence.reserved[0] = 0; sequence.spsId = 1;
        CHECK(api.nvEncGetSequenceParams(encoder, &sequence) == NV_ENC_ERR_UNSUPPORTED_PARAM);
        sequence.spsId = 0; sequence.inBufferSize = 0;
        CHECK(api.nvEncGetSequenceParams(encoder, &sequence) == NV_ENC_ERR_INVALID_PARAM);
        sequence.inBufferSize = 1;
        CHECK(api.nvEncGetSequenceParams(encoder, &sequence) != NV_ENC_SUCCESS);
        CHECK(header_size == 12345 && header_bytes[0] == 0xa5);
        sequence.inBufferSize = sizeof(header_bytes);
        CHECK(api.nvEncGetSequenceParams(encoder, &sequence) == NV_ENC_SUCCESS);
        CHECK(header_size > 8 && header_size < sizeof(header_bytes));
        CHECK(header_size == before_init_size && !memcmp(header_bytes, before_init_headers, header_size));
        CHECK(api.nvEncGetSequenceParamEx(encoder, NULL, &sequence) == NV_ENC_SUCCESS);
        NV_ENC_INITIALIZE_PARAMS ignored_init = {0};
        CHECK(api.nvEncGetSequenceParamEx(encoder, &ignored_init, &sequence) == NV_ENC_SUCCESS);
        CHECK(header_size == before_init_size && !memcmp(header_bytes, before_init_headers, header_size));
        printf("SEQUENCE_EX %s preinit_and_active_headers_match\n", name);
        CHECK(snprintf(filename, sizeof(filename), "%s/%s.sequence.bin", argv[1], name) < (int)sizeof(filename));
        HANDLE header_file = CreateFileA(filename, GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_TEMPORARY, NULL);
        CHECK(header_file != INVALID_HANDLE_VALUE);
        DWORD header_written = 0;
        BOOL header_ok = WriteFile(header_file, header_bytes, header_size, &header_written, NULL);
        CloseHandle(header_file);
        CHECK(header_ok && header_written == header_size);
        CHECK(api.nvEncInitializeEncoder(encoder, &init) == NV_ENC_ERR_INVALID_CALL);
        CHECK(api.nvEncOpenEncodeSessionEx(&open, &other) == NV_ENC_SUCCESS);
        CHECK(api.nvEncInitializeEncoder(other, &init) == NV_ENC_SUCCESS);
        NV_ENC_REGISTERED_PTR resources[2];
        NV_ENC_OUTPUT_PTR outputs[2];
        for (unsigned i = 0; i < 2; ++i) {
            NV_ENC_REGISTER_RESOURCE registered = {.version = NV_ENC_REGISTER_RESOURCE_VER,
                .resourceType = NV_ENC_INPUT_RESOURCE_TYPE_DIRECTX, .width = 640, .height = 360,
                .resourceToRegister = textures[i], .bufferUsage = NV_ENC_INPUT_IMAGE,
                .bufferFormat = nv12 ? NV_ENC_BUFFER_FORMAT_NV12 : NV_ENC_BUFFER_FORMAT_ARGB};
            registered.reserved1[0] = 1; registered.registeredResource = (void *)(uintptr_t)0x1234;
            NV_ENC_REGISTER_RESOURCE before_register = registered;
            CHECK(api.nvEncRegisterResource(encoder, &registered) == NV_ENC_ERR_UNSUPPORTED_PARAM &&
                  !memcmp(&registered, &before_register, sizeof(registered)));
            registered.reserved1[0] = 0;
            CHECK(api.nvEncRegisterResource(encoder, &registered) == NV_ENC_SUCCESS);
            resources[i] = registered.registeredResource;
            CHECK(api.nvEncUnregisterResource(other, resources[i]) == NV_ENC_ERR_RESOURCE_NOT_REGISTERED);
            NV_ENC_CREATE_BITSTREAM_BUFFER buffer = {.version = NV_ENC_CREATE_BITSTREAM_BUFFER_VER};
            CHECK(api.nvEncCreateBitstreamBuffer(encoder, &buffer) == NV_ENC_SUCCESS);
            outputs[i] = buffer.bitstreamBuffer;
            NV_ENC_LOCK_BITSTREAM locked = {.version = NV_ENC_LOCK_BITSTREAM_VER, .outputBitstream = outputs[i]};
            CHECK(api.nvEncLockBitstream(encoder, &locked) == NV_ENC_ERR_LOCK_BUSY);
            CHECK(api.nvEncDestroyBitstreamBuffer(other, outputs[i]) == NV_ENC_ERR_INVALID_PARAM);
        }
        /* Fill both independent limits, then ensure refusal is transactional. */
        NV_ENC_REGISTERED_PTR extra_inputs[14];
        NV_ENC_OUTPUT_PTR extra_outputs[14];
        for (unsigned i = 0; i < 15; ++i) {
            NV_ENC_REGISTER_RESOURCE registered = {.version = NV_ENC_REGISTER_RESOURCE_VER,
                .resourceType = NV_ENC_INPUT_RESOURCE_TYPE_DIRECTX, .width = 640, .height = 360,
                .resourceToRegister = textures[0], .bufferUsage = NV_ENC_INPUT_IMAGE,
                .bufferFormat = nv12 ? NV_ENC_BUFFER_FORMAT_NV12 : NV_ENC_BUFFER_FORMAT_ARGB,
                .registeredResource = (void *)(uintptr_t)0x1234};
            NV_ENC_CREATE_BITSTREAM_BUFFER buffer = {.version = NV_ENC_CREATE_BITSTREAM_BUFFER_VER,
                .bitstreamBuffer = (void *)(uintptr_t)0x5678};
            if (i == 14) {
                NV_ENC_REGISTER_RESOURCE before = registered;
                NV_ENC_CREATE_BITSTREAM_BUFFER before_buffer = buffer;
                CHECK(api.nvEncRegisterResource(encoder, &registered) == NV_ENC_ERR_OUT_OF_MEMORY &&
                      !memcmp(&registered, &before, sizeof(before)));
                CHECK(api.nvEncCreateBitstreamBuffer(encoder, &buffer) == NV_ENC_ERR_OUT_OF_MEMORY &&
                      !memcmp(&buffer, &before_buffer, sizeof(buffer)));
            } else {
                CHECK(api.nvEncRegisterResource(encoder, &registered) == NV_ENC_SUCCESS);
                extra_inputs[i] = registered.registeredResource;
                CHECK(api.nvEncCreateBitstreamBuffer(encoder, &buffer) == NV_ENC_SUCCESS);
                extra_outputs[i] = buffer.bitstreamBuffer;
            }
        }
        for (unsigned i = 0; i < 14; ++i) {
            CHECK(api.nvEncUnregisterResource(encoder, extra_inputs[i]) == NV_ENC_SUCCESS);
            CHECK(api.nvEncDestroyBitstreamBuffer(encoder, extra_outputs[i]) == NV_ENC_SUCCESS);
        }
        for (unsigned frame = 0; frame < 64; ++frame) {
            if (frame == 32) {
                if (negotiated) {
                    init.frameRateNum = 60; init.frameRateDen = 1;
                    config.rcParams.averageBitRate = 6000000;
                    config.rcParams.maxBitRate = 16000000;
                    config.rcParams.vbvBufferSize = 200000;
                    config.rcParams.vbvInitialDelay = 100000;
                } else uurb_nvenc_reference_config(&preset.presetCfg, 640, 360, codec, 20000000, &config, &init);
                NV_ENC_RECONFIGURE_PARAMS change = {.version = NV_ENC_RECONFIGURE_PARAMS_VER,
                    .reInitEncodeParams = init, .resetEncoder = 1, .forceIDR = 1};
                change.reInitEncodeParams.encodeWidth = 1280;
                CHECK(api.nvEncReconfigureEncoder(encoder, &change) == NV_ENC_ERR_UNSUPPORTED_PARAM);
                change.reInitEncodeParams.encodeWidth = 640;
                CHECK(api.nvEncReconfigureEncoder(encoder, &change) == NV_ENC_SUCCESS);
            }
            if (negotiated && frame == 40) {
                config.rcParams.averageBitRate = 8000000;
                NV_ENC_RECONFIGURE_PARAMS change = {.version = NV_ENC_RECONFIGURE_PARAMS_VER,
                    .reInitEncodeParams = init};
                CHECK(api.nvEncReconfigureEncoder(encoder, &change) == NV_ENC_SUCCESS);
            }
            if (negotiated && (frame == 44 || frame == 52)) {
                init.presetGUID = frame == 44 ? NV_ENC_PRESET_P4_GUID : NV_ENC_PRESET_P1_GUID;
                init.tuningInfo = frame == 44 ? NV_ENC_TUNING_INFO_HIGH_QUALITY : NV_ENC_TUNING_INFO_ULTRA_LOW_LATENCY;
                NV_ENC_RECONFIGURE_PARAMS change = {.version = NV_ENC_RECONFIGURE_PARAMS_VER,
                    .reInitEncodeParams = init, .resetEncoder = 1, .forceIDR = 1};
                CHECK(api.nvEncReconfigureEncoder(encoder, &change) == NV_ENC_SUCCESS);
                printf("PRESET_SWITCH %s %u passed\n", name, frame);
            }
            unsigned index = frame % 2;
            const float color[] = {(frame & 3) / 3.0f, ((frame >> 2) & 3) / 3.0f, ((frame >> 4) & 3) / 3.0f, 1};
            if (nv12) {
                CHECK(SUCCEEDED(nv12_fixture_draw(&fixture, device, context, targets[index], color)));
            } else ID3D11DeviceContext_ClearRenderTargetView(context, targets[index][0], color);
            if (render_textures[index] != textures[index])
                ID3D11DeviceContext_CopyResource(context, (ID3D11Resource *)textures[index], (ID3D11Resource *)render_textures[index]);
            NV_ENC_MAP_INPUT_RESOURCE mapped = {.version = NV_ENC_MAP_INPUT_RESOURCE_VER, .registeredResource = resources[index]};
            NV_ENC_MAP_INPUT_RESOURCE unchanged_map = mapped;
            CHECK(api.nvEncMapInputResource(other, &mapped) == NV_ENC_ERR_RESOURCE_NOT_REGISTERED &&
                  !memcmp(&mapped, &unchanged_map, sizeof(mapped)));
            CHECK(api.nvEncMapInputResource(encoder, &mapped) == NV_ENC_SUCCESS);
            CHECK(api.nvEncUnregisterResource(encoder, resources[index]) == NV_ENC_ERR_INVALID_CALL);
            CHECK(api.nvEncMapInputResource(encoder, &mapped) == NV_ENC_ERR_INVALID_CALL);
            NV_ENC_PIC_PARAMS picture = {.version = NV_ENC_PIC_PARAMS_VER, .inputWidth = 640, .inputHeight = 360,
                .inputBuffer = mapped.mappedResource, .bufferFmt = mapped.mappedBufferFmt,
                .outputBitstream = outputs[index], .inputTimeStamp = 1000 + frame, .inputDuration = 1,
                .frameIdx = frame, .pictureStruct = NV_ENC_PIC_STRUCT_FRAME,
                .encodePicFlags = frame == 21 ? NV_ENC_PIC_FLAG_FORCEIDR | NV_ENC_PIC_FLAG_OUTPUT_SPSPPS : 0};
            picture.inputPitch = 1;
            CHECK(api.nvEncEncodePicture(encoder, &picture) == NV_ENC_ERR_UNSUPPORTED_PARAM);
            picture.inputPitch = 0;
            if (index) {
                picture.outputBitstream = outputs[0];
                CHECK(api.nvEncEncodePicture(encoder, &picture) == NV_ENC_ERR_INVALID_CALL);
                picture.outputBitstream = outputs[index];
            }
            NVENCSTATUS encoded = api.nvEncEncodePicture(encoder, &picture);
            if (encoded) fprintf(stderr, "Encode status: %d frame %u\n", encoded, frame);
            CHECK(encoded == NV_ENC_SUCCESS);
            if (frame != 63) {
                CHECK(api.nvEncUnmapInputResource(encoder, mapped.mappedResource) == NV_ENC_SUCCESS);
                CHECK(api.nvEncUnmapInputResource(encoder, mapped.mappedResource) == NV_ENC_ERR_RESOURCE_NOT_MAPPED);
                picture.inputTimeStamp++;
                picture.outputBitstream = outputs[1-index];
                CHECK(api.nvEncEncodePicture(encoder, &picture) == NV_ENC_ERR_INVALID_PARAM);
            }
            if (!index) continue; /* Retain first packet while encoding the next. */
            for (unsigned i = 0; i < 2; ++i) {
                NV_ENC_LOCK_BITSTREAM locked = {.version = NV_ENC_LOCK_BITSTREAM_VER, .outputBitstream = outputs[i]};
                CHECK(api.nvEncLockBitstream(encoder, &locked) == NV_ENC_SUCCESS);
                CHECK(locked.outputTimeStamp == 1000 + frame - 1 + i && locked.frameIdx == frame - 1 + i &&
                      locked.outputDuration == 1 && locked.bitstreamBufferPtr && locked.bitstreamSizeInBytes);
                if (negotiated && locked.frameIdx == 40) CHECK(locked.pictureType != NV_ENC_PIC_TYPE_IDR);
                CHECK(api.nvEncDestroyBitstreamBuffer(encoder, outputs[i]) == NV_ENC_ERR_INVALID_CALL);
                NV_ENC_LOCK_BITSTREAM unchanged = locked;
                CHECK(api.nvEncLockBitstream(encoder, &locked) == NV_ENC_ERR_INVALID_CALL && !memcmp(&locked, &unchanged, sizeof(locked)));
                DWORD written;
                CHECK(WriteFile(output, locked.bitstreamBufferPtr, locked.bitstreamSizeInBytes, &written, NULL) &&
                      written == locked.bitstreamSizeInBytes);
                printf("ENCODE {\"codec\":\"%s\",\"frame\":%u,\"timestamp\":%llu,\"bytes\":%u,\"idr\":%s}\n", name,
                    locked.frameIdx, (unsigned long long)locked.outputTimeStamp, locked.bitstreamSizeInBytes,
                    locked.pictureType == NV_ENC_PIC_TYPE_IDR ? "true" : "false");
                if (frame != 63 || i != 1) {
                    CHECK(api.nvEncUnlockBitstream(encoder, outputs[i]) == NV_ENC_SUCCESS);
                    CHECK(api.nvEncUnlockBitstream(encoder, outputs[i]) == NV_ENC_ERR_INVALID_CALL);
                }
            }
        }
        NV_ENC_PIC_PARAMS eos = {.version = NV_ENC_PIC_PARAMS_VER, .encodePicFlags = NV_ENC_PIC_FLAG_EOS};
        CHECK(api.nvEncEncodePicture(encoder, &eos) == NV_ENC_SUCCESS);
        CHECK(api.nvEncEncodePicture(encoder, &eos) == NV_ENC_ERR_INVALID_CALL);
        /* Leave the last mapped texture and locked packet for session cleanup. */
        for (unsigned i = 0; i < 1; ++i) {
            CHECK(api.nvEncUnregisterResource(encoder, resources[i]) == NV_ENC_SUCCESS);
            CHECK(api.nvEncUnregisterResource(encoder, resources[i]) == NV_ENC_ERR_RESOURCE_NOT_REGISTERED);
            CHECK(api.nvEncDestroyBitstreamBuffer(encoder, outputs[i]) == NV_ENC_SUCCESS);
            CHECK(api.nvEncDestroyBitstreamBuffer(encoder, outputs[i]) == NV_ENC_ERR_INVALID_PARAM);
        }
        void *stale = encoder;
        CHECK(api.nvEncDestroyEncoder(encoder) == NV_ENC_SUCCESS); encoder = NULL;
        CHECK(api.nvEncDestroyEncoder(stale) == NV_ENC_ERR_INVALID_ENCODERDEVICE);
        CHECK(api.nvEncUnmapInputResource(stale, resources[1]) == NV_ENC_ERR_INVALID_ENCODERDEVICE);
        CHECK(api.nvEncDestroyEncoder(other) == NV_ENC_SUCCESS); other = NULL;
        CHECK(CloseHandle(output)); output = INVALID_HANDLE_VALUE;
    }
    puts("ENCODE ABI checks passed; real PE, two source textures and two retained packets, no UU session");
    result = 0;
done:
    if (other) api.nvEncDestroyEncoder(other);
    if (encoder) api.nvEncDestroyEncoder(encoder);
    nv12_fixture_close(&fixture);
    if (output != INVALID_HANDLE_VALUE) CloseHandle(output);
    for (unsigned i = 0; i < 2; ++i) {
        for (unsigned p = 0; p < 2; ++p) if (targets[i][p]) ID3D11RenderTargetView_Release(targets[i][p]);
        if (textures[i]) ID3D11Texture2D_Release(textures[i]);
        if (render_textures[i]) ID3D11Texture2D_Release(render_textures[i]);
    }
    if (context) ID3D11DeviceContext_Release(context);
    if (device) ID3D11Device_Release(device);
    if (adapter) IDXGIAdapter1_Release(adapter);
    if (factory) IDXGIFactory1_Release(factory);
    if (library) FreeLibrary(library);
    return result;
}
