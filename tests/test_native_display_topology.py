import copy
import os
from pathlib import Path
import sys
import tempfile
import shutil
import subprocess
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import native_display_topology as topology
from native_display_config import decode_monitors
from native_display_guardian import DisplayRouter


def fixture():
    specs = [('DP-fixture', 'vendor', 'screen', 'private'), ('Virtual-fixture', 'vendor', 'screen', 'private')]
    monitors = [(spec, [('1080p', 1920, 1080, 60., 1., [1., 2.], {'is-current': True}),
                        ('720p', 1280, 720, 60., 1., [1., 2.], {})], {}) for spec in specs]
    return (7, monitors, [(0, 0, 1., 0, True, [specs[0]], {}),
                          (1920, 0, 1., 0, False, [specs[1]], {})], {'layout-mode': 1})


class FakeBus:
    def __init__(self):
        self.reply = fixture()
        self.calls = []

    def call_sync(self, owner, path, interface, method, arguments, *unused):
        if method == 'GetCurrentState':
            return topology._PlanningVariant(copy.deepcopy(self.reply))
        if method != 'ApplyMonitorsConfig':
            raise AssertionError(method)
        serial, operation, configuration, properties = arguments.unpack()
        if serial != self.reply[0]:
            raise RuntimeError('stale serial')
        self.calls.append(operation)
        # Accept only adjacent, nonoverlapping fixture outputs, just as Mutter
        # requires. Inspect the actual production D-Bus argument tree.
        physical = copy.deepcopy(self.reply[1])
        positions = []
        layouts = []
        for x, y, scale, transform, primary, members in configuration:
            self.assert_member(members)
            name, mode_id, props = members[0]
            spec, modes, _ = next(item for item in physical if item[0][0] == name)
            mode = next(item for item in modes if item[0] == mode_id)
            for item in modes:
                item[-1]['is-current'] = item[0] == mode_id
            positions.append((x, x + mode[1]))
            layouts.append((x, y, scale, transform, primary, [spec], {}))
        positions.sort()
        if positions[0][1] != positions[1][0]:
            raise ValueError('gap or overlap')
        if operation:
            self.reply = (serial + 1, physical, layouts, self.reply[3])
        return topology._PlanningVariant(())

    @staticmethod
    def assert_member(members):
        if len(members) != 1:
            raise AssertionError('unexpected clone group')


class Selected(topology.SelectedMutterDisplay):
    def __init__(self, connector, bus):
        self.connector, self.bus, self.owner = connector, bus, 'fixture-owner'

    def identity(self):
        return ['fixture-boot', 'fixture-bus', self.owner, self.connector]

    def _reply(self):
        return copy.deepcopy(self.bus.reply)


