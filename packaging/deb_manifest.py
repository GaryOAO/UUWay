#!/usr/bin/python3
"""Everything the uuway .deb installs, in one place.

``stage-deb.py`` builds the package tree from this list and
``tests/test_deb_layout.py`` checks it against what ``package-uu-native-runtime.py``
reads, so a new runtime input cannot be forgotten.

The tree under /usr/lib/uuway has the same shape as a repository checkout
(scripts/, assets/, config/, systemd/, build/...).  The runtime tools compute
their root from ``__file__`` and expect that shape; ``uuway setup`` then packs
a per-user, user-owned bundle out of it (the capture producer must belong to
the user that runs it, so the bundle cannot live in the root-owned /usr tree).
"""

from pathlib import Path

LIB = 'usr/lib/uuway'

# Runtime Python only.  Build, audit and stage tools stay out of the package.
RUNTIME_SCRIPTS = (
    'configure-uu-wine-mappings.py',
    'install-uu-native-service.py',
    'native_display_config.py',
    'native_display_confirmation.py',
    'native_display_guardian.py',
    'native_launcher_migration.py',
    'native_runtime_state.py',
    'native_text_portal.py',
    'native_text_revision.py',
    'package-uu-native-runtime.py',
    'probe-wayland-portal.py',
    'uu-native-capture-broker.py',
    'uu-native-display-service.py',
    'uu-native-service.py',
    'uu-native-text-service.py',
    'uu-native-text.py',
    'uu-native-trial.py',
    'uuway_cli.py',
    'uuway_console.py',
)

SYSTEMD_TEMPLATES = (
    'uu-native-bridge.service.in',
    'uu-native-display.service.in',
    'uu-native-text.service.in',
)

# Executables the user runs by name; each is a symlink into the tree.
COMMANDS = {
    'usr/bin/uuway': '../lib/uuway/scripts/uuway_cli.py',
    'usr/bin/uuway-console': '../lib/uuway/scripts/uuway_console.py',
}

# Built artifacts that package-uu-native-runtime.py packs into the bundle
# (its INPUTS, INPUT_RUNTIME and PINNED_CAPTURE), plus the consent probe that
# probe-wayland-portal.py uses by default.
NATIVE_PRESENTER = (
    'uurb-dxgi-capture-loader.dll',
    'uurb-dxgi-capture.dll.so',
    'uurb-nvenc-encode-loader.dll',
    'uurb-nvenc-encode.dll.so',
    'uu-native-bootstrap.exe',
    'uu-native-winlogon.exe',
    'uu-native-input',
    'uu-native-input-broker.exe',
    'uu-native-input-bridge.dll',
    'uu-native-input-injector.exe',
    'uurb-native-display-loader.dll',
    'uurb-native-display.dll.so',
    'uu-pipewire-native-probe',
    'uu-pipewire-dmabuf-probe',
)
EXECUTABLE_ELF = {'uu-native-input', 'uu-pipewire-native-probe', 'uu-pipewire-dmabuf-probe'}

DXVK_STAGE = ('dxgi.dll', 'd3d11.dll', 'DXVK_LICENSE')

# Copied verbatim into <prefix>/compat/ by `uuway setup`; install.sh copies the
# whole directory with a glob, so it must hold exactly these five files.
HELPERS = (
    'uu-clipboard-bridge',
    'uu-clipboard-files.exe',
    'uu-conpty.dll',
    'uu-terminal-bridge',
    'uu-terminal-proxy.exe',
)
EXECUTABLE_HELPERS = {'uu-clipboard-bridge', 'uu-terminal-bridge'}


def script_mode(name):
    """Executable only when the script can be run by itself (it has a shebang)."""
    try:
        with (Path(__file__).resolve().parents[1] / 'scripts' / name).open('rb') as handle:
            return 0o755 if handle.read(2) == b'#!' else 0o644
    except OSError:
        return 0o644


def files():
    """Return (source relative to the repo, destination relative to /, mode)."""
    entries = []
    for name in RUNTIME_SCRIPTS:
        entries.append((f'scripts/{name}', f'{LIB}/scripts/{name}', script_mode(name)))
    for name in SYSTEMD_TEMPLATES:
        entries.append((f'systemd/{name}', f'{LIB}/systemd/{name}', 0o644))
    entries += [
        ('assets/uuway-penguin.bmp', f'{LIB}/assets/uuway-penguin.bmp', 0o644),
        ('assets/uuway-icon.svg', f'{LIB}/assets/uuway-icon.svg', 0o644),
        ('assets/uuway-icon.svg', 'usr/share/icons/hicolor/scalable/apps/uuway.svg', 0o644),
        ('config/dxvk-native.conf', f'{LIB}/config/dxvk-native.conf', 0o644),
        ('config/70-uurb-native-input.rules', 'usr/lib/udev/rules.d/70-uurb-native-input.rules', 0o644),
        ('build/native-ime/libuurb-native-ime.so', f'{LIB}/fcitx5/libuurb-native-ime.so', 0o644),
        ('packaging/deb/uuway.desktop', 'usr/share/applications/uuway.desktop', 0o644),
        ('config/uuway-wine.preferences', f'{LIB}/config/uuway-wine.preferences', 0o644),
        ('packaging/deb/lintian-overrides', 'usr/share/lintian/overrides/uuway', 0o644),
        ('LICENSE', 'usr/share/doc/uuway/LICENSE', 0o644),
        ('NOTICE', 'usr/share/doc/uuway/NOTICE', 0o644),
        ('packaging/deb/copyright', 'usr/share/doc/uuway/copyright', 0o644),
    ]
    for name in DXVK_STAGE:
        entries.append((f'build/dxvk-capture/stage/{name}', f'{LIB}/build/dxvk-capture/stage/{name}', 0o644))
    for name in NATIVE_PRESENTER:
        mode = 0o755 if name in EXECUTABLE_ELF else 0o644
        entries.append((f'build/native-presenter/{name}', f'{LIB}/build/native-presenter/{name}', mode))
    for name in HELPERS:
        mode = 0o755 if name in EXECUTABLE_HELPERS else 0o644
        entries.append((f'build/helpers/{name}', f'{LIB}/build/helpers/{name}', mode))
    return entries


def sources():
    return {source for source, _, _ in files()}
