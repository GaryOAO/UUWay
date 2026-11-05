#!/usr/bin/python3
"""Install Arch's official Wine 11.0 binary in an isolated directory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

URL = 'https://archive.archlinux.org/packages/w/wine/wine-11.0-1-x86_64.pkg.tar.zst'
SHA256 = 'c93a939de66f3ddfa378b3ea2fb01f800477955f4cb623a81e94da1835b20c56'


def default_directory():
    return Path.home() / '.local/share/uuway/wine11'


def install(directory=None):
    directory = Path(directory) if directory is not None else default_directory()
    if directory.exists() or directory.is_symlink():
        raise ValueError(f'Wine 目录已存在，请先检查或移走：{directory}')
    directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.wine11-', dir=directory.parent) as temporary:
        temporary = Path(temporary)
        archive = temporary / 'wine.pkg.tar.zst'
        subprocess.run(['curl', '-fsSL', '--proto', '=https', '--proto-redir', '=https',
                        '--retry', '3', '--connect-timeout', '30', '--max-time', '300',
                        '--max-filesize', str(256 << 20), URL, '-o', str(archive)], check=True)
        digest = hashlib.sha256()
        with archive.open('rb') as source:
            for block in iter(lambda: source.read(1 << 20), b''):
                digest.update(block)
        if digest.hexdigest() != SHA256:
            raise ValueError('Wine 官方归档包校验失败。')
        payload = temporary / 'payload'
        payload.mkdir(mode=0o700)
        subprocess.run(['bsdtar', '--no-same-owner', '-xf', str(archive), '-C', str(payload)], check=True)
        executable = payload / 'usr/bin/wine'
        result = subprocess.run([str(executable), '--version'], capture_output=True, text=True, check=True)
        if result.stdout.strip() != 'wine-11.0':
            raise ValueError('Wine 官方归档包不是 11.0。')
        (payload / 'uuway-origin.json').write_text(json.dumps(dict(url=URL, sha256=SHA256, version='11.0')) + '\n')
        os.replace(payload, directory)
    return directory / 'usr/bin'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    try:
        print(install(args.destination))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f'Wine 安装失败：{error}\n')
