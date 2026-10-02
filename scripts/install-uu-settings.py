#!/usr/bin/python3
"""Install the Python/GTK UUWay console and its menu entry."""
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile

from native_launcher_migration import retire_legacy_launchers

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/uuway_console.py"
PENGUIN = ROOT / "assets/uuway-penguin.bmp"
MARKER = "# Managed by UUWay console installer\n"
OLD_MARKER = "# Managed by UURB native settings installer\n"


def _safe_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError(f"Unsafe console installation directory: {path}")


def run():
    info = SOURCE.lstat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size < 2 * 1024 * 1024:
        raise ValueError("Build the UUWay console first")
    if not PENGUIN.is_file() or PENGUIN.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Missing UUWay penguin brand image")
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    parent = Path.home() / ".local/lib/uuway-console"
    menu = Path.home() / ".local/share/applications"
    _safe_directory(parent); _safe_directory(menu)
    release = parent / digest
    if not release.exists():
        with tempfile.TemporaryDirectory(prefix=".uuway-stage-", dir=parent) as name:
            staged = Path(name) / "release"
            (staged / "assets").mkdir(mode=0o700, parents=True)
            target = staged / "uuway-console"
            shutil.copyfile(SOURCE, target); target.chmod(0o700)
            shutil.copyfile(PENGUIN, staged / "assets/uuway-penguin.bmp")
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError("UUWay console changed during installation")
            staged.rename(release)
    target = release / "uuway-console"
    if (release.is_symlink() or target.is_symlink() or
            hashlib.sha256(target.read_bytes()).hexdigest() != digest):
        raise ValueError("Changed UUWay console release; refusing replacement")
    if any(c in str(target) for c in ("\n", "\r", '"', "\\", "%", "`", "$")):
        raise ValueError("Unsupported desktop command path")
    desktop = menu / "uuway.desktop"
    if desktop.exists() or desktop.is_symlink():
        info = desktop.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not desktop.read_text().startswith(MARKER):
            raise ValueError("Refusing to replace an unrelated menu entry")
    old_desktop = menu / "uurb-settings.desktop"
    if old_desktop.exists() or old_desktop.is_symlink():
        info = old_desktop.lstat()
        if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and
                old_desktop.read_text().startswith(OLD_MARKER)):
            old_desktop.unlink()
    content = MARKER + (
        "[Desktop Entry]\nType=Application\nName=UUWay 控制台\n"
        "Keywords=UU;UUWay;Bridge;设置;控制台;\n"
        "Comment=UUWay Linux 原生 bridge：服务、输入、显示和文件接收\n"
        f"Exec=\"{target}\"\nIcon=preferences-desktop-peripherals\n"
        "Terminal=false\nCategories=Settings;Utility;\nStartupNotify=true\n")
    fd, name = tempfile.mkstemp(prefix=".uuway-console-", dir=menu)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.chmod(name, 0o644); os.replace(name, desktop)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    migration = retire_legacy_launchers()
    print("UUWay 控制台已安装；菜单入口：UUWay 控制台；未重启服务")
    if migration["changed"]:
        print("旧 UU 入口已停用并备份：" + migration["backup"])
        print("运行 systemctl --user daemon-reload 以加载旧服务保护配置")


if __name__ == "__main__":
    run()
