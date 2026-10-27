import importlib.util
import copy
import os
import socket
import shutil
import subprocess
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('native_trial', Path(__file__).resolve().parents[1] / 'scripts/uu-native-trial.py')
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


class ServerLivenessTests(unittest.TestCase):
    def test_live_workers_keep_the_owned_prefix_busy_after_the_leader_exits(self):
        with tempfile.TemporaryDirectory() as name:
            proc = Path(name)
            leader = proc / '123'
            worker = leader / 'task/124'
            worker.mkdir(parents=True)
            (leader / 'stat').write_text('123 (GameViewerServe) Z 1')
            (leader / 'cmdline').write_bytes(b'')
            (worker / 'stat').write_text('124 (worker) S 1')
            (worker / 'cmdline').write_bytes(b'C:\\UU\\GameViewerServer.exe\0')
            (worker / 'environ').write_bytes(b'WINEPREFIX=/owned/prefix\0')
            with patch.object(trial.subprocess, 'check_output', return_value='123 GameViewerServe\n'):
                self.assertEqual(trial.prefix_processes(Path('/owned/prefix'), proc),
                                 [dict(pid=123, name='GameViewerServe')])
                self.assertTrue(trial.uu_server_alive(Path('/owned/prefix'), proc))
                self.assertFalse(trial.uu_server_alive(Path('/another/prefix'), proc))
                (worker / 'stat').write_text('124 (worker) Z 1')
                self.assertFalse(trial.uu_server_alive(Path('/owned/prefix'), proc))
                self.assertEqual(trial.prefix_processes(Path('/owned/prefix'), proc), [])

    def test_a_missing_server_triggers_recovery_but_a_short_restart_is_tolerated(self):
        state = {'now': 0, 'alive': False}
        watch = trial.ServerLiveness(lambda: state['alive'], lambda: state['now'])
        self.assertTrue(watch.healthy())
        state['now'] = 5
        state['alive'] = True
        self.assertTrue(watch.healthy())
        state['now'] = 10
        state['alive'] = False
        self.assertTrue(watch.healthy())
        state['now'] = 20
        self.assertFalse(watch.healthy())

    def test_a_zombie_or_another_prefix_does_not_count_as_a_live_server(self):
        with tempfile.TemporaryDirectory() as name:
            proc = Path(name)
            server = proc / '123'
            server.mkdir()
            (server / 'cmdline').write_bytes(b'C:\\UU\\GameViewerServer.exe\0')
            with patch.object(trial, 'prefix_processes', return_value=[{'pid': 123, 'name': 'GameViewerServe'}]):
                (server / 'stat').write_text('123 (GameViewerServe) Z 1')
                self.assertFalse(trial.uu_server_alive(Path('/owned/prefix'), proc))
                (server / 'stat').write_text('123 (GameViewerServe) S 1')
                self.assertTrue(trial.uu_server_alive(Path('/owned/prefix'), proc))
            with patch.object(trial, 'prefix_processes', return_value=[]):
                self.assertFalse(trial.uu_server_alive(Path('/owned/prefix'), proc))


