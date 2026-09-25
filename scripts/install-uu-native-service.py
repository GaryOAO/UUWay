#!/usr/bin/python3
"""Stage user-owned native service config; never starts/stops a live service.

System udev access is a separate explicit administrator installation. Existing
non-UURB unit files/configurations are not overwritten. No RDP unit is touched.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import stat
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_install_trial', ROOT / 'scripts/uu-native-trial.py')
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)
MARKER = '# Managed by UURB native installer;'


def service_root(name, bundle, verified):
    # Text owns live clipboard bytes; the guardian owns rollback deadlines.
    # Do not migrate/restart either implicitly during a video-only upgrade.
    return bundle.resolve() if name == 'uu-native-bridge.service' and verified.get('native_main_scripts_included') else ROOT


def existing_display_policy(content):
    """Preserve only the managed main ExecStart option, never comments/drop-ins."""
    section = None
    commands = []
    for line in content.splitlines():
        if line.startswith('[') and line.endswith(']'):
            section = line
        elif section == '[Service]' and line.startswith('ExecStart='):
            commands.append(shlex.split(line[len('ExecStart='):]))
    if len(commands) != 1:
        raise ValueError('Unrecognized managed main service command')
    command = commands[0]
    if (len(command) not in (4, 5) or command[0] != '/usr/bin/python3' or
            not command[1].endswith('/scripts/uu-native-service.py') or
            command[2:4] != ['--config', '%h/.config/uurb/native-runtime.json'] or
            (len(command) == 5 and command[4] != '--preserve-display-session')):
        raise ValueError('Unrecognized managed main service options; preserving existing file')
    return len(command) == 5


def install(bundle, prefix, restore_state, state_parent, text_socket, cursor_mode=None, preserve_display_session=None):
    verified = trial.bundle_tools.verify(bundle)
    if not verified.get('native_service_lifetime_included'):
        raise ValueError('Expected a continuous-lifetime capable native bundle')
    for directory in (prefix, restore_state.parent, state_parent, text_socket.parent):
        trial.private_directory(directory)
    # Fixed schema prevents shell/environment injection; never store credentials.
    config = dict(schema=1, prefix=str(prefix.resolve()), bundle=str(bundle.resolve()),
                  restore_state=str(restore_state.absolute()), state_parent=str(state_parent.resolve()),
                  text_socket=str(text_socket.absolute()), cursor_mode=cursor_mode)
    config_dir = Path.home() / '.config/uurb'
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    trial.private_directory(config_dir)
    config_path = config_dir / 'native-runtime.json'
    if config_path.exists() or config_path.is_symlink():
        previous = trial.state_tools.read_private(config_path)
        if set(previous) != set(config) or previous.get('schema') != 1:
            raise ValueError('Refusing an unrecognized service configuration')
        if cursor_mode is None:
            cursor_mode = previous.get('cursor_mode')
    elif cursor_mode is None:
        cursor_mode = 'metadata'
    if (cursor_mode not in ('embedded', 'metadata', 'composited') or
            (cursor_mode == 'embedded' and not verified.get('native_embedded_cursor_included')) or
            (cursor_mode == 'composited' and not verified.get('native_composited_cursor_included'))):
        raise ValueError('Unreviewed service cursor policy or incompatible bundle')
    config['cursor_mode'] = cursor_mode
    unit_dir = Path.home() / '.config/systemd/user'
    unit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = unit_dir.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('Unsafe user unit directory')
    units = [unit_dir / ('uu-native-' + name + '.service') for name in ('text', 'display', 'bridge')]
    old_display_policy = False
    for unit in units:
        if unit.exists() or unit.is_symlink():
            info = unit.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_mode & 0o022 or info.st_size > 16384):
                raise ValueError('Refusing an unrecognized user service')
            content = unit.read_text()
            if not content.startswith(MARKER):
                raise ValueError('Refusing an unrecognized user service')
            if unit.name == 'uu-native-bridge.service':
                old_display_policy = existing_display_policy(content)
    if preserve_display_session is None:
        preserve_display_session = old_display_policy
    if type(preserve_display_session) is not bool:
        raise ValueError('Display preservation must be an explicit boolean')
    if preserve_display_session and not verified.get('native_display_geometry_included'):
        raise ValueError('Cannot preserve display session with an incompatible bundle')
    roots = {unit: str(service_root(unit.name, bundle, verified)) for unit in units}
    if any(any(c in root for c in ('\n', '\r', '"', '\\')) for root in roots.values()):
        raise ValueError('Unsupported service installation path')
    trial.state_tools.write_private(config_path, config)
    for unit in units:
        content = (ROOT / 'systemd' / (unit.name + '.in')).read_text().replace('@ROOT@', roots[unit].replace('%', '%%'))
        content = content.replace('@DISPLAY_POLICY@', ' --preserve-display-session' if preserve_display_session else '')
        fd, temporary = tempfile.mkstemp(prefix='.uurb-service-', dir=unit_dir)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, unit)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print(json.dumps(dict(event='native_service_staged', units=[str(unit) for unit in units], release_id=verified['release_id'],
                          active_service_changed=False, system_permissions_changed=False)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bundle', 'prefix', 'restore-state', 'state-parent', 'text-socket'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--cursor-mode', choices=['metadata', 'embedded', 'composited'],
                        help='Keep the saved cursor mode when omitted (fresh install: metadata)')
    parser.add_argument('--preserve-display-session', action=argparse.BooleanOptionalAction, default=None,
                        help='Keep the installed main-unit policy when omitted; explicit enable requires geometry support')
    args = parser.parse_args()
    install(args.bundle, args.prefix, args.restore_state, args.state_parent, args.text_socket, args.cursor_mode,
            args.preserve_display_session)
