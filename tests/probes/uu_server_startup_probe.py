#!/usr/bin/python3
"""Observe the official controlled-side process in an offline disposable prefix.
No account files, live-prefix changes, desktop capture or live UU connection.
Optional SCM registration exists only for the disposable prefix lifetime.
Raw application logs stay temporary and are never included in the report.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
BIN = (Path(os.environ.get('WINEPREFIX', Path.home() / '.local/share/wineprefixes/uu-remote')) / 'drive_c/Program Files/Netease/GameViewer/bin')
PINNED = {
    'GameViewerServer.exe': '5a6452f1b47e3f5568a9fa92b338c4f515da053b142a2628035c521708299ae0',
    'streamer.dll': 'ad16f5b95a0e6461312ccd5ea2df63ae4665bf78e1bfb2256d7bde2454c61e8e',
    'nrd_renderer_ipc.dll': 'd3960b8b14ec0020b89ccbc51f7414cdbfa67897aa5fa29b2014ed8e84e6aa1a',
    'StreamerCodecDetector.exe': 'ed79229f5b80320542e93f1875a824d9913888c58c3d61b545b0e26ed1e4f454',
}
RUNTIMES = ('msvcp140.dll', 'msvcp140_atomic_wait.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')
SERVICE_PINNED = {'GameViewerService.exe': '4abe8624e63e0123aea139e49e030184a6ac9a1b6782fee750b4ad0b5f07c56e'}
GUI_PINNED = {'GameViewer.exe': '1587925c43fe9f5841ea6292b64894dde3dd59bb01832f0f288f910b553c2590'}
GUI_ROOT_SHA = '16662ab03e9d72f44959466b747fa235d88407fe3ee45db663fc650d080f0173'
GUI_DEPENDENCIES = ('Qt5Core.dll', 'Qt5Gui.dll', 'Qt5Network.dll', 'Qt5Svg.dll',
                    'Qt5Widgets.dll', 'Qt5WinExtras.dll', 'WebView2Loader.dll', 'XInput9_1_0.dll',
                    'libssl-3-x64.dll', 'libcrypto-3-x64.dll')
IPC_EVENTS = ('ipc_connect result=requested', 'ipc_connection result=connected',
    'ipc_client_ui_ready result=dispatched', 'ipc_connection result=closed', 'ipc_connection result=failed',
    'ipc_server_init result=success', 'ipc_connection result=rejected reason=client_verification_failed',
    'ipc_connection result=accepted', 'ipc_client_verify result=success',
    'ipc_client_signature result=failed', 'ipc_client_signature result=success',
    'ipc_client_signature result=skipped reason=test_environment')
GRAPHICS_EVENTS = ('D3D11InternalCreateDevice: Adapter is not a DXVK adapter',
    'D3D11CreateDevice: Unsupported driver type', 'D3D11CreateDevice: Failed to create a DXGI factory',
    'D3D11CreateDevice: No default adapter available',
    'D3D11CreateDevice: Failed to query DXGI factory from DXGI adapter',
    'D3D11InternalCreateDevice: Failed to create D3D11 device')
SERVICE_EVENTS = ('server_launch stage=start executable=server',
    'server_launch result=failed executable=server', 'server_launch result=success executable=server',
    'healthd_launch result=failed', 'healthd_launch result=success',
    'winlogon_token result=failed stage=open_process', 'winlogon_token result=failed stage=duplicate_token',
    'winlogon_token result=success', 'process_restart_backoff result=scheduled reason=early_exit')
STARTUP_EVENTS = ('server_start stage=initialize', 'server_runtime result=started',
    'server_start result=ignored reason=already_started', 'server_foundations_cleanup result=started',
    'ipc_server_listener result=started', 'ipc_server_listener result=failed stage=start_thread')


def process_snapshot(prefix, descriptor=None, inode=None, proc_root=Path('/proc')):
    matched = []
    needle = ('WINEPREFIX=' + str(prefix)).encode()
    listing = subprocess.check_output(['ps', '-u', str(os.getuid()), '-o', 'pid=,comm='], text=True, timeout=5)
    wine_names = {'wine', 'wine64', 'wine64-preloade', 'wineserver', 'services.exe',
                  'explorer.exe', 'winedevice.exe', 'svchost.exe', 'plugplay.exe'}
    for line in listing.splitlines():
        pid, comm = line.strip().split(None, 1)
        if not pid.isdecimal() or (comm not in wine_names and not comm.startswith('GameViewer')):
            continue
        path = proc_root / pid
        try:
            if needle not in (path / 'environ').read_bytes().split(b'\0'):
                continue
            # No command-line arguments or environment values are returned.
            entry = dict(pid=int(pid), name=comm)
            arguments = (path / 'cmdline').read_bytes().split(b'\0')
            known = ('GameViewerService.exe', 'GameViewerServer.exe', 'GameViewer.exe')
            entry['known_executable'] = next((name for name in known if any(
                arg.replace(b'\\', b'/').rsplit(b'/', 1)[-1].lower() == name.lower().encode()
                for arg in arguments)), None)
            if descriptor is not None:
                target = 'socket:[' + str(inode) + ']'
                entry['test_socket_inherited'] = False
                entry['test_socket_at_original_fd'] = False
                for item in (path / 'fd').iterdir():
                    try:
                        if os.readlink(item) == target:
                            entry['test_socket_inherited'] = True
                            entry['test_socket_at_original_fd'] |= item.name == str(descriptor)
                    except OSError:
                        continue
            matched.append(entry)
        except (OSError, ValueError):
            continue
    return matched


def summarize(output, code, timeout, files):
    # Only known module basenames and fixed diagnostics, no arbitrary log text.
    loaded = [name for name in files if any('Loaded ' in line and ('\\\\' + name.lower() + '"') in line.lower()
              for line in output.splitlines())]
    loader_errors = bool(re.search(r'err:module:.*(?:import_dll|LdrInitializeThunk)', output))
    return dict(observation='loader_failure' if loader_errors else 'alive_at_observation_deadline' if timeout else 'process_exited',
        exit_code=code, loader_errors=loader_errors, loaded_app_modules=loaded,
        server_executable_loaded='GameViewerServer.exe' in loaded,
        streamer_module_loaded='streamer.dll' in loaded,
        startup_events=[event for event in STARTUP_EVENTS if event in output],
        nvenc_calls=[json.loads(line.split('UURB_NVENC_TRACE ', 1)[1]) for line in output.splitlines()
                     if 'UURB_NVENC_TRACE {' in line],
        network_namespace_isolated=True, account_files_copied=False, portal_accessed=False,
        uu_session_tested=False, desktop_captured=False, service_installed=False)


def graphics_loads(output):
    result = []
    allowed = {'dxgi.dll', 'd3d11.dll', 'nvencodeapi64.dll', 'uurb-nvenc-audit.dll.so',
               'uurb-dxgi-capture-loader.dll', 'uurb-dxgi-capture.dll.so'}
    for line in output.splitlines():
        match = re.search(r'Loaded L"([^"]+)".*: (native|builtin)', line)
        if not match:
            continue
        path = re.sub(r'\\+', '/', match[1]).lower()
        name = path.rsplit('/', 1)[-1]
        if name not in allowed:
            continue
        source = 'private_app_bin' if '/app/bin/' in path else 'prefix_system32' if '/windows/system32/' in path else 'other'
        result.append(dict(module=name, source=source, loader_kind=match[2]))
    return result


def detector_invocations(output):
    """Only the detector's bounded numeric CLI, never general process args."""
    result = []
    for line in output.splitlines():
        if ':process:' not in line or 'StreamerCodecDetector.exe' not in line:
            continue
        normalized = line.replace('\\"', '"')
        for match in re.finditer(r'StreamerCodecDetector\.exe"?\s+((?:--batch\s+)?[0-9]+(?:\s+[0-9]+){0,6})(?:"|$)', normalized):
            tokens = match[1].split()
            batch = tokens[0] == '--batch'
            values = [int(token) for token in tokens[1:] if batch] if batch else [int(token) for token in tokens]
            if any(value > 0xffffffffffffffff for value in values):
                continue
            entry = dict(batch=batch, numeric_arguments=values)
            if entry not in result:
                result.append(entry)
    return result


