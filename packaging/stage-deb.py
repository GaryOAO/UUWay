#!/usr/bin/python3
"""Assemble the uuway .deb from a finished build tree.

Run from a checkout whose build/ directory holds the artifacts produced by
packaging/container-build.sh.  What goes into the package is decided by
packaging/deb_manifest.py; nothing is picked up by wildcard.
"""
import argparse
import gzip
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import deb_manifest  # noqa: E402

MAINTAINER_SCRIPTS = ('postinst', 'prerm', 'postrm')
CONFFILES = ()  # nothing under /etc: the package must not change apt's behaviour on its own
# Provided by the NVIDIA driver on the user's machine, never by a package we depend on.
DRIVER_LIBS = re.compile(r'libcuda\.so|libnvidia-')
ELF_MAGIC = b'\x7fELF'
PE_MAGIC = b'MZ'


def check_sources(entries):
    problems = []
    for source, dest, _ in entries:
        path = ROOT / source
        if path.is_symlink() or not path.is_file():
            problems.append(f'missing or not a regular file: {source}')
            continue
        if any(part in dest for part in ('libcuda', 'stubs')):
            problems.append(f'refusing to ship a CUDA stub: {dest}')
        head = path.read_bytes()[:4]
        if dest.endswith(('.so', '.dll.so')) and not head.startswith(ELF_MAGIC):
            problems.append(f'not an ELF file: {source}')
        if dest.endswith(('.exe', '.dll')) and not head.startswith(PE_MAGIC):
            problems.append(f'not a PE file: {source}')
    if problems:
        raise SystemExit('Cannot stage the package:\n  ' + '\n  '.join(problems))


def copy_tree(entries, stage, epoch):
    for source, dest, mode in entries:
        target = stage / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / source, target)
        os.chmod(target, mode)
    for dest, link_target in deb_manifest.COMMANDS.items():
        link = stage / dest
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(link_target)
        resolved = (link.parent / link_target).resolve()
        if not str(resolved).startswith(str(stage.resolve())) or not resolved.is_file():
            raise SystemExit(f'Command link does not resolve inside the package: {dest} -> {link_target}')
    for path in sorted(stage.rglob('*'), reverse=True):
        os.utime(path, (epoch, epoch), follow_symlinks=False)
    for directory in [stage] + [p for p in stage.rglob('*') if p.is_dir() and not p.is_symlink()]:
        os.chmod(directory, 0o755)


