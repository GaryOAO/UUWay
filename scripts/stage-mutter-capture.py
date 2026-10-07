#!/usr/bin/python3
"""Stage a fresh private library bundle; validate against installed GNOME ABI.
No service/config changes. Never overwrites a bundle or the system libraries.

The bundle carries start-gnome-shell, which loads the private libmutter only
while the system Mutter it was built against is still installed.
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


SYSTEM = Path('/usr/lib/x86_64-linux-gnu')
GATE = """#!/bin/sh
# Start GNOME Shell on this bundle's capture-pacing libmutter only while the
# system Mutter it was built against is still installed. The private library
# keeps the system Clutter/Cogl; after a Mutter update their class sizes no
# longer match and the shell would crash at every start, so fall back to the
# stock library instead.
bundle=$(dirname "$(readlink -f "$0")")
if sha256sum --status --check "$bundle/system-mutter.sha256" 2>/dev/null; then
    LD_LIBRARY_PATH="$bundle${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export LD_LIBRARY_PATH
else
    echo "uurb: system Mutter changed since $bundle was built; starting the stock library" >&2
fi
exec @SHELL@ "$@"
"""


def system_library(name):
    return SYSTEM / name if name == 'libmutter-14.so.0.0.0' else SYSTEM / 'mutter-14' / name


def gate_script(shell='/usr/bin/gnome-shell'):
    return GATE.replace('@SHELL@', shell)


def debian_version(changelog):
    """Version in the first entry of a Debian changelog, e.g. 46.2-1ubuntu0.24.04.16."""
    first = changelog.read_text().split('\n', 1)[0]
    return first[first.index('(') + 1:first.index(')')]


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
    built = debian_version(build / 'mutter-46.2/debian/changelog')
    installed_version = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', 'libmutter-14-0'], text=True)
    if built != installed_version:
        parser.error(f'Build is Mutter {built} but {installed_version} is installed')
    patch = (ROOT / 'patches/mutter-46.2-capture-jitter-candidate.patch').read_bytes()
    finish_patch = (ROOT / 'patches/mutter-46.2-screencast-dmabuf-finish.patch').read_bytes()
    source_dir = build / 'mutter-46.2/src/backends'
    # Without it a busy GPU makes the remote picture jump back to old frames, so never stage an older build.
    if 'cogl_framebuffer_finish (dmabuf_fbo)' not in (source_dir / 'meta-screen-cast-stream-src.c').read_text():
        parser.error('Build lacks the dma-buf finish patch; rebuild with scripts/build-mutter-capture.sh')
    report = dict(build=str(build), patch_sha256=hashlib.sha256(patch).hexdigest(),
                  dmabuf_finish_patch_sha256=hashlib.sha256(finish_patch).hexdigest(),
                  mutter_package_version=built, system_support_libraries=args.core_only, libraries={})
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
        installed = system_library(name)
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
    # Every system Mutter library, not just the one replaced: the private build
    # is only valid beside the exact Clutter/Cogl/Mtk it was compiled against.
    pinned = {str(system_library(name)): hashlib.sha256(system_library(name).read_bytes()).hexdigest()
              for name in LIBRARIES}
    (output / 'system-mutter.sha256').write_text(''.join(f'{digest}  {path}\n' for path, digest in pinned.items()))
    gate = output / 'start-gnome-shell'
    gate.write_text(gate_script())
    gate.chmod(0o755)
    report['system_mutter_sha256'] = pinned
    report['installed'] = False
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
