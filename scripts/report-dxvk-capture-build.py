#!/usr/bin/python3
"""Record actual pinned source and built PE hashes, not runtime acceptance."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def report():
    manifest = json.loads((ROOT / 'config/dxvk-capture-dependencies.json').read_text())
    source = ROOT / 'build/dxvk-capture/source-v3.1'
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != manifest['dxvk_revision']:
        raise RuntimeError('DXVK source revision changed')
    for name, expected in manifest['submodules'].items():
        actual = subprocess.check_output(['git', '-C', str(source / name), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != expected:
            raise RuntimeError('DXVK submodule revision changed: ' + name)
    files = {}
    for name in ('dxgi.dll', 'd3d11.dll'):
        data = (ROOT / 'build/dxvk-capture/stage' / name).read_bytes()
        if data[:2] != b'MZ':
            raise RuntimeError('Expected built PE module: ' + name)
        files[name] = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    return dict(dxvk_revision=revision, submodules=manifest['submodules'],
                packages=manifest['packages'], files=files,
                hook_sha256=hashlib.sha256((ROOT / 'src/dxvk_capture_hook.h').read_bytes()).hexdigest(),
                patch_sha256=hashlib.sha256((ROOT / 'patches/dxvk-3.1-private-capture.patch').read_bytes()).hexdigest(),
                private_build=True, default_capture_enabled=False, runtime_tested=False, uu_session_tested=False)


if __name__ == '__main__':
    print(json.dumps(report(), indent=2))