def elf_files(stage):
    found = []
    for path in sorted(stage.rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        # The Fcitx5 add-on is only ever loaded by fcitx5, which brings its own libraries;
        # fcitx5 stays a Recommends so a UUWay install does not pull the framework in.
        if 'fcitx5' in path.relative_to(stage).parts:
            continue
        with path.open('rb') as handle:
            if handle.read(4) == ELF_MAGIC:
                found.append(path)
    return found


def shlibs_depends(stage):
    """Let dpkg-shlibdeps name the packages that provide the native libraries."""
    binaries = elf_files(stage)
    if not binaries or not shutil.which('dpkg-shlibdeps'):
        return None
    with tempfile.TemporaryDirectory() as scratch:
        scratch = Path(scratch)
        (scratch / 'debian').mkdir()
        (scratch / 'debian/control').write_text(
            'Source: uuway\n\nPackage: uuway\nArchitecture: amd64\n')
        # The driver's libraries are absent on a build machine; give dpkg-shlibdeps an empty
        # shared object with the right SONAME so they are skipped rather than fatal.
        (scratch / 'libs').mkdir()
        for soname in ('libcuda.so.1',):
            subprocess.run(['gcc', '-shared', '-x', 'c', '-', '-o', str(scratch / 'libs' / soname),
                            '-Wl,-soname,' + soname], input='', text=True, check=True)
        command = ['dpkg-shlibdeps', '-O', '--ignore-missing-info', '-l' + str(scratch / 'libs')]
        # Wine's own libraries (/opt/wine-stable) have no Debian shlibs entry here either.
        command += ['-l/opt/wine-stable/lib', '-l/opt/wine-stable/lib64']
        command += [str(path) for path in binaries]
        result = subprocess.run(command, cwd=scratch, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit('dpkg-shlibdeps failed')
    if os.environ.get('UUWAY_VERBOSE'):
        sys.stderr.write(result.stderr)
    match = re.search(r'^shlibs:Depends=(.*)$', result.stdout, re.M)
    names = [item.strip() for item in match.group(1).split(',')] if match else []
    return [item for item in names if item and not DRIVER_LIBS.search(item)]


DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def write_changelog(stage, version, epoch):
    """A Debian changelog pointing at the release notes (the package is native to this repository)."""
    moment = time.gmtime(epoch)
    stamp = (f'{DAYS[moment.tm_wday]}, {moment.tm_mday:02d} {MONTHS[moment.tm_mon - 1]} {moment.tm_year} '
             f'{moment.tm_hour:02d}:{moment.tm_min:02d}:{moment.tm_sec:02d} +0000')
    text = (f'uuway ({version}) noble; urgency=medium\n\n'
            f'  * UUWay {version}. Release notes: https://github.com/GaryOAO/UUWay/releases\n\n'
            f' -- GaryOAO <62834599+GaryOAO@users.noreply.github.com>  {stamp}\n')
    target = stage / 'usr/share/doc/uuway/changelog.gz'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(gzip.compress(text.encode(), compresslevel=9, mtime=0))
    os.chmod(target, 0o644)
    os.utime(target, (epoch, epoch))


def write_control(stage, version, depends):
    template = (ROOT / 'packaging/deb/control.in').read_text()
    size = sum(p.stat().st_size for p in stage.rglob('*') if p.is_file() and not p.is_symlink())
    extra = ''.join(', ' + item for item in depends) if depends else ''
    control = (template.replace('@VERSION@', version)
               .replace('@SIZE@', str((size + 1023) // 1024))
               .replace('@SHLIBS@', extra))
    debian = stage / 'DEBIAN'
    debian.mkdir()
    (debian / 'control').write_text(control)
    for name in MAINTAINER_SCRIPTS:
        shutil.copyfile(ROOT / 'packaging/deb' / name, debian / name)
        os.chmod(debian / name, 0o755)
    if CONFFILES:
        (debian / 'conffiles').write_text(''.join('/' + name + '\n' for name in CONFFILES))
    lines = []
    for path in sorted(stage.rglob('*')):
        if path.is_symlink() or not path.is_file() or debian in path.parents:
            continue
        digest = hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
        lines.append(f'{digest}  {path.relative_to(stage)}\n')
    (debian / 'md5sums').write_text(''.join(lines))
    return control


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True, help='Debian version, e.g. 1.0.0')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'build/deb')
    parser.add_argument('--epoch', type=int, default=None,
                        help='mtime for every packaged file (default: SOURCE_DATE_EPOCH or HEAD commit time)')
    parser.add_argument('--no-shlibs', action='store_true', help='skip dpkg-shlibdeps')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9][A-Za-z0-9.+~-]*', args.version):
        raise SystemExit('Invalid Debian version: ' + args.version)
    epoch = args.epoch
    if epoch is None and os.environ.get('SOURCE_DATE_EPOCH'):
        epoch = int(os.environ['SOURCE_DATE_EPOCH'])
    if epoch is None:
        head = subprocess.run(['git', '-C', str(ROOT), 'log', '-1', '--format=%ct'],
                              capture_output=True, text=True)
        if head.returncode != 0 or not head.stdout.strip().isdigit():
            raise SystemExit('Not a git checkout: pass --epoch or set SOURCE_DATE_EPOCH')
        epoch = int(head.stdout.strip())
    entries = deb_manifest.files()
    check_sources(entries)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        stage = Path(scratch) / 'uuway'
        stage.mkdir()
        copy_tree(entries, stage, epoch)
        write_changelog(stage, args.version, epoch)
        depends = None if args.no_shlibs else shlibs_depends(stage)
        if depends is None and not args.no_shlibs:
            print('warning: dpkg-shlibdeps unavailable; native library dependencies not computed',
                  file=sys.stderr)
        write_control(stage, args.version, depends or [])
        deb = args.output_dir / f'uuway_{args.version}_amd64.deb'
        subprocess.run(['dpkg-deb', '--root-owner-group', '-Zxz', '--build', str(stage), str(deb)],
                       check=True, capture_output=True)
    print(deb)


if __name__ == '__main__':
    main()
