"""Hyprland's initial backend must never mutate the compositor configuration."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from argparse import Namespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from native_desktop import backend_name, hyprland_state, HyprlandDisplay, HyprlandVirtualDisplays
from native_display_guardian import DisplayRouter


MONITOR = dict(name='DP-1', width=1920, height=1080, refreshRate=60.0, x=0, y=0,
               scale=1.0, transform=0, mirrorOf='none', serial='private serial')


class HyprlandTests(unittest.TestCase):
    def test_explicit_portal_capture_does_not_require_a_fake_restore_token(self):
        spec = importlib.util.spec_from_file_location('hypr_broker_test', ROOT / 'scripts/uu-native-capture-broker.py')
        broker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(broker)
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / 'no-restore-token.json'
            with patch.dict(os.environ, XDG_CURRENT_DESKTOP='Hyprland'):
                broker.validate_restore_state(missing, explicit_outputs=True)
                with self.assertRaises(ValueError):
                    broker.validate_restore_state(missing)
            with patch.dict(os.environ, XDG_CURRENT_DESKTOP='GNOME'):
                with self.assertRaises(FileNotFoundError):
                    broker.validate_restore_state(missing, explicit_outputs=True)

    def test_desktop_selection_is_explicit(self):
        self.assertEqual(backend_name({'XDG_CURRENT_DESKTOP': 'Hyprland'}), 'hyprland')
        self.assertEqual(backend_name({'XDG_CURRENT_DESKTOP': 'ubuntu:GNOME'}), 'gnome')
        self.assertEqual(backend_name({'XDG_CURRENT_DESKTOP': 'GNOME', 'HYPRLAND_INSTANCE_SIGNATURE': 'stale'}), 'gnome')

    def test_monitor_data_is_bounded_and_does_not_export_serials(self):
        state = hyprland_state([MONITOR])
        self.assertTrue(state['read_only'])
        self.assertEqual(state['modes'][0]['width'], 1920)
        self.assertNotIn('private serial', json.dumps(state))
        for changes in ({'scale': 1.25}, {'transform': 1}, {'x': 100}, {'refreshRate': 240},
                        {'width': 8192}, {'width': '1920'}, {'refreshRate': float('nan')},
                        {'mirrorOf': 'HDMI-A-1'}, {'name': 'DP-1; dispatch exit'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                hyprland_state([dict(MONITOR, **changes)])
        for monitors in ([], [MONITOR, dict(MONITOR, name='DP-2')]):
            with self.assertRaises(ValueError):
                hyprland_state(monitors)

    def test_geometry_change_invalidates_enumeration_serial(self):
        before = hyprland_state([MONITOR])
        self.assertEqual(before['serial'], hyprland_state([copy.deepcopy(MONITOR)])['serial'])
        self.assertNotEqual(before['serial'], hyprland_state([dict(MONITOR, width=1280, height=720)])['serial'])

    def test_display_rpc_can_inspect_and_reapply_current_mode_but_never_write(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([MONITOR]))
        with tempfile.TemporaryDirectory() as temporary, \
                patch('native_desktop.subprocess.run', side_effect=run), \
                patch.dict(os.environ, HYPRLAND_INSTANCE_SIGNATURE='test-instance'):
            backend = HyprlandDisplay()
            router = DisplayRouter(backend, Path(temporary), selected_factory=HyprlandDisplay)
            state = backend.current()
            result = router.request(dict(version=1, op='inspect'))
            self.assertTrue(result['monitors'][0]['read_only'])
            request = dict(version=1, op='apply', serial=state['serial'], width=1920, height=1080)
            self.assertFalse(router.request(request)['changed'])
            with self.assertRaises(ValueError):
                router.request(dict(request, width=1280, height=720))
            virtual = HyprlandVirtualDisplays(backend)
            self.assertFalse(virtual.status()['supported'])
            self.assertEqual(virtual.release_all(), {'released': 0})
            with self.assertRaises(RuntimeError):
                virtual.ioctl(0, [])
        self.assertTrue(calls)
        self.assertTrue(all(call == ['hyprctl', '-j', 'monitors'] for call in calls))

    def test_capture_uses_the_public_portal_with_exact_output_size(self):
        spec = importlib.util.spec_from_file_location('hypr_capture_test', ROOT / 'scripts/hyprland-capture.py')
        capture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(capture)
        args = Namespace(connector='DP-1', gpu_relay_fd=8, duration_seconds=0,
                         capture_binary=Path('/private/producer'), cursor_mode='embedded',
                         cursor_state_fd=-1, capture_status_fd=9)
        with patch.object(HyprlandDisplay, 'current', return_value=hyprland_state([MONITOR])):
            argv = capture.command(args)
        self.assertEqual(Path(argv[1]).name, 'probe-wayland-portal.py')
        self.assertIn('--expected-size', argv)
        self.assertEqual(argv[argv.index('--expected-size') + 1:argv.index('--expected-size') + 3], ['1920', '1080'])
        self.assertNotIn('--restore-state', argv)
        self.assertNotIn('--cursor-state-fd', argv)


if __name__ == '__main__':
    unittest.main()
