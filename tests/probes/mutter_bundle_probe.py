#!/usr/bin/python3
"""Start a private headless GNOME Shell; never replace the physical compositor."""
import argparse
import json
import os
import signal
from pathlib import Path
import subprocess
import tempfile
import time


def inside(bundle, virtual_capture=False, virtual_lifecycle=False, virtual_eis=False, capture_stop=None, selected_display=False):
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    with tempfile.TemporaryFile(mode='w+') as log:
        pipewire = None
        if virtual_capture or virtual_lifecycle or virtual_eis or capture_stop:
            # Only the synthetic shell's private PipeWire, never the login bus.
            runtime = Path(os.environ['XDG_RUNTIME_DIR']).resolve()
            if not runtime.parent.name.startswith('uurb-mutter-test-') or runtime.parent.parent != Path('/tmp'):
                raise RuntimeError('Refusing capture outside owned private test runtime')
            pipewire = subprocess.Popen(['/usr/bin/pipewire'], stdout=log, stderr=log)
            deadline = time.monotonic() + 5
            while not (runtime / 'pipewire-0').exists() and time.monotonic() < deadline:
                time.sleep(0.05)
        # The standalone test compositor avoids GNOME session-manager startup
        # dependencies. It uses the same staged Mutter core and capture code.
        compositor = str(bundle / 'mutter-headless-probe') if virtual_capture or virtual_lifecycle or virtual_eis or capture_stop or selected_display else '/usr/bin/gnome-shell'
        extra = ['--mutter-plugin=' + str(bundle / 'libdefault.so')] if virtual_capture or virtual_lifecycle or virtual_eis or capture_stop or selected_display else []
        if selected_display:
            extra += ['--virtual-monitor', '1920x1080@60']
        process = subprocess.Popen([compositor, *extra, '--headless', '--wayland', '--no-x11',
            '--virtual-monitor', '1920x1080@60', '--wayland-display', 'uurb-private-test'],
            env=dict(os.environ, LD_LIBRARY_PATH=str(bundle)), stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    state = bus.call_sync('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig',
                        'org.gnome.Mutter.DisplayConfig', 'GetCurrentState', None, None,
                        Gio.DBusCallFlags.NO_AUTO_START, 1000, None).unpack()
                    if state[1] and state[2]:
                        # Give JS startup a chance to fail before accepting just a bus name.
                        time.sleep(3)
                        if process.poll() is not None:
                            break
                        maps = Path(f'/proc/{process.pid}/maps').read_text()
                        if str(bundle / 'libmutter-14.so.0.0.0') not in maps:
                            raise RuntimeError('Private Shell did not map the candidate library')
                        capture = {}
                        if virtual_capture or virtual_lifecycle or virtual_eis or capture_stop:
                            from mutter_private_virtual_capture import capture as run_capture
                            try:
                                capture = run_capture(bus, process.pid, lifecycle=virtual_lifecycle,
                                                      joint_input=virtual_eis, stop_binaries=capture_stop)
                            except Exception as error:
                                log.seek(0)
                                raise RuntimeError(str(error) + '\nPrivate compositor log: ' + log.read()[-3500:]) from error
                        display_result = {}
                        if selected_display:
                            from mutter_selected_display import check
                            display_result = check(bus, process.pid)
                        print('UURB_REPORT:' + json.dumps(dict(private_headless=True, physical_session_replaced=False,
                            candidate_library_loaded=True, shell_pid=process.pid, monitors=state[1],
                            logical_monitors=state[2], properties=state[3], virtual_capture=capture,
                            selected_display=display_result)), flush=True)
                        return
                except GLib.Error:
                    pass
                time.sleep(0.25)
            log.seek(0)
            raise RuntimeError('Private Shell failed startup: ' + log.read()[-6000:])
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if pipewire:
                pipewire.terminate()
                try:
                    pipewire.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pipewire.kill()
                    pipewire.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--inside', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--virtual-capture', action='store_true')
    mode.add_argument('--virtual-lifecycle', action='store_true')
    mode.add_argument('--virtual-eis', action='store_true')
    mode.add_argument('--selected-display', action='store_true',
                      help='Private two-output DPI transaction, preserving the other output')
    mode.add_argument('--capture-stop', nargs=2, type=Path, metavar=('PRODUCER', 'RECEIVER'),
                      help='Private actual GPU/NVENC stop test using explicit candidate binaries')
    args = parser.parse_args()
    bundle = args.bundle.resolve(strict=True)
    if args.inside:
        inside(bundle, args.virtual_capture, args.virtual_lifecycle, args.virtual_eis, args.capture_stop, args.selected_display)
        return
    with tempfile.TemporaryDirectory(prefix='uurb-mutter-test-') as directory:
        env = dict(os.environ)
        for key in ['DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER',
                    'LD_PRELOAD', 'GNOME_SHELL_SESSION_MODE', 'GNOME_SETUP_DISPLAY']:
            env.pop(key, None)
        for key, relative in [('XDG_RUNTIME_DIR', 'run'), ('XDG_CONFIG_HOME', 'config'),
                              ('XDG_DATA_HOME', 'data'), ('XDG_CACHE_HOME', 'cache')]:
            folder = Path(directory) / relative
            folder.mkdir(mode=0o700)
            env[key] = str(folder)
        env['XDG_SESSION_TYPE'] = 'wayland'
        # This synthetic desktop needs no remote-volume service or FUSE mount.
        # Bus activation of gvfs can otherwise leave a dead mount in the owned
        # temporary directory after its process group has been reaped.
        env['GIO_USE_VFS'] = 'local'
        # Bus-activated helpers can inherit stdout beyond dbus-run-session's
        # lifetime. Files avoid pipe-EOF hangs; cleanup targets only our group.
        with tempfile.TemporaryFile(mode='w+') as output, tempfile.TemporaryFile(mode='w+') as errors:
            process = subprocess.Popen(['dbus-run-session', '--', '/usr/bin/python3', str(Path(__file__).resolve()),
                str(bundle), '--inside', *(['--virtual-capture'] if args.virtual_capture else []),
                *(['--virtual-lifecycle'] if args.virtual_lifecycle else []),
                *(['--virtual-eis'] if args.virtual_eis else []),
                *(['--selected-display'] if args.selected_display else []),
                *(['--capture-stop', *map(str,args.capture_stop)] if args.capture_stop else [])],
                env=env, stdout=output, stderr=errors, start_new_session=True)
            try:
                code = process.wait(timeout=55)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            output.seek(0)
            errors.seek(0)
            stdout, stderr = output.read(), errors.read()
        if code:
            raise RuntimeError('Private session failed: ' + stderr[-8000:])
        records = [line.removeprefix('UURB_REPORT:') for line in stdout.splitlines()
                   if line.startswith('UURB_REPORT:')]
        if len(records) != 1:
            raise RuntimeError('Missing unambiguous private-session report')
        print(json.dumps(json.loads(records[0]), indent=2))


if __name__ == '__main__':
    main()
