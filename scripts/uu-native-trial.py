#!/usr/bin/python3
"""Reversible UU native bring-up using the existing account prefix.

Own DLL bytes remain in a versioned bridge bundle. Only absent loader links
are placed in Wine lookup locations; updater/user replacements are preserved.
No RDP, account copying, service installation, GNOME restart or Portal restart.
"""
import argparse
import fcntl
import getpass
import importlib.util
import json
import os
from pathlib import Path
import re
import pwd
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
WINE = '/opt/wine-stable/bin/wine'
WINESERVER = '/opt/wine-stable/bin/wineserver'
spec = importlib.util.spec_from_file_location('native_bundle', ROOT / 'scripts/package-uu-native-runtime.py')
bundle_tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle_tools)
spec = importlib.util.spec_from_file_location('native_runtime_state', ROOT / 'scripts/native_runtime_state.py')
state_tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state_tools)
spec = importlib.util.spec_from_file_location('native_display_confirmation', ROOT / 'scripts/native_display_confirmation.py')
confirmation_tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(confirmation_tools)


def event(name, **values):
    print(json.dumps(dict(event=name, **values)), flush=True)


def worker_environment():
    values = dict(os.environ)
    # Only this supervisor reports readiness/watchdog, not Wine/Xvfb/children.
    for name in ('NOTIFY_SOCKET', 'WATCHDOG_PID', 'WATCHDOG_USEC'):
        values.pop(name, None)
    return values


def wine_locale(env):
    """Let the session LANG, not a desktop-wide LC_ALL, choose Wine's locale.

    Wine derives the ANSI code page from the locale. UU reads clipboard text
    in that code page, so an LC_ALL of en_US.UTF-8 over a zh_CN session turned
    every CJK character copied to the phone into '?'.
    """
    if env.get('LANG'):
        env.pop('LC_ALL', None)
    return env


def private_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Expected an owned private directory: ' + path.name)


def prefix_processes(prefix):
    needle = ('WINEPREFIX=' + str(prefix)).encode()
    found = []
    listing = subprocess.check_output(['ps', '-u', str(os.getuid()), '-o', 'pid=,comm='], text=True, timeout=5,
                                      env=worker_environment())
    for line in listing.splitlines():
        pid, name = line.strip().split(None, 1)
        # UU's GUI can replace argv[0]/comm with a launch-source argument.
        # The exact prefix environment, not its mutable process name, scopes it.
        try:
            if needle in (Path('/proc') / pid / 'environ').read_bytes().split(b'\0'):
                found.append(dict(pid=int(pid), name=name))
        except OSError:
            continue
    return found


def winpath(path):
    return 'Z:' + str(path.resolve()).replace('/', '\\')


def boot_events(path, cursor=None):
    if not path.exists():
        return []
    # No raw UU log, account data, command line or environment is exported.
    result = []
    allowed = {'preflight_passed', 'configuration_conflict', 'configuration_applied', 'winlogon_started',
               'service_bootstrap_accepted', 'gui_started', 'configuration_removed', 'bootstrap_exit', 'recovery_finished',
               'native_input_broker_ready', 'native_input_hook_ready'}
    with path.open(errors='replace') as stream:
        if cursor is not None:
            stream.seek(cursor[0])
        while True:
            position = stream.tell()
            line = stream.readline()
            if not line:
                break
            if cursor is not None and not line.endswith('\n'):
                stream.seek(position)
                break
            match = re.fullmatch(r'UURB_NATIVE_BOOT (\{.*\})\s*', line)
            if not match:
                continue
            try:
                value = json.loads(match[1])
            except ValueError:
                continue
            if set(value) == {'event', 'code'} and value['event'] in allowed and type(value['code']) is int:
                result.append(value)
        if cursor is not None:
            cursor[0] = stream.tell()
    return result


