#!/usr/bin/env bash
# Check the uuway .deb the way a user meets it, inside a clean Ubuntu 24.04 (root, no GPU,
# no Wine, no desktop session).  packaging/test-deb.sh runs this in a throwaway container.
#
#     packaging/container-test.sh /path/uuway_X_amd64.deb
#
# What a container cannot show (live capture, NVENC, the phone) is covered by the manual
# acceptance on a real machine; everything that does not need a GPU is asserted here.
set -uo pipefail

deb="${1:?usage: container-test.sh path/to/uuway.deb}"
export DEBIAN_FRONTEND=noninteractive
lib=/usr/lib/uuway
failures=()
out="$(mktemp)"

check() {
    local name="$1"
    shift
    if "$@" >"$out" 2>&1; then
        printf '  ✓ %s\n' "$name"
    else
        printf '  ✗ %s\n' "$name"
        sed 's/^/      /' "$out" | head -25
        failures+=("$name")
    fi
}
as_user() { runuser -u tester -- env HOME=/home/tester PATH="$PATH" "$@"; }
section() { printf '\n── %s\n' "$*"; }

section "Install (minimal: no recommends, so Wine and Fcitx5 are absent, as on a fresh machine)"
apt-get update -qq
check "apt resolves every dependency from the Ubuntu archive" \
    apt-get install -y -qq --no-install-recommends "$deb"
check "package is installed" dpkg -s uuway
useradd -m tester

section "Layout"
check "everything is root-owned and sits where it should" /usr/bin/python3 - <<'PY'
import os, subprocess, sys
allowed = ('/usr/lib/uuway', '/usr/bin/uuway', '/usr/bin/uuway-console', '/usr/share/doc/uuway', '/usr/share/applications/uuway.desktop',
           '/usr/share/icons/hicolor/scalable/apps/uuway.svg',
           '/usr/lib/udev/rules.d/70-uurb-native-input.rules', '/usr/share/lintian/overrides/uuway')
parents = {'/', '/usr', '/usr/bin', '/usr/lib', '/usr/share', '/usr/share/doc', '/usr/share/applications',
           '/usr/share/icons', '/usr/share/icons/hicolor', '/usr/share/icons/hicolor/scalable',
           '/usr/share/icons/hicolor/scalable/apps', '/usr/lib/udev', '/usr/lib/udev/rules.d',
           '/usr/share/lintian', '/usr/share/lintian/overrides', '/.'}
problems = []
for path in subprocess.run(['dpkg', '-L', 'uuway'], capture_output=True, text=True, check=True).stdout.split():
    if path in parents:
        continue
    if not any(path == prefix or path.startswith(prefix + '/') for prefix in allowed):
        problems.append('unexpected path: ' + path)
    if not os.path.lexists(path):
        # The Docker image of Ubuntu excludes /usr/share/doc from every package it installs.
        if not path.startswith('/usr/share/doc/'):
            problems.append(f'listed but missing: {path}')
        continue
    info = os.lstat(path)
    if info.st_uid or info.st_gid:
        problems.append(f'not root-owned: {path}')
    if any(word in path.lower() for word in ('libcuda', 'libnvidia', 'gameviewer', 'uuremote', 'netease')):
        problems.append('must not be shipped: ' + path)
