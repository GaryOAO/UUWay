#!/usr/bin/python3
"""Stage a fresh private library bundle; validate against installed GNOME ABI.
No service/config changes. Never overwrites a bundle or the system libraries.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
LIBRARIES = {
    'libmutter-14.so.0.0.0': 'src',
    'libmutter-clutter-14.so.0.0.0': 'clutter/clutter',
    'libmutter-cogl-14.so.0.0.0': 'cogl/cogl',
    'libmutter-cogl-pango-14.so.0.0.0': 'cogl/cogl-pango',
    'libmutter-mtk-14.so.0.0.0': 'mtk/mtk',
}


def symbols(library):
    output = subprocess.check_output(['nm', '-D', '--defined-only', str(library)], text=True)
    return {line.split()[-1] for line in output.splitlines() if line.split()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--core-only', action='store_true', default=True,
                        help='Default and only supported mode: keep system support libraries for GNOME GI compatibility')
    args = parser.parse_args()
    build = args.build.resolve(strict=True)
    if not build.is_relative_to(ROOT / 'build/mutter-build'):
        parser.error('Expected a private Mutter build beneath build/mutter-build')
    output = args.output.absolute()
    if output.exists() or output.is_symlink():
        parser.error('Output must be a new, nonexistent bundle directory')
    patch = (ROOT / 'patches/mutter-46.2-capture-jitter-candidate.patch').read_bytes()
    report = dict(build=str(build), patch_sha256=hashlib.sha256(patch).hexdigest(),
                  system_support_libraries=args.core_only, libraries={})
    source_dir = build / 'mutter-46.2/src/backends'
    report['built_source_sha256'] = {
        name: hashlib.sha256((source_dir / name).read_bytes()).hexdigest()
        for name in ['meta-screen-cast-stream-src.c', 'meta-screen-cast-virtual-stream-src.c']}
    report['virtual_presentation_timestamp_candidate'] = 'clutter_frame_get_target_presentation_time' in (
        source_dir / 'meta-screen-cast-virtual-stream-src.c').read_text()
    output.mkdir(parents=True, mode=0o755)
    for name, relative in LIBRARIES.items():
        if args.core_only and name != 'libmutter-14.so.0.0.0':
            continue
        source = build / 'compiled' / relative / name
        system = Path('/usr/lib/x86_64-linux-gnu')
        if name != 'libmutter-14.so.0.0.0':
            system /= 'mutter-14'
        installed = system / name
        old, new = symbols(installed), symbols(source)
        if old - new:
            raise RuntimeError(f'Missing installed ABI symbols in {name}: {sorted(old - new)}')
        target = output / name
        shutil.copyfile(source, target)
        target.chmod(0o755)
        rpath = '$ORIGIN:/usr/lib/x86_64-linux-gnu/mutter-14' if args.core_only else '$ORIGIN'
        subprocess.run([str(build / 'sysroot/usr/bin/patchelf'), '--set-rpath', rpath, str(target)], check=True)
        soname = name.removesuffix('.0.0')
        (output / soname).symlink_to(name)
        report['libraries'][name] = dict(sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            original_sha256=hashlib.sha256(installed.read_bytes()).hexdigest(),
            installed_symbols=len(old), added_symbols=len(new - old))
    env = dict(os.environ, LD_LIBRARY_PATH=str(output))
    probe = output / 'mutter-headless-probe'
    shutil.copyfile(build / 'compiled/src/mutter', probe)
    probe.chmod(0o755)
    subprocess.run([str(build / 'sysroot/usr/bin/patchelf'), '--set-rpath', rpath, str(probe)], check=True)
    report['private_test_compositor_sha256'] = hashlib.sha256(probe.read_bytes()).hexdigest()
    shutil.copyfile(build / 'compiled/src/compositor/plugins/libdefault.so', output / 'libdefault.so')
    linked = subprocess.run(['ldd', '-r', '/usr/bin/gnome-shell'], env=env,
                            text=True, capture_output=True, check=True, timeout=20)
    if 'not found' in linked.stdout + linked.stderr or 'undefined symbol' in linked.stdout + linked.stderr:
        raise RuntimeError('Staged GNOME Shell has unresolved runtime dependencies: ' + linked.stdout + linked.stderr)
    report['shell_version'] = subprocess.check_output(['/usr/bin/gnome-shell', '--version'], env=env, text=True, timeout=10).strip()
    report['installed'] = False
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
