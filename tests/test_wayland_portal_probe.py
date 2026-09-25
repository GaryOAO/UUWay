import subprocess
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/probe-wayland-portal.py'
BINARY = ROOT / 'build/native-presenter/uu-pipewire-dmabuf-probe'
spec = importlib.util.spec_from_file_location('wayland_portal_probe', SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class WaylandPortalProbeTests(unittest.TestCase):
    def test_cursor_policy_is_explicit_and_capability_checked(self):
        self.assertEqual(probe.cursor_mode_value('embedded', 7), 2)
        self.assertEqual(probe.cursor_mode_value('hidden', 7), 1)
        self.assertEqual(probe.cursor_mode_value('metadata', 7), 4)
        self.assertEqual(probe.cursor_mode_value('composited', 7), 4)
        with self.assertRaises(RuntimeError):
            probe.cursor_mode_value('composited', 3)
        for name, available in [('hidden', 2), ('embedded', 1), ('hidden', 0)]:
            with self.subTest(name=name, available=available), self.assertRaises(RuntimeError):
                probe.cursor_mode_value(name, available)
        with self.assertRaises(ValueError):
            probe.cursor_mode_value('unknown', 7)

    def test_new_diagnostic_arguments_fail_before_requesting_permission(self):
        for args in [['--source', 'rdp'], ['--authorization-timeout', '4'],
                     ['--authorization-timeout', '121'], ['--snapshot', '/unused'],
                     ['--cursor-mode', 'metadata'], ['--cursor-mode', 'composited'], ['--capture-status-fd', '3'],
                     ['--capture-status-fd', '3', '--gpu-relay-fd', '3']]:
            result = subprocess.run(['/usr/bin/python3', str(SCRIPT), *args],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn('Portal CreateSession:', result.stderr)

    def test_composition_report_requires_real_and_consistent_counts(self):
        good = 'UURB_CURSOR_COMPOSITE {"cursor_only_frames":3,"desktop_frames":7}'
        self.assertEqual(probe.composition_report(good, 10),
                         dict(cursor_composited=True, cursor_only_frames=3, desktop_frames=7))
        for value, frames in [('', 10), (good + '\n' + good, 10), (good, 11), (good, True),
                ('UURB_CURSOR_COMPOSITE {"cursor_only_frames":10,"desktop_frames":0}', 10),
                ('UURB_CURSOR_COMPOSITE {"cursor_only_frames":true,"desktop_frames":9}', 10),
                ('UURB_CURSOR_COMPOSITE {"cursor_only_frames":-1,"desktop_frames":11}', 10)]:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                probe.composition_report(value, frames)

    def test_private_restore_state_roundtrip_and_rotation(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / 'state.json'
            self.assertIsNone(probe.load_restore_token(state))
            probe.save_restore_token(state, 'test-token')
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)
            self.assertEqual(probe.load_restore_token(state), 'test-token')
            probe.save_restore_token(state, 'rotated')
            self.assertEqual(probe.load_restore_token(state), 'rotated')
            self.assertEqual(len(list(Path(temporary).iterdir())), 1)

    def test_insecure_or_symlink_restore_state_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / 'state.json'
            probe.save_restore_token(state, 'test-token')
            state.chmod(0o644)
            with self.assertRaises(RuntimeError):
                probe.load_restore_token(state)
            link = Path(temporary) / 'link'
            link.symlink_to(state)
            with self.assertRaises(OSError):
                probe.load_restore_token(link)
            fifo = Path(temporary) / 'fifo'
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(RuntimeError):
                probe.load_restore_token(fifo)

    def test_invalid_token_cannot_replace_valid_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / 'state.json'
            probe.save_restore_token(state, 'keep')
            for invalid in ['', None, 1, 'x' * 4097]:
                with self.assertRaises(RuntimeError):
                    probe.save_restore_token(state, invalid)
                self.assertEqual(probe.load_restore_token(state), 'keep')

    def test_capture_modes_are_mutually_exclusive(self):
        result = subprocess.run(['/usr/bin/python3', str(SCRIPT), '--native-dmabuf', '--capture-check'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)

    def test_help_does_not_request_capture(self):
        result = subprocess.run(['/usr/bin/python3', str(SCRIPT), '--help'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--capture-check', result.stdout)
        self.assertNotIn('Portal CreateSession:', result.stderr)

    def test_unknown_mode_is_rejected_before_portal_access(self):
        result = subprocess.run(['/usr/bin/python3', str(SCRIPT), '--cpu-fallback'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('Portal CreateSession:', result.stderr)

    @unittest.skipUnless(BINARY.is_file(), 'Build the native PipeWire probe first')
    def test_native_invalid_arguments_fail_before_capture(self):
        for args in [[], ['-1', '1'], ['0', '1'], ['3', '0'],
                     ['3', '1', '--cpu-fallback']]:
            with self.subTest(args=args):
                result = subprocess.run([str(BINARY), *args], capture_output=True,
                                        text=True, timeout=5)
                self.assertEqual(result.returncode, 2)
                self.assertIn('usage:', result.stderr)


if __name__ == '__main__':
    unittest.main()