class CleanupTests(unittest.TestCase):
    def test_cursor_environment_retains_native_metadata_for_every_output(self):
        directory = Path('/private/trial')
        for mode, embedded, expected in [('metadata', False, True), ('composited', False, True),
                                         ('embedded', True, True), ('embedded', False, False),
                                         ('hidden', False, False)]:
            with self.subTest(mode=mode, embedded=embedded):
                environment = dict(UURB_CURSOR_PRESENTATION='client', UURB_CURSOR_STATE_PATH='stale',
                                   UURB_CURSOR_STATE_DIR='stale', OTHER_SETTING='retained')
                args = trial.cursor_environment(environment, directory, mode, embedded)
                self.assertNotIn('UURB_CURSOR_PRESENTATION', environment)
                self.assertEqual(environment['OTHER_SETTING'], 'retained')
                if expected:
                    self.assertEqual(environment['UURB_CURSOR_STATE_PATH'],
                                     trial.winpath(directory / 'cursor.state'))
                    self.assertEqual(environment['UURB_CURSOR_STATE_DIR'], trial.winpath(directory))
                    self.assertEqual(args, ['--cursor-state', str(directory / 'cursor.state')])
                else:
                    self.assertNotIn('UURB_CURSOR_STATE_PATH', environment)
                    self.assertNotIn('UURB_CURSOR_STATE_DIR', environment)
                    self.assertEqual(args, [])

    @unittest.skipUnless(shutil.which('xvfb-run'), 'requires Xvfb')
    def test_runtime_keeps_wine_on_a_separate_display_for_clipboard_files(self):
        # Exercise the actual launcher, including an inherited desktop DISPLAY.
        # A real desktop launch would make clipboard discover() reject the pair.
        with patch.object(trial, 'WINE', '/usr/bin/python3'):
            command = trial.wine_launch_command(
                ['-c', 'import os; print(os.environ["DISPLAY"])'],
                dict(width=640, height=360))
        result = subprocess.run(command, env=dict(os.environ, DISPLAY=':9876'),
                                capture_output=True, text=True, check=True, timeout=10)
        self.assertTrue(result.stdout.strip().startswith(':'))
        self.assertNotEqual(result.stdout.strip(), ':9876')

    def test_deferred_ime_allows_only_missing_exact_private_endpoint(self):
        with tempfile.TemporaryDirectory() as name:
            parent = Path(name); endpoint = parent / 'ime.sock'
            trial.validate_text_endpoint(endpoint, parent, True, True)
            with self.assertRaises(FileNotFoundError):
                trial.validate_text_endpoint(endpoint, parent, True)
            for path, with_input in [(parent / 'text.sock', True), (None, True), (endpoint, False)]:
                with self.assertRaises(ValueError):
                    trial.validate_text_endpoint(path, parent, with_input, True)
            endpoint.touch(mode=0o600)
            with self.assertRaises(ValueError):
                trial.validate_text_endpoint(endpoint, parent, True, True)
            endpoint.unlink()
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
                listener.bind(str(endpoint)); endpoint.chmod(0o600)
                trial.validate_text_endpoint(endpoint, parent, True, True)
                endpoint.chmod(0o666)
                with self.assertRaises(ValueError):
                    trial.validate_text_endpoint(endpoint, parent, True, True)
            endpoint.unlink()
            endpoint.symlink_to(parent / 'missing')
            with self.assertRaises(ValueError):
                trial.validate_text_endpoint(endpoint, parent, True, True)
            self.assertTrue(endpoint.is_symlink())

    def test_pinned_bundle_refuses_a_mutable_development_supervisor(self):
        with tempfile.TemporaryDirectory() as name:
            directory=Path(name)
            with patch.object(trial.bundle_tools,'verify',return_value=dict(
                    native_capture_producer_included=True,native_main_scripts_included=True)):
                with self.assertRaisesRegex(ValueError,'version-pinned scripts'):
                    trial.run(directory,directory/'bundle',directory/'restore.json',directory,1)

    def test_video_recreation_requires_same_output_layout_scale_and_valid_pixels(self):
        before=dict(generation=1,connectors=['fixture-1'],modes=[dict(width=1920,height=1080)],
            layout=[dict(x=0,y=0,scale=1.,transform=0,outputs=1)])
        after=copy.deepcopy(before);after['generation']=2;after['modes']=[dict(width=1280,height=720)]
        self.assertTrue(trial.can_recreate_display_in_place(before,after))
        for change in ('connector','scale','origin','rotation','count','odd','oversize','missing'):
            invalid=copy.deepcopy(after)
            if change=='connector':invalid['connectors']=['fixture-2']
            if change=='scale':invalid['layout'][0]['scale']=2.
            if change=='origin':invalid['layout'][0]['x']=1
            if change=='rotation':invalid['layout'][0]['transform']=1
            if change=='count':invalid['connectors'].append('fixture-2')
            if change=='odd':invalid['modes'][0]['width']=1279
            if change=='oversize':invalid['modes'][0]['width']=8192
            if change=='missing':del invalid['connectors']
            with self.subTest(change=change):self.assertFalse(trial.can_recreate_display_in_place(before,invalid))

    def test_super_screen_origin_handoff_retains_uu_only_for_owned_connectors(self):
        physical = dict(connectors=['DP-fixture'], modes=[dict(width=1920, height=1080)],
                        layout=[dict(x=0, y=0, scale=1., transform=0, outputs=1)])
        virtual = copy.deepcopy(physical)
        virtual['connectors'] = ['Meta-fixture']
        self.assertFalse(trial.can_recreate_display_in_place(physical, virtual))
        self.assertTrue(trial.can_recreate_display_in_place(physical, virtual, ['Meta-fixture']))
        self.assertTrue(trial.can_recreate_display_in_place(virtual, physical, ['Meta-fixture']))

    def test_cold_recovery_does_not_stop_an_already_idle_wineserver(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            with patch.object(trial.subprocess, 'run') as run, patch.object(trial.subprocess, 'Popen') as popen, \
                 patch.object(trial, 'event'):
                failures = trial.cleanup_trial(None, None, {}, directory, ['owned-bootstrap'], None, [], recovery_only=True)
            self.assertEqual(failures, [])
            run.assert_not_called(); popen.assert_not_called()

    def test_private_checkpoint_roundtrip_and_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            checkpoint = directory / 'active-runtime.json'
            trial.state_tools.write_private(checkpoint, {'directory': 'trial-known'})
            self.assertEqual(checkpoint.stat().st_mode & 0o777, 0o600)
            self.assertEqual(trial.state_tools.read_private(checkpoint), {'directory': 'trial-known'})
            trial.state_tools.write_private(checkpoint, {'directory': 'trial-new'})
            checkpoint.rename(directory / 'saved')
            checkpoint.symlink_to(directory / 'saved')
            with self.assertRaises(OSError):
                trial.state_tools.write_private(checkpoint, {'directory': 'trial-unsafe'})
            self.assertEqual(trial.state_tools.read_private(directory / 'saved'), {'directory': 'trial-new'})

    def test_checkpoint_directory_cannot_escape_or_cross_symlink(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'trial-owned').mkdir(mode=0o700)
            self.assertEqual(trial.state_tools.state_directory(directory, {'directory': 'trial-owned'}), directory / 'trial-owned')
            (directory / 'trial-link').symlink_to(directory / 'trial-owned')
            for value in ('../trial-other', '/tmp/trial-other', 'trial-link', 'trial-owned/../trial-other'):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    trial.state_tools.state_directory(directory, {'directory': value})

    def test_persisted_symlink_identity_survives_nonoverwriting_install(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            staged, target, destination = directory / 'staged', directory / 'bundle.dll', directory / 'dxgi.dll'
            staged.symlink_to(target)
            identity = staged.lstat()
            os.link(staged, destination, follow_symlinks=False)
            staged.unlink()
            self.assertEqual((destination.lstat().st_dev, destination.lstat().st_ino), (identity.st_dev, identity.st_ino))
            with self.assertRaises(FileExistsError):
                os.link(destination, destination, follow_symlinks=False)

    def test_recovery_refuses_live_prefix_and_retains_the_owned_cursor_policy(self):
        with tempfile.TemporaryDirectory() as name:
            parent = Path(name)
            directory = parent / 'trial-test'; directory.mkdir(mode=0o700)
            prefix, bundle = parent / 'prefix', parent / 'bundle'
            app = prefix / 'drive_c/Program Files/Netease/GameViewer'
            journal = dict(version=2, prefix=str(prefix), bundle=str(bundle), release_id='fixture',
                arguments=[str(directory / 'bootstrap.exe'), '--run', str(directory / 'capture.sock'),
                           trial.winpath(bundle / 'dxvk.conf'), trial.winpath(app / 'GameViewer.exe'), 'UURB-NATIVE-' + 'a' * 32],
                aliases=[dict(path=str(app / 'bin/dxgi.dll'), target=str(bundle / 'app/bin/dxgi.dll'), identity=[1, 2])])
            trial.state_tools.write_private(parent / 'active-runtime.json', dict(directory=directory.name))
            trial.state_tools.write_private(directory / 'journal.json', journal)
            with patch.object(trial.bundle_tools, 'verify', return_value={'release_id': 'fixture'}), \
                 patch.object(trial.bundle_tools, 'digest', return_value={'sha256': 'same'}), \
                 patch.object(trial.bundle_tools, 'INPUTS', {'app/bin/dxgi.dll': 'unused'}), \
                 patch.object(trial, 'prefix_processes', return_value=[{'pid': 123}]), \
                 patch.object(trial, 'cleanup_trial') as cleanup:
                with self.assertRaisesRegex(RuntimeError, 'remain idle'):
                    trial.recover_interrupted(prefix, parent)
                cleanup.assert_not_called()
            # Recovering an old bundle removes its former client-only registry
            # value; recovering a native-metadata bundle must not invent it.
            for presentation in (None, 'client', 'native'):
                with self.subTest(presentation=presentation):
                    journal['capture_cursor_mode'] = 'metadata'
                    journal.pop('cursor_presentation', None)
                    if presentation is not None:
                        journal['cursor_presentation'] = presentation
                    trial.state_tools.write_private(directory / 'journal.json', journal)

                    def cleanup_fixture(*args, **kwargs):
                        trial.state_tools.write_private(directory / 'cleanup.json', {})
                        return []

                    with patch.object(trial.bundle_tools, 'verify', return_value={'release_id': 'fixture'}), \
                         patch.object(trial.bundle_tools, 'digest', return_value={'sha256': 'same'}), \
                         patch.object(trial.bundle_tools, 'INPUTS', {'app/bin/dxgi.dll': 'unused'}), \
                         patch.object(trial, 'prefix_processes', return_value=[]), \
                         patch.object(trial, 'event'), \
                         patch.object(trial, 'cleanup_trial', side_effect=cleanup_fixture) as cleanup:
                        trial.recover_interrupted(prefix, parent)
                    environment = cleanup.call_args.args[2]
                    self.assertEqual(environment.get('UURB_CURSOR_PRESENTATION'),
                                     None if presentation == 'native' else 'client')
                    self.assertEqual(environment['UURB_CURSOR_STATE_PATH'],
                                     trial.winpath(directory / 'cursor.state'))
                    (directory / 'cleanup.json').unlink()

    def test_broker_failure_does_not_skip_owned_links(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            link = directory / 'dxgi.dll'
            target = directory / 'bundle.dll'
            link.symlink_to(target)
            info = link.lstat()
            with patch.object(trial, 'end_process', side_effect=TimeoutError), patch.object(trial, 'event'):
                failures = trial.cleanup_trial(None, object(), None, directory, None, None,
                    [(link, target, (info.st_dev, info.st_ino))])
            self.assertEqual(failures, ['stop_broker'])
            self.assertFalse(link.is_symlink())
            self.assertTrue((directory / 'cleanup.json').is_file())

    def test_replaced_link_is_preserved_even_with_same_target(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            link = directory / 'dxgi.dll'
            target = directory / 'bundle.dll'
            link.symlink_to(target)
            info = link.lstat()
            link.rename(directory / 'old-link')
            link.symlink_to(target)
            with patch.object(trial, 'event'):
                trial.cleanup_trial(None, None, None, directory, None, None,
                    [(link, target, (info.st_dev, info.st_ino))])
            self.assertTrue(link.is_symlink())

    def test_incremental_events_wait_for_complete_line_and_hide_raw_logs(self):
        with tempfile.TemporaryDirectory() as name:
            log = Path(name) / 'wine.log'
            log.write_text('private UU content\nUURB_NATIVE_BOOT {"event":"gui_started","code":0}')
            cursor = [0]
            self.assertEqual(trial.boot_events(log, cursor), [])
            with log.open('a') as stream:
                stream.write('\n')
            self.assertEqual(trial.boot_events(log, cursor), [dict(event='gui_started', code=0)])
            self.assertEqual(trial.boot_events(log, cursor), [])


class TopologyPollTests(unittest.TestCase):
    def test_debounce_retains_first_candidate_frame_fence(self):
        stabilizer = trial.TopologyStabilizer({'width': 1920}, required=3)
        target = {'width': 1280}
        self.assertIsNone(stabilizer.observe(target, 100))
        self.assertIsNone(stabilizer.observe(target, 200))
        self.assertEqual(stabilizer.observe(target, 300), target)
        self.assertEqual(stabilizer.accepted_after_ns, 100)
        # A different candidate starts its own fence.
        self.assertIsNone(stabilizer.observe({'width': 1600}, 400))
        self.assertIsNone(stabilizer.observe({'width': 1920}, 500))
        self.assertIsNone(stabilizer.observe({'width': 1920}, 600))
        self.assertEqual(stabilizer.observe({'width': 1920}, 700), {'width': 1920})
        self.assertEqual(stabilizer.accepted_after_ns, 500)

    def test_extra_output_serial_bump_does_not_recreate_video(self):
        initial = dict(generation=1, connectors=['a'], layout=[dict(x=0, y=0, scale=1., transform=0, outputs=1)],
                       modes=[dict(width=1920, height=1080)])
        # A second monitor changes Mutter's serial, not the tracked output.
        extra = dict(initial, generation=2)
        stabilizer = trial.TopologyStabilizer(initial, required=3)
        for _ in range(10):
            self.assertIsNone(stabilizer.observe(extra))
        self.assertEqual(stabilizer.current, initial)
        # A real change of the tracked output still wins.
        changed = dict(extra, modes=[dict(width=1280, height=720)])
        self.assertIsNone(stabilizer.observe(changed))
        self.assertIsNone(stabilizer.observe(changed))
        self.assertEqual(stabilizer.observe(changed), changed)

    def test_topology_changes_are_debounced_until_stable(self):
        initial = dict(generation=1, connectors=['a'], layout=[], modes=[dict(width=1024, height=768)])
        next_mode = dict(generation=1, connectors=['a'], layout=[], modes=[dict(width=1920, height=1080)])
        other_mode = dict(generation=1, connectors=['a'], layout=[], modes=[dict(width=1280, height=720)])
        stabilizer = trial.TopologyStabilizer(initial, required=3)
        self.assertIsNone(stabilizer.observe(next_mode))
        self.assertIsNone(stabilizer.observe(other_mode))
        self.assertIsNone(stabilizer.observe(next_mode))
        self.assertIsNone(stabilizer.observe(next_mode))
        self.assertEqual(stabilizer.observe(next_mode), next_mode)
        self.assertIsNone(stabilizer.observe(initial))
        self.assertIsNone(stabilizer.observe(next_mode))

    def test_transient_query_failures_are_absorbed_and_reported(self):
        failures = [0]
        transient = [subprocess.TimeoutExpired('dbus', 5), subprocess.CalledProcessError(1, 'dbus'), ValueError('bad json')]
        for error in transient[:trial.TOPOLOGY_POLL_FAILURE_LIMIT - 1]:
            with patch.object(trial, 'native_topology', side_effect=error), patch.object(trial, 'event') as event:
                self.assertIsNone(trial.poll_topology(failures))
                event.assert_called_once_with('native_topology_poll_failed', error_type=type(error).__name__,
                                              consecutive=failures[0])

    def test_success_resets_the_consecutive_failure_count(self):
        failures = [0]
        good = dict(generation=1, connectors=['a'], layout=[], modes=[])
        for _ in range(3):
            with patch.object(trial, 'event'):
                with patch.object(trial, 'native_topology', side_effect=subprocess.TimeoutExpired('dbus', 5)):
                    self.assertIsNone(trial.poll_topology(failures))
                with patch.object(trial, 'native_topology', return_value=good):
                    self.assertEqual(trial.poll_topology(failures), good)
            self.assertEqual(failures, [0])

    def test_persistent_query_failure_still_stops_the_worker(self):
        failures = [0]
        with patch.object(trial, 'native_topology', side_effect=subprocess.TimeoutExpired('dbus', 5)), \
                patch.object(trial, 'event'):
            for _ in range(trial.TOPOLOGY_POLL_FAILURE_LIMIT - 1):
                self.assertIsNone(trial.poll_topology(failures))
            with self.assertRaises(subprocess.TimeoutExpired):
                trial.poll_topology(failures)

    def test_unsupported_layout_is_never_tolerated(self):
        failures = [0]
        with patch.object(trial, 'native_topology', side_effect=RuntimeError('Native input currently requires one physical output')), \
                patch.object(trial, 'event') as event:
            with self.assertRaises(RuntimeError):
                trial.poll_topology(failures)
            event.assert_not_called()
            self.assertEqual(failures, [0])

    def test_startup_topology_retry_absorbs_transient_dbus_disconnect(self):
        good = dict(generation=1, connectors=['a'], layout=[], modes=[])
        failures = [subprocess.CalledProcessError(1, 'dbus'), ValueError('bad json')]
        with patch.object(trial, 'native_topology', side_effect=[*failures, good]), \
                patch.object(trial, 'event') as event, \
                patch.object(trial.time, 'sleep') as sleep:
            self.assertEqual(trial.topology_with_retry(), good)
        self.assertEqual(event.call_count, 2)
        self.assertEqual(sleep.call_count, 2)

    def test_startup_topology_retry_stops_without_another_dbus_call(self):
        with patch.object(trial, 'native_topology') as topology, \
                patch.object(trial, 'event') as event:
            self.assertIsNone(trial.topology_with_retry(lambda: True))
        topology.assert_not_called()
        event.assert_not_called()

    def test_startup_topology_retry_keeps_a_persistent_failure_bounded(self):
        with patch.object(trial, 'native_topology', side_effect=subprocess.TimeoutExpired('dbus', 5)), \
                patch.object(trial, 'event'), \
                patch.object(trial.time, 'sleep') as sleep:
            with self.assertRaises(subprocess.TimeoutExpired):
                trial.topology_with_retry()
        self.assertEqual(sleep.call_count, trial.TOPOLOGY_RETRY_LIMIT - 1)


class CleanupHeartbeatTests(unittest.TestCase):
    def beats(self, managed, hold, **limits):
        sent = []
        with patch.object(trial, 'CLEANUP_HEARTBEAT_INTERVAL', limits.get('interval', 0.02)), \
                patch.object(trial, 'CLEANUP_HEARTBEAT_SECONDS', limits.get('seconds', 150)), \
                patch.object(trial.state_tools, 'notify', side_effect=sent.append):
            with trial.cleanup_heartbeat(managed):
                time.sleep(hold)
            during = len(sent)
            time.sleep(0.1)
        return sent, during

    def test_managed_cleanup_keeps_feeding_the_watchdog_and_stops_when_done(self):
        sent, during = self.beats(True, 0.25)
        self.assertGreaterEqual(during, 3)
        self.assertEqual(set(sent), {'WATCHDOG=1'})
        self.assertEqual(len(sent), during)

    def test_standalone_cleanup_sends_no_heartbeat(self):
        sent, _ = self.beats(False, 0.1)
        self.assertEqual(sent, [])

    def test_a_wedged_cleanup_is_only_fed_for_a_bounded_time(self):
        sent, _ = self.beats(True, 0.3, interval=0.01, seconds=0.05)
        self.assertGreaterEqual(len(sent), 1)
        self.assertLess(len(sent), 12)

    def test_failing_notify_socket_ends_the_heartbeat_quietly(self):
        with patch.object(trial, 'CLEANUP_HEARTBEAT_INTERVAL', 0.01), \
                patch.object(trial.state_tools, 'notify', side_effect=OSError('gone')) as notify:
            with trial.cleanup_heartbeat(True):
                time.sleep(0.1)
        self.assertEqual(notify.call_count, 1)


if __name__ == '__main__':
    unittest.main()
