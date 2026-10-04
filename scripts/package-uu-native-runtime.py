#!/usr/bin/python3
"""Content-addressed bridge bundles, independent of the installed UU release.

Packages only our bridge components. Never patches UU, copies accounts, changes
an active release, installs services, or runs the legacy RDP installer.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import tempfile

ROOT = Path(__file__).resolve().parents[1]
INPUTS = {
    'app/bin/dxgi.dll': 'build/dxvk-capture/stage/dxgi.dll',
    'app/bin/d3d11.dll': 'build/dxvk-capture/stage/d3d11.dll',
    'app/bin/uurb-dxgi-capture-loader.dll': 'build/native-presenter/uurb-dxgi-capture-loader.dll',
    'app/bin/uurb-dxgi-capture.dll.so': 'build/native-presenter/uurb-dxgi-capture.dll.so',
    'system32/nvEncodeAPI64.dll': 'build/native-presenter/uurb-nvenc-encode-loader.dll',
    'system32/uurb-nvenc-encode.dll.so': 'build/native-presenter/uurb-nvenc-encode.dll.so',
    'dxvk.conf': 'config/dxvk-native.conf',
    'licenses/DXVK_LICENSE': 'build/dxvk-capture/stage/DXVK_LICENSE',
}
CONTRACT = {
    'schema_version': 1,
    'capture_boundary': 'IDXGIOutput1/5 + IDXGIOutputDuplication',
    'encoding_boundary': 'NVENC function table',
    'gpu_message_version': 2,
    'gpu_message_bytes': 72,
    'capture_configuration': ['UURB_DXGI_CAPTURE_SOCKET', 'UURB_DXGI_CAPTURE_OUTPUT'],
    'legacy_fd_mode': 'one_shot_only',
    'uu_release_policy': 'validate_actual_interfaces_and_negotiation_not_version_allowlist',
    'private_uu_offsets_required_for_capture_or_encode': False,
    'live_uu_acceptance_required': True,
    'accepted_encoder_configuration': 'reviewed_reference_only_until_live_negotiation_adapter_is_implemented',
    'unsupported_configuration': 'explicit_error_no_silent_cpu_or_rdp_fallback',
    'production_ready': False,
}
INPUT_RUNTIME = {
    'runtime/bootstrap.exe': 'build/native-presenter/uu-native-bootstrap.exe',
    'runtime/winlogon.exe': 'build/native-presenter/uu-native-winlogon.exe',
    'runtime/uu-native-input': 'build/native-presenter/uu-native-input',
    'runtime/native-input-broker.exe': 'build/native-presenter/uu-native-input-broker.exe',
    'runtime/native-input-bridge.dll': 'build/native-presenter/uu-native-input-bridge.dll',
    'runtime/native-input-injector.exe': 'build/native-presenter/uu-native-input-injector.exe',
}
INPUT_CONTRACT = dict(CONTRACT, schema_version=2,
    input_boundary='USER32 SendInput import + native uinput; one output only',
    input_wire_version=3, input_no_replay=True, input_no_rdp_fallback=True,
    input_lifetime='same_owned_prefix_as_capture; privileged_open_then_drop_uid')
REFERENCE_CONTRACT, REFERENCE_INPUT_CONTRACT = CONTRACT, INPUT_CONTRACT
RATE_CONTRACT_FIELDS = dict(
    accepted_encoder_configuration='bounded_standard_p1_ull_8bit_cbr_vbr_rates_v1',
    dynamic_encoder_parameters=['averageBitRate', 'maxBitRate', 'vbvBufferSize', 'vbvInitialDelay',
                                'frameRateNum', 'frameRateDen', 'resetEncoder', 'forceIDR'],
    unsupported_encoder_modes=['B_frames', 'lookahead', 'async_encoding', 'dynamic_resolution', 'HDR', '10bit'])
CONTRACT = dict(REFERENCE_CONTRACT, schema_version=3, **RATE_CONTRACT_FIELDS)
INPUT_CONTRACT = dict(REFERENCE_INPUT_CONTRACT, schema_version=4, **RATE_CONTRACT_FIELDS)
RATE_INPUT_CONTRACT = INPUT_CONTRACT
INPUT_CONTRACT = dict(RATE_INPUT_CONTRACT, schema_version=5,
    cursor_boundary='Portal metadata -> bounded private snapshot -> USER32 GetCursorInfo/GetCursorPos',
    cursor_snapshot_version=1, cursor_snapshot_bytes=64 + 384 * 384 * 4,
    cursor_video_mode='metadata_opt_in_no_embedded_pointer', cursor_max_cached_shapes=128,
    cursor_live_uu_acceptance_required=True)
CURSOR_INPUT_CONTRACT = INPUT_CONTRACT
INPUT_CONTRACT = dict(CURSOR_INPUT_CONTRACT, schema_version=6,
    text_boundary='pure KEYEVENTF_UNICODE commits -> private same-UID daemon -> authorized public Portal clipboard paste',
    text_wire_version=1, text_max_utf16_units=2048, text_mixed_revision_batches='not_yet_supported',
    text_no_replay=True, text_no_payload_logging=True, text_live_uu_acceptance_required=True)
PURE_TEXT_INPUT_CONTRACT = INPUT_CONTRACT
INPUT_CONTRACT = dict(PURE_TEXT_INPUT_CONTRACT, schema_version=7,
    text_boundary='KEYEVENTF_UNICODE commits and leading Backspace revisions -> same-UID daemon -> authorized public Portal clipboard paste',
    text_wire_version=2, text_response_version=1,
    text_mixed_revision_batches='leading_backspace_then_nonempty_unicode_only',
    text_revision_guard='AT-SPI witnessed own suffix; same focused editable object and caret; no existing selection',
    text_revision_offset_units='Unicode_codepoints_not_graphemes_or_UTF16_units',
    text_revision_max_delete=2048, text_revision_credit_seconds=2,
    text_revision_unsupported='unwitnessed_or_password_fields; deletion_only; interleaved_edits')
REVISION_INPUT_CONTRACT = INPUT_CONTRACT
INPUT_CONTRACT = dict(REVISION_INPUT_CONTRACT, schema_version=8,
    input_lifetime='bounded_trial_or_explicit_continuous_supervised_service',
    input_device_access='active_session_uaccess_as_user_or_privileged_open_then_drop_uid')
SERVICE_INPUT_CONTRACT = INPUT_CONTRACT
LEGACY_INPUT_RUNTIME = INPUT_RUNTIME
INPUT_RUNTIME = dict(LEGACY_INPUT_RUNTIME, **{
    'runtime/uurb-native-display-loader.dll': 'build/native-presenter/uurb-native-display-loader.dll',
    'runtime/uurb-native-display.dll.so': 'build/native-presenter/uurb-native-display.dll.so',
})
INPUT_CONTRACT = dict(SERVICE_INPUT_CONTRACT, schema_version=9,
    display_boundary='USER32 EnumDisplaySettingsW/ExW and bounded ChangeDisplaySettingsExW -> private native guardian -> Mutter',
    display_native_abi_version=1, display_changes='temporary_with_30_second_confirmation_deadline',
    display_unsupported=['persistent_registry_changes', 'multi_output', 'SetDisplayConfig_changes', 'DPI_device_info_changes'],
    display_live_uu_acceptance_required=True)
DISPLAY_INPUT_CONTRACT = INPUT_CONTRACT
INPUT_CONTRACT = dict(DISPLAY_INPUT_CONTRACT, schema_version=10,
    cursor_video_mode='metadata_or_compositor_embedded_with_separate_USER32_sprite_hidden',
    cursor_embedded_policy_field='snapshot_reserved0_equals_1; no_GetCursorPos_hook',
    input_scroll='evdev_high_resolution_120_units_with_per_connection_legacy_accumulation')
EMBEDDED_INPUT_CONTRACT = INPUT_CONTRACT
INPUT_CONTRACT = dict(EMBEDDED_INPUT_CONTRACT, schema_version=11,
    display_changes='temporary_30_second_rollback_with_opt_in_owned_UU_fresh_GPU_ACK_confirmation',
    display_confirmation_is_remote_presentation_proof=False)
GPU_CONFIRM_INPUT_CONTRACT = INPUT_CONTRACT
INPUT_CONTRACT = dict(GPU_CONFIRM_INPUT_CONTRACT, schema_version=12,
    input_settings_version=1, input_settings_reload_ms=500,
    input_settings='private_JSON_relative_and_wheel_percent_25_to_400_with_fractional_remainders; absolute_unchanged')
SETTINGS_INPUT_CONTRACT = INPUT_CONTRACT
INPUT_CONTRACT = dict(SETTINGS_INPUT_CONTRACT, schema_version=13,
    input_logo_keys='left_right_meta_VK_or_scan_with_explicit_missing_or_packed_E0_extended_normalization',
    input_meta_diagnostics='bounded_logo_key_masks_only_no_text_or_other_key_identity')
META_INPUT_CONTRACT = INPUT_CONTRACT
INPUT_CONTRACT = dict(META_INPUT_CONTRACT, schema_version=14,
    display_default_api='UurbDisplayDefault; bounded_owned_UU_virtual_machine_default_not_Linux_system_default',
    display_default_changes='CDS_UPDATEREGISTRY_user_or_GLOBAL_with_optional_RESET; durable_candidate_before_apply; rollback_or_confirm_commit',
    display_unsupported=['CDS_NORESET_deferred_changes', 'multi_output', 'SetDisplayConfig_changes', 'DPI_device_info_changes'])
DEFAULT_DISPLAY_INPUT_CONTRACT = INPUT_CONTRACT
INPUT_CONTRACT = dict(DEFAULT_DISPLAY_INPUT_CONTRACT, schema_version=15,
    text_diagnostics='bounded_128_batch_event_classes_and_native_failure_reason; no_key_values_or_payloads')
TEXT_DIAGNOSTIC_INPUT_CONTRACT = INPUT_CONTRACT
INPUT_CONTRACT = dict(TEXT_DIAGNOSTIC_INPUT_CONTRACT, schema_version=16,
    text_empty_input_policy='ignore_exact_all_zero_MOUSEINPUT_only_within_Unicode_batches; real_mixed_mouse_still_rejected')
PADDED_TEXT_INPUT_CONTRACT = INPUT_CONTRACT
INPUT_CONTRACT = dict(PADDED_TEXT_INPUT_CONTRACT, schema_version=17,
    virtual_display_discovery='passthrough_SetupDiGetClassDevsA_W_and_EnumDeviceInterfaces; reviewed_IDD_GUID_only; bounded_numeric_trace',
    virtual_display_emulation=False)
VIRTUAL_TRACE_INPUT_CONTRACT = INPUT_CONTRACT
INPUT_CONTRACT = dict(VIRTUAL_TRACE_INPUT_CONTRACT, schema_version=18,
    display_geometry_queries='native_pixel_GetMonitorInfoA_W_GetSystemMetrics_EnumDisplayMonitors_null_HDC_QueryDisplayConfig_active_single_path',
    display_geometry_identity='preserve_Wine_handles_adapter_LUIDs_source_target_ids_and_mode_indices; no_new_monitor',
    display_virtual_timing='captured_pixel_view_zero_blanking; not_physical_EDID_timing',
    display_change_notify='broadcast_after_releasing_geometry_lock',
    display_session_preserving_reconfiguration=False)
GEOMETRY_INPUT_CONTRACT = INPUT_CONTRACT
INPUT_CONTRACT = dict(GEOMETRY_INPUT_CONTRACT, schema_version=19,
    display_session_preserving_reconfiguration='explicit_service_or_trial_opt_in_candidate; same_connector_layout_scale; GPU_only_retirement; not_remote_accepted')
RECREATE_INPUT_CONTRACT, PRE_TIMING_CONTRACT = INPUT_CONTRACT, CONTRACT
SOURCE_TIMING_FIELDS = dict(gpu_message_version=3, gpu_message_bytes=88,
    capture_source_timing='negotiated_fraction_and_variable_rate_ceiling; not_measured_fps; unknown_not_60Hz',
    capture_timing_change='DXGI_ACCESS_LOST_requires_new_generation')
INPUT_CONTRACT = dict(RECREATE_INPUT_CONTRACT, schema_version=20, **SOURCE_TIMING_FIELDS)
CONTRACT = dict(PRE_TIMING_CONTRACT, schema_version=21, **SOURCE_TIMING_FIELDS)
SOURCE_TIMING_INPUT_CONTRACT, SOURCE_TIMING_CONTRACT = INPUT_CONTRACT, CONTRACT
LIFECYCLE_FIELDS = dict(encoder_lifecycle_evidence='bounded_64_per_operation_init_reconfigure_first_frame_destroy; numeric_parameters_only')
INPUT_CONTRACT = dict(SOURCE_TIMING_INPUT_CONTRACT, schema_version=22, **LIFECYCLE_FIELDS)
CONTRACT = dict(SOURCE_TIMING_CONTRACT, schema_version=23, **LIFECYCLE_FIELDS)
LIFECYCLE_INPUT_CONTRACT, LIFECYCLE_CONTRACT = INPUT_CONTRACT, CONTRACT
PINNED_CAPTURE = {'capture/uu-pipewire-native-probe': 'build/native-presenter/uu-pipewire-native-probe'}
INPUT_CONTRACT = dict(LIFECYCLE_INPUT_CONTRACT, schema_version=24, capture_producer='versioned_bundle_executable')
CONTRACT = dict(LIFECYCLE_CONTRACT, schema_version=25, capture_producer='versioned_bundle_executable')
PINNED_INPUT_CONTRACT, PINNED_CONTRACT = INPUT_CONTRACT, CONTRACT
FAILURE_FIELDS = dict(native_failure_evidence='bounded_reconfigure_mismatch_and_numeric_fields; GPU_send_receive_stage_and_wait; no_payloads')
INPUT_CONTRACT = dict(PINNED_INPUT_CONTRACT, schema_version=26, **FAILURE_FIELDS)
CONTRACT = dict(PINNED_CONTRACT, schema_version=27, **FAILURE_FIELDS)
FAILURE_INPUT_CONTRACT, FAILURE_CONTRACT = INPUT_CONTRACT, CONTRACT
PRESET_RECONFIGURE_FIELDS = dict(encoder_preset_reconfigure='standard_P1_P7_HQ_LL_ULL_with_explicit_unchanged_codec_config_except_rates; driver_final_authority; initial_advertisement_remains_P1_ULL')
INPUT_CONTRACT = dict(FAILURE_INPUT_CONTRACT, schema_version=28, **PRESET_RECONFIGURE_FIELDS)
CONTRACT = dict(FAILURE_CONTRACT, schema_version=29, **PRESET_RECONFIGURE_FIELDS)
PRESET_INPUT_CONTRACT = INPUT_CONTRACT
PINNED_MAIN_SCRIPTS = {('scripts/' + name): ('scripts/' + name) for name in (
    'uu-native-service.py', 'uu-native-trial.py', 'uu-native-capture-broker.py',
    'probe-wayland-portal.py', 'native_runtime_state.py',
    'native_display_confirmation.py', 'package-uu-native-runtime.py')}
# The supervisor applies the default cover from inside its immutable bundle.
# Keep the artwork beside the pinned Python entrypoints so a source-only
# repack cannot accidentally point a new service at a missing development
# checkout asset.
PINNED_MAIN_SCRIPTS['assets/uuway-penguin.bmp'] = 'assets/uuway-penguin.bmp'
INPUT_CONTRACT = dict(PRESET_INPUT_CONTRACT, schema_version=30,
    main_runtime_sources='versioned_bundle_Python_entrypoint_supervisor_broker_portal_wrapper_and_imports',
    independent_service_sources='text_and_display_guardian_not_migrated',
    display_reset_policy='target_dimensions_first_ACK_and_generation_fence_with_bounded_natural_recovery')
SOURCE_PINNED_INPUT_CONTRACT = INPUT_CONTRACT
INPUT_CONTRACT = dict(SOURCE_PINNED_INPUT_CONTRACT, schema_version=31,
    text_backend_selection='private_text-backend.json_v1_explicit_portal_or_fcitx; no_automatic_fallback',
    native_ime_boundary='independent_same_UID_Fcitx_addon; focused_context_commit; clipboard_untouched',
    native_ime_revision_support='not_yet_implemented; reject_without_deletion')
NATIVE_IME_INPUT_CONTRACT = INPUT_CONTRACT
INPUT_CONTRACT = dict(NATIVE_IME_INPUT_CONTRACT, schema_version=32,
    cursor_composited_policy_field='reserved[0]=2; metadata_source; GPU_single_sprite; native_USER32_positions',
    cursor_composition='Vulkan_premultiplied_BGRA; bounded_shape_upload; clean_GPU_background_for_cursor_only_source_events',
    cursor_composited_timing='actual_PipeWire_PTS; prior_desktop_required; existing_GPU_consumer_ACK',
    capture_target_serial='optional_explicit_object_serial; no_fallback_to_reused_node_id')
COMPOSITED_INPUT_CONTRACT = INPUT_CONTRACT
INPUT_CONTRACT = dict(COMPOSITED_INPUT_CONTRACT, schema_version=33,
    native_ime_startup='explicit_fcitx_only; private_ime.sock_may_appear_later; native_client_reconnects_each_new_text_batch',
    native_ime_outage='no_text_queue_or_replay; no_clipboard_fallback; video_and_key_input_remain_available')
DEFERRED_IME_INPUT_CONTRACT = INPUT_CONTRACT
# These two exact schema34 contracts escaped with unchanged schema33 DLLs.
# Retain them for verification/recovery only; they prove no SetDisplayConfig
# capability and a source-only repack must remove their unsupported claim.
LEGACY_DISPLAY_SET_INPUT_CONTRACT = dict(DEFERRED_IME_INPUT_CONTRACT, schema_version=34,
    display_set_config='explicit_supplied_single_active_path_verify_or_temporary_apply_to_native_guardian',
    display_set_config_scope='single_active_primary_path; no_database_save; no_rotation_or_arbitrary_timing',
    display_set_config_concurrency='fresh_serial_geometry_rate_and_unique_mode_required_before_apply')
LEGACY_DISPLAY_SET_REVISED_INPUT_CONTRACT = dict(LEGACY_DISPLAY_SET_INPUT_CONTRACT,
    display_unsupported=['persistent_registry_changes', 'multi_output', 'DPI_device_info_changes',
                         'rotation_or_arbitrary_physical_timing'])
LEGACY_DISPLAY_SET_CONTRACTS = (LEGACY_DISPLAY_SET_INPUT_CONTRACT, LEGACY_DISPLAY_SET_REVISED_INPUT_CONTRACT)
INPUT_CONTRACT = dict(DEFERRED_IME_INPUT_CONTRACT, schema_version=35,
    display_set_config='explicit_supplied_single_active_path_verify_or_temporary_apply_to_native_guardian',
    display_set_config_scope='single_active_primary_path; no_database_save; no_rotation_or_arbitrary_timing',
    display_set_config_concurrency='fresh_guardian_serial; strict_refresh_without_ALLOW_CHANGES',
    display_set_config_binary_export='UurbInputBridgeDisplaySetVersion',
    display_unsupported=['CDS_NORESET_deferred_changes', 'CCD_database_save_or_topology_changes',
                         'multi_output', 'DPI_device_info_changes', 'rotation_or_arbitrary_physical_timing'])
INPUT_BRIDGE_COMPONENT = 'runtime/native-input-bridge.dll'
DISPLAY_SET_EXPORT = b'UurbInputBridgeDisplaySetVersion'
DISPLAY_DPI_V2_EXPORT = b'UurbInputBridgeDisplayDpiV2Version'
DISPLAY_QUERY_V2_EXPORT = b'UurbDisplayQueryV2'
# Schema 35 deliberately advertised DPI as unsupported.  Keep that exact
# contract accepted for rollback, and publish DPI support as a new immutable
# contract only when all three display-side runtime artifacts are replaced
# together.  This prevents a mixed old/new bundle from being promoted.
DPI_INPUT_CONTRACT = dict(INPUT_CONTRACT, schema_version=36,
    display_unsupported=['CDS_NORESET_deferred_changes', 'CCD_database_save_or_topology_changes',
                         'multi_output', 'rotation_or_arbitrary_physical_timing'],
    dpi_boundary='DISPLAYCONFIG_DEVICE_INFO_GET_DPI_SCALE_and_SET_DPI_SCALE_to_native_guardian',
    dpi_scale_values=[100, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450, 500],
    dpi_live_uu_acceptance_required=True)
DPI_V2_INPUT_CONTRACT = dict(DPI_INPUT_CONTRACT, schema_version=37,
    display_native_abi_version=2, display_snapshot_bytes=4140,
    display_snapshot_scale_mask='discrete_scale_mask_at_offset_4136',
    dpi_boundary='DISPLAYCONFIG_DEVICE_INFO_GET_DPI_SCALE_and_SET_DPI_SCALE_to_native_guardian_v2',
    dpi_discrete_scale_mask=True, dpi_sparse_get_unsupported=True,
    display_binary_exports=['UurbInputBridgeDisplayDpiV2Version', 'UurbDisplayQueryV2'])
DISPLAY_RUNTIME_COMPONENTS = (
    'runtime/native-input-bridge.dll',
    'runtime/uurb-native-display-loader.dll',
    'runtime/uurb-native-display.dll.so',
)
DISPLAY_RUNTIME_SOURCE_NAMES = {
    'runtime/native-input-bridge.dll': 'uu-native-input-bridge.dll',
    'runtime/uurb-native-display-loader.dll': 'uurb-native-display-loader.dll',
    'runtime/uurb-native-display.dll.so': 'uurb-native-display.dll.so',
}


def digest(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 128 * 1024 * 1024:
        raise ValueError('Expected a bounded regular component: ' + path.name)
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return dict(bytes=info.st_size, sha256=value.hexdigest())


def release_id(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def verify_pe_export(path, export=DISPLAY_SET_EXPORT, label='Input bridge'):
    """Check the real x64 PE export table, without loading or executing the DLL.

    A marker is a packaging capability gate, not runtime/UU acceptance. In
    particular a name in strings/imports or a forwarded export is not enough.
    """
    digest(path)
    data = path.read_bytes()

    def fail():
        raise ValueError(label + ' requires an x64 PE DLL with a native ' + export.decode() + ' export')

    def unpack(fmt, offset):
        size = struct.calcsize(fmt)
        if offset < 0 or offset + size > len(data):
            fail()
        return struct.unpack_from(fmt, data, offset)

    if len(data) < 64 or data[:2] != b'MZ':
        fail()
    pe, = unpack('<I', 60)
    if pe < 64 or data[pe:pe + 4] != b'PE\0\0':
        fail()
    machine, count, _, _, _, optional_size, characteristics = unpack('<HHIIIHH', pe + 4)
    optional = pe + 24
    if machine != 0x8664 or not 1 <= count <= 96 or not characteristics & 0x2000 or optional_size < 120:
        fail()
    magic, = unpack('<H', optional)
    directory_count, = unpack('<I', optional + 108)
    if magic != 0x20b or not 1 <= directory_count <= 16 or optional_size < 112 + directory_count * 8:
        fail()
    headers_size, = unpack('<I', optional + 60)
    section_table = optional + optional_size
    if not section_table + count * 40 <= headers_size <= len(data):
        fail()
    sections = []
    for index in range(count):
        section = section_table + index * 40
        virtual_size, virtual_address, raw_size, raw_offset = unpack('<IIII', section + 8)
        flags, = unpack('<I', section + 36)
        if raw_size and (raw_offset < headers_size or raw_offset + raw_size > len(data)):
            fail()
        sections.append((virtual_address, max(virtual_size, raw_size), raw_offset, raw_size, flags))

    def mapped(rva, size, executable=False):
        if rva <= 0 or size <= 0 or rva + size > 0x100000000:
            fail()
        matches = [(start, raw, raw_size, flags) for start, extent, raw, raw_size, flags in sections
                   if start <= rva and rva + size <= start + extent]
        if len(matches) != 1:
            fail()
        start, raw, raw_size, flags = matches[0]
        if rva - start + size > raw_size or executable and not flags & 0x20000000:
            fail()
        return raw + rva - start

    export_rva, export_size = unpack('<II', optional + 112)
    if export_size < 40 or export_size > 1024 * 1024:
        fail()
    directory = mapped(export_rva, export_size)
    functions, names, function_rva, name_rva, ordinal_rva = unpack('<IIIII', directory + 20)
    if not 1 <= functions <= 65536 or not 1 <= names <= 65536:
        fail()
    function_table = mapped(function_rva, functions * 4)
    name_table = mapped(name_rva, names * 4)
    ordinal_table = mapped(ordinal_rva, names * 2)
    found = False
    for index in range(names):
        name_address, = unpack('<I', name_table + index * 4)
        # Read at most the required name plus its terminator. Other valid
        # shorter exports need not have this much raw data behind their name.
        position = mapped(name_address, 1)
        if data[position:position + len(export) + 1] != export + b'\0':
            continue
        mapped(name_address, len(export) + 1)
        ordinal, = unpack('<H', ordinal_table + index * 2)
        if ordinal >= functions or found:
            fail()
        function_address, = unpack('<I', function_table + ordinal * 4)
        if export_rva <= function_address < export_rva + export_size:
            fail()
        mapped(function_address, 1, executable=True)
        found = True
    if not found:
        fail()
    return True


def verify_display_set_bridge(path):
    """Compatibility wrapper for the schema35/36 display-set marker."""
    return verify_pe_export(path)


def verify_elf_export(path, export=DISPLAY_QUERY_V2_EXPORT, label='Display Winelib'):
    """Require an x86-64 ELF DYN object's defined global query export.

    This parses only ELF headers and the dynamic symbol table; it never loads
    or executes the shared object and therefore cannot validate runtime ABI.
    """
    digest(path)
    data = path.read_bytes()

    def fail():
        raise ValueError(label + ' requires an x86-64 ELF export ' + export.decode())

    def unpack(fmt, offset):
        size = struct.calcsize(fmt)
        if offset < 0 or offset + size > len(data):
            fail()
        return struct.unpack_from(fmt, data, offset)

    if len(data) < 64 or data[:4] != b'\x7fELF' or data[4] != 2 or data[5] != 1:
        fail()
    ident = data[:16]
    e_type, machine = unpack('<HH', 16)
    if e_type != 3 or machine != 0x3e:
        fail()
    _, _, _, _, _, e_shoff, _, _, _, _, e_shentsize, e_shnum, e_shstrndx = unpack('<HHIQQQIHHHHHH', 16)
    if e_shentsize < 64 or not 1 <= e_shnum <= 4096 or e_shstrndx >= e_shnum:
        fail()
    if e_shoff + e_shentsize * e_shnum > len(data):
        fail()
    sections = []
    for index in range(e_shnum):
        offset = e_shoff + index * e_shentsize
        name, section_type, flags, address, file_offset, size, link, info, align, entsize = unpack('<IIQQQQIIQQ', offset)
        if file_offset + size > len(data):
            fail()
        sections.append((name, section_type, file_offset, size, link, entsize))
    # Only .dynsym is an exported ABI surface; accepting symtab-only names
    # would allow a stripped/hidden implementation to claim this capability.
    for _, section_type, file_offset, size, link, entsize in sections:
        if section_type != 11:  # SHT_DYNSYM
            continue
        if entsize < 24 or size % entsize or link >= len(sections):
            fail()
        _, string_type, string_offset, string_size, _, _ = sections[link]
        if string_type != 3:
            fail()
        strings = data[string_offset:string_offset + string_size]
        for position in range(file_offset, file_offset + size, entsize):
            st_name, info_byte, _, st_shndx, _, _ = unpack('<IBBHQQ', position)
            if st_name >= len(strings):
                fail()
            end = strings.find(b'\0', st_name)
            if end < 0:
                fail()
            if (strings[st_name:end] == export and st_shndx != 0 and
                    (info_byte & 0x0f) == 2 and (info_byte >> 4) in (1, 2)):
                return True
    fail()


def has_export(checker, path, export):
    """Return whether an artifact has an export, treating old ABI artifacts
    as a negative capability.  Structural errors are still rejected by the
    selected contract's final verifier below.
    """
    try:
        checker(path, export)
        return True
    except ValueError:
        return False


def display_runtime_contract(sources):
    """Select v1/v2 only when all replacement artifacts agree."""
    bridge = sources[INPUT_BRIDGE_COMPONENT]
    loader = sources['runtime/uurb-native-display-loader.dll']
    winelib = sources['runtime/uurb-native-display.dll.so']
    bridge_v2 = has_export(verify_pe_export, bridge, DISPLAY_DPI_V2_EXPORT)
    loader_v2 = has_export(verify_pe_export, loader, DISPLAY_QUERY_V2_EXPORT)
    winelib_v2 = has_export(verify_elf_export, winelib, DISPLAY_QUERY_V2_EXPORT)
    if bridge_v2:
        if not (loader_v2 and winelib_v2):
            raise ValueError('DPI v2 display replacement requires matching UurbDisplayQueryV2 exports in loader and Winelib')
        return DPI_V2_INPUT_CONTRACT
    if loader_v2 or winelib_v2:
        raise ValueError('Mixed DPI v1/v2 display replacement is not allowed')
    verify_display_set_bridge(bridge)
    return DPI_INPUT_CONTRACT


def verify(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Expected a real bundle directory')
    manifest_file = directory / 'manifest.json'
    if manifest_file.is_symlink() or manifest_file.stat().st_size > 65536:
        raise ValueError('Invalid bundle manifest')
    manifest = json.loads(manifest_file.read_text())
    contract = manifest.get('contract')
    if contract not in (CONTRACT, INPUT_CONTRACT, DPI_INPUT_CONTRACT, DPI_V2_INPUT_CONTRACT, DEFERRED_IME_INPUT_CONTRACT, COMPOSITED_INPUT_CONTRACT, NATIVE_IME_INPUT_CONTRACT, SOURCE_PINNED_INPUT_CONTRACT, PRESET_INPUT_CONTRACT, FAILURE_CONTRACT, FAILURE_INPUT_CONTRACT, PINNED_CONTRACT, PINNED_INPUT_CONTRACT, LIFECYCLE_CONTRACT, LIFECYCLE_INPUT_CONTRACT, SOURCE_TIMING_CONTRACT, SOURCE_TIMING_INPUT_CONTRACT, PRE_TIMING_CONTRACT, RECREATE_INPUT_CONTRACT, GEOMETRY_INPUT_CONTRACT, VIRTUAL_TRACE_INPUT_CONTRACT, PADDED_TEXT_INPUT_CONTRACT, TEXT_DIAGNOSTIC_INPUT_CONTRACT, DEFAULT_DISPLAY_INPUT_CONTRACT, META_INPUT_CONTRACT, SETTINGS_INPUT_CONTRACT, GPU_CONFIRM_INPUT_CONTRACT, EMBEDDED_INPUT_CONTRACT, DISPLAY_INPUT_CONTRACT, SERVICE_INPUT_CONTRACT, REVISION_INPUT_CONTRACT, PURE_TEXT_INPUT_CONTRACT, CURSOR_INPUT_CONTRACT, RATE_INPUT_CONTRACT, REFERENCE_CONTRACT, REFERENCE_INPUT_CONTRACT, *LEGACY_DISPLAY_SET_CONTRACTS) or set(manifest) != {'contract', 'files'}:
        raise ValueError('Unreviewed bridge contract')
    # Feature fields are inspected only AFTER exact known-contract matching.
    # This keeps old releases usable without broadening accepted manifests.
    has_input = 'input_wire_version' in contract
    runtime = INPUT_RUNTIME if 'display_native_abi_version' in contract else LEGACY_INPUT_RUNTIME
    inputs = dict(INPUTS, **runtime) if has_input else INPUTS
    if 'capture_producer' in contract:
        inputs = dict(inputs, **PINNED_CAPTURE)
    if 'main_runtime_sources' in contract:
        inputs = dict(inputs, **PINNED_MAIN_SCRIPTS)
        # Releases created before the bundled default cover was introduced
        # remain verifiable and reusable; new releases always carry it.
        if 'assets/uuway-penguin.bmp' not in manifest['files']:
            inputs.pop('assets/uuway-penguin.bmp', None)
    if set(manifest['files']) != set(inputs):
        raise ValueError('Missing or unexpected bridge component')
    for name, expected in manifest['files'].items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Invalid bundle path')
        component = directory / name
        if any(parent.is_symlink() for parent in component.parents if parent != directory.parent):
            raise ValueError('Bundle paths must not cross symlinks')
        if digest(component) != expected:
            raise ValueError('Changed bridge component: ' + name)
    has_display_set = contract in (INPUT_CONTRACT, DPI_INPUT_CONTRACT, DPI_V2_INPUT_CONTRACT)
    if has_display_set:
        if contract == DPI_V2_INPUT_CONTRACT:
            verify_pe_export(directory / INPUT_BRIDGE_COMPONENT, DISPLAY_DPI_V2_EXPORT, 'Input bridge')
            verify_pe_export(directory / 'runtime/uurb-native-display-loader.dll', DISPLAY_QUERY_V2_EXPORT,
                             'Display loader')
            verify_elf_export(directory / 'runtime/uurb-native-display.dll.so', DISPLAY_QUERY_V2_EXPORT)
        else:
            verify_display_set_bridge(directory / INPUT_BRIDGE_COMPONENT)
    return dict(release_id=release_id(manifest), components_verified=len(inputs),
                native_input_included=has_input,
                negotiated_encoder_rates='dynamic_encoder_parameters' in contract,
                native_cursor_included='cursor_snapshot_version' in contract,
                native_text_included='text_wire_version' in contract,
                native_text_revisions_included=contract.get('text_wire_version') == 2,
                native_service_lifetime_included='input_device_access' in contract,
                native_display_included='display_native_abi_version' in contract,
                native_display_set_config_included=has_display_set,
                native_display_abi_version=contract.get('display_native_abi_version', 0),
                native_display_dpi_v2_included=contract == DPI_V2_INPUT_CONTRACT,
                legacy_display_set_config_claim_unverified=contract in LEGACY_DISPLAY_SET_CONTRACTS,
                native_embedded_cursor_included='cursor_embedded_policy_field' in contract,
                native_composited_cursor_included='cursor_composited_policy_field' in contract,
                native_ime_deferred_start_included='native_ime_startup' in contract,
                native_gpu_display_confirmation_included='display_confirmation_is_remote_presentation_proof' in contract,
                native_input_settings_included='input_settings_version' in contract,
                native_display_geometry_included='display_geometry_queries' in contract,
                native_source_timing_included=contract.get('gpu_message_version') == 3,
                native_capture_producer_included='capture_producer' in contract,
                native_main_scripts_included='main_runtime_sources' in contract,
                uu_session_tested=False, production_ready=False)


CAPTURE_BACKEND_COMPONENT = 'app/bin/uurb-dxgi-capture.dll.so'
# Components a source-only repack may swap one by one: same ABI and contract.
# The bootstrap is included here because display reconfiguration policy is
# carried through Wine registry values during process startup.  Reusing an
# older bootstrap with a newer input bridge silently leaves the bridge in
# read-only mode, so the two artifacts must be replaceable in one atomic
# content-addressed repack.
REPLACEABLE_COMPONENTS = (
    CAPTURE_BACKEND_COMPONENT,
    'runtime/uu-native-input',
    'runtime/bootstrap.exe',
)


def package(parent, with_input=False, reuse_runtime_from=None, replace_input_bridge=None,
            replace_display_runtime=None, replace_capture_backend=None, replace_components=None):
    replace_components = dict(replace_components or {})
    if replace_capture_backend is not None:
        replace_components[CAPTURE_BACKEND_COMPONENT] = replace_capture_backend
    if replace_input_bridge is not None and (not with_input or reuse_runtime_from is None):
        raise ValueError('Input bridge replacement requires --with-input and --reuse-runtime-from')
    if replace_display_runtime is not None and (not with_input or reuse_runtime_from is None):
        raise ValueError('Display runtime replacement requires --with-input and --reuse-runtime-from')
    for name, source in list(replace_components.items()):
        source = Path(source)
        if name not in REPLACEABLE_COMPONENTS:
            raise ValueError('Not a replaceable component: ' + name)
        if reuse_runtime_from is None:
            raise ValueError('Component replacement requires --reuse-runtime-from')
        if source.is_symlink() or not source.is_file():
            raise ValueError('Component replacement must be a regular file: ' + name)
        replace_components[name] = source
    if replace_input_bridge is not None and replace_display_runtime is not None:
        raise ValueError('Input bridge and display runtime replacements are mutually exclusive')
    if replace_display_runtime is not None:
        replace_display_runtime = Path(replace_display_runtime)
        if replace_display_runtime.is_symlink() or not replace_display_runtime.is_dir():
            raise ValueError('Display runtime replacement must be a real directory')
        for name in DISPLAY_RUNTIME_COMPONENTS:
            candidate = replace_display_runtime / DISPLAY_RUNTIME_SOURCE_NAMES[name]
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError('Display runtime replacement is missing: ' + candidate.name)
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError('Expected a real bundle parent directory')
    # Copy first, then hash the exact bytes being promoted. Never mix files
    # directly into an active installation or delete/overwrite an old release.
    with tempfile.TemporaryDirectory(prefix='.uurb-stage-', dir=parent) as temporary:
        stage = Path(temporary) / 'bundle'
        stage.mkdir(mode=0o700)
        inputs = dict(INPUTS, **INPUT_RUNTIME, **PINNED_CAPTURE, **PINNED_MAIN_SCRIPTS) if with_input else dict(INPUTS, **PINNED_CAPTURE)
        sources = {name: ROOT / source for name, source in inputs.items()}
        contract = INPUT_CONTRACT if with_input else CONTRACT
        if reuse_runtime_from is not None:
            verify(reuse_runtime_from)
            base_contract = json.loads((reuse_runtime_from / 'manifest.json').read_text())['contract']
            if not with_input or base_contract not in (SOURCE_PINNED_INPUT_CONTRACT, NATIVE_IME_INPUT_CONTRACT, COMPOSITED_INPUT_CONTRACT, DEFERRED_IME_INPUT_CONTRACT, INPUT_CONTRACT, DPI_INPUT_CONTRACT, DPI_V2_INPUT_CONTRACT, *LEGACY_DISPLAY_SET_CONTRACTS):
                raise ValueError('Source-only update requires a compatible source-pinned runtime')
            sources.update({name: reuse_runtime_from / name for name in inputs if name not in PINNED_MAIN_SCRIPTS})
            # Only reviewed Python-only capabilities may advance here: 30->31
            # and 32->33. Schema34's native claim was invalid; repair it to33.
            if base_contract == DPI_V2_INPUT_CONTRACT:
                contract = DPI_V2_INPUT_CONTRACT
            elif base_contract == DPI_INPUT_CONTRACT:
                contract = DPI_INPUT_CONTRACT
            elif base_contract == INPUT_CONTRACT:
                contract = INPUT_CONTRACT
            elif base_contract in (COMPOSITED_INPUT_CONTRACT, DEFERRED_IME_INPUT_CONTRACT, *LEGACY_DISPLAY_SET_CONTRACTS):
                contract = DEFERRED_IME_INPUT_CONTRACT
            else:
                contract = NATIVE_IME_INPUT_CONTRACT
            if replace_input_bridge is not None:
                if base_contract not in (DEFERRED_IME_INPUT_CONTRACT, INPUT_CONTRACT,
                                         DPI_INPUT_CONTRACT, DPI_V2_INPUT_CONTRACT):
                    raise ValueError('Input bridge replacement requires a verified schema33/35/36/37 runtime')
                sources[INPUT_BRIDGE_COMPONENT] = replace_input_bridge
                # Preserve the display ABI of the reused runtime.  A v2
                # bridge cannot be paired with a v1 loader, and downgrading a
                # v2 bundle would silently remove the complete mode list.
                if base_contract == DPI_V2_INPUT_CONTRACT:
                    verify_pe_export(sources[INPUT_BRIDGE_COMPONENT], DISPLAY_DPI_V2_EXPORT,
                                     'Input bridge')
                    contract = DPI_V2_INPUT_CONTRACT
                else:
                    if has_export(verify_pe_export, sources[INPUT_BRIDGE_COMPONENT], DISPLAY_DPI_V2_EXPORT):
                        raise ValueError('Input bridge replacement requires matching DPI v2 display runtime')
                    verify_display_set_bridge(sources[INPUT_BRIDGE_COMPONENT])
                    contract = DPI_INPUT_CONTRACT if base_contract == DPI_INPUT_CONTRACT else INPUT_CONTRACT
            if replace_display_runtime is not None:
                if base_contract not in (INPUT_CONTRACT, DPI_INPUT_CONTRACT, DPI_V2_INPUT_CONTRACT):
                    raise ValueError('Display runtime replacement requires a verified schema35/36/37 runtime')
                for name in DISPLAY_RUNTIME_COMPONENTS:
                    sources[name] = replace_display_runtime / DISPLAY_RUNTIME_SOURCE_NAMES[name]
                contract = display_runtime_contract(sources)
            for name, source in replace_components.items():
                if name not in sources:
                    raise ValueError('Component is not part of this bundle: ' + name)
                sources[name] = source
        if with_input and contract in (INPUT_CONTRACT, DPI_INPUT_CONTRACT, DPI_V2_INPUT_CONTRACT):
            if contract == DPI_V2_INPUT_CONTRACT:
                verify_pe_export(sources[INPUT_BRIDGE_COMPONENT], DISPLAY_DPI_V2_EXPORT, 'Input bridge')
                verify_pe_export(sources['runtime/uurb-native-display-loader.dll'], DISPLAY_QUERY_V2_EXPORT,
                                 'Display loader')
                verify_elf_export(sources['runtime/uurb-native-display.dll.so'], DISPLAY_QUERY_V2_EXPORT)
            else:
                verify_display_set_bridge(sources[INPUT_BRIDGE_COMPONENT])
        before = {name: digest(source) for name, source in sources.items()}
        for name, source in inputs.items():
            destination = stage / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest(sources[name])
            shutil.copyfile(sources[name], destination)
            destination.chmod(0o700 if name == 'runtime/uu-native-input' or name in PINNED_CAPTURE else 0o600)
        manifest = dict(contract=contract,
                        files={name: digest(stage / name) for name in inputs})
        after = {name: digest(source) for name, source in sources.items()}
        if manifest['files'] != before or before != after:
            raise ValueError('Bridge artifacts changed while packaging; retry after the build completes')
        with (stage / 'manifest.json').open('x') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(manifest, output, indent=2, sort_keys=True)
            output.write('\n')
            output.flush(); os.fsync(output.fileno())
        result = verify(stage)
        target = parent / result['release_id']
        if target.exists() or target.is_symlink():
            existing = verify(target)
            if existing != result:
                raise ValueError('Existing release does not match its content address')
        else:
            stage.rename(target)
        result['bundle'] = str(target.resolve())
        result['active_release_changed'] = False
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('package')
    create.add_argument('parent', type=Path)
    create.add_argument('--with-input', action='store_true')
    create.add_argument('--reuse-runtime-from', type=Path,
                        help='Copy unchanged native components from a verified compatible bundle, updating only main scripts')
    create.add_argument('--replace-input-bridge', type=Path,
                        help='With a verified schema33/35/36/37 runtime, replace only this explicit capability-checked x64 input DLL')
    create.add_argument('--replace-display-runtime', type=Path,
                        help='With a verified schema35/36 runtime, replace the input bridge and native display loader/.so together to publish DPI support')
    create.add_argument('--replace-capture-backend', type=Path,
                        help='Shorthand for --replace-component ' + CAPTURE_BACKEND_COMPONENT + '=PATH')
    create.add_argument('--replace-component', action='append', default=[], metavar='NAME=PATH',
                        help='With --reuse-runtime-from, replace one of: ' + ', '.join(REPLACEABLE_COMPONENTS))
    check = commands.add_parser('verify')
    check.add_argument('bundle', type=Path)
    args = parser.parse_args()
    print(json.dumps(package(args.parent, args.with_input, args.reuse_runtime_from,
                             args.replace_input_bridge, args.replace_display_runtime,
                             args.replace_capture_backend,
                             dict(item.split('=', 1) for item in args.replace_component))
                   if args.command == 'package' else verify(args.bundle), indent=2))
