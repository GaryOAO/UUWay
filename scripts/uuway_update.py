#!/usr/bin/python3
"""uuway_update: ask GitHub for the newest UUWay release, and fetch it.

The user's half of `uuway update`.  It runs unprivileged, so the user's own proxy settings
apply, and it believes as little as it can: the answer is only a tag name (checked against
a strict pattern), every URL is built here from that tag instead of being copied from the
answer, redirects may only lead to GitHub's own hosts, and each response has a size limit
and a deadline.  What it downloads is checked by uuway_update_apply before anything is installed.
"""
import collections
import http.client
import json
import os
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

import uuway_update_apply as checks

REPO = 'GaryOAO/UUWay'
API_URL = f'https://api.github.com/repos/{REPO}/releases/latest'
DOWNLOAD_URL = f'https://github.com/{REPO}/releases/download/{{tag}}/{{name}}'
RELEASE_PAGE = f'https://github.com/{REPO}/releases/tag/{{tag}}'
ALLOWED_HOSTS = ('github.com', 'api.github.com')
ALLOWED_SUFFIX = '.githubusercontent.com'  # where GitHub serves release assets from
SMALL_LIMIT = 1 << 20  # the API answer and SHA256SUMS
DEB_LIMIT = 300 << 20
DEADLINE = 900  # seconds for everything one command downloads
REQUEST_TIMEOUT = 30  # seconds without an answer from one request
USER_AGENT = 'uuway-update'

Latest = collections.namedtuple('Latest', 'tag version published etag url')


class FetchError(Exception):
    """GitHub could not be asked, or its answer cannot be used."""


class NotFound(FetchError):
    pass


class NotModified(Exception):
    """The cached answer is still the newest one."""


def host_allowed(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != 'https' or parts.username or parts.password:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or '').lower()
    if port not in (None, 443):
        return False
    return host in ALLOWED_HOSTS or (host.endswith(ALLOWED_SUFFIX) and len(host) > len(ALLOWED_SUFFIX))


class RedirectPolicy(urllib.request.HTTPRedirectHandler):
    """urllib would follow a redirect to any http, https or ftp address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not host_allowed(newurl):
            raise FetchError(f'GitHub 把下载重定向到了不受信任的地址，已拒绝：{newurl[:80]}')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_opener():
    """https only, the user's proxy settings, and the redirect policy above."""
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler(), urllib.request.HTTPSHandler(), RedirectPolicy(),
                    urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


def explain(error):
    """The exception to raise for an HTTP error status."""
    code = error.code
    if code == 304:
        return NotModified()
    if code == 404:
        return NotFound('GitHub 上没有找到这个文件或版本。')
    if code in (403, 429) and error.headers.get('X-RateLimit-Remaining') == '0':
        return FetchError('GitHub 访问次数已用完（限流），请稍后再试。')
    return FetchError(f'GitHub 返回了 HTTP {code}。')