sys.exit('\n'.join(problems) if problems else 0)
PY
check "commands are symlinks into the tree" bash -c "[ \"\$(readlink /usr/bin/uuway)\" = ../lib/uuway/scripts/uuway_cli.py ] && [ \"\$(readlink /usr/bin/uuway-console)\" = ../lib/uuway/scripts/uuway_console.py ]"
check "udev rule is installed" test -f /usr/lib/udev/rules.d/70-uurb-native-input.rules
check "the package changes nothing under /etc (the Wine pin is written by uuway setup, with consent)" bash -c "! dpkg -L uuway | grep -E '^/etc/'"
check "the Wine pin is shipped as data for uuway setup" test -f $lib/config/uuway-wine.preferences
check "build/ holds every artifact the packager reads, and the consent probe" /usr/bin/python3 - <<'PY'
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('p', '/usr/lib/uuway/scripts/package-uu-native-runtime.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
need = {value for table in vars(module).values() if isinstance(table, dict)
        for value in table.values() if isinstance(value, str) and value.startswith('build/')}
need.add('build/native-presenter/uu-pipewire-dmabuf-probe')
have = {str(path.relative_to('/usr/lib/uuway')) for path in pathlib.Path('/usr/lib/uuway/build').rglob('*') if path.is_file()}
missing, extra = sorted(need - have), sorted(have - need - {f'build/helpers/{n}' for n in (
    'uu-clipboard-bridge', 'uu-clipboard-files.exe', 'uu-conpty.dll', 'uu-terminal-bridge', 'uu-terminal-proxy.exe')})
sys.exit(f'missing {missing}\nunexpected {extra}' if missing or extra else 0)
PY

section "Native libraries"
elf_missing() {
    # Every "file: library" the system cannot supply, except the NVIDIA driver's libcuda and
    # the Fcitx5 libraries (the add-on is only ever loaded by fcitx5, which has them).
    local file
    while IFS= read -r file; do
        [ "$(head -c4 "$file" | tail -c3)" = ELF ] || continue
        ldd "$file" 2>/dev/null | awk -v f="$file" '/not found/ {print f ": " $1}'
    done < <(find "$lib" -type f)
}
check "every ELF file resolves, apart from libcuda.so.1 (the NVIDIA driver provides it)" bash -c "
    $(declare -f elf_missing); lib=$lib
    ! elf_missing | grep -vE ': libcuda\.so\.1\$' | grep -vE 'libuurb-native-ime\.so: libFcitx5(Core|Utils)\.so' | grep ."

section "Python tree"
check "every script compiles" /usr/bin/python3 - <<'PY'
import pathlib, sys
failed = []
for path in sorted(pathlib.Path('/usr/lib/uuway/scripts').glob('*.py')):
    try:
        compile(path.read_text(), str(path), 'exec')
    except SyntaxError as error:
        failed.append(f'{path}: {error}')
sys.exit('\n'.join(failed) if failed else 0)
PY
check "uuway --version reports the package version" bash -c "uuway --version | grep -F \"\$(dpkg-query -W -f='\${Version}' uuway)\""
check "uuway --help lists setup, refresh, doctor, uninstall" bash -c "uuway --help | grep -q setup && uuway --help | grep -q refresh && uuway --help | grep -q doctor && uuway --help | grep -q uninstall"
check "the console starts from the installed tree through its /usr/bin link (GTK bindings, assets)" bash -c "runuser -u tester -- env HOME=/home/tester uuway-console --status | grep -q 'UUWay 状态与能力'"

section "Per-user runtime from the read-only tree (what 'uuway setup' does after the UU install)"
as_user mkdir -m 700 -p /home/tester/.local/share/uuway/releases
check "a normal user can pack a bundle out of /usr/lib/uuway" bash -c "
    runuser -u tester -- env HOME=/home/tester /usr/bin/python3 $lib/scripts/package-uu-native-runtime.py package --with-input /home/tester/.local/share/uuway/releases > /tmp/package.json"
bundle="$(/usr/bin/python3 -c "import json; print(json.load(open('/tmp/package.json'))['bundle'])" 2>/dev/null || true)"
check "the packager reported a bundle inside the user's home" bash -c "case '$bundle' in /home/tester/*) true ;; *) false ;; esac"
check "the bundle verifies" as_user /usr/bin/python3 "$lib/scripts/package-uu-native-runtime.py" verify "$bundle"
check "the bundle is owned by the user (the capture producer must be)" bash -c "
    [ \"\$(stat -c %U '$bundle/capture/uu-pipewire-native-probe')\" = tester ] && [ \"\$(stat -c %a '$bundle/capture/uu-pipewire-native-probe')\" = 700 ]"
check "repacking gives the same content address" bash -c "
    runuser -u tester -- env HOME=/home/tester /usr/bin/python3 $lib/scripts/package-uu-native-runtime.py package --with-input /home/tester/.local/share/uuway/releases | /usr/bin/python3 -c \"import json,sys; assert json.load(sys.stdin)['bundle'] == '$bundle'\""

section "Service units from the installed tree"
as_user mkdir -m 700 -p /home/tester/.local/share/wineprefixes/uu-remote /home/tester/.local/state/uurb
check "install-uu-native-service.py stages config and units" as_user /usr/bin/python3 "$lib/scripts/install-uu-native-service.py" \
    --bundle "$bundle" --prefix /home/tester/.local/share/wineprefixes/uu-remote \
    --restore-state /home/tester/.local/state/uurb/portal-probe.json --state-parent /home/tester/.local/state/uurb \
    --text-socket /home/tester/.local/state/uurb/text.sock --cursor-mode metadata --preserve-display-session
units=/home/tester/.config/systemd/user
check "text and display run from /usr/lib/uuway, the bridge from the user's bundle" bash -c "
    grep -q 'ExecStart=/usr/bin/python3 \"$lib/scripts/uu-native-text-service.py\"' $units/uu-native-text.service &&
    grep -q 'ExecStart=/usr/bin/python3 \"$lib/scripts/uu-native-display-service.py\"' $units/uu-native-display.service &&
    grep -q 'ExecStart=/usr/bin/python3 \"$bundle/scripts/uu-native-service.py\"' $units/uu-native-bridge.service"
check "re-running the installer over its own units succeeds (upgrade path)" as_user /usr/bin/python3 "$lib/scripts/install-uu-native-service.py" \
    --bundle "$bundle" --prefix /home/tester/.local/share/wineprefixes/uu-remote \
    --restore-state /home/tester/.local/state/uurb/portal-probe.json --state-parent /home/tester/.local/state/uurb \
    --text-socket /home/tester/.local/state/uurb/text.sock

section "uuway command in a machine that has no GPU, Wine or desktop"
check "doctor reports the missing pieces and exits non-zero" bash -c "
    ! runuser -u tester -- env HOME=/home/tester uuway doctor > /tmp/doctor.txt; grep -q 'NVIDIA' /tmp/doctor.txt && grep -q 'WineHQ' /tmp/doctor.txt"
mkdir -p /tmp/fakebin
printf '#!/bin/sh\necho "GPU 0: Fake GPU (UUID: GPU-test)"\n' > /tmp/fakebin/nvidia-smi
chmod +x /tmp/fakebin/nvidia-smi
check "setup --dry-run plans every step from the installed tree and changes nothing" bash -c "
    before=\$(find /home/tester -type f | sort | md5sum)
    runuser -u tester -- env HOME=/home/tester PATH=/tmp/fakebin:\$PATH XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=ubuntu:GNOME \
        uuway setup --dry-run --yes > /tmp/plan.txt 2>&1
    grep -q 'package-uu-native-runtime.py package' /tmp/plan.txt &&
    grep -q '$lib/scripts/install-uu-native-service.py' /tmp/plan.txt &&
    grep -q 'probe-wayland-portal.py --capture-check' /tmp/plan.txt &&
    [ \"\$(find /home/tester -type f | sort | md5sum)\" = \"\$before\" ]"
check "uninstall removes only what uuway wrote and keeps the Wine prefix" bash -c "
    runuser -u tester -- env HOME=/home/tester uuway uninstall --yes --purge > /tmp/uninstall.txt 2>&1
    [ ! -e $units/uu-native-bridge.service ] && [ -d /home/tester/.local/share/wineprefixes/uu-remote ]"

section "Lifecycle"
check "reinstalling the same package works" dpkg -i "$deb"
check "removal succeeds and takes the tree with it" bash -c "apt-get remove -y -qq uuway && [ ! -e $lib ] && [ ! -e /usr/bin/uuway ]"
check "purge succeeds and leaves home directories alone" bash -c "apt-get purge -y -qq uuway && [ -d /home/tester/.local/share/wineprefixes/uu-remote ]"

if [ "${UUWAY_LINT:-1}" = 1 ]; then
    section "lintian"
    apt-get install -y -qq --no-install-recommends lintian >/dev/null 2>&1
    lintian --tag-display-limit 0 --display-info "$deb" > /tmp/lintian.txt 2>&1 || true
    sed 's/^/  /' /tmp/lintian.txt | head -60
    check "lintian reports no errors" bash -c "! grep -E '^E: ' /tmp/lintian.txt"
fi

printf '\n'
if ((${#failures[@]})); then
    printf '✗ %d check(s) failed:\n' "${#failures[@]}"
    printf '  - %s\n' "${failures[@]}"
    exit 1
fi
printf '✓ all checks passed\n'