def end_process(process):
    if process is not None and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def cleanup_trial(launcher, broker, env, directory, arguments, token, created_aliases, native_input=None,
                  recovery_only=False):
    """Attempt every independent cleanup even if a previous action fails."""
    failures = []

    def attempt(label, action):
        try:
            action()
        except Exception as error:
            # Exception text may contain private process arguments or output.
            failures.append(label)
            event('cleanup_failed', step=label, error_type=type(error).__name__)

    if (launcher is not None or recovery_only) and env is not None:
        def wine_action(args, timeout):
            with (directory / 'stop.log').open('a') as log:
                process = subprocess.Popen(
                    ['xvfb-run', '-a', '--server-args=-screen 0 1920x1080x24 -nolisten tcp', WINE, *args],
                    env=env, stdout=log, stderr=log, start_new_session=True)
                try:
                    code = process.wait(timeout=timeout)
                    if code:
                        raise RuntimeError('Wine cleanup helper failed')
                finally:
                    end_process(process)

        def stop_server():
            with (directory / 'stop.log').open('a') as log:
                subprocess.run([WINESERVER, '-k'], env=env, stdout=log, stderr=log,
                               timeout=10, check=True)
                subprocess.run([WINESERVER, '-w'], env=env, stdout=log, stderr=log,
                               timeout=10, check=True)

        if launcher is not None and launcher.poll() is None:
            attempt('signal_bootstrap', lambda: wine_action([arguments[0], '--stop', token], 15))
            attempt('wait_bootstrap', lambda: launcher.wait(timeout=10))
        # Only the prefix verified idle and locked by this trial is stopped.
        # Cold recovery has just proven the prefix idle. wineserver -k returns
        # an error for that expected condition; do not turn it into an outage.
        if not recovery_only:
            attempt('stop_owned_prefix', stop_server)
        attempt('stop_launcher', lambda: end_process(launcher))

        def recover():
            records = boot_events(directory / 'wine.log')
            if any(r == dict(event='preflight_passed', code=0) for r in records) and not any(
                    r == dict(event='configuration_removed', code=0) for r in records):
                try:
                    wine_action([arguments[0], '--recover', *arguments[2:]], 30)
                finally:
                    attempt('stop_recovery_prefix', stop_server)
        attempt('recover_registry', recover)
    attempt('stop_broker', lambda: end_process(broker))
    if native_input is not None:
        attempt('stop_native_input', lambda: end_process(native_input))
    removed = 0
    preserved = 0
    for path, target, identity in created_aliases:
        def remove_owned_link():
            nonlocal removed, preserved
            try:
                info = path.lstat()
            except FileNotFoundError:
                return
            if (info.st_dev, info.st_ino) == identity and path.is_symlink() and os.readlink(path) == str(target):
                path.unlink()
                removed += 1
            else:
                preserved += 1
        attempt('remove_loader_link_' + path.name, remove_owned_link)
    if directory is not None:
        report = dict(loader_links_removed=removed, changed_links_preserved=preserved,
                      cleanup_failures=failures, account_files_copied=False)
        def save_report():
            state_tools.write_private(directory / 'cleanup.json', report)
        attempt('save_cleanup_report', save_report)
        event('native_trial_stopped', state_directory=str(directory), **report)
    return failures


