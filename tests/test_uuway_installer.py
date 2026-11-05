"""Official downloads must be complete executables, with atomic replacement."""
import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import uuway_installer as installer
import uuway_platform as platform


def executable():
    value = bytearray(128)
    value[:2] = b'MZ'
    struct.pack_into('<I', value, 60, 64)
    value[64:70] = b'PE\x00\x00\x4c\x01'
    return bytes(value)


class Response(io.BytesIO):
    def __init__(self, body, length=None, url='https://a56.gdl.netease.com/installer.exe'):
        super().__init__(body)
        self.headers = {'Content-Length': str(len(body) if length is None else length)}
        self.url = url

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, response):
        self.response = response

    def open(self, request, timeout):
        assert request.full_url == installer.INSTALLER_URL
        return self.response


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / 'private/UU.exe'

    def test_an_official_executable_download_is_private_and_complete(self):
        result = installer.download(self.destination, opener=Opener(Response(executable())))
        self.assertEqual(self.destination.read_bytes(), executable())
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result['bytes'], len(executable()))
        self.assertEqual(len(result['sha256']), 64)

    def test_bad_or_partial_download_preserves_the_previous_file_and_cleans_up(self):
        self.destination.parent.mkdir(mode=0o700)
        self.destination.write_bytes(b'previous')
        for response in (Response(b'<html>error</html>'), Response(executable(), length=999),
                         Response(executable(), url='https://malicious.example/UU.exe'),
                         Response(executable(), length=installer.MAX_BYTES + 1)):
            with self.assertRaises(installer.DownloadError):
                installer.download(self.destination, opener=Opener(response))
            self.assertEqual(self.destination.read_bytes(), b'previous')
            self.assertEqual(list(self.destination.parent.iterdir()), [self.destination])

    def test_redirect_policy_rejects_insecure_and_lookalike_hosts(self):
        for url in ('http://a56.gdl.netease.com/x', 'https://evilnetease.com/x',
                    'https://a56.gdl.netease.com.evil.example/x',
                    'https://user@a56.gdl.netease.com/x', 'https://a56.gdl.netease.com:444/x'):
            self.assertFalse(installer.allowed_url(url))
        self.assertTrue(installer.allowed_url(installer.INSTALLER_URL))
        self.assertTrue(installer.allowed_url('https://a56.gdl.netease.com/UU.exe'))

    def test_a_shared_download_directory_is_rejected(self):
        self.destination.parent.mkdir(mode=0o755)
        with self.assertRaises(installer.DownloadError):
            installer.download(self.destination, opener=Opener(Response(executable())))


class DistributionTests(unittest.TestCase):
    def test_winehq_sources_match_the_actual_distribution(self):
        for distro, codename in (('ubuntu', 'noble'), ('ubuntu', 'resolute'), ('debian', 'trixie')):
            name, url = platform.winehq_source({'ID': distro, 'VERSION_CODENAME': codename})
            self.assertEqual(name, f'winehq-{codename}.sources')
            self.assertIn(f'/{distro}/dists/{codename}/', url)
        for distro in ('arch', 'fedora'):
            with self.assertRaises(ValueError):
                platform.winehq_source({'ID': distro, 'VERSION_CODENAME': 'noble'})

    def test_wine_server_version_is_exact(self):
        for version in ('wine-11.0', 'wine-11.0.1'):
            self.assertTrue(platform.wine_compatible(version))
        for version in (None, 'wine-11.21', 'wine-11.01', 'wine-11.0-rc1', 'wine-10.0'):
            self.assertFalse(platform.wine_compatible(version))

    def test_native_packages_cannot_opt_into_the_ubuntu_updater_by_omission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'package-info.json').write_text('{"target":"debian-13","version":"1.4.3"}')
            self.assertFalse(platform.ota_supported(root))


if __name__ == '__main__':
    unittest.main()
