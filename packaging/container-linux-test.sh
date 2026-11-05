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
    fedora-44) dnf install -y --setopt=install_weak_deps=False "$output"/*.rpm ;;
    arch)
        pacman -Syu --noconfirm
        pacman -U --noconfirm "$output"/*.pkg.tar.zst
        ;;
    *) exit 2 ;;
esac
/opt/uuway-wine11/bin/wine --version | grep -Fx wine-11.0
uuway --version
uuway --help >/dev/null
uuway-console --status >/dev/null
useradd -m uuway-test
timeout 120 runuser -u uuway-test -- env WINEPREFIX=/home/uuway-test/wine-smoke WINEDEBUG=-all \
    WINEDLLOVERRIDES=mscoree,mshtml= xvfb-run -a /opt/uuway-wine11/bin/wine cmd /c echo UUWAY_WINE64_OK | tr -d '\r' | grep -Fx UUWAY_WINE64_OK
timeout 60 runuser -u uuway-test -- env WINEPREFIX=/home/uuway-test/wine-smoke WINEDEBUG=-all \
    WINEDLLOVERRIDES=mscoree,mshtml= xvfb-run -a /opt/uuway-wine11/bin/wine \
    'C:\windows\syswow64\cmd.exe' /c echo UUWAY_WINE32_OK | tr -d '\r' | grep -Fx UUWAY_WINE32_OK
runuser -u uuway-test -- env WINEPREFIX=/home/uuway-test/wine-smoke /opt/uuway-wine11/bin/wineserver -k || true
timeout 30 runuser -u uuway-test -- env WINEPREFIX=/home/uuway-test/wine-smoke /opt/uuway-wine11/bin/wineserver -w
runuser -u uuway-test -- mkdir -m 700 /home/uuway-test/releases
for backend in nvidia amd; do
    runuser -u uuway-test -- /usr/bin/python3 /usr/lib/uuway/scripts/package-uu-native-runtime.py \
        package --with-input --backend "$backend" "/home/uuway-test/releases/$backend" > "/tmp/uuway-$backend.json"
done
python3 - <<'PY'
import json, pathlib, subprocess
root = pathlib.Path('/usr/lib/uuway')
for backend in ('nvidia', 'amd'):
    bundle = json.loads(pathlib.Path(f'/tmp/uuway-{backend}.json').read_text())['bundle']
    subprocess.run(['runuser', '-u', 'uuway-test', '--', '/usr/bin/python3',
                    str(root / 'scripts/package-uu-native-runtime.py'), 'verify', bundle], check=True)
errors = []
for path in [*root.rglob('*'), *pathlib.Path('/opt/uuway-wine11').rglob('*')]:
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
assert info['ota'] is False, 'distribution packages must not install Ubuntu OTA artifacts'
print('Both GPU bundles verify; installed ELF dependencies resolve. Live desktop acceptance remains required.')
PY