class Fetcher:
    def __init__(self, opener=None, *, deadline=DEADLINE, clock=time.monotonic):
        self.opener = opener or default_opener()
        self.deadline = deadline
        self.clock = clock
        self.started = None

    def _check_deadline(self):
        now = self.clock()
        if self.started is None:
            self.started = now
        if now - self.started > self.deadline:
            raise FetchError('下载超时，已放弃。')

    def _open(self, url, headers):
        if not host_allowed(url):
            raise FetchError(f'拒绝访问不受信任的地址：{url[:80]}')
        self._check_deadline()
        api = urllib.parse.urlsplit(url).hostname == 'api.github.com'
        request = urllib.request.Request(url, headers={
            'User-Agent': USER_AGENT, 'Accept': 'application/vnd.github+json' if api else '*/*', **(headers or {})})
        try:
            return self.opener.open(request, timeout=REQUEST_TIMEOUT)
        except urllib.error.HTTPError as error:
            problem = explain(error)
            error.close()
            raise problem from None
        except FetchError:
            raise
        except (urllib.error.URLError, OSError, ssl.SSLError, http.client.HTTPException) as error:
            reason = getattr(error, 'reason', None) or error
            raise FetchError(f'无法连接 GitHub：{reason}') from None

    def _read(self, response, limit, sink):
        announced = response.headers.get('Content-Length')
        announced = int(announced) if announced and announced.isdigit() else None
        if announced is not None and announced > limit:
            raise FetchError(f'GitHub 返回的内容过大（{announced} 字节），已拒绝。')
        total = 0
        while True:
            self._check_deadline()
            try:
                block = response.read(65536)
            except (OSError, http.client.HTTPException, ssl.SSLError) as error:
                # A connection can break halfway through a long download, after _open has succeeded.
                raise FetchError(f'下载中断：{getattr(error, "reason", None) or error}') from None
            if not block:
                if announced is not None and total != announced:
                    # http.client ends a body that stops early without complaining; a short deb
                    # would otherwise be taken for a whole one and later look like tampering.
                    raise FetchError(f'下载中断：只收到 {total} / {announced} 字节。')
                return
            total += len(block)
            if total > limit:
                raise FetchError('GitHub 返回的内容过大，已拒绝。')
            sink(block)

    def get(self, url, limit, headers=None):
        """(body, response headers with lower-case names)."""
        chunks = []
        with self._open(url, headers) as response:
            self._read(response, limit, chunks.append)
        return b''.join(chunks), {key.lower(): value for key, value in response.headers.items()}

    def save(self, url, path, limit):
        """Download to `path`: private, and either all of it or nothing."""
        path = Path(path)
        part = path.with_name(path.name + '.part')
        with self._open(url, None) as response:
            try:
                part.unlink(missing_ok=True)  # a symlink left there is removed, never followed
                descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            except OSError as error:
                raise FetchError(f'无法保存下载（{error.strerror}）：{part}') from None
            try:
                with os.fdopen(descriptor, 'wb') as handle:
                    self._read(response, limit, handle.write)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(part, path)
            except FetchError:
                part.unlink(missing_ok=True)
                raise
            except OSError as error:  # a full disk, mostly
                part.unlink(missing_ok=True)
                raise FetchError(f'无法保存下载（{error.strerror}）：{path}') from None
            except BaseException:
                part.unlink(missing_ok=True)
                raise


def latest_release(fetcher, etag=None):
    """The newest published release as a Latest, or None when `etag` says nothing changed."""
    try:
        body, headers = fetcher.get(API_URL, SMALL_LIMIT, {'If-None-Match': etag} if etag else {})
    except NotModified:
        return None
    except NotFound:
        raise FetchError('GitHub 上还没有已发布的正式版本。') from None
    try:
        data = json.loads(body)
    except ValueError:
        raise FetchError('GitHub 返回的版本信息无法解析。') from None
    tag = data.get('tag_name') if isinstance(data, dict) else None
    match = checks.TAG.fullmatch(tag) if isinstance(tag, str) else None
    if not match or data.get('draft') or data.get('prerelease'):
        raise FetchError('GitHub 返回了意料之外的版本信息，已忽略。')
    published = data.get('published_at')
    return Latest(tag, match.group(1), published[:32] if isinstance(published, str) else '',
                  headers.get('etag'), RELEASE_PAGE.format(tag=tag))


def download_release(fetcher, tag, dest, before_deb=None, *, target=checks.DEFAULT_TARGET):
    """Fetch SHA256SUMS and the deb of `tag` into `dest` (created 0700).

    `before_deb(dest)` runs once SHA256SUMS is there and before the deb is fetched, so a release
    that is not laid out as expected costs a kilobyte instead of the whole download.
    """
    match = checks.TAG.fullmatch(tag)
    if not match:
        raise FetchError(f'发布标签（tag）必须形如 vX.Y.Z，收到的是 {tag!r}。')
    dest = Path(dest)
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    package = checks.package_name(match.group(1), target)
    for name, limit in (('SHA256SUMS', SMALL_LIMIT), (package, DEB_LIMIT)):
        if name == package and before_deb is not None:
            before_deb(dest)
        try:
            fetcher.save(DOWNLOAD_URL.format(tag=tag, name=name), dest / name, limit)
        except NotFound:
            raise FetchError(f'{tag} 缺少 {name}。') from None
    return dest


def download_previous(fetcher, version, dest, *, target):
    """Keep a verified old native package locally before an RPM/Arch update starts."""
    if checks.TARGETS[target][0] == 'deb':
        return
    tag = 'v' + version
    if not checks.TAG.fullmatch(tag):
        raise FetchError('开发构建没有正式回滚包，请先手动安装当前发行版的正式安装包。')
    previous = Path(dest) / 'previous'
    download_release(fetcher, tag, previous, target=target,
                     before_deb=lambda stage: checks.check_checksums(stage, tag, target=target))
    checks.check_release(previous, tag, version, target=target, require_newer=False)