class TopologyTests(unittest.TestCase):
    def make(self, directory):
        bus = FakeBus()
        backend = SimpleNamespace(monitors=lambda: decode_monitors(bus.reply))
        router = DisplayRouter(backend, directory, lambda name: Selected(name, bus))
        return router, bus

    def test_primary_resize_moves_adjacent_output_and_confirmed_default(self):
        flags = SimpleNamespace(DBusCallFlags=SimpleNamespace(NO_AUTO_START=0))
        with tempfile.TemporaryDirectory() as temporary, patch.object(topology, 'Gio', flags):
            router, bus = self.make(Path(temporary))
            result = router.request(dict(version=1, op='apply', output=0, serial=7, width=1280, height=720,
                                         confirmation='gpu_frame', default_scope='global'))
            self.assertTrue(result['changed'])
            monitors = decode_monitors(bus.reply)
            self.assertEqual([(m['x'], m['width']) for m in monitors], [(0, 1280), (1280, 1920)])
            self.assertEqual(monitors[1]['height'], 1080)
            router.request(dict(version=1, op='confirm', output=0, serial=8, transaction=result['transaction']))
            self.assertIsNone(router.guardians[0].transaction.pending)
            self.assertEqual(router.request(dict(version=1, op='inspect'))['registry']['width'], 1280)

    def test_secondary_resize_rolls_back_and_blocks_competing_transaction(self):
        flags = SimpleNamespace(DBusCallFlags=SimpleNamespace(NO_AUTO_START=0))
        with tempfile.TemporaryDirectory() as temporary, patch.object(topology, 'Gio', flags):
            router, bus = self.make(Path(temporary))
            before = decode_monitors(bus.reply)
            result = router.request(dict(version=1, op='apply', output=1, serial=7, width=1280, height=720,
                                         confirmation='gpu_frame'))
            self.assertTrue(result['changed'])
            after = decode_monitors(bus.reply)
            self.assertEqual((after[0]['width'], after[0]['height'], after[1]['x']), (1920, 1080, 1920))
            with self.assertRaises(RuntimeError):
                router.request(dict(version=1, op='apply', serial=8, width=1280, height=720))
            router.guardians[1].deadline = 0
            self.assertEqual(router.expire(), {1: 'rolled_back'})
            self.assertEqual(decode_monitors(bus.reply), before)

    def test_stale_and_invalid_output_requests_do_not_write(self):
        flags = SimpleNamespace(DBusCallFlags=SimpleNamespace(NO_AUTO_START=0))
        with tempfile.TemporaryDirectory() as temporary, patch.object(topology, 'Gio', flags):
            router, bus = self.make(Path(temporary))
            for output in (-1, 2, True, '1'):
                with self.assertRaises(ValueError):
                    router.request(dict(version=1, op='inspect', output=output))
            with self.assertRaises(RuntimeError):
                router.request(dict(version=1, op='apply', output=1, serial=6, width=1280, height=720))
            self.assertEqual(bus.calls, [])

    def test_exclusive_output_preserves_layout_and_unblocks_enumeration(self):
        flags = SimpleNamespace(DBusCallFlags=SimpleNamespace(NO_AUTO_START=0))
        with tempfile.TemporaryDirectory() as temporary, patch.object(topology, 'Gio', flags):
            router, bus = self.make(Path(temporary))
            router.request(dict(version=1, op='apply', serial=7, width=1280, height=720,
                                confirmation='gpu_frame'))
            old = router.guardians[0]
            # Model the compositor's exclusive virtual layout, including the
            # physical connector remaining connected but no longer active.
            serial, physical, logical, properties = copy.deepcopy(bus.reply)
            for mode in physical[0][1]:
                mode[-1]['is-current'] = False
            virtual = logical[1]
            bus.reply = (serial + 1, physical, [(0, 0, 1., 0, True, virtual[5], {})], properties)
            exclusive = copy.deepcopy(bus.reply)
            state = router.request(dict(version=1, op='inspect'))
            self.assertEqual(state['monitors'][0]['connector'], 'Virtual-fixture')
            self.assertFalse(state['pending'])
            self.assertIsNone(old.transaction.pending)
            self.assertEqual(bus.reply, exclusive)
            self.assertEqual(bus.calls, [0, 1])
            self.assertIsNone(router.expire())

    def test_inactive_pending_output_expires_without_observing_or_restoring_it(self):
        flags = SimpleNamespace(DBusCallFlags=SimpleNamespace(NO_AUTO_START=0))
        with tempfile.TemporaryDirectory() as temporary, patch.object(topology, 'Gio', flags):
            router, bus = self.make(Path(temporary))
            router.request(dict(version=1, op='apply', serial=7, width=1280, height=720))
            old = router.guardians[0]
            serial, physical, logical, properties = copy.deepcopy(bus.reply)
            for mode in physical[0][1]:
                mode[-1]['is-current'] = False
            bus.reply = (serial + 1, physical, [(0, 0, 1., 0, True, logical[1][5], {})], properties)
            with patch.object(old.backend, 'current', side_effect=AssertionError('inactive output observed')):
                self.assertEqual(router.expire(), {0: 'inactive_output_preserved'})
            self.assertIsNone(old.transaction.pending)
            self.assertIsNone(old.registry_candidate)
            self.assertEqual(bus.calls, [0, 1])


@unittest.skipUnless(Path('/opt/wine-stable/bin/wine').exists() and shutil.which('xvfb-run')
                     and shutil.which('x86_64-w64-mingw32-gcc'), 'Wine, MinGW and Xvfb required')
class NativeMultiChangeTests(unittest.TestCase):
    def test_production_win32_shim_routes_uu_flags_and_output_index(self):
        with tempfile.TemporaryDirectory(prefix='uuway-multi-win32-') as temporary:
            directory = Path(temporary)
            executable = directory / 'multi.exe'
            subprocess.run(['x86_64-w64-mingw32-gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-municode',
                            str(ROOT / 'tests/probes/native_display_multi_change.c'), '-luser32', '-lgdi32', '-lws2_32',
                            '-o', str(executable)], capture_output=True, check=True, timeout=30)
            env = dict(os.environ, WINEPREFIX=str(directory / 'wine'), WINEDEBUG='-all',
                       WINEDLLOVERRIDES='mscoree,mshtml=')
            env.pop('WINEDLLPATH', None)
            try:
                with (directory / 'probe.log').open('w') as log:
                    result = subprocess.run(['xvfb-run', '-a', '/opt/wine-stable/bin/wine', str(executable)],
                                            env=env, stdout=log, stderr=log, timeout=45)
                output = (directory / 'probe.log').read_text()
                self.assertEqual(result.returncode, 0, output[-5000:])
                self.assertIn('MULTI Win32 selected-output and UU reset flags passed', output)
            finally:
                subprocess.run(['/opt/wine-stable/bin/wineserver', '-k'], env=env, capture_output=True, timeout=10)


if __name__ == '__main__':
    unittest.main()
