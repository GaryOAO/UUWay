"""Distribution details shared by setup and the immutable runtime.

New distribution packages carry package-info.json and a private, versioned Wine
installation.  Never select the rolling distribution's arbitrary Wine version.
"""
import json
from pathlib import Path
import re

PACKAGE_ROOT = Path('/usr/lib/uuway')
PRIVATE_WINE = Path('/opt/uuway-wine11/bin')
# Only these AMD files link FFmpeg. Fedora's shared RPM carries both ABIs.
FEDORA_AMD_VARIANTS = frozenset(('uurb-nvenc-encode.dll.so', 'uu-pipewire-native-probe', 'uu-vaapi-probe'))


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
    if is_arch():
        return Path.home() / '.local/share/uuway/wine11/usr/bin'
    if os_release().get('ID') == 'fedora' and os_release().get('VERSION_ID') == '45':
        return Path('/opt/uuway-wine11/usr/bin')
    return PRIVATE_WINE if (PRIVATE_WINE / 'wine').is_file() else Path('/opt/wine-stable/bin')


def is_arch():
    release = os_release()
    return release.get('ID') == 'arch' or 'arch' in release.get('ID_LIKE', '').split()


def wine_pin_path():
    return Path('/etc/dnf/versionlock.toml') if os_release().get('ID') == 'fedora' else Path('/etc/apt/preferences.d/uuway-wine')


def private_wine():
    return is_arch() or wine_directory().is_relative_to(Path('/opt/uuway-wine11'))


def backend_file(root, backend, name, release=None):
    root = Path(root)
    release = os_release() if release is None else release
    if backend == 'amd' and name in FEDORA_AMD_VARIANTS and release.get('ID') == 'fedora' and release.get('VERSION_ID') == '45':
        variant = root / 'build/compat/fedora45' / name
        if variant.is_file():
            return variant
    return root / 'build/backends' / backend / 'native-presenter' / name


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
    return (info.get('ota') is True and info.get('target') in ('ubuntu', 'debian-13', 'fedora', 'fedora-44', 'fedora-45', 'arch')
            if info else os_release().get('ID') == 'ubuntu')
