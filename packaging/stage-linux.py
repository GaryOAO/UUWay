#!/usr/bin/python3
"""Assemble target-native packages from this container's completed build.

Wine and UUWay are separate packages. Never convert Ubuntu ELF binaries into
RPM/Arch archives. The common manifest is also used by the existing Ubuntu deb.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stage_deb', ROOT / 'packaging/stage-deb.py')
deb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deb)

RUNTIME = {
    'deb': 'python3, python3-gi, python3-dbus, gir1.2-gtk-3.0, gir1.2-atspi-2.0, xvfb, xauth, xclip, procps, libgtk-3-bin, udev, systemd',
    'rpm': 'python3, python3-gobject, python3-dbus, gtk3, at-spi2-core, xorg-x11-server-Xvfb, xorg-x11-xauth, xclip, procps-ng, gtk-update-icon-cache, systemd, systemd-udev',
    'arch': 'python python-gobject python-dbus gtk3 at-spi2-core xorg-server-xvfb xorg-xauth xclip procps-ng gtk-update-icon-cache systemd json-c libx11 libxfixes pipewire vulkan-icd-loader gstreamer gst-plugins-base-libs ffmpeg libva',
}
WINE_RUNTIME = {
    'deb': 'fontconfig, libfreetype6, libgnutls30t64, libx11-6, libxext6, libxrandr2, libxrender1, libxinerama1, libxcursor1, libxfixes3, libxi6, libasound2t64, libpulse0, libudev1, libvulkan1',
    'rpm': 'fontconfig, freetype, gnutls, libX11, libXext, libXrandr, libXrender, libXinerama, libXcursor, libXfixes, libXi, alsa-lib, pulseaudio-libs, systemd-libs, vulkan-loader',
    'arch': 'glibc fontconfig freetype2 gnutls libx11 libxext libxrandr libxrender libxinerama libxcursor libxfixes libxi alsa-lib libpulse systemd-libs vulkan-icd-loader',
}


def run(argv, **kwargs):
    subprocess.run([str(v) for v in argv], check=True, **kwargs)


def build_deb(stage, output, name, version, depends, suite, *, runtime=False):
    control = stage / 'DEBIAN'
    control.mkdir()
    (control / 'control').write_text(
        f'Package: {name}\nVersion: {version}\nArchitecture: amd64\n'
        'Maintainer: GaryOAO <62834599+GaryOAO@users.noreply.github.com>\n'
        f'Depends: {depends}\nSection: utils\nPriority: optional\n'
        'Homepage: https://github.com/GaryOAO/UUWay\n'
        f'Description: {"UUWay private Wine 11.0 runtime" if not runtime else "UU Remote Linux host (experimental GNOME Wayland support)"}\n')
    if runtime:
        for script in deb.MAINTAINER_SCRIPTS:
            shutil.copyfile(ROOT / 'packaging/deb' / script, control / script)
            (control / script).chmod(0o755)
    filename = f'{name}_{version}_{suite}_amd64.deb'
    run(['dpkg-deb', '--root-owner-group', '-Zxz', '--build', stage, output / filename])


def build_rpm(stage, work, output, name, version, depends, suite, *, runtime=False):
    top = work / ('rpm-' + name)
    for directory in ('SOURCES', 'SPECS', 'BUILD', 'BUILDROOT', 'RPMS', 'SRPMS'):
        (top / directory).mkdir(parents=True)
    archive = top / 'SOURCES/payload.tar.gz'
    run(['tar', '-czf', archive, '-C', stage, '.'])
    paths = '/usr/lib/uuway\n/usr/bin/uuway\n/usr/bin/uuway-console\n/usr/share/doc/uuway\n/usr/share/applications/uuway.desktop\n/usr/share/icons/hicolor/scalable/apps/uuway.svg\n/usr/lib/udev/rules.d/70-uurb-native-input.rules' if runtime else '/opt/uuway-wine11'
    hooks = ''
    if runtime:
        hooks = ('\n%post\n' + (ROOT / 'packaging/deb/postinst').read_text().replace('case "$1" in', 'case configure in').replace('exit 0', ':')
                 + '\n%preun\nif [ "$1" -eq 0 ]; then\n'
                 + (ROOT / 'packaging/deb/prerm').read_text().replace('case "$1" in', 'case remove in').replace('exit 0', ':') + '\nfi\n')
    specfile = top / 'SPECS' / (name + '.spec')
    specfile.write_text(
        '%global debug_package %{nil}\n%global _build_id_links none\n'
        '%global __requires_exclude ^(libcuda\\.so\\.1|libFcitx5Core\\.so\\.7|libFcitx5Utils\\.so\\.2).*$\n'
        '%global __os_install_post %{nil}\n'
        f'Name: {name}\nVersion: {version}\nRelease: 1\n'
        f'Summary: {"UU Remote Linux host" if runtime else "UUWay private Wine 11.0"}\n'
        f'License: {"AGPL-3.0-only" if runtime else "LGPL-2.1-or-later"}\n'
        'URL: https://github.com/GaryOAO/UUWay\nSource0: payload.tar.gz\n'
        f'Requires: {depends}\n\n%description\n'
        'Distribution-native build for UUWay. GNOME Wayland hardware validation is required.\n'
        '\n%prep\n%build\n%install\nmkdir -p %{buildroot}\ntar -xzf %{SOURCE0} -C %{buildroot}\n'
        f'{hooks}\n%files\n{paths}\n')
    run(['rpmbuild', '--define', '_topdir ' + str(top), '-bb', specfile])
    for package in (top / 'RPMS').rglob('*.rpm'):
        shutil.copyfile(package, output / f'{name}-{version}-1.{suite}.x86_64.rpm')


def build_arch(stage, work, output, name, version, depends, *, runtime=False):
    directory = work / ('arch-' + name)
    directory.mkdir()
    shutil.copytree(stage, directory / 'payload', symlinks=True)
    hook = ''
    if runtime:
        (directory / 'uuway.install').write_text(
            'post_install() {\n  udevadm control --reload 2>/dev/null || true\n'
            '  udevadm trigger --subsystem-match=misc --sysname-match=uinput --action=add 2>/dev/null || true\n'
            '  echo "Run uuway setup as your normal user. GNOME Wayland support is experimental."\n}\n'
            'post_upgrade() { post_install; }\n'
            'pre_remove() {\n' + (ROOT / 'packaging/deb/prerm').read_text().replace('case "$1" in', 'case remove in').replace('exit 0', ':') + '\n}\n')
        hook = 'install=uuway.install\n'
    (directory / 'PKGBUILD').write_text(
        f'pkgname={name}\npkgver={version.replace("~", ".").replace("+", ".")}\npkgrel=1\n'
        f'pkgdesc={shlex.quote("UU Remote Linux host" if runtime else "UUWay private Wine 11.0")}\n'
        "arch=('x86_64')\nurl='https://github.com/GaryOAO/UUWay'\n"
        f'license=({shlex.quote("AGPL-3.0-only" if runtime else "LGPL-2.1-or-later")})\n'
        f'depends=({" ".join(shlex.quote(v) for v in depends.split())})\n'
        "options=('!strip' '!debug')\n" + hook +
        'package() { cp -a "$startdir/payload/." "$pkgdir/"; }\n')
    if subprocess.run(['id', 'uuway-builder'], capture_output=True).returncode:
        run(['useradd', '-m', 'uuway-builder'])
    # makepkg refuses root. Give its user access only to this temporary workspace.
    work.chmod(0o755)
    run(['chown', '-R', 'uuway-builder:uuway-builder', directory])
    run(['runuser', '-u', 'uuway-builder', '--', 'makepkg', '--nodeps', '--noconfirm', '--nosign'], cwd=directory)
    for package in directory.glob('*.pkg.tar.zst'):
        shutil.copyfile(package, output / package.name)
    shutil.copyfile(directory / 'PKGBUILD', output / (name + '.PKGBUILD'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    targets = json.loads((ROOT / 'packaging/linux-targets.json').read_text())
    parser.add_argument('--target', choices=targets, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[.+~][A-Za-z0-9.]+)?', args.version):
        raise SystemExit('Invalid package version')
    target = targets[args.target]
    epoch = int(os.environ.get('SOURCE_DATE_EPOCH', '0'))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='uuway-stage-') as temporary:
        work = Path(temporary)
        stage, wine = work / 'runtime', work / 'wine'
        stage.mkdir()
        entries = [entry for entry in deb.deb_manifest.files()
                   if target['format'] == 'deb' or 'lintian' not in entry[1]]
        deb.check_sources(entries)
        deb.copy_tree(entries, stage, epoch)
        (stage / 'usr/lib/uuway/package-info.json').write_text(json.dumps(dict(
            version=args.version, target=args.target, format=target['format'], ota=False,
            desktop='GNOME Wayland / Hyprland (single-output experiment)', status='experimental', wine='11.0'), indent=2) + '\n')
        shutil.copytree('/opt/uuway-wine11', wine / 'opt/uuway-wine11', symlinks=True)
        fmt = target['format']
        if fmt == 'deb':
            native = deb.shlibs_depends(stage)
            wine_native = deb.shlibs_depends(wine)
            if native is None or wine_native is None:
                raise SystemExit('dpkg-shlibdeps is required')
            build_deb(wine, args.output_dir, 'uuway-wine11', '11.0-1', ', '.join([WINE_RUNTIME[fmt], *wine_native]), target['suite'])
            build_deb(stage, args.output_dir, 'uuway', args.version,
                      ', '.join([RUNTIME[fmt], 'uuway-wine11 (= 11.0-1)', *native]), target['suite'], runtime=True)
        elif fmt == 'rpm':
            build_rpm(wine, work, args.output_dir, 'uuway-wine11', '11.0', WINE_RUNTIME[fmt], target['suite'])
            build_rpm(stage, work, args.output_dir, 'uuway', args.version,
                      RUNTIME[fmt] + ', uuway-wine11 = 11.0-1', target['suite'], runtime=True)
        else:
            build_arch(wine, work, args.output_dir, 'uuway-wine11', '11.0', WINE_RUNTIME[fmt])
            build_arch(stage, work, args.output_dir, 'uuway', args.version,
                       RUNTIME[fmt] + ' uuway-wine11=11.0-1', runtime=True)
    # LGPL corresponding source is distributed beside the private Wine binaries.
    shutil.copyfile(ROOT / 'build/multidistro/downloads/wine-11.0.tar.xz', args.output_dir / 'wine-11.0.tar.xz')
    shutil.copyfile(ROOT / 'packaging/build-wine11.sh', args.output_dir / 'build-wine11.sh')
    lines = [f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n'
             for path in sorted(args.output_dir.iterdir()) if path.is_file() and path.name != 'SHA256SUMS']
    (args.output_dir / 'SHA256SUMS').write_text(''.join(lines))


if __name__ == '__main__':
    main()
