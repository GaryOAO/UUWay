import importlib.util
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_text_portal', ROOT / 'scripts/native_text_portal.py')
portal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portal)
sys.modules['native_text_portal'] = portal
spec = importlib.util.spec_from_file_location('native_text_daemon', ROOT / 'scripts/uu-native-text.py')
daemon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daemon)


class NativeTextTests(unittest.TestCase):
    def test_text_trace_is_bounded_and_payload_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = str(Path(temporary) / 'text-trace')
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT / 'src'),
                str(ROOT / 'tests/probes/native_text_trace.c'), '-o', binary],
                check=True, capture_output=True, timeout=15)
            subprocess.run([binary], check=True, capture_output=True, timeout=5)

    def test_native_utf16_conversion_and_bounds(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = str(Path(temporary) / 'text-encode')
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT / 'src'),
                str(ROOT / 'src/native_text_client.c'), str(ROOT / 'tests/probes/native_text_encode.c'),
                '-o', binary], check=True, capture_output=True, timeout=15)
            subprocess.run([binary], check=True, capture_output=True, timeout=5)

    def make(self):
        with patch.object(portal.Gio, 'bus_get_sync', return_value=Mock()):
            backend = portal.TextPortal(Path('/private/test-restore.json'))
        backend.session = '/owned/text/session'
        backend.pump_until = lambda predicate, seconds: predicate()
        return backend

    def owner(self, backend, values, session='/owned/text/session'):
        parameters = Mock()
        parameters.unpack.return_value = (session, values)
        backend._owner_changed(None, None, None, None, None, parameters, None)

    def test_owner_format_versions_and_wrong_session(self):
        backend = self.make()
        for types in [['text/plain'], (['text/plain'],)]:
            self.owner(backend, {'session_is_owner': False, 'mime_types': types})
            self.assertEqual(backend.types, ['text/plain'])
            self.assertTrue(backend.types_known)
        epoch = backend.owner_epoch
        self.owner(backend, {'mime_types': ['ignored']}, session='/another/session')
        self.assertEqual(backend.owner_epoch, epoch)

    def test_unknown_formats_are_not_treated_as_empty_clipboard(self):
        backend = self.make()
        self.owner(backend, {'session_is_owner': False})
        with self.assertRaisesRegex(RuntimeError, 'omitted format'):
            backend.preserve()
        self.owner(backend, {})
        self.assertEqual(backend.preserve()[0], {})

    def test_decode_exact_utf8_packet_including_supplementary_character(self):
        text = '中文🙂\n\t'
        payload = text.encode()
        packet = daemon.HEADER.pack(daemon.MAGIC, 1, 7, len(payload)) + payload
        self.assertEqual(daemon.decode_request(packet), (7, text, 0))
        revision = daemon.HEADER.pack(daemon.MAGIC, 2, 8, len(payload) + 4) + (2).to_bytes(4, 'little') + payload
        self.assertEqual(daemon.decode_request(revision), (8, text, 2))
        for bad in [b'', packet[:-1], packet + b'x', daemon.HEADER.pack(daemon.MAGIC, 1, 0, len(payload)) + payload,
                    daemon.HEADER.pack(daemon.MAGIC, 1, 1, 1) + b'\0',
                    daemon.HEADER.pack(daemon.MAGIC, 1, 1, 1) + b'\xff']:
            with self.subTest(length=len(bad)), self.assertRaises(ValueError):
                daemon.decode_request(bad)

    def test_rejected_preservation_never_types_or_changes_clipboard(self):
        backend = self.make()
        backend.preserve = Mock(side_effect=RuntimeError('refused'))
        backend.key = Mock(); backend.offer = Mock()
        with self.assertRaises(RuntimeError):
            backend.commit('known fixture')
        backend.key.assert_not_called(); backend.offer.assert_not_called()
        self.assertFalse(backend.busy)

    def test_failed_paste_releases_keys_once_and_restores_owned_data(self):
        backend = self.make()
        backend.preserve = Mock(return_value=({'text/plain': b'prior fixture'}, 0))
        offered = []
        def offer(payload):
            offered.append(payload); backend.owner = True; backend.owner_epoch += 1
        backend.offer = offer
        backend.key = Mock(side_effect=[None, RuntimeError('ambiguous'), None, None])
        with self.assertRaises(RuntimeError):
            backend.commit('fixture')
        self.assertEqual([c.args for c in backend.key.call_args_list], [(29, True), (47, True), (47, False), (29, False)])
        self.assertEqual(offered[-1], {'text/plain': b'prior fixture'})
        self.assertFalse(backend.busy)

    def test_user_copy_during_paste_is_never_overwritten(self):
        backend = self.make()
        backend.preserve = Mock(return_value=({'text/plain': b'prior fixture'}, 0))
        def offer(payload):
            backend.owner = True; backend.owner_epoch += 1
        backend.offer = Mock(side_effect=offer)
        def changed(*unused):
            backend.owner = False; backend.owner_epoch += 1
        backend.key = Mock(side_effect=changed)
        with self.assertRaises(RuntimeError):
            backend.commit('fixture')
        self.assertEqual(backend.offer.call_count, 1)


if __name__ == '__main__':
    unittest.main()
