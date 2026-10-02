"""Retire only recognized old UU launchers; preserve originals and live services."""
import configparser
import os
from pathlib import Path
import stat
import tempfile

MARKER = '# Managed by UURB native launcher migration\n'
GUARD = MARKER + '[Unit]\nConditionPathExists=!%h/.config/uurb/native-runtime.json\n'
RETIRED = (MARKER + '[Desktop Entry]\nType=Application\nName=UU 远程（旧入口，已停用）\n'
           'Exec=/usr/bin/gtk-launch uuway\nHidden=true\nNoDisplay=true\nTerminal=false\n')
UNITS = ('uu-remote-bridge', 'uu-remote-console', 'uu-shared-physical-vnc')


def directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('Unsafe native launcher directory')


def safe_tree(home, path):
    directory(home)
    current = home
    for part in path.relative_to(home).parts:
        current /= part
        directory(current)


def owned_text(path):
    if not path.exists() and not path.is_symlink():
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'r') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_size > 16384:
            raise ValueError('Unsafe existing native launcher file')
        text = stream.read(16385)
        if len(text) > 16384:
            raise ValueError('Oversized native launcher file')
        return text


def recognized_menu(text, home, wine):
    if text == RETIRED or text.startswith(('# Retired UURB launcher:', '# Retired direct Wine launcher;')):
        return True
    ini = configparser.ConfigParser(interpolation=None)
    try:
        ini.read_string(text)
        entry = ini['Desktop Entry']
        command = entry['Exec']
        if not wine:
            return command == str(home / '.local/bin/uu-remote') + ' open'
        return (entry.get('StartupWMClass', '').lower() == 'gameviewer.exe' and
                command.startswith('env "WINEPREFIX=' + str(home / '.local/share/wineprefixes/uu-remote') + '" wine ') and
                entry.get('Path') == str(home / '.local/share/wineprefixes/uu-remote/drive_c/Program Files/Netease/GameViewer'))
    except (configparser.Error, KeyError):
        return False


def atomic_text(path, text):
    fd, name = tempfile.mkstemp(prefix='.uurb-launcher-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
        parent_fd = os.open(path.parent, os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def retire_legacy_launchers(home=None):
    home = Path.home() if home is None else Path(home)
    # Configuration presence, not service liveness, controls ownership. A
    # stopped native service must not let an old launcher take its prefix.
    if not (home / '.config/uurb/native-runtime.json').exists():
        return dict(changed=0, backup=None, daemon_reload_required=False)
    safe_tree(home, home / '.config/uurb')
    owned_text(home / '.config/uurb/native-runtime.json')
    menu = home / '.local/share/applications'
    unit_dir = home / '.config/systemd/user'
    targets = [(menu / 'uu-remote.desktop', False), (menu / 'wine/Programs/UU远程.desktop', True)]
    changes = []
    for path, wine in targets:
        # Check each existing parent instead of following a vendor-created
        # symlink to an unrelated application tree.
        safe_tree(home, path.parent)
        old = owned_text(path)
        if old is not None and not recognized_menu(old, home, wine):
            raise ValueError('Unrecognized UU menu entry; preserving it')
        if old != RETIRED:
            changes.append((path, old, RETIRED))
    safe_tree(home, unit_dir)
    for unit in UNITS:
        parent = unit_dir / (unit + '.service.d')
        directory(parent)
        path = parent / '99-uurb-native-guard.conf'
        old = owned_text(path)
        # Also adopt the exact minimal guards installed during incident recovery.
        previous = ('[Unit]\nConditionPathExists=!%h/.config/uurb/native-runtime.json\n',
                    '# Do not launch the obsolete relay against the native runtime\'s Wine prefix.\n'
                    '[Unit]\nConditionPathExists=!%h/.config/uurb/native-runtime.json\n')
        if old is not None and old != GUARD and old not in previous:
            raise ValueError('Unrecognized legacy service guard; preserving it')
        if old != GUARD:
            changes.append((path, old, GUARD))
    if not changes:
        return dict(changed=0, backup=None, daemon_reload_required=False)
    backup_parent = home / '.local/state/uurb'
    safe_tree(home, backup_parent)
    backup = Path(tempfile.mkdtemp(prefix='launcher-migration-', dir=backup_parent))
    for path, old, new in changes:
        if owned_text(path) != old:
            raise ValueError('Launcher changed during migration; preserving newer file')
        if old is not None:
            saved = backup / path.relative_to(home)
            directory(saved.parent)
            atomic_text(saved, old)
        atomic_text(path, new)
    return dict(changed=len(changes), backup=str(backup), daemon_reload_required=True)
