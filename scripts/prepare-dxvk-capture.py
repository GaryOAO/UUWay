#!/usr/bin/python3
"""Pinned private build dependencies; no apt install or system compiler changes."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def prepare():
    manifest = json.loads((ROOT / 'config/dxvk-capture-dependencies.json').read_text())
    downloads = ROOT / 'build/gpu-relay/downloads'
    toolchain = ROOT / 'build/dxvk-capture/toolchain'
    downloads.mkdir(parents=True, exist_ok=True)
    toolchain.mkdir(parents=True, exist_ok=True)
    for package in manifest['packages']:
        archive = downloads / package['file']
        if not archive.is_file():
            partial = archive.with_suffix('.deb.part')
            subprocess.run(['curl', '--fail', '--location', '--proto', '=https', '--proto-redir', '=https',
                            '--retry', '3', '--max-time', '120', package['url'], '-o', str(partial)],
                           check=True, timeout=150)
            if hashlib.sha256(partial.read_bytes()).hexdigest() != package['sha256']:
                raise RuntimeError('Downloaded DXVK dependency hash mismatch: ' + package['file'])
            partial.rename(archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != package['sha256']:
            raise RuntimeError('Cached DXVK dependency hash mismatch: ' + package['file'])
        if not (toolchain / package['marker']).is_file():
            subprocess.run(['dpkg-deb', '-x', str(archive), str(toolchain)], check=True, timeout=30)
    print('Verified and prepared private DXVK build tools; system packages untouched.')


if __name__ == '__main__':
    prepare()
