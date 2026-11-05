#!/usr/bin/python3
"""Download the official UU Windows installer without running it."""
import argparse
import hashlib
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

INSTALLER_URL = 'https://api.nrd.nie.163.com/api/v1/release/dl/1?channel=gwqd'
MAX_BYTES = 512 << 20
DEADLINE_SECONDS = 900


class DownloadError(Exception):
    pass


def allowed_url(url):
    parts = urllib.parse.urlsplit(url)
    try:
        return (parts.scheme == 'https' and not parts.username and not parts.password
                and parts.port in (None, 443)
                and (parts.hostname == 'api.nrd.nie.163.com'
                     or (parts.hostname or '').endswith('.gdl.netease.com')))
    except ValueError:
        return False


class OfficialRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise DownloadError('官方下载跳转到了不支持的地址，请用 --installer 指定本地安装包。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_executable(path):
    with path.open('rb') as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b'MZ':
            raise DownloadError('下载结果不是 Windows 安装程序。')
        offset = struct.unpack_from('<I', header, 60)[0]
        if not 64 <= offset <= (1 << 20):
            raise DownloadError('安装程序的 PE 文件头无效。')
        stream.seek(offset)
        if stream.read(4) != b'PE\x00\x00' or stream.read(2) not in (b'\x4c\x01', b'\x64\x86'):
            raise DownloadError('安装程序不是可识别的 x86/x64 Windows 程序。')


def download(destination, *, opener=None):
    destination = Path(destination)
    directory = destination.parent
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = directory.lstat()
    if directory.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DownloadError('安装包下载目录必须仅当前用户可访问。')
    opener = opener or urllib.request.build_opener(OfficialRedirects())
    descriptor, name = tempfile.mkstemp(prefix='.UU-installer-', dir=directory)
    temporary = Path(name)
    started, total = time.monotonic(), 0
    digest = hashlib.sha256()
    try:
        request = urllib.request.Request(INSTALLER_URL, headers={'User-Agent': 'UUWay-installer'})
        with os.fdopen(descriptor, 'wb') as stream:
            with opener.open(request, timeout=30) as response:
                if not allowed_url(response.geturl()):
                    raise DownloadError('下载来源不是支持的 UU 官方下载地址。')
                expected = response.headers.get('Content-Length')
                if expected is not None:
                    try:
                        expected = int(expected)
                    except ValueError as error:
                        raise DownloadError('下载接口返回了无效文件大小。') from error
                    if not 0 < expected <= MAX_BYTES:
                        raise DownloadError('安装包大小超出允许范围。')
                while True:
                    if time.monotonic() - started > DEADLINE_SECONDS:
                        raise DownloadError('安装包下载超时，请稍后重试或使用 --installer。')
                    block = response.read(1 << 20)
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_BYTES:
                        raise DownloadError('安装包超过 512 MiB，已停止下载。')
                    stream.write(block)
                    digest.update(block)
                if total == 0 or (expected is not None and total != expected):
                    raise DownloadError('安装包下载不完整，请重试。')
            stream.flush()
            os.fsync(stream.fileno())
        check_executable(temporary)
        os.replace(temporary, destination)
    except (OSError, urllib.error.URLError) as error:
        raise DownloadError('无法下载 UU 官方安装包，请检查网络或使用 --installer 指定本地文件。') from error
    finally:
        temporary.unlink(missing_ok=True)
    return dict(path=str(destination), bytes=total, sha256=digest.hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = download(args.output)
    except DownloadError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f'UU 官方安装包已下载：{result["path"]}（{result["bytes"]} bytes，SHA256 {result["sha256"]}）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