def recover_interrupted(prefix, state_parent):
    """Called only under the prefix lock, after confirming no prefix process.

    Systemd must first terminate all surviving members of the old service
    cgroup. Never infer a dead process from a missing heartbeat or kill a
    current Wine prefix in order to make recovery possible.
    """
    active = state_parent / 'active-runtime.json'
    if not active.exists() and not active.is_symlink():
        return
    directory = state_tools.state_directory(state_parent, state_tools.read_private(active))
    journal = state_tools.read_private(directory / 'journal.json')
    if journal.get('version') != 2 or journal.get('prefix') != str(prefix):
        raise ValueError('Unreviewed runtime recovery checkpoint')
    if (directory / 'cleanup.json').exists():
        report = state_tools.read_private(directory / 'cleanup.json')
        if not report.get('cleanup_failures') and not report.get('changed_links_preserved'):
            return
    bundle = Path(journal['bundle'])
    verified = bundle_tools.verify(bundle)
    if journal.get('release_id') != verified['release_id']:
        raise ValueError('Changed runtime recovery bundle')
    # Only execute the bootstrap whose exact bytes came from that bundle.
    if bundle_tools.digest(directory / 'bootstrap.exe') != bundle_tools.digest(bundle / 'runtime/bootstrap.exe'):
        raise ValueError('Changed runtime recovery helper')
    app = prefix / 'drive_c/Program Files/Netease/GameViewer'
    arguments = journal.get('arguments')
    if (not isinstance(arguments, list) or len(arguments) != 6 or
        arguments[:5] != [str(directory / 'bootstrap.exe'), '--run', str(directory / 'capture.sock'),
                          winpath(bundle / 'dxvk.conf'), winpath(app / 'GameViewer.exe')] or
        not isinstance(arguments[5], str) or not re.fullmatch('UURB-NATIVE-[0-9a-f]{32}', arguments[5])):
        raise ValueError('Invalid runtime recovery invocation')
    allowed = {}
    for name in bundle_tools.INPUTS:
        if name.startswith('app/bin/'):
            allowed[str(app / 'bin' / Path(name).name)] = str((bundle / name).resolve())
        elif name.startswith('system32/'):
            allowed[str(prefix / 'drive_c/windows/system32' / Path(name).name)] = str((bundle / name).resolve())
    aliases = journal.get('aliases', [])
    if {entry['path']: entry['target'] for entry in aliases} != allowed or len(aliases) != len(allowed):
        raise ValueError('Unexpected runtime recovery link set')
    created = []
    for entry in aliases:
        identity = entry.get('identity')
        if identity is not None:
            if not isinstance(identity, list) or len(identity) != 2 or any(type(i) is not int or i < 0 for i in identity):
                raise ValueError('Invalid runtime link identity')
            created.append((Path(entry['path']), Path(entry['target']), tuple(identity)))
    env = dict(worker_environment(), WINEPREFIX=str(prefix), WINELOADER=WINE, WINEDEBUG='-all',
               UURB_NATIVE_BOOTSTRAP='1', WINEDLLOVERRIDES='mscoree,mshtml=')
    wine_locale(env)
    env.pop('UURB_CURSOR_STATE_PATH', None)
    env.pop('UURB_DISPLAY_SOCKET', None)
    if journal.get('display_socket') is not None:
        if journal['display_socket'] != str(state_parent / 'display.sock'):
            raise ValueError('Unreviewed display recovery endpoint')
        env['UURB_DISPLAY_SOCKET'] = journal['display_socket']
    if journal.get('capture_cursor_mode') in ('metadata', 'composited') or journal.get('embedded_cursor_adapter'):
        env['UURB_CURSOR_STATE_PATH'] = winpath(directory / 'cursor.state')
    if prefix_processes(prefix):
        raise RuntimeError('Recovery requires the owned prefix to remain idle')
    event('native_runtime_recovery_started')
    failures = cleanup_trial(None, None, env, directory, arguments, arguments[5], created, recovery_only=True)
    report = state_tools.read_private(directory / 'cleanup.json')
    if failures or report.get('changed_links_preserved'):
        raise RuntimeError('Interrupted runtime needs component review')
    event('native_runtime_recovery_finished')


def native_topology():
    # Use the distribution Python owning dbus, independent of the caller's
    # virtualenv. Output only display geometry, never monitor serial numbers.
    code = '''import dbus,json
i=dbus.Interface(dbus.SessionBus().get_object('org.gnome.Mutter.DisplayConfig','/org/gnome/Mutter/DisplayConfig'),'org.gnome.Mutter.DisplayConfig')
s,m,l,p=i.GetCurrentState()
print(json.dumps(dict(generation=int(s),connectors=[str(v[0][0]) for v in m],layout=[dict(x=int(v[0]),y=int(v[1]),scale=float(v[2]),transform=int(v[3]),outputs=len(v[5])) for v in l],modes=[dict(width=int(t[1]),height=int(t[2])) for v in m for t in v[1] if t[-1].get('is-current')])))
'''
    value = json.loads(subprocess.check_output(['/usr/bin/python3', '-c', code], text=True, timeout=5,
                                              env=worker_environment()))
    if len(value['layout']) != 1 or len(value['modes']) != 1:
        raise RuntimeError('Native input currently requires one physical output')
    layout = value['layout'][0]
    if layout['outputs'] != 1 or layout['x'] or layout['y'] or layout['transform']:
        raise RuntimeError('Unsupported native input display layout')
    if any(not 2 <= value['modes'][0][key] <= 8192 for key in ('width', 'height')):
        raise RuntimeError('Native input display geometry exceeds bounds')
    return value


