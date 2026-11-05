"""Distribution details shared by setup and the immutable runtime.

New distribution packages carry package-info.json and a private, versioned Wine
installation.  Never select the rolling distribution's arbitrary Wine version.
"""
import json
from pathlib import Path
import re

PACKAGE_ROOT = Path('/usr/lib/uuway')
PRIVATE_WINE = Path('/opt/uuway-wine11/bin')


def os_release(path=Path('/etc/os-release')):
    try:
        return {key: value.strip().strip('\"\'') for line in path.read_text().splitlines()
                if not line.startswith('#') and '=' in line
                for key, value in [line.split('=', 1)]}
    except OSError:
        return {}


def package_info(root=None):
    try:
        value = ((root or PACKAGE_ROOT) / 'package-info.json').read_text()
        data = json.loads(value)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def wine_directory():
    return PRIVATE_WINE if (PRIVATE_WINE / 'wine').is_file() else Path('/opt/wine-stable/bin')


def wine_compatible(version):
    # 11.01 and 11.0-rc1 are not the Wine 11.0 server protocol we build against.
    return isinstance(version, str) and re.fullmatch(r'wine-11\.0(?:\.[0-9]+)?', version) is not None


def winehq_source(release=None):
    release = os_release() if release is None else release
    distro = release.get('ID')
    codename = release.get('VERSION_CODENAME', '')
    allowed = {'ubuntu': {'noble', 'resolute'}, 'debian': {'trixie'}}
    if codename not in allowed.get(distro, set()):
        raise ValueError('当前系统没有自动安装 WineHQ 的适配，请安装配套的 uuway-wine11 包。')
    name = f'winehq-{codename}.sources'
    return name, f'https://dl.winehq.org/wine-builds/{distro}/dists/{codename}/{name}'


def uninstall_command(release=None):
    release = os_release() if release is None else release
    if release.get('ID') == 'fedora':
        return 'sudo dnf remove uuway'
    if release.get('ID') == 'arch' or 'arch' in release.get('ID_LIKE', '').split():
        return 'sudo pacman -R uuway'
    return 'sudo apt remove uuway'


def ota_supported(root=None):
    info = package_info(root)
    # Legacy Ubuntu packages have no metadata. New packages must opt in.
    return info.get('ota', False) if info else os_release().get('ID') == 'ubuntu'
