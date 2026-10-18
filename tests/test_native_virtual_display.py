import importlib.util
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import copy
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from native_virtual_display import VirtualDisplays, validate_modes, TOPOLOGY_CONTROL


class VirtualControlTests(unittest.TestCase):
    def manager(self):
        manager = VirtualDisplays.__new__(VirtualDisplays)
        manager.screens = {}
        manager.modes = {}
        manager.available = True
        manager.create = Mock()
        manager.remove = Mock()
        manager._negotiate = Mock()
        manager._event = Mock()
        manager.display_ids = {}
        manager.exclusive_baseline = None
        manager.recovery_path = None
        manager.owner_fd = None
        manager.owner_pid = None
        manager.parent_fd = None
        manager.session_updated = 0
        manager.idle_since = None
        manager.managed_connectors = set()
        return manager

    def test_display_identity_survives_exclusive_mode_and_restore(self):
        manager = self.manager()
        physical = dict(connector='DP-1', modes=[], virtual=False)
        virtual = dict(connector='Meta-0', modes=[], virtual=True)
        self.assertEqual([item['gdi_name'] for item in manager.decorate([physical, virtual])],
                         ['\\\\.\\DISPLAY1', '\\\\.\\DISPLAY2'])
        self.assertEqual(manager.decorate([dict(virtual)])[0]['gdi_name'], '\\\\.\\DISPLAY2')
        self.assertEqual(manager.decorate([dict(physical)])[0]['gdi_name'], '\\\\.\\DISPLAY1')

    def test_virtual_owner_stream_never_embeds_a_cursor_into_the_video_source(self):
        manager = self.manager()
        physical = dict(connector='DP-1',width=1920,height=1080,refresh=60,scale=1,x=0,y=0,primary=True)
        virtual = dict(physical,connector='Meta-0',virtual=True,x=1920,primary=False)
        manager.backend = Mock()
        manager.backend.monitors.side_effect = [[physical], [physical,virtual], [physical,virtual]]
        manager._topology = Mock(return_value=dict(topology=dict(logical=[])))
        manager._preserve_layout = Mock()
        manager.GLib = Mock()
        manager.GLib.Variant.side_effect = lambda signature,value: value
        manager.Gio = Mock()
        manager.bus = Mock()
        def subscribe(*args):
            args[-1](Mock(unpack=lambda: (77,)))
            return 1
        manager.bus.signal_subscribe.side_effect = subscribe
        manager.call = Mock(side_effect=[('/session',),('/stream',),()])
        VirtualDisplays.create(manager,0,[[1920,1080,60]])
        options = manager.call.call_args_list[1].args[-1][0]
        self.assertEqual(options['cursor-mode'],0)
        self.assertEqual(manager.screens[0]['connector'],'Meta-0')

    def test_exclusive_topology_then_exit_restores_the_saved_physical_layout(self):
        manager = self.manager()
        manager.display_ids = {'DP-1': 0, 'Meta-0': 1}
        manager.screens = {0: dict(slot=0, connector='Meta-0')}
        manager.modes = {0: [(2752, 2064, 144)]}
        state = dict(serial=7, topology=dict(logical=[
            dict(monitors=[dict(connector='DP-1')], size=[1920, 1080], x=0, y=0, primary=True),
            dict(monitors=[dict(connector='Meta-0')], size=[2752, 2064], x=1920, y=0, primary=False)]))
        manager._topology = Mock(return_value=state)
        manager._preserve_layout = Mock()
        manager.backend = Mock()
        manager.backend.monitors.return_value = [dict(connector='Meta-0', width=2752, height=2064, refresh=144)]
        enter = struct.pack('<3I4I2i', 7, 0, 1, 1, 2752, 2064, 144000, 0, 0)
        manager.ioctl(TOPOLOGY_CONTROL, list(enter))
        selected = manager._preserve_layout.call_args.args[0]['topology']['logical']
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['monitors'][0]['connector'], 'Meta-0')
        self.assertEqual((selected[0]['x'], selected[0]['primary']), (0, True))
        self.assertEqual(len(manager.exclusive_baseline['topology']['logical']), 1)
        self.assertEqual(manager.exclusive_baseline['topology']['logical'][0]['monitors'][0]['connector'], 'DP-1')
        manager._restore_layout = Mock()
        leave = struct.pack('<3I4I2i', 7, 0, 1, 0, 1920, 1080, 60000, 0, 0)
        manager.ioctl(TOPOLOGY_CONTROL, list(leave))
        manager._restore_layout.assert_called_once()
        self.assertIsNone(manager.exclusive_baseline)

    def test_uu_enters_with_an_advertised_mode_different_from_initial_plug(self):
        manager = self.manager()
        manager.display_ids = {'DP-1': 0, 'Meta-0': 1}
        manager.screens = {0: dict(slot=0, connector='Meta-0')}
        manager.modes = {0: [(3840, 2160, 144), (2752, 2064, 144)]}
        state = dict(serial=7, topology=dict(logical=[
            dict(monitors=[dict(connector='DP-1')], size=[1920, 1080], x=0, y=0, primary=True),
            dict(monitors=[dict(connector='Meta-0')], size=[3840, 2160], x=1920, y=0, primary=False)]))
        manager._topology = Mock(return_value=state)
        manager._preserve_layout = Mock()
        manager.backend = Mock()
        manager.backend.monitors.return_value = [dict(connector='Meta-0', width=3840, height=2160, refresh=144)]
        packet = struct.pack('<3I4I2i', 7, 0, 1, 1, 2752, 2064, 144000, 0, 0)
        manager.ioctl(TOPOLOGY_CONTROL, list(packet))
        manager._negotiate.assert_called_once_with(manager.screens[0], (2752, 2064, 144))
        manager._preserve_layout.assert_called_once()

    def test_resize_hotplug_does_not_add_reactivated_outputs_to_restore_baseline(self):
        manager = self.manager()
        manager.display_ids = {'DP-1':0,'Meta-0':1,'Virtual-1':2}
        manager.screens = {0:dict(slot=0,connector='Meta-0')}
        manager.modes = {0:[(3840,2160,144),(1680,1050,144)]}
        physical = dict(monitors=[dict(connector='DP-1')],size=[1920,1080],x=0,y=0,primary=True)
        virtual = dict(monitors=[dict(connector='Meta-0')],size=[3840,2160],x=1920,y=0,primary=False)
        unwanted = dict(monitors=[dict(connector='Virtual-1')],size=[1024,768],x=0,y=1080,primary=False)
        before = dict(serial=7,topology=dict(logical=[physical,virtual]))
        after = dict(serial=8,topology=dict(logical=[physical,unwanted,dict(virtual,size=[1680,1050])]))
        manager._topology = Mock(side_effect=[before,after])
        manager._preserve_layout = Mock()
        manager.backend = Mock()
        manager.backend.monitors.return_value = [dict(connector='Meta-0',width=3840,height=2160,refresh=144)]
        packet = struct.pack('<3I4I2i',7,0,1,1,1680,1050,144000,0,0)
        manager.ioctl(TOPOLOGY_CONTROL,list(packet))
        saved = manager.exclusive_baseline['topology']['logical']
        self.assertEqual(saved,[physical])

    def test_restore_uses_live_virtual_modes_instead_of_obsolete_pre_resize_modes(self):
        manager = self.manager()
        manager.screens = {0: dict(slot=0, connector='Meta-0')}
        physical = dict(monitors=[dict(connector='DP-1', mode='1080p')], size=[1920,1080],
                        x=0,y=0,primary=True)
        old = dict(monitors=[dict(connector='Meta-0',mode='3840x2160')],size=[3840,2160],x=1920,y=0,primary=False)
        live = dict(monitors=[dict(connector='Meta-0',mode='2752x2064')],size=[2752,2064],x=0,y=0,primary=True)
        baseline = dict(topology=dict(logical=[physical,old]))
        current = dict(topology=dict(logical=[live]))
        observed = dict(topology=dict(logical=[physical,live]))
        manager._topology = Mock(return_value=observed)
        manager._preserve_layout = Mock()
        manager._restore_layout(baseline,current)
        target = manager._preserve_layout.call_args.args[0]['topology']['logical']
        self.assertEqual([m['mode'] for item in target for m in item['monitors']], ['1080p','2752x2064'])
        self.assertEqual((target[1]['x'],target[1]['primary']), (1920,False))

    def test_track_recreation_does_not_release_but_disconnect_does(self):
        manager = self.manager()
        manager.screens = {0: dict(connector='Meta-0')}
        manager.release_all = Mock()
        manager.idle_since = 100
        with patch('native_virtual_display.time.monotonic', return_value=114):
            manager.poll_lifetime()
        manager.release_all.assert_not_called()
        with patch('native_virtual_display.time.monotonic', return_value=116):
            manager.poll_lifetime(allow_idle=False)
            manager.release_all.assert_not_called()
            manager.poll_lifetime()
        manager.release_all.assert_called_once()

    def test_restore_preserves_external_mutter_output(self):
        manager = self.manager()
        manager.screens = {0: dict(slot=0, connector='Meta-owned')}
        physical = dict(monitors=[dict(connector='DP-1')], size=[1920,1080], x=0,y=0,primary=True)
        external = dict(monitors=[dict(connector='Meta-external')], size=[1280,720], x=1920,y=0,primary=False)
        owned = dict(monitors=[dict(connector='Meta-owned')], size=[1280,720], x=0,y=0,primary=True)
        baseline = dict(topology=dict(logical=[physical,external]))
        current = dict(topology=dict(logical=[owned]))
        manager._topology = Mock(return_value=dict(topology=dict(logical=[physical,external,owned])))
        manager._preserve_layout = Mock()
        manager._restore_layout(baseline,current)
        target = manager._preserve_layout.call_args.args[0]['topology']['logical']
        self.assertEqual([m['connector'] for layout in target for m in layout['monitors']],
                         ['DP-1','Meta-external','Meta-owned'])

    def test_manual_output_disable_preserves_physical_and_protects_last_output(self):
        manager = self.manager()
        physical = dict(monitors=[dict(connector='DP-1')], size=[1920,1080], x=0,y=0,primary=True)
        kernel = dict(monitors=[dict(connector='Virtual-1')], size=[1024,768], x=1920,y=0,primary=False)
        manager._topology = Mock(return_value=dict(topology=dict(logical=[physical,kernel])))
        manager._preserve_layout = Mock()
        manager.set_output('Virtual-1',False)
        args, kwargs = manager._preserve_layout.call_args
        self.assertEqual(args[0]['topology']['logical'], [physical])
        self.assertEqual(kwargs, {})
        manager._topology.return_value = dict(topology=dict(logical=[physical]))
        manager._preserve_layout.reset_mock()
        with self.assertRaisesRegex(ValueError,'last active'):
            manager.set_output('DP-1',False)
        manager._preserve_layout.assert_not_called()

    def test_actual_owner_process_exit_releases_even_with_active_tracks(self):
        manager = self.manager()
        owner = subprocess.Popen(['sleep', '20'])
        try:
            manager.session_state(owner.pid, 1)
            manager.release_all = Mock()
            owner.terminate(); owner.wait(timeout=3)
            manager.poll_lifetime()
            manager.release_all.assert_called_once()
            self.assertIsNone(manager.parent_fd)
        finally:
            if owner.poll() is None:
                owner.kill(); owner.wait(timeout=3)
            manager.close()

    def test_owner_exit_during_registration_keeps_previous_owner_and_closes_new_fd(self):
        manager = self.manager()
        manager.owner_pid, manager.owner_fd, manager.parent_fd = 7, 41, 42
        manager.release_all = Mock()
        with patch('native_virtual_display.os.pidfd_open', side_effect=[51, ProcessLookupError]), \
             patch.object(Path, 'read_text', return_value='9 (fixture) S 8 0'), \
             patch('native_virtual_display.os.close') as close:
            with self.assertRaises(ProcessLookupError):
                manager.session_state(9, 1)
        self.assertEqual((manager.owner_pid, manager.owner_fd, manager.parent_fd), (7, 41, 42))
        manager.release_all.assert_not_called()
        close.assert_called_once_with(51)

    def test_invalid_later_topology_entry_does_not_resize_or_remove_any_output(self):
        manager = self.manager()
        manager.display_ids = {'DP-1': 0, 'Meta-0': 1, 'Meta-1': 2, 'Meta-2': 3}
        manager.screens = {slot: dict(slot=slot, connector='Meta-%d' % slot) for slot in range(3)}
        manager.modes = {slot: [(3840,2160,60), (1920,1080,60)] for slot in range(3)}
        manager._topology = Mock(return_value=dict(serial=7, topology=dict(logical=[])))
        manager.backend = Mock()
        manager.backend.monitors.return_value = [dict(connector='Meta-%d' % slot,
            width=3840,height=2160,refresh=60) for slot in range(3)]
        packet = struct.pack('<3I',7,0,2) + struct.pack('<4I2i',1,1920,1080,60000,0,0) + \
            struct.pack('<4I2i',2,1920,1080,145000,1920,0)
        with self.assertRaisesRegex(ValueError,'advertised virtual mode'):
            manager.ioctl(TOPOLOGY_CONTROL,list(packet))
        manager.remove.assert_not_called()
        manager._negotiate.assert_not_called()

    def test_uus_44_mode_packet_and_full_64_mode_catalogue_are_accepted(self):
        for count in (44, 64):
            with self.subTest(count=count):
                manager = self.manager()
                modes = [(3840 - index * 2, 2160, 60) for index in range(count)]
                packet = struct.pack('<2I', 0, count) + b''.join(struct.pack('<3I', *mode) for mode in modes)
                self.assertEqual(len(packet), 8 + count * 12)
                manager.ioctl(0x30c010, list(packet))
                self.assertEqual(manager.modes[0], modes)
                manager.ioctl(0x30c004, list(struct.pack('<6I', 0, 0, 0, 0, 0, 0)))
                manager.create.assert_called_once_with(0)

    def test_driver_control_wire_selects_only_owned_slots_and_modes(self):
        manager = self.manager()
        packet = struct.pack('<8I', 2, 2, 2560, 1440, 60, 3840, 2160, 30)
        manager.ioctl(0x30c010, list(packet))
        self.assertEqual(manager.modes[2], [(2560, 1440, 60), (3840, 2160, 30)])
        manager.ioctl(0x30c004, list(struct.pack('<6I', 2, 0, 0, 0, 0, 0)))
        manager.create.assert_called_once_with(2)
        manager.ioctl(0x30c008, list(struct.pack('<I', 2)))
        manager.remove.assert_called_once_with(2)
        manager.screens[2] = {'connector': 'fixture-owned'}
        manager.ioctl(0x30c00c, list(packet))
        manager._negotiate.assert_called_once_with(manager.screens[2], (2560, 1440, 60))

    def test_48_mode_catalogue_including_144_hz_and_mode_selection(self):
        manager = self.manager()
        # Real UU failure: 48 entries / 584 bytes, up to 3840x2160 at 144 Hz.
        sizes = [(3840 - index * 160, 2160 - index * 90) for index in range(16)]
        modes = [(width, height, rate) for width, height in sizes for rate in (60, 120, 144)]
        packet = struct.pack('<2I', 0, len(modes)) + b''.join(struct.pack('<3I', *mode) for mode in modes)
        self.assertEqual(len(packet), 584)
        manager.ioctl(0x30c010, list(packet))
        self.assertEqual(manager.modes[0], modes)
        manager.screens[0] = dict(slot=0, connector='owned')
        selected = struct.pack('<5I', 0, 1, 3840, 2160, 144)
        manager.ioctl(0x30c00c, list(selected))
        manager._negotiate.assert_called_once_with(manager.screens[0], (3840, 2160, 144))

    def test_invalid_catalogue_preserves_previous_modes_and_logs_the_rejected_entry(self):
        manager = self.manager()
        manager.modes[0] = [(1920, 1080, 60)]
        packet = struct.pack('<8I', 0, 2, 1920, 1080, 60, 2560, 1440, 145)
        with self.assertRaisesRegex(ValueError, 'index 1: 2560x1440@145'):
            manager.ioctl(0x30c010, list(packet))
        self.assertEqual(manager.modes[0], [(1920, 1080, 60)])
        self.assertEqual(manager._event.call_args.args[0], 'modes_rejected')
        manager._negotiate.assert_not_called()

    def test_malformed_driver_controls_never_create_or_remove_a_screen(self):
        manager = self.manager()
        for code, payload in [(0x30c004, [0] * 23), (0x30c004, [0] * 25),
                (0x30c010, list(struct.pack('<5I', 0, 5, 1920, 1080, 60))),
                (0x30c010, list(struct.pack('<5I', 8, 1, 1920, 1080, 60))),
                (0x30c010, list(struct.pack('<5I', 0, 1, 1921, 1080, 60))),
                (0x30c010, [0] * 1025), (0xffffffff, []), (True, []), (0, [True])]:
            with self.subTest(code=code, payload_length=len(payload)), self.assertRaises(ValueError):
                manager.ioctl(code, payload)
        manager.create.assert_not_called()
        manager.remove.assert_not_called()

    def test_unowned_outputs_are_never_relabelled_as_super_screens(self):
        manager = self.manager()
        manager.screens[0] = dict(slot=0, connector='owned')
        manager.modes[0] = [(1280, 720, 60)]
        items = manager.decorate([dict(connector='physical', virtual=False, friendly='physical', modes=[]),
                                  dict(connector='owned', virtual=False, friendly='native', modes=[])])
        self.assertFalse(items[0]['virtual'])
        self.assertEqual(items[0]['friendly'], 'physical')
        self.assertTrue(items[1]['virtual'])
        self.assertEqual(items[1]['virtual_slot'], 0)
        self.assertEqual(items[1]['modes'][0]['width'], 1280)

    def test_mode_bounds_are_checked_before_any_desktop_operation(self):
        for modes in ([], [[0, 720, 60]], [[1921, 1080, 60]], [[1920, 1080, 145]],
                      [[1920, True, 60]], [[1920, 1080]], [[1920, 1080, 60]] * 65):
            with self.assertRaises(ValueError):
                validate_modes(modes)

    def test_legacy_runtime_never_advertises_a_nonexistent_virtual_backend(self):
        manager = self.manager()
        manager.available = False
        with self.assertRaises(RuntimeError):
            manager.ioctl(0, [])
        with self.assertRaises(RuntimeError):
            VirtualDisplays.create(manager, 0)
        manager.create.assert_not_called()