def can_recreate_display_in_place(before, after):
    """Conservative first rollout: same output/layout/scale, pixel-mode only.

    Native uinput uses an unchanged 0..65535 absolute range. New GPU allocation
    dimensions must come from a fresh consumer generation, not old textures.
    Missing connector identity, DPI/layout changes and added outputs stay out.
    """
    try:
        connector = before['connectors']
        if len(connector) != 1 or not isinstance(connector[0], str) or not connector[0] or connector != after['connectors']:
            return False
        if before['layout'] != after['layout'] or len(after['layout']) != 1:
            return False
        layout = after['layout'][0]
        if layout['outputs'] != 1 or layout['x'] or layout['y'] or layout['transform']:
            return False
        for value in (before, after):
            if len(value['modes']) != 1:
                return False
            if any(type(value['modes'][0][k]) is not int or not 2 <= value['modes'][0][k] <= 4096 or value['modes'][0][k] % 2
                   for k in ('width', 'height')):
                return False
        return True
    except (KeyError, TypeError, IndexError):
        return False


def native_input_ready(path):
    if not path.exists():
        return None
    for line in path.read_text(errors='replace').splitlines():
        if not line.startswith('UURB_NATIVE_INPUT '):
            continue
        try:
            value = json.loads(line.split(' ', 1)[1])
        except ValueError:
            continue
        if value.get('event') == 'ready' and value.get('uid') == os.getuid() and value.get('single_output_only') is True:
            port = value.get('port')
            if type(port) is int and 1 <= port <= 65535:
                return port
    return None


def validate_text_endpoint(text_socket, state_parent, with_input, pending_native_ime=False):
    if pending_native_ime and (not with_input or text_socket != state_parent / 'ime.sock'):
        raise ValueError('Deferred native IME requires the exact private Fcitx endpoint')
    if text_socket is None:
        return
    if not with_input or not text_socket.is_absolute():
        raise ValueError('Native text requires native input and an absolute private endpoint')
    private_directory(text_socket.parent)
    try:
        info = text_socket.lstat()
    except FileNotFoundError:
        if not pending_native_ime:
            raise
        # Native client resolves/connects anew for each submitted text batch.
        # Missing endpoint is not READY, and failed text is never queued/replayed.
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Expected the private authorized native text service endpoint')