def service_records(output):
    records = []
    for line in output.splitlines():
        if 'UURB_SERVICE_PROBE {' not in line:
            continue
        value = json.loads(line.split('UURB_SERVICE_PROBE ', 1)[1])
        # Even the test launcher is not a source of arbitrary report strings.
        if value.get('stage') not in ('guard_rejected', 'snapshot_failed', 'children', 'standin_failed',
                'standin_started', 'scm_open_failed', 'service_create_failed', 'service_created',
                'service_start_failed', 'service_start_requested', 'service_query_failed', 'status',
                'service_stop_requested', 'service_delete_requested', 'child_environment', 'child_environment_failed',
                'observer_environment', 'private_config_staged', 'private_config_removed', 'bootstrap_control_133',
                'gui_started', 'gui_start_failed', 'gui_environment'):
            raise ValueError('Unknown service diagnostic stage')
        if any(key != 'stage' and (key not in ('code', 'server', 'healthd', 'gui', 'server_pid', 'state', 'exit_code', 'sample',
                'sentinel_present', 'overrides_present', 'capture_fd_present', 'capture_fd_matches', 'capture_output_matches',
                'dxvk_config_present', 'dxvk_config_matches')
                or type(item) is not int or item < 0 or item > 0xffffffff) for key, item in value.items()):
            raise ValueError('Invalid service diagnostic scalar')
        records.append(value)
    return records


