"""Cursor policy stays identical for physical and virtual RecordMonitor sources."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import subprocess

capture = None
try:
    spec = importlib.util.spec_from_file_location('mutter_capture',
        Path(__file__).resolve().parents[1] / 'scripts/mutter-direct-capture.py')
    capture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capture)
except ImportError:
    capture = None


@unittest.skipUnless(capture is not None, 'PyGObject is required')
class CaptureCursorPolicyTests(unittest.TestCase):
    def test_every_policy_overrides_stale_inherited_cursor_settings(self):
        dirty = dict(UURB_CURSOR_COMPOSITE='1', UURB_CURSOR_STATE_FD='99',
                     UURB_CURSOR_METADATA_BACKEND='spa', UURB_CAPTURE_STATUS_FD='98',
                     KEEP_ME='unchanged')
        for mode, mutter_mode in [('hidden',0),('embedded',1),('metadata',2),('composited',2)]:
            with self.subTest(mode=mode):
                args = SimpleNamespace(cursor_mode=mode,duration_seconds=0,cursor_state_fd=4,capture_status_fd=5)
                env = capture.producer_environment(args,dirty)
                self.assertEqual(capture.CURSOR_MODES[mode],mutter_mode)
                self.assertEqual(env.get('UURB_CURSOR_COMPOSITE'),'1' if mode=='composited' else None)
                self.assertEqual(env.get('UURB_CURSOR_METADATA_BACKEND'),
                                 'mutter' if mode in ('metadata','composited') else None)
                self.assertEqual(env.get('UURB_CURSOR_STATE_FD'),'4' if mode in ('metadata','composited') else None)
                self.assertEqual(env['UURB_CAPTURE_STATUS_FD'],'5')
                self.assertEqual(env['KEEP_ME'],'unchanged')

    def test_status_descriptor_is_not_inherited_when_absent(self):
        args = SimpleNamespace(cursor_mode='metadata',duration_seconds=0,cursor_state_fd=4,capture_status_fd=-1)
        env = capture.producer_environment(args,dict(UURB_CAPTURE_STATUS_FD='99'))
        self.assertNotIn('UURB_CAPTURE_STATUS_FD',env)

    def test_record_failure_stops_the_created_session(self):
        args = SimpleNamespace(cursor_mode='hidden', duration_seconds=1, cursor_state_fd=-1,
                               capture_status_fd=-1, gpu_relay_fd=3,
                               capture_binary=Path('/fixture/capture'), connector='fixture')
        bus = Mock()
        with patch.object(capture.Gio, 'bus_get_sync', return_value=bus), \
             patch.object(capture, 'call', side_effect=[('/session',), RuntimeError('record failed'), ()]) as call, \
             patch.object(capture.signal, 'signal'):
            with self.assertRaisesRegex(RuntimeError, 'record failed'):
                capture.run(args)
        self.assertEqual(call.call_args.args[3], 'Stop')
        bus.signal_unsubscribe.assert_not_called()

    def test_timeout_reaps_child_and_releases_every_owned_resource(self):
        args = SimpleNamespace(cursor_mode='hidden', duration_seconds=1, cursor_state_fd=-1,
                               capture_status_fd=-1, gpu_relay_fd=3,
                               capture_binary=Path('/fixture/capture'), connector='fixture')
        bus, child = Mock(), Mock()
        def subscribe(*arguments):
            arguments[-1](None, None, None, None, None, SimpleNamespace(unpack=lambda: (77,)))
            return 9
        bus.signal_subscribe.side_effect = subscribe
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('capture', 6),
                                 subprocess.TimeoutExpired('capture', 2), 0]
        with patch.object(capture.Gio, 'bus_get_sync', return_value=bus), \
             patch.object(capture, 'call', side_effect=[('/session',), ('/stream',), (), ()]) as call, \
             patch.object(capture.socket, 'socket') as socket, \
             patch.object(capture.subprocess, 'Popen', return_value=child), \
             patch.object(capture.signal, 'signal'), \
             patch.dict(capture.os.environ, XDG_RUNTIME_DIR='/fixture/runtime'):
            socket.return_value.__enter__.return_value.fileno.return_value = 4
            with self.assertRaises(subprocess.TimeoutExpired):
                capture.run(args)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 3)
        self.assertEqual(call.call_args.args[3], 'Stop')
        bus.signal_unsubscribe.assert_called_once_with(9)
        socket.return_value.__exit__.assert_called_once()
