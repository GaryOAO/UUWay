#!/usr/bin/env bash
# Package installation and headless checks. A GPU/desktop session is tested separately.
set -Eeuo pipefail
target="${1:?target}" output="${2:?output directory}"
case "$target" in
    debian-13)
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y --no-install-recommends "$output"/*.deb
        ;;
    fedora-44|fedora-45) dnf install -y --setopt=install_weak_deps=False "$output"/*.rpm ;;
    arch)
        pacman -Syu --noconfirm
        pacman -U --noconfirm "$output"/*.pkg.tar.zst
        ;;
    *) exit 2 ;;
esac
uuway --version
uuway --help >/dev/null
uuway-console --status >/dev/null
useradd -m uuway-test
if [[ "$target" == arch ]]; then
    runuser -u uuway-test -- python3 /usr/lib/uuway/scripts/uuway_wine_archive.py
    wine_bin=/home/uuway-test/.local/share/uuway/wine11/usr/bin
else
    bash /source/scripts/install-wine11.sh "$target"
    wine_bin=/opt/wine-stable/bin
    [[ "$target" != fedora-45 ]] || wine_bin=/opt/uuway-wine11/usr/bin
fi
"$wine_bin/wine" --version | grep -Fx wine-11.0
timeout 120 runuser -u uuway-test -- env WINEPREFIX=/home/uuway-test/wine-smoke WINEDEBUG=-all \
    WINEDLLOVERRIDES=mscoree,mshtml= xvfb-run -a "$wine_bin/wine" cmd /c echo UUWAY_WINE64_OK | tr -d '\r' | grep -Fx UUWAY_WINE64_OK
timeout 60 runuser -u uuway-test -- env WINEPREFIX=/home/uuway-test/wine-smoke WINEDEBUG=-all \
    WINEDLLOVERRIDES=mscoree,mshtml= xvfb-run -a "$wine_bin/wine" \
    'C:\windows\syswow64\cmd.exe' /c echo UUWAY_WINE32_OK | tr -d '\r' | grep -Fx UUWAY_WINE32_OK
runuser -u uuway-test -- env WINEPREFIX=/home/uuway-test/wine-smoke "$wine_bin/wineserver" -k || true
timeout 30 runuser -u uuway-test -- env WINEPREFIX=/home/uuway-test/wine-smoke "$wine_bin/wineserver" -w
runuser -u uuway-test -- mkdir -m 700 /home/uuway-test/releases
for backend in nvidia amd; do
    runuser -u uuway-test -- /usr/bin/python3 /usr/lib/uuway/scripts/package-uu-native-runtime.py \
        package --with-input --backend "$backend" "/home/uuway-test/releases/$backend" > "/tmp/uuway-$backend.json"
done
python3 - "$output" <<'PY'
import json, pathlib, subprocess, sys
root = pathlib.Path('/usr/lib/uuway')
for backend in ('nvidia', 'amd'):
    bundle = json.loads(pathlib.Path(f'/tmp/uuway-{backend}.json').read_text())['bundle']
    subprocess.run(['runuser', '-u', 'uuway-test', '--', '/usr/bin/python3',
                    str(root / 'scripts/package-uu-native-runtime.py'), 'verify', bundle], check=True)
errors = []
for path in root.rglob('*'):
    if not path.is_file() or path.is_symlink():
        continue
    with path.open('rb') as stream:
        if stream.read(4) != b'\x7fELF':
            continue
    result = subprocess.run(['ldd', str(path)], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if 'not found' in line and 'libcuda.so.1' not in line and not ('fcitx5' in str(path) and 'libFcitx5' in line):
            errors.append(f'{path}: {line.strip()}')
if errors:
    raise SystemExit('\n'.join(errors))
info = json.loads((root / 'package-info.json').read_text())
assert info['ota'] is True
sys.path.insert(0, str(root / 'scripts'))
import uuway_update_apply as update
assert update.installed_target() == info['target']
suffix = {'deb': '*.deb', 'rpm': '*.rpm', 'arch': '*.pkg.tar.zst'}[info['format']]
packages = list(pathlib.Path(sys.argv[1]).glob(suffix))
assert len(packages) == 1, packages
control = update.package_control(packages[0], info['target'])
assert control['Package'] == 'uuway'
assert control['Architecture'] == update.TARGETS[info['target']][1]
expected_version = info['version'].replace('~', '.').replace('+', '.') if info['format'] == 'arch' else info['version']
assert control['Version'] == expected_version, control
print('Target-aware OTA asset:', update.package_name('1.5.0', info['target']))
print('Both GPU bundles verify; installed ELF dependencies resolve. Live desktop acceptance remains required.')
PY
# Exercise the real native package manager; desktop health is supplied by the fixture.
if [[ "$target" == fedora-* ]]; then
    dnf install -y --setopt=install_weak_deps=False rpm-build
fi
python3 /source/packaging/update-native-e2e.py