@unittest.skipUnless(Path('/opt/wine-stable/bin/wine').exists() and
                     shutil.which('x86_64-w64-mingw32-gcc') and shutil.which('xvfb-run'),
                     'Wine, MinGW and Xvfb required')
class VirtualDeviceTests(unittest.TestCase):
    def test_real_win32_discovery_handles_control_validation_and_passthrough(self):
        with tempfile.TemporaryDirectory(prefix='uurb-virtual-device-') as temporary:
            directory = Path(temporary)
            executable = directory / 'device.exe'
            subprocess.run(['x86_64-w64-mingw32-gcc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                '-I', str(ROOT / 'src'), str(ROOT / 'tests/probes/native_virtual_device.c'),
                '-luser32', '-lws2_32', '-o', str(executable)], check=True, capture_output=True, timeout=30)
            env = dict(os.environ, WINEPREFIX=str(directory / 'wine'), WINEDEBUG='-all',
                       WINEDLLOVERRIDES='mscoree,mshtml=')
            env.pop('WINEDLLPATH', None)
            try:
                with (directory / 'probe.log').open('w') as log:
                    result = subprocess.run(['xvfb-run', '-a', '/opt/wine-stable/bin/wine', str(executable)],
                        cwd=directory, env=env, stdout=log, stderr=log, timeout=45)
                output = (directory / 'probe.log').read_text()
                self.assertEqual(result.returncode, 0, output)
                self.assertIn('control validation and passthrough passed', output)
            finally:
                subprocess.run(['/opt/wine-stable/bin/wineserver', '-k'], env=env,
                               capture_output=True, timeout=10)
