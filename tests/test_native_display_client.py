import ctypes
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DisplayMode(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in
                ('width', 'height', 'refresh_millihz', 'scale_milli')]


class DisplaySnapshot(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in ('version', 'serial', 'count', 'pending')] + [
        ('current', DisplayMode), ('modes', DisplayMode * 256),
        ('scale_min_milli', ctypes.c_uint32), ('scale_max_milli', ctypes.c_uint32),
        ('scale_mask', ctypes.c_uint32)]


SNAPSHOT_BYTES = ctypes.sizeof(DisplaySnapshot)
assert SNAPSHOT_BYTES == 4140, 'display snapshot ABI v2 must match native_display_api.h'
SNAPSHOT_CANARY = b'\xa5\x3c\x7e\xc1' * 4


class GuardedDisplaySnapshot(ctypes.Structure):
    _fields_ = [('snapshot', DisplaySnapshot), ('canary', ctypes.c_ubyte * len(SNAPSHOT_CANARY))]


assert GuardedDisplaySnapshot.canary.offset == SNAPSHOT_BYTES, 'canary must immediately follow snapshot'


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='uurb-display-client-')
        cls.directory = Path(cls.temporary.name)
        library = cls.directory / 'client.so'
        subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
            str(ROOT / 'src/native_display_client.c'), '-ljson-c', '-o', str(library)],
            check=True, capture_output=True, timeout=15)
        cls.library = ctypes.CDLL(str(library))
        cls.query = cls.library.uurb_native_display_query_v2
        cls.query.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint32]
        cls.query.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def exchange(self, payload):
        endpoint = self.directory / 'display.sock'
        errors = []
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as server:
            server.bind(str(endpoint)); endpoint.chmod(0o600); server.listen(1); server.settimeout(3)
            def reply():
                try:
                    with server.accept()[0] as client:
                        client.settimeout(3)
                        self.assertEqual(json.loads(client.recv(2048)), {'version': 1, 'op': 'inspect'})
                        client.sendall(payload)
                except Exception as error:
                    errors.append(type(error).__name__)
            worker = threading.Thread(target=reply); worker.start()
            output = GuardedDisplaySnapshot()
            ctypes.memset(ctypes.byref(output.snapshot), ord('Z'), SNAPSHOT_BYTES)
            output.canary[:] = SNAPSHOT_CANARY
            try:
                status = self.query(str(endpoint).encode(), ctypes.byref(output.snapshot), SNAPSHOT_BYTES)
            finally:
                worker.join(timeout=4); endpoint.unlink()
        self.assertFalse(errors)
        self.assertFalse(worker.is_alive())
        self.assertEqual(bytes(output.canary), SNAPSHOT_CANARY, 'native query wrote past the snapshot ABI')
        return status, ctypes.string_at(ctypes.byref(output.snapshot), SNAPSHOT_BYTES)

    def test_real_snapshot_shape_and_malformed_no_partial_output(self):
        mode = {'width': 1920, 'height': 1080, 'refresh': 60, 'scales': [1, 2]}
        value = dict(mode, ok=True, serial=7, scale=1., pending=False, modes=[mode])
        status, output = self.exchange(json.dumps(value).encode())
        self.assertEqual(status, 0)
        snapshot = DisplaySnapshot.from_buffer_copy(output)
        self.assertEqual(ctypes.sizeof(snapshot), 4140)
        self.assertEqual(snapshot.version, 2)
        self.assertEqual(snapshot.serial, 7)
        self.assertEqual(snapshot.count, 1)
        self.assertEqual((snapshot.scale_min_milli, snapshot.scale_max_milli), (1000, 2000))
        self.assertEqual(snapshot.scale_mask, (1 << 0) | (1 << 4))
        invalid = [b'', b'{}', b'{"ok":true} garbage', json.dumps(dict(value, serial=True)).encode(),
                   json.dumps(dict(value, modes=[])).encode(), json.dumps(dict(value, refresh=float('nan'))).encode(), b' ' * 65537]
        for payload in invalid:
            with self.subTest(size=len(payload)):
                status, output = self.exchange(payload)
                self.assertNotEqual(status, 0)
                self.assertEqual(output, b'Z' * SNAPSHOT_BYTES)

    def test_native_rejection_stays_an_error(self):
        for kind, status in [('ValueError', 3), ('RuntimeError', 4), ('DisplayDefaultWriteError', 5)]:
            actual, output = self.exchange(json.dumps(dict(ok=False, error_type=kind)).encode())
            self.assertEqual(actual, status)
            self.assertEqual(output, b'Z' * SNAPSHOT_BYTES)

    def test_query_size_and_unsized_legacy_fail_without_writing(self):
        output = GuardedDisplaySnapshot()
        ctypes.memset(ctypes.byref(output.snapshot), ord('Z'), SNAPSHOT_BYTES)
        output.canary[:] = SNAPSHOT_CANARY
        for size in (0, 4128, 4136, SNAPSHOT_BYTES - 1, SNAPSHOT_BYTES + 4, 0xffffffff):
            self.assertEqual(self.query(b'/unused/display.sock', ctypes.byref(output.snapshot), size), 1)
        legacy = self.library.uurb_native_display_query
        legacy.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        legacy.restype = ctypes.c_int
        self.assertEqual(legacy(b'/unused/display.sock', ctypes.byref(output.snapshot)), 1)
        self.assertEqual(ctypes.string_at(ctypes.byref(output.snapshot), SNAPSHOT_BYTES), b'Z' * SNAPSHOT_BYTES)
        self.assertEqual(bytes(output.canary), SNAPSHOT_CANARY)

    def test_exact_scales_do_not_round_to_unadvertised_windows_percentages(self):
        mode = dict(width=1920, height=1080, refresh=60, scales=[1, 1.2504, 1.5, 2])
        value = dict(ok=True, serial=7, width=1920, height=1080, refresh=60, scale=1, pending=False, modes=[mode])
        status, output = self.exchange(json.dumps(value).encode())
        self.assertEqual(status, 0)
        snapshot = DisplaySnapshot.from_buffer_copy(output)
        self.assertEqual(snapshot.scale_mask, (1 << 0) | (1 << 2) | (1 << 4))
        self.assertEqual((snapshot.scale_min_milli, snapshot.scale_max_milli), (1000, 2000))

    def test_missing_or_ambiguous_current_mode_scales_fail_without_partial_output(self):
        mode = dict(width=1920, height=1080, refresh=60, scales=[1, 2])
        value = dict(ok=True, serial=7, width=1920, height=1080, refresh=60, scale=1, pending=False, modes=[mode])
        alternatives = [[dict(mode, width=1280)], [mode, dict(mode, scales=[1, 1.25, 2])],
                        [dict(mode, scales=[1] * 65)]]
        for modes in alternatives:
            with self.subTest(modes=modes):
                status, output = self.exchange(json.dumps(dict(value, modes=modes)).encode())
                self.assertNotEqual(status, 0)
                self.assertEqual(output, b'Z' * SNAPSHOT_BYTES)

    def test_actual_uu_default_reset_request_reaches_guardian_wire(self):
        endpoint = self.directory / 'display.sock'
        errors = []
        change = self.library.uurb_native_display_change
        change.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p]
        change.restype = ctypes.c_int
        request = (ctypes.c_uint32 * 8)(1, 4, 7, 1280, 720, 0, 0, 1)
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as server:
            server.bind(str(endpoint)); endpoint.chmod(0o600); server.listen(1); server.settimeout(3)
            def reply():
                try:
                    with server.accept()[0] as client:
                        client.settimeout(3)
                        self.assertEqual(json.loads(client.recv(2048)), dict(version=1, op='apply', serial=7,
                            width=1280, height=720, refresh=0, confirmation='gpu_frame', default_scope='global', force_reset=True))
                        client.sendall(b'{"ok":true,"serial":8,"changed":true}')
                except Exception as error:
                    errors.append(type(error).__name__)
            worker = threading.Thread(target=reply); worker.start()
            output = (ctypes.c_uint32 * 4)(0, 0, 0, 0)
            try:
                status = change(str(endpoint).encode(), request, output)
            finally:
                worker.join(timeout=4); endpoint.unlink()
        self.assertEqual(status, 0)
        self.assertEqual(list(output), [1, 8, 1, 0])
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