def run(prefix, bundle, restore_state, state_parent, duration, with_input=False, cursor_mode='embedded', text_socket=None,
        input_access='sudo', display_socket=None, preserve_display_session=False, pending_native_ime=False,
        stop_requested=None):
    if stop_requested is not None and stop_requested():
        return
    continuous = duration is None
    if input_access not in ('sudo', 'user') or (continuous and input_access != 'user'):
        raise ValueError('Continuous service requires noninteractive user device access')
    if with_input and input_access == 'user' and not os.access('/dev/uinput', os.W_OK):
        raise RuntimeError('Active desktop user has no uinput access')
    if cursor_mode not in ('embedded', 'hidden', 'metadata', 'composited'):
        raise ValueError('Unsupported capture cursor policy')
    if cursor_mode in ('metadata', 'composited') and not with_input:
        raise ValueError('Metadata cursor mode requires native USER32 adaptation')
    validate_text_endpoint(text_socket, state_parent, with_input, pending_native_ime)
    prefix = prefix.resolve()
    private_directory(prefix)
    verified = bundle_tools.verify(bundle)
    if pending_native_ime and not verified.get('native_ime_deferred_start_included'):
        raise ValueError('Deferred native IME requires a reviewed startup contract')
    if not verified.get('native_capture_producer_included'):
        raise ValueError('Native runtime requires a version-pinned capture producer; unpinned rollback is unsafe')
    if verified.get('native_main_scripts_included') and ROOT != bundle.resolve():
        raise ValueError('This bundle requires its version-pinned scripts/uu-native-service.py or uu-native-trial.py entrypoint')
    if preserve_display_session and (not with_input or display_socket is None or not verified.get('native_display_geometry_included')):
        raise ValueError('Video-only display recreation requires native input, guardian and geometry-aware bundle')
    if display_socket is not None:
        if not with_input or not verified.get('native_display_included') or display_socket != state_parent / 'display.sock':
            raise ValueError('Native display requires its verified bundle and owned guardian endpoint')
        info = display_socket.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Expected a private native display guardian socket')
    if with_input and not verified.get('native_input_included'):
        raise ValueError('Native input requires a verified input-enabled runtime bundle')
    if cursor_mode == 'metadata' and not verified.get('native_cursor_included'):
        raise ValueError('Metadata cursor mode requires a verified cursor-aware runtime bundle')
    if cursor_mode == 'composited' and not verified.get('native_composited_cursor_included'):
        raise ValueError('GPU-composited cursor mode requires a verified composition-aware runtime bundle')
    if text_socket is not None and not verified.get('native_text_included'):
        raise ValueError('Native text requires a text-aware verified bundle')
    topology = native_topology() if with_input else None
    if duration is not None and not 10 <= duration <= 3600:
        raise ValueError('Trial duration must be from 10 to 3600 seconds')
    for unit in ('uu-remote-bridge.service', 'gnome-remote-desktop.service'):
        if subprocess.run(['systemctl', '--user', 'is-active', '--quiet', unit], env=worker_environment()).returncode == 0:
            raise RuntimeError('Native bring-up requires the legacy/RDP unit inactive: ' + unit)
    if prefix_processes(prefix):
        raise RuntimeError('The selected prefix is busy; no existing Wine process will be stopped')
    app = prefix / 'drive_c/Program Files/Netease/GameViewer'
    gui = app / 'GameViewer.exe'
    for name in ('GameViewer.exe', 'GameViewerService.exe', 'bin/GameViewerServer.exe'):
        if not (app / name).is_file():
            raise RuntimeError('Official UU component missing: ' + name)
    if not (prefix / 'system.reg').is_file():
        raise RuntimeError('An existing installed UU prefix is required')
    state_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_directory(state_parent)
    lock_fd = os.open(prefix / '.uurb-native.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    broker = launcher = None
    native_input = None
    aliases = []
    created_aliases = []
    arguments = token = None
    directory = None
    env = None
    stopping = False

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True

    def is_stopping():
        return stopping or (stop_requested is not None and stop_requested())

    # A persistent supervisor owns signals across worker generations.
    previous = ({s: signal.signal(s, request_stop) for s in (signal.SIGTERM, signal.SIGINT)}
                if stop_requested is None else {})
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if prefix_processes(prefix):
            raise RuntimeError('The prefix became busy before native startup')
        if continuous:
            recover_interrupted(prefix, state_parent)
        if is_stopping():
            return
        directory = Path(tempfile.mkdtemp(prefix='trial-', dir=state_parent))
        endpoint = directory / 'capture.sock'
        if len(os.fsencode(endpoint)) >= 108:
            raise ValueError('Runtime socket path exceeds the Unix path limit')
        if verified.get('native_input_included'):
            runtime = bundle_tools.INPUT_RUNTIME if verified.get('native_display_included') else bundle_tools.LEGACY_INPUT_RUNTIME
            for name in runtime:
                shutil.copy2(bundle / name, directory / Path(name).name)
        else:
            for name, destination in (('uu-native-bootstrap.exe', 'bootstrap.exe'), ('uu-native-winlogon.exe', 'winlogon.exe')):
                shutil.copy2(ROOT / 'build/native-presenter' / name, directory / destination)
        for name in bundle_tools.INPUTS:
            if name.startswith('app/bin/'):
                destination = app / 'bin' / Path(name).name
            elif name.startswith('system32/'):
                destination = prefix / 'drive_c/windows/system32' / Path(name).name
            else:
                continue
            if destination.exists() or destination.is_symlink():
                raise RuntimeError('Refusing to overwrite an existing Wine component: ' + destination.name)
            aliases.append((destination, (bundle / name).resolve()))
        token = 'UURB-NATIVE-' + uuid.uuid4().hex
        arguments = [str(directory / 'bootstrap.exe'), '--run', str(endpoint), winpath(bundle / 'dxvk.conf'), winpath(gui), token]
        journal = dict(version=2, prefix=str(prefix), bundle=str(bundle.resolve()), release_id=verified['release_id'],
                       arguments=arguments, aliases=[dict(path=str(p), target=str(t)) for p, t in aliases],
                       account_files_copied=False, native_input_requested=with_input, initial_topology=topology,
                       capture_cursor_mode=cursor_mode, native_text_requested=text_socket is not None)
        journal['display_socket'] = str(display_socket) if display_socket is not None else None
        journal['preserve_display_session'] = preserve_display_session
        journal['pending_native_ime'] = pending_native_ime
        journal['embedded_cursor_adapter'] = bool(with_input and cursor_mode == 'embedded' and verified.get('native_embedded_cursor_included'))
        state_tools.write_private(directory / 'journal.json', journal)
        if continuous:
            state_tools.write_private(state_parent / 'active-runtime.json', dict(directory=directory.name))
        for index, (destination, target) in enumerate(aliases):
            # Persist inode ownership BEFORE exposing the loader link. Hard
            # linking a symlink preserves its identity and refuses replacement.
            staged = directory / ('loader-' + str(index))
            staged.symlink_to(target)
            info = staged.lstat()
            journal['aliases'][index]['identity'] = [info.st_dev, info.st_ino]
            state_tools.write_private(directory / 'journal.json', journal)
            created_aliases.append((destination, target, (info.st_dev, info.st_ino)))
            os.link(staged, destination, follow_symlinks=False)
            staged.unlink()
        env = dict(worker_environment(), WINEPREFIX=str(prefix), WINELOADER=WINE, WINEDEBUG='-all,err+module',
                   UURB_NATIVE_BOOTSTRAP='1', WINEDLLOVERRIDES='mscoree,mshtml=')
        wine_locale(env)
        for name in ('WINEDLLPATH', 'UURB_DXGI_CAPTURE_FD', 'UURB_DXGI_CAPTURE_SOCKET', 'UURB_DXGI_CAPTURE_OUTPUT',
                     'UURB_NVENC_SYNTHETIC_OUTPUT', 'VK_INSTANCE_LAYERS', 'DXVK_CONFIG', 'UURB_NATIVE_INPUT_ENABLED',
                     'UURB_X11_INPUT_PORT', 'UURB_X11_INPUT_TOKEN', 'UURB_X11_INPUT_SEMANTIC_ONLY',
                     'UURB_CURSOR_STATE_PATH', 'UURB_DISPLAY_SOCKET'):
            env.pop(name, None)
        if display_socket is not None:
            env['UURB_DISPLAY_SOCKET'] = str(display_socket)
        cursor_args = []
        if cursor_mode in ('metadata', 'composited') or journal['embedded_cursor_adapter']:
            cursor_path = directory / 'cursor.state'
            env['UURB_CURSOR_STATE_PATH'] = winpath(cursor_path)
            cursor_args = ['--cursor-state', str(cursor_path)]
        if is_stopping():
            return
        if with_input:
            input_token = secrets.token_hex(32)
            with (directory / 'input.token').open('x') as output:
                os.chmod(directory / 'input.token', 0o600)
                output.write(input_token)
                input_token_info = os.fstat(output.fileno())
            # No credential in command arguments, environment, journal or log.
            # sudo opens only the two uinput descriptors; helper drops root
            # before device configuration and listening.
            password = getpass.getpass('sudo（仅打开原生输入设备）: ') if input_access == 'sudo' else None
            try:
                with (directory / 'input.log').open('w') as log:
                    native_input = subprocess.Popen([*(['/usr/bin/sudo', '-S', '-p', ''] if input_access == 'sudo' else []),
                        str(directory / 'uu-native-input'), '--user', pwd.getpwuid(os.getuid()).pw_name,
                        '--token-file', str(directory / 'input.token'), '--seconds', str(0 if continuous else min(duration + 60, 3600)),
                        '--single-output', *(['--text-socket', str(text_socket)] if text_socket is not None else []),
                        *(['--settings-file', str(Path.home() / '.config/uurb/input-settings.json')]
                          if verified.get('native_input_settings_included') else [])],
                        stdin=subprocess.PIPE if password is not None else subprocess.DEVNULL,
                        stdout=log, stderr=log, start_new_session=True, env=worker_environment())
                    if password is not None:
                        native_input.stdin.write((password + '\n').encode())
                        native_input.stdin.close()
            finally:
                password = None
            deadline = time.monotonic() + 8
            port = None
            while not port:
                port = native_input_ready(directory / 'input.log')
                if is_stopping():
                    return
                if native_input.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('Native input helper did not become ready')
                if not port:
                    time.sleep(0.05)
            current = (directory / 'input.token').lstat()
            if (current.st_dev, current.st_ino) != (input_token_info.st_dev, input_token_info.st_ino):
                raise RuntimeError('Trial input token file was replaced during startup')
            # Helper has consumed it. Do not retain a credential in the trial
            # journal directory for the remaining runtime or later inspection.
            (directory / 'input.token').unlink()
            env.update(UURB_NATIVE_INPUT_ENABLED='1', UURB_X11_INPUT_PORT=str(port),
                       UURB_X11_INPUT_TOKEN=input_token,
                       UU_INPUT_BROKER_LOG=winpath(directory / 'input-broker.log'))
            scope = 'single_output_physical_events_only'
            if text_socket is not None:
                scope = ('single_output_physical_unicode_and_witnessed_prefix_revisions'
                         if verified.get('native_text_revisions_included')
                         else 'single_output_physical_and_pure_unicode_commits')
            event('native_input_ready', input_scope=scope)
            if native_topology() != topology:
                raise RuntimeError('Display layout changed during native input startup')
        if is_stopping():
            return
        with (directory / 'capture.log').open('w') as capture_log, (directory / 'wine.log').open('w') as wine_log:
            broker = subprocess.Popen(['/usr/bin/python3', str(ROOT / 'scripts/uu-native-capture-broker.py'),
                '--socket', str(endpoint), '--restore-state', str(restore_state), '--cursor-mode', cursor_mode,
                '--capture-binary', str(bundle / 'capture/uu-pipewire-native-probe'),
                *(['--display-reset-state', str(directory / 'display-reset.json')] if preserve_display_session else []),
                '--duration-seconds', str(0 if continuous else duration), *cursor_args],
                stdout=capture_log, stderr=capture_log, start_new_session=True, env=worker_environment())
            deadline = time.monotonic() + 5
            while not endpoint.exists():
                if is_stopping():
                    return
                if broker.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('Native capture broker did not start')
                time.sleep(0.05)
            if is_stopping():
                return
            geometry = topology['modes'][0] if topology else dict(width=1920, height=1080)
            screen = f"--server-args=-screen 0 {geometry['width']}x{geometry['height']}x24 -nolisten tcp"
            launcher = subprocess.Popen(['xvfb-run', '-a', screen,
                WINE, *arguments], env=env, cwd=app, stdout=wine_log, stderr=wine_log, start_new_session=True)
            event('native_runtime_started' if continuous else 'native_trial_started', state_directory=str(directory), release_id=verified['release_id'],
                  uu_session_tested=False, capture_cursor_mode=cursor_mode)
            deadline = float('inf') if continuous else time.monotonic() + duration
            startup_deadline = time.monotonic() + 60
            ready_events = set()
            required_events = {'gui_started', 'service_bootstrap_accepted'}
            if with_input:
                required_events.add('native_input_hook_ready')
            notified_ready = False
            watchdog_at = 0
            failure = None
            cursor = [0]
            topology_check = time.monotonic()
            topology_observed_ns = time.monotonic_ns()
            display_confirmation = confirmation_tools.DisplayConfirmation(directory, prefix,
                app / 'bin/GameViewerServer.exe', bundle, topology, state_tools.read_private) if display_socket else None
            while not is_stopping() and time.monotonic() < deadline:
                for item in boot_events(directory / 'wine.log', cursor):
                    event('bootstrap', detail=item)
                    if item['code'] == 0:
                        ready_events.add(item['event'])
                if required_events <= ready_events and not notified_ready:
                    notified_ready = True
                    state_tools.notify('READY=1\nSTATUS=UU native bridge workers ready; remote acceptance separate')
                if not notified_ready and time.monotonic() >= startup_deadline:
                    failure = 'Native service bootstrap readiness timed out'
                    break
                if time.monotonic() >= watchdog_at:
                    state_tools.notify('WATCHDOG=1')
                    watchdog_at = time.monotonic() + 5
                if launcher.poll() is not None or broker.poll() is not None:
                    event('native_worker_exited', launcher_exit=launcher.poll(), broker_exit=broker.poll())
                    failure = 'Native service worker exited'
                    break
                if native_input is not None:
                    if native_input.poll() is not None:
                        event('native_input_exited_stopping_owned_trial')
                        failure = 'Native input worker exited'
                        break
                    if time.monotonic() >= topology_check:
                        observation_ns = time.monotonic_ns()
                        observed_topology = native_topology()
                        if observed_topology != topology:
                            if preserve_display_session and can_recreate_display_in_place(topology, observed_topology):
                                # Target-aware bounded fallback: UU may already
                                # have recreated video before this topology poll.
                                if display_confirmation:
                                    display_confirmation.reset_for_topology(observed_topology, topology_observed_ns)
                                state_tools.write_private(directory / 'display-reset.json', dict(version=1,
                                    **observed_topology['modes'][0], after_ns=topology_observed_ns,
                                    requested_ns=time.monotonic_ns(), require_fresh=topology['modes'] == observed_topology['modes']))
                                broker.send_signal(signal.SIGUSR1)
                                topology = observed_topology
                                event('display_mode_changed_recreating_video', generation=topology['generation'],
                                      uu_process_retained=True, remote_recovery_verified=False,
                                      remote_recovery_observation='unavailable_without_vendor_ack')
                            else:
                                event('display_layout_changed_stopping_for_input_remap')
                                failure = 'Native display remap requires restart'
                                break
                        topology_observed_ns = observation_ns
                        topology_check = time.monotonic() + 1
                if any(not path.is_symlink() or os.readlink(path) != str(target) for path, target in aliases):
                    event('component_changed_stopping_for_upgrade_review')
                    failure = 'Native components changed; upgrade review required'
                    break
                if display_confirmation:
                    result = display_confirmation.tick(notified_ready, display_socket)
                    if result:
                        event('display_frame_confirmation', result=result, remote_presentation_verified=False,
                              remote_presentation_observation='local_gpu_ack_only')
                time.sleep(0.25)
            if failure is not None:
                raise RuntimeError(failure)
    finally:
        try:
            state_tools.notify(('STOPPING=1' if stop_requested is None else 'WATCHDOG=1') +
                               '\nSTATUS=Cleaning up owned UU runtime')
            failures = cleanup_trial(launcher, broker, env, directory, arguments, token, created_aliases, native_input)
            if failures:
                raise RuntimeError('Native trial cleanup incomplete; inspect its private cleanup.json')
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            os.close(lock_fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--restore-state', type=Path, required=True)
    parser.add_argument('--state-parent', type=Path, required=True)
    parser.add_argument('--trial-seconds', type=int, default=1800)
    parser.add_argument('--continuous', action='store_true', help='Supervised lifetime, no one-hour cutoff; requires --input-access user')
    parser.add_argument('--input-access', choices=['sudo', 'user'], default='sudo')
    parser.add_argument('--native-input', action='store_true', help='Single-output native physical input; needs sudo for uinput')
    parser.add_argument('--cursor-mode', choices=['embedded', 'hidden', 'metadata', 'composited'], default='embedded',
                        help='Use hidden only when the UU controller supplies its own visible pointer')
    parser.add_argument('--text-socket', type=Path, help='Separately authorized native text daemon endpoint')
    parser.add_argument('--display-socket', type=Path, help='Independent native display guardian endpoint')
    parser.add_argument('--preserve-display-session', action='store_true',
                        help='Candidate: recreate video only for same-output pixel-mode changes; requires geometry-aware bundle')
    args = parser.parse_args()
    try:
        run(args.prefix, args.bundle, args.restore_state, args.state_parent, None if args.continuous else args.trial_seconds,
            args.native_input, args.cursor_mode, args.text_socket, args.input_access, args.display_socket, args.preserve_display_session)
    except Exception as error:
        event('native_runtime_failed', error_type=type(error).__name__)
        raise SystemExit(1)
