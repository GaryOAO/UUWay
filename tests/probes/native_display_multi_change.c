/* The production Win32 shim and standard UU flags, without vendor processes. */
#define UURB_NATIVE_INPUT_ONLY
#include "../../src/uu_input_bridge.c"
#define CHECK(value) do { if (!(value)) { fprintf(stderr, "MULTI check failed: %s\n", #value); return 1; } } while (0)
static unsigned changed;
static struct uurb_display_request sent;
static BOOL exclusive;
static UINT32 topology_packet[51], topology_bytes;
static DWORD WINAPI topology_fixture(const char *path, struct uurb_display_topology *out, DWORD bytes)
{
    (void)path;
    if (bytes != sizeof(*out)) return UURB_DISPLAY_INVALID;
    ZeroMemory(out, sizeof(*out));
    out->version = UURB_DISPLAY_TOPOLOGY_VERSION; out->serial = 7;
    out->monitor_count = 2; out->mode_count = 6;
    for (unsigned i = 0; i < 2; ++i) {
        out->monitors[i] = (struct uurb_display_monitor){.index = i, .x = i * 1920,
            .width = 1920, .height = 1080, .refresh_millihz = 60000, .scale_milli = 1000,
            .scale_mask = 1 | (1 << 4),
            .mode_offset = i * 3, .mode_count = 3};
        out->modes[i * 3] = (struct uurb_display_mode){1920, 1080, 60000, 1000};
        out->modes[i * 3 + 1] = (struct uurb_display_mode){1280, 720, 60000, 1000};
        out->modes[i * 3 + 2] = (struct uurb_display_mode){1280, 720, 144000, 1000};
        snprintf(out->monitors[i].gdi_name, sizeof(out->monitors[i].gdi_name), "\\\\.\\DISPLAY%u", i + 1);
    }
    if (exclusive) {
        out->monitors[0] = out->monitors[1]; out->monitors[0].index = 0;
        out->monitors[0].flags = UURB_DISPLAY_MONITOR_PRIMARY | UURB_DISPLAY_MONITOR_VIRTUAL;
        out->monitors[0].x = 0;
        out->monitor_count = 1;
    }
    return UURB_DISPLAY_OK;
}
static DWORD WINAPI control_fixture(const char *path, DWORD code, const void *data, DWORD bytes)
{
    (void)path;
    if (code != UURB_VIRTUAL_TOPOLOGY_CONTROL || bytes > sizeof(topology_packet)) return UURB_DISPLAY_INVALID;
    memcpy(topology_packet, data, bytes); topology_bytes = bytes;
    return UURB_DISPLAY_OK;
}
static DWORD WINAPI change_fixture(const char *path, const struct uurb_display_request *request,
                                   struct uurb_display_reply *reply)
{
    (void)path;
    ++changed; sent = *request;
    *reply = (struct uurb_display_reply){.version = 1, .serial = 8};
    return UURB_DISPLAY_OK;
}
int wmain(void)
{
    strcpy(native_display_endpoint, "/fixture/display.sock");
    native_display_topology_api = topology_fixture;
    native_display_change_api = change_fixture;
    native_display_reconfigure_enabled = TRUE;
    DEVMODEW mode = {.dmSize = sizeof(mode), .dmFields = DM_PELSWIDTH | DM_PELSHEIGHT,
                    .dmPelsWidth = 1280, .dmPelsHeight = 720};
    CHECK(native_multi_change_mode(L"\\\\.\\DISPLAY2", &mode, NULL, CDS_TEST, NULL) == DISP_CHANGE_SUCCESSFUL);
    CHECK(!changed);
    CHECK(native_multi_change_mode(L"\\\\.\\DISPLAY2", &mode, NULL,
          CDS_RESET | CDS_UPDATEREGISTRY | CDS_GLOBAL, NULL) == DISP_CHANGE_SUCCESSFUL);
    CHECK(changed == 1 && sent.width == 1280 && sent.height == 720 && sent.serial == 7);
    CHECK(sent.operation == UURB_DISPLAY_APPLY_GLOBAL_DEFAULT);
    CHECK(sent.reserved == (UURB_DISPLAY_OUTPUT_SELECTED | (1u << UURB_DISPLAY_OUTPUT_SHIFT) | UURB_DISPLAY_FORCE_RESET));
    CHECK(native_multi_change_mode(L"\\\\.\\DISPLAY1", &mode, NULL, CDS_UPDATEREGISTRY, NULL) == DISP_CHANGE_SUCCESSFUL);
    CHECK(changed == 2 && sent.reserved == UURB_DISPLAY_OUTPUT_SELECTED);
    CHECK(native_multi_change_mode(L"\\\\.\\DISPLAY2", NULL, NULL, 0, NULL) == DISP_CHANGE_SUCCESSFUL && changed == 2);
    mode.dmFields |= DM_POSITION;
    CHECK(native_multi_change_mode(L"\\\\.\\DISPLAY2", &mode, NULL, 0, NULL) == DISP_CHANGE_BADMODE);
    mode.dmFields &= ~DM_POSITION;
    native_display_reconfigure_enabled = FALSE;
    CHECK(native_multi_change_mode(L"\\\\.\\DISPLAY2", &mode, NULL, 0, NULL) == DISP_CHANGE_NOTUPDATED);
    CHECK(changed == 2);
    native_display_reconfigure_enabled = TRUE;
    struct uurb_native_dpi_set dpi = {.header = {.type = (DISPLAYCONFIG_DEVICE_INFO_TYPE)-4,
        .size = sizeof(dpi), .adapterId = native_multi_luid(), .id = 2}, .scale_rel = 0};
    LONG dpi_result;
    CHECK(native_multi_device_info(NULL, &dpi.header, TRUE, &dpi_result) == 1 && dpi_result == ERROR_SUCCESS);
    CHECK(changed == 2); /* Same scale is idempotent, including Super Screen initialization. */
    dpi.scale_rel = 1;
    CHECK(native_multi_device_info(NULL, &dpi.header, TRUE, &dpi_result) == 1 && dpi_result == ERROR_BAD_CONFIGURATION);
    CHECK(changed == 2); /* Never treat a sparse scale mask as a continuous range. */
    dpi.scale_rel = 4;
    CHECK(native_multi_device_info(NULL, &dpi.header, TRUE, &dpi_result) == 1 && dpi_result == ERROR_SUCCESS);
    CHECK(changed == 3 && sent.scale_milli == 2000 && sent.width == 1920 && sent.height == 1080);
    CHECK(sent.reserved == (UURB_DISPLAY_OUTPUT_SELECTED | (1u << UURB_DISPLAY_OUTPUT_SHIFT)));
    mode.dmFields |= DM_DISPLAYFREQUENCY;
    mode.dmDisplayFrequency = 144;
    CHECK(native_multi_change_mode(L"\\\\.\\DISPLAY2", &mode, NULL, CDS_TEST, NULL) == DISP_CHANGE_SUCCESSFUL);
    CHECK(changed == 3);
    CHECK(native_multi_change_mode(L"\\\\.\\DISPLAY2", &mode, NULL, 0, NULL) == DISP_CHANGE_SUCCESSFUL);
    CHECK(changed == 4 && sent.refresh_millihz == 144000);
    mode.dmDisplayFrequency = 145;
    CHECK(native_multi_change_mode(L"\\\\.\\DISPLAY2", &mode, NULL, 0, NULL) == DISP_CHANGE_BADMODE);
    CHECK(changed == 4);
    native_virtual_control_api = control_fixture;
    UINT32 path_count = 2, mode_count = 4;
    DISPLAYCONFIG_PATH_INFO paths[2]; DISPLAYCONFIG_MODE_INFO modes[4]; LONG result;
    CHECK(native_multi_query_config(QDC_ONLY_ACTIVE_PATHS, &path_count, paths, &mode_count, modes, NULL, &result) == 1);
    CHECK(native_virtual_set_topology(1, paths + 1, mode_count, modes,
        SDC_USE_SUPPLIED_DISPLAY_CONFIG | SDC_APPLY | SDC_SAVE_TO_DATABASE |
        SDC_ALLOW_CHANGES | SDC_FORCE_MODE_ENUMERATION) == ERROR_SUCCESS);
    CHECK(topology_bytes == 36 && topology_packet[0] == 7 && topology_packet[2] == 1);
    CHECK(topology_packet[3] == 1 && topology_packet[4] == 1920 && topology_packet[5] == 1080);
    exclusive = TRUE; native_multi_time = 0;
    struct uurb_display_topology only;
    CHECK(native_multi_active(&only) && only.monitor_count == 1);
    DEVMODEW current = {.dmSize = sizeof(current)};
    CHECK(native_multi_enum_mode(L"\\\\.\\DISPLAY2", ENUM_CURRENT_SETTINGS, &current, 0) == 1);
    CHECK(native_multi_enum_mode(L"\\\\.\\DISPLAY1", ENUM_CURRENT_SETTINGS, &current, 0) == -1);
    path_count = 2; mode_count = 4;
    CHECK(native_multi_query_config(QDC_ONLY_ACTIVE_PATHS, &path_count, paths, &mode_count, modes, NULL, &result) == 1);
    CHECK(path_count == 1 && paths[0].sourceInfo.id == 2 && paths[0].targetInfo.id == UURB_MULTI_TARGET_BASE + 1);
    puts("MULTI Win32 selected-output and UU reset flags passed");
    return 0;
}