def run(service=False, winlogon_standin=False, native_config=False, bootstrap_control=False, gui=False, process_debug=False):
    if winlogon_standin and not service:
        raise ValueError('The winlogon stand-in requires service mode')
    if native_config and not service:
        raise ValueError('Native configuration requires service mode')
    if bootstrap_control and not service:
        raise ValueError('Bootstrap control requires service mode')
    if gui and not service:
        raise ValueError('The offline GUI requires service mode')
    subprocess.run(['unshare', '--user', '--map-current-user', '--net', 'true'], check=True, capture_output=True, timeout=5)
    pinned = dict(PINNED, **(SERVICE_PINNED if service else {}))
    if gui:
        pinned.update(GUI_PINNED)
        if hashlib.sha256((BIN.parent / 'GameViewer.exe').read_bytes()).hexdigest() != GUI_ROOT_SHA:
            raise RuntimeError('Unreviewed root GUI binary')
    for name, expected in pinned.items():
        if hashlib.sha256((BIN / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Unreviewed official binary: ' + name)
    stage = ROOT / 'build/native-presenter'
    # An inert socket is only an inheritance witness; it never receives GPU
    # handles, account data, capture requests or any application traffic.
    with tempfile.TemporaryDirectory(prefix='uurb-offline-server-') as temporary, \
            socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as witness:
        directory = Path(temporary)
        app = directory / 'app' / 'bin' if service else directory / 'app'
        app.mkdir(parents=True)
        launch_dir = app.parent if service else app
        files = [*pinned, *RUNTIMES, *(GUI_DEPENDENCIES if gui else ())]
        for name in files:
            shutil.copy2(BIN / name, app / name)
        for name in ('d3d11.dll', 'dxgi.dll'):
            shutil.copy2(ROOT / 'build/dxvk-capture/stage' / name, app / name)
        executable, arguments = 'GameViewerServer.exe', []
        if service:
            executable = 'uu-service-startup-probe.exe'
            shutil.copy2(stage / executable, launch_dir / executable)
            shutil.copy2(app / 'GameViewerService.exe', launch_dir / 'GameViewerService.exe')
            if winlogon_standin:
                shutil.copy2(stage / 'uu-offline-winlogon-standin.exe', launch_dir / 'winlogon.exe')
                arguments = ['--winlogon-standin']
            if native_config:
                arguments.append('--native-config')
                shutil.copy2(ROOT / 'config/dxvk-native.conf', launch_dir / 'dxvk.conf')
                for name in ('uurb-dxgi-capture-loader.dll', 'uurb-dxgi-capture.dll.so'):
                    shutil.copy2(stage / name, app / name)
            if bootstrap_control:
                arguments.append('--bootstrap-control')
            if gui:
                arguments.append('--gui')
                shutil.copy2(BIN.parent / 'GameViewer.exe', launch_dir / 'GameViewer.exe')
                platforms = app / 'plugins/platforms'
                platforms.mkdir(parents=True)
                shutil.copy2(BIN / 'plugins/platforms/qwindows.dll', platforms / 'qwindows.dll')
        system32 = directory / 'wine/drive_c/windows/system32'
        system32.mkdir(parents=True)
        shutil.copy2(stage / 'uurb-nvenc-audit-loader.dll', system32 / 'nvEncodeAPI64.dll')
        shutil.copy2(stage / 'uurb-nvenc-audit.dll.so', system32 / 'uurb-nvenc-audit.dll.so')
        env = dict(os.environ, WINEPREFIX=str(directory / 'wine'), WINELOADER='/opt/wine-stable/bin/wine',
            WINEDEBUG='-all,err+module,+loaddll,+debugstr', WINEDLLOVERRIDES='mscoree,mshtml=;d3d11,dxgi,nvEncodeAPI64=n',
            DXVK_CONFIG_FILE=str(ROOT / 'config/dxvk-native.conf'), DXVK_LOG_LEVEL='warn', DXVK_LOG_PATH=temporary)
        for name in ('WINEDLLPATH', 'DXVK_CONFIG', 'DXVK_FILTER_DEVICE_NAME', 'VK_INSTANCE_LAYERS',
                     'UURB_DXGI_CAPTURE_FD', 'UURB_DXGI_CAPTURE_OUTPUT', 'UURB_NVENC_SYNTHETIC_OUTPUT',
                     'UURB_OFFLINE_SERVICE_PROBE', 'UURB_TEST_CAPTURE_FD'):
            env.pop(name, None)
        if service:
            env['UURB_OFFLINE_SERVICE_PROBE'] = '1'
        if native_config:
            env['UURB_TEST_CAPTURE_FD'] = str(witness.fileno())
        if process_debug:
            env['WINEDEBUG'] += ',+process'
        process = None
        mid_observation = []
        try:
            with (directory / 'probe.log').open('w') as log:
                process = subprocess.Popen(['unshare', '--user', '--map-current-user', '--net',
                    'xvfb-run', '-a', '--server-args=-screen 0 1920x1080x24 -nolisten tcp',
                    '/opt/wine-stable/bin/wine', str(launch_dir / executable), *arguments],
                    env=env, cwd=launch_dir, stdout=log, stderr=log, start_new_session=True,
                    pass_fds=(witness.fileno(),) if service else ())
                code, timed_out = None, False
                try:
                    if service:
                        try:
                            code = process.wait(timeout=12)
                        except subprocess.TimeoutExpired:
                            mid_observation = process_snapshot(directory / 'wine', witness.fileno(),
                                                               os.fstat(witness.fileno()).st_ino)
                            code = process.wait(timeout=33)
                    else:
                        code = process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    timed_out = True
                observed_processes = process_snapshot(directory / 'wine')
                # Observation timeout is not mistaken for a natural exit. Stop
                # only our disposable prefix after recording the observation.
                subprocess.run(['/opt/wine-stable/bin/wineserver', '-k'], env=env, capture_output=True, timeout=10)
                if process.poll() is None:
                    process.wait(timeout=5)
            output = (directory / 'probe.log').read_text(errors='replace')
            report = summarize(output, code, timed_out, files)
            report['observed_executable'] = executable
            report['wine_module_error_channel_enabled'] = True
            report['graphics_module_loads'] = graphics_loads(output)
            report['detector_invocations'] = detector_invocations(output)
            report['graphics_events'] = [event for event in GRAPHICS_EVENTS if event in output]
            report['server_binary_variant'] = 'installed_sendinput_compatibility_patch'
            report['service_probe_records'] = service_records(output)
            report['service_installed'] = any(item['stage'] == 'service_created' for item in report['service_probe_records'])
            report['service_install_scope'] = 'disposable_prefix_only' if service else None
            report['service_layout'] = 'root_executable_with_service_flag_and_bin_children' if service else None
            report['winlogon_standin_used'] = winlogon_standin
            report['private_native_configuration_requested'] = native_config
            report['bootstrap_control_requested'] = bootstrap_control
            report['offline_gui_requested'] = gui
            report['account_login_tested'] = False
            report['healthd_staged'] = False
            report['service_events'] = [event for event in SERVICE_EVENTS if event in output]
            report['ipc_events'] = [event for event in IPC_EVENTS if event in output]
            report['prefix_processes_before_test_cleanup'] = observed_processes
            report['mid_observation_inert_socket_witness'] = mid_observation
            report['copied_binaries_sha256'] = {name: hashlib.sha256((app / name).read_bytes()).hexdigest() for name in files}
            test_artifacts = [app / 'dxgi.dll', app / 'd3d11.dll', system32 / 'nvEncodeAPI64.dll',
                             system32 / 'uurb-nvenc-audit.dll.so']
            if service:
                test_artifacts.append(launch_dir / executable)
                if native_config:
                    test_artifacts += [app / name for name in ('uurb-dxgi-capture-loader.dll', 'uurb-dxgi-capture.dll.so')]
                    test_artifacts.append(launch_dir / 'dxvk.conf')
                if winlogon_standin:
                    test_artifacts.append(launch_dir / 'winlogon.exe')
            report['test_artifacts_sha256'] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                                for path in test_artifacts}
            if gui:
                report['additional_gui_artifacts_sha256'] = {
                    'root/GameViewer.exe': hashlib.sha256((launch_dir / 'GameViewer.exe').read_bytes()).hexdigest(),
                    'bin/plugins/platforms/qwindows.dll': hashlib.sha256((app / 'plugins/platforms/qwindows.dll').read_bytes()).hexdigest()}
            # Application logs may contain generated identifiers: report names
            # and sizes only. Never copy their contents out of this prefix.
            report['temporary_app_logs'] = [dict(name=path.name, bytes=path.stat().st_size)
                for path in directory.rglob('*.log') if path.name != 'probe.log']
            for path in directory.rglob('*.log'):
                if path.name == 'probe.log' or path.stat().st_size > 8 * 1024 * 1024:
                    continue
                content = path.read_text(errors='replace')
                for key, events in (('startup_events', STARTUP_EVENTS), ('service_events', SERVICE_EVENTS),
                                    ('ipc_events', IPC_EVENTS), ('graphics_events', GRAPHICS_EVENTS)):
                    report[key] = sorted(set(report[key]) | {event for event in events if event in content})
            return report
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            subprocess.run(['/opt/wine-stable/bin/wineserver', '-k'], env=env, capture_output=True, timeout=10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', action='store_true')
    parser.add_argument('--winlogon-standin', action='store_true')
    parser.add_argument('--native-config', action='store_true')
    parser.add_argument('--bootstrap-control', action='store_true')
    parser.add_argument('--gui', action='store_true')
    parser.add_argument('--process-debug', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.service, args.winlogon_standin, args.native_config, args.bootstrap_control, args.gui, args.process_debug), indent=2))
