#!/usr/bin/python3
"""Install the owned native settings app and menu entry; never restart services."""
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
from native_launcher_migration import retire_legacy_launchers

ROOT = Path(__file__).resolve().parents[1]
MARKER = '# Managed by UURB native settings installer\n'

def run():
    binary = ROOT / 'native/settings/target/release/uurb-settings'
    info = binary.lstat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size < 32 * 1024 * 1024:
        raise ValueError('Build the native settings application first')
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    parent = Path.home() / '.local/lib/uurb-settings'
    menu = Path.home() / '.local/share/applications'
    for path in (parent, menu):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError('Unsafe settings installation directory')
    release = parent / digest
    if not release.exists():
        with tempfile.TemporaryDirectory(prefix='.settings-stage-', dir=parent) as name:
            staged = Path(name) / 'release'; staged.mkdir(mode=0o700)
            target = staged / 'uurb-settings'; shutil.copyfile(binary, target); target.chmod(0o700)
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError('Settings build changed during installation')
            staged.rename(release)
    target = release / 'uurb-settings'
    if release.is_symlink() or target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise ValueError('Changed settings release; refusing replacement')
    if any(c in str(target) for c in ('\n', '\r', '"', '\\', '%', '`', '$')):
        raise ValueError('Unsupported desktop command path')
    desktop = menu / 'uurb-settings.desktop'
    if desktop.exists() or desktop.is_symlink():
        info = desktop.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not desktop.read_text().startswith(MARKER):
            raise ValueError('Refusing to replace an unrelated menu entry')
    content = MARKER + ('[Desktop Entry]\nType=Application\nName=UUWay 控制台\n'
        'Keywords=UU;Bridge;设置;控制台;\n'
        'Comment=UU 原生 Linux 客户端：服务状态、输入和光标控制\n'
        f'Exec="{target}"\nIcon=preferences-desktop-peripherals\nTerminal=false\n'
        'Categories=Settings;Utility;\nStartupNotify=true\n')
    fd, name = tempfile.mkstemp(prefix='.uurb-settings-', dir=menu)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.chmod(name, 0o644); os.replace(name, desktop)
    finally:
        if os.path.exists(name): os.unlink(name)
    migration = retire_legacy_launchers()
    print('Native client installed; menu entry: UUWay 控制台; no service restarted')
    if migration['changed']:
        print('Legacy launchers retired with backup: ' + migration['backup'])
        print('Run systemctl --user daemon-reload to load the staged legacy-service guards')

if __name__ == '__main__': run()
