"""Exercise the directory commit used by UU 4.42 -> 4.46 without Wine."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile
from unittest.mock import call, patch

spec = importlib.util.spec_from_file_location('update_trial', Path(__file__).resolve().parents[1] / 'scripts/uu-native-trial.py')
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


class VendorUpdateTests(unittest.TestCase):
    def test_a_started_process_is_insufficient_when_the_new_gui_hook_rejects_it(self):
        ordinary = [dict(event=name, code=0) for name in
                    ('gui_started', 'service_bootstrap_accepted', 'native_input_hook_ready')]
        ready = set()
        with patch.object(trial, 'event'):
            self.assertEqual(trial.bootstrap_progress(ordinary, ready, True, True), 'waiting')
            self.assertEqual(trial.bootstrap_progress([dict(event='native_gui_hook_ready', code=1)],
                                                     ready, True, True), 'failed')
            hooks = [dict(event=name, code=0) for name in ('native_gui_hook_ready', 'native_service_hook_ready')]
            self.assertEqual(trial.bootstrap_progress(hooks, ready, True, True), 'ready')

    def aliases(self, root):
        app = root / 'GameViewer'
        (app / 'bin').mkdir(parents=True)
        aliases = []
        for name in ('dxgi.dll', 'd3d11.dll'):
            path, target = app / 'bin' / name, root / 'bundle' / name
            path.symlink_to(target)
            info = path.lstat()
            aliases.append((path, target, (info.st_dev, info.st_ino)))
        return app, aliases

    def test_only_original_links_in_a_real_version_directory_identify_update(self):
        with tempfile.TemporaryDirectory() as name:
            app, aliases = self.aliases(Path(name))
            old = app / '4.42.0.2770'
            shutil.copytree(app / 'bin', old, symlinks=True)
            self.assertEqual(trial.relocated_loader_aliases(aliases), [])
            shutil.rmtree(old)
            (app / 'bin').rename(old)
            moved = trial.relocated_loader_aliases(aliases)
            self.assertEqual([p for p, _, _ in moved], [old / p.name for p, _, _ in aliases])
            old.rename(app / 'bak')
            self.assertEqual([p for p, _, _ in trial.relocated_loader_aliases(aliases)],
                             [app / 'bak' / p.name for p, _, _ in aliases])
            old.symlink_to(app / 'bak', target_is_directory=True)
            self.assertEqual([p for p, _, _ in trial.relocated_loader_aliases(aliases)],
                             [app / 'bak' / p.name for p, _, _ in aliases])

    def test_cleanup_removes_renamed_owned_links_and_preserves_new_vendor_files(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            app, aliases = self.aliases(root)
            old = app / '4.42.0.2770'
            (app / 'bin').rename(old)
            (app / 'bin').mkdir()
            replacement = app / 'bin/dxgi.dll'
            replacement.write_bytes(b'vendor replacement')
            with patch.object(trial, 'event'):
                self.assertEqual(trial.cleanup_trial(None, None, None, root, None, None, aliases), [])
            self.assertEqual(list(old.iterdir()), [])
            self.assertEqual(replacement.read_bytes(), b'vendor replacement')

    def test_updater_detection_is_prefix_scoped_and_uses_executable_name(self):
        with tempfile.TemporaryDirectory() as name:
            proc = Path(name)
            (proc / '123').mkdir()
            for executable, expected in [(b'C:\\UU\\setup\\UURemote_Setup_4.46.exe', True),
                                         (b'C:\\UU\\bin\\Upgrade.exe', True),
                                         (b'C:\\UU\\bin\\GameViewerServer.exe', False)]:
                (proc / '123/cmdline').write_bytes(executable + b'\0/S\0')
                with patch.object(trial, 'prefix_processes', return_value=[dict(pid=123)]) as processes:
                    self.assertEqual(trial.uu_updater_alive(Path('/owned/prefix'), proc), expected)
                    processes.assert_called_once_with(Path('/owned/prefix'))
            with patch.object(trial, 'prefix_processes', return_value=[]):
                self.assertFalse(trial.uu_updater_alive(Path('/another/prefix'), proc))

    def test_wait_keeps_installer_alive_then_requires_stable_complete_payload(self):
        with tempfile.TemporaryDirectory() as name:
            app = Path(name)
            now = [0.]

            def sleep(seconds):
                now[0] += seconds
                if now[0] == 3:
                    (app / 'bin').mkdir()
                    for path in ('GameViewer.exe', 'GameViewerService.exe', 'bin/GameViewerServer.exe',
                                 'bin/GameViewerHealthd.exe', 'bin/streamer.dll'):
                        (app / path).write_bytes(b'new vendor payload')

            with patch.object(trial.time, 'monotonic', side_effect=lambda: now[0]), \
                    patch.object(trial.time, 'sleep', side_effect=sleep), \
                    patch.object(trial, 'uu_updater_alive', side_effect=lambda prefix: now[0] < 7), \
                    patch.object(trial.state_tools, 'notify') as notify, patch.object(trial, 'event'):
                self.assertTrue(trial.wait_for_vendor_update(app, app, lambda: False))
            self.assertGreaterEqual(now[0], 9)
            self.assertGreaterEqual(notify.call_args_list.count(call('WATCHDOG=1\nEXTEND_TIMEOUT_USEC=30000000')), 2)

    def test_wait_is_bounded_and_cancellable_without_running_wine_cleanup(self):
        with tempfile.TemporaryDirectory() as name:
            app = Path(name)
            now = [0.]
            with patch.object(trial.time, 'monotonic', side_effect=lambda: now[0]), \
                    patch.object(trial.time, 'sleep', side_effect=lambda seconds: now.__setitem__(0, now[0] + 1)), \
                    patch.object(trial, 'VENDOR_UPDATE_TIMEOUT', 3), \
                    patch.object(trial.state_tools, 'notify'), patch.object(trial, 'event'), \
                    patch.object(trial.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'deadline'):
                    trial.wait_for_vendor_update(app, app, lambda: False)
                self.assertFalse(trial.wait_for_vendor_update(app, app, lambda: True))
                run.assert_not_called()


class VendorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prefix = self.root / 'prefix'
        self.prefix.mkdir(mode=0o700)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.app = self.prefix / 'drive_c/Program Files/Netease/GameViewer'
        (self.app / 'bin').mkdir(parents=True)
        (self.prefix / 'drive_c/windows/system32').mkdir(parents=True)
        (self.app / 'config').mkdir()
        (self.app / 'config/user.json').write_text('user configuration')
        self.payload('old')
        self.registry('4.46.0.4716', 'initial user state')
        self.account = self.prefix / 'drive_c/users/test/AppData/Local/GameViewer/setting_user.ini'
        self.account.parent.mkdir(parents=True)
        self.account.write_text('current account')
        self.recovery = trial.VendorUpdateRecovery(self.prefix, self.state)
        self.recovery.prepare()

    def payload(self, value):
        for name in trial.VendorUpdateRecovery.REQUIRED:
            path = self.app / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'MZ ' + value.encode() + name.encode())
        (self.app / 'bin/resource.txt').write_text(value)

    def registry(self, version, other):
        (self.prefix / 'system.reg').write_text(
            'WINE REGISTRY Version 2\n\n' + r'[Software\\Netease\\GameViewerSetup]' + '\n'
            f'"Version"="{version}"\n"LastVersion"="4.42.0.2770"\n"Other"="{other}"\n')
        (self.prefix / 'user.reg').write_text(
            'WINE REGISTRY Version 2\n\n' + r'[Software\\Netease\\GameViewer]' + '\n'
            f'"Version"="{version}"\n"AccountState"="{other}"\n\n' +
            r'[Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\GameViewer]' + '\n'
            f'"DisplayVersion"="{version}"\n')

    def assert_old(self):
        self.assertEqual((self.app / 'bin/resource.txt').read_text(), 'old')
        self.assertIn(b'old', (self.app / 'GameViewer.exe').read_bytes())
        self.assertIn('"Version"="4.46.0.4716"', (self.prefix / 'system.reg').read_text())

    def test_snapshot_contains_program_files_without_accounts_configuration_or_runtime_tokens(self):
        archive = self.recovery.root / self.recovery.state['known']['archive']
        with zipfile.ZipFile(archive) as payload:
            self.assertEqual(set(payload.namelist()), set(trial.VendorUpdateRecovery.REQUIRED) | {'bin/resource.txt'})
        self.assertEqual(self.recovery.versions()['system.reg'][r'Software\\Netease\\GameViewerSetup']['Version'], '4.46.0.4716')
        again = trial.VendorUpdateRecovery(self.prefix, self.state)
        again.prepare()
        self.assertEqual(again.state['known'], self.recovery.state['known'])
        self.assertEqual(len(list(again.root.glob('snapshot-*.zip'))), 1)

    def test_interrupted_install_restores_missing_bin_and_versions_without_overwriting_user_data(self):
        self.recovery.begin()
        (self.app / 'bin').rename(self.app / 'some-future-backup-name')
        (self.app / 'GameViewer.exe').write_bytes(b'MZ incomplete new launcher')
        self.registry('5.0.0.1', 'new user state')
        self.account.write_text('new account state')
        reboot = trial.VendorUpdateRecovery(self.prefix, self.state)
        with patch.object(trial, 'prefix_processes', return_value=[]):
            reboot.prepare()
        self.assert_old()
        self.assertIsNone(reboot.state['pending'])
        self.assertEqual(reboot.state['outcome'], 'rolled_back')
        self.assertEqual(self.account.read_text(), 'new account state')
        self.assertEqual((self.app / 'config/user.json').read_text(), 'user configuration')
        self.assertIn('new user state', (self.prefix / 'user.reg').read_text())
        self.assertTrue((self.app / 'some-future-backup-name').is_dir())

    def test_an_unrecorded_offline_update_is_probed_before_replacing_known_good_snapshot(self):
        known = self.recovery.state['known'].copy()
        self.payload('new')
        self.recovery.prepare()
        self.assertEqual(self.recovery.state['pending'], 'booting')
        self.assertEqual(self.recovery.state['known'], known)

    def test_crash_during_new_version_startup_rolls_back_on_next_start(self):
        self.recovery.begin()
        self.payload('new')
        self.recovery.installed()
        self.recovery.prepare()
        self.assertEqual(self.recovery.state['pending'], 'booting')
        restarted = trial.VendorUpdateRecovery(self.prefix, self.state)
        with patch.object(trial, 'prefix_processes', return_value=[]):
            restarted.prepare()
        self.assert_old()

    def test_candidate_must_stay_healthy_for_full_period_before_snapshot_promotion(self):
        known = self.recovery.state['known'].copy()
        self.recovery.begin()
        self.payload('new')
        self.recovery.installed()
        self.recovery.prepare()
        now = [0.]
        with patch.object(trial.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(trial, 'uu_server_alive', return_value=True):
            self.recovery.tick(True)
            now[0] = 29
            self.recovery.tick(True, True)
            self.assertEqual(self.recovery.state['known'], known)
            self.recovery.tick(False)
            now[0] = 30
            self.recovery.tick(True)
            now[0] = 60
            self.recovery.tick(True)
        self.assertIsNone(self.recovery.state['pending'])
        self.assertNotEqual(self.recovery.state['known']['archive'], known['archive'])
        self.assertTrue((self.recovery.root / known['archive']).is_file())

    def test_readiness_without_a_real_frame_times_out_without_promoting_new_vendor_code(self):
        known = self.recovery.state['known'].copy()
        self.recovery.begin()
        self.payload('new')
        self.recovery.installed()
        self.recovery.prepare()
        self.recovery.probe_started = 0
        now = [0.]
        with patch.object(trial.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(trial, 'uu_server_alive', return_value=True):
            self.recovery.tick(True)
            now[0] = 1000
            with self.assertRaisesRegex(RuntimeError, 'verified capture frame'):
                self.recovery.tick(True)
        self.assertEqual(self.recovery.state['known'], known)
        self.assertEqual(self.recovery.state['pending'], 'booting')

    def test_snapshot_promotion_failure_does_not_kill_a_working_candidate(self):
        self.recovery.begin()
        self.payload('new')
        self.recovery.installed()
        self.recovery.prepare()
        now = [0.]
        with patch.object(trial.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(trial, 'uu_server_alive', return_value=True), \
                patch.object(self.recovery, 'capture', side_effect=OSError('disk full')):
            self.recovery.tick(True, True)
            now[0] = 31
            self.recovery.tick(True, True)
        self.assertEqual(self.recovery.state['pending'], 'booting')

    def test_backup_corruption_never_overwrites_current_program(self):
        self.payload('new')
        self.recovery.begin()
        archive = self.recovery.root / self.recovery.state['known']['archive']
        archive.write_bytes(b'corrupt backup')
        with patch.object(trial, 'prefix_processes', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'integrity'):
                self.recovery.rollback()
        self.assertIn(b'new', (self.app / 'GameViewer.exe').read_bytes())

    def test_path_traversal_in_snapshot_is_rejected_before_changing_active_code(self):
        archive = self.recovery.root / self.recovery.state['known']['archive']
        with zipfile.ZipFile(archive, 'w') as payload:
            payload.writestr('../outside', b'bad')
        self.recovery.state['known']['sha256'] = self.recovery.digest(archive)
        with patch.object(trial, 'prefix_processes', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'path'):
                self.recovery.rollback()
        self.assert_old()
        self.assertFalse((self.root / 'outside').exists())

    def test_live_prefix_is_never_modified_by_rollback(self):
        self.payload('new')
        with patch.object(trial, 'prefix_processes', return_value=[dict(pid=123)]):
            with self.assertRaisesRegex(RuntimeError, 'idle'):
                self.recovery.rollback()
        self.assertIn(b'new', (self.app / 'GameViewer.exe').read_bytes())

    def test_interruption_halfway_through_rollback_is_resumed(self):
        self.recovery.begin()
        self.payload('new')
        self.registry('5.0.0.1', 'preserved user state')
        with patch.object(trial, 'prefix_processes', return_value=[]), \
                patch.object(self.recovery, 'restore_versions', side_effect=OSError('simulated power loss')):
            with self.assertRaises(OSError):
                self.recovery.rollback()
        self.assertEqual(self.recovery.state['pending'], 'restoring')
        restarted = trial.VendorUpdateRecovery(self.prefix, self.state)
        with patch.object(trial, 'prefix_processes', return_value=[]):
            restarted.prepare()
        self.assert_old()
        self.assertIn('preserved user state', (self.prefix / 'user.reg').read_text())

    def test_failed_new_worker_runs_real_rollback_then_requests_supervisor_restart(self):
        self.recovery.begin()
        self.payload('new')
        self.recovery.installed()
        with patch.object(trial.bundle_tools, 'verify', return_value={'native_capture_producer_included': True}), \
                patch.object(trial.subprocess, 'run') as command, \
                patch.object(trial, 'prefix_processes', return_value=[]), \
                patch.object(trial.shutil, 'copy2', side_effect=RuntimeError('new version startup failed')), \
                patch.object(trial, 'cleanup_trial', return_value=[]):
            command.return_value.returncode = 1
            with self.assertRaisesRegex(RuntimeError, trial.VENDOR_UPDATE_ROLLBACK):
                trial.run(self.prefix, self.root / 'bundle', self.root / 'restore.json', self.state,
                          None, input_access='user', stop_requested=lambda: False)
        self.assert_old()

    def test_failed_checkpoint_write_does_not_replace_in_memory_known_good_snapshot(self):
        known = self.recovery.state['known'].copy()
        self.recovery.begin()
        self.payload('new')
        with patch.object(self.recovery, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.recovery.capture()
        self.assertEqual(self.recovery.state['known'], known)
        self.assertTrue((self.recovery.root / known['archive']).exists())

    def test_snapshot_restores_vendor_conpty_and_lets_current_runtime_restore_its_own_proxy(self):
        compat = self.prefix / 'compat'
        compat.mkdir()
        (compat / 'uu-terminal-proxy.exe').write_bytes(b'MZ current proxy')
        (compat / 'uu-conpty.dll').write_bytes(b'MZ current shim')
        (self.app / 'bin/powershell.exe').write_bytes(b'MZ current proxy')
        (self.app / 'bin/conpty.dll').write_bytes(b'MZ current shim')
        (self.app / 'bin/conpty.dll.uurb-vendor').write_bytes(b'MZ Microsoft original')
        self.recovery.capture()
        with zipfile.ZipFile(self.recovery.root / self.recovery.state['known']['archive']) as payload:
            self.assertNotIn('bin/powershell.exe', payload.namelist())
            self.assertEqual(payload.read('bin/conpty.dll'), b'MZ Microsoft original')

    def test_repeated_failed_updates_keep_only_two_recorded_failed_payloads(self):
        unrelated = self.app / '.uuway-failed-custom-data'
        unrelated.mkdir()
        with patch.object(trial, 'prefix_processes', return_value=[]):
            for index in range(3):
                self.recovery.begin()
                self.payload('new-' + str(index))
                self.recovery.rollback()
        self.assertEqual(len(self.recovery.state['failed_payloads']), 2)
        self.assertEqual(len(list(self.app.glob('.uuway-failed-*'))), 3)
        self.assertTrue(unrelated.is_dir())

    def test_restored_directories_are_durable_before_rollback_is_marked_complete(self):
        self.recovery.begin()
        self.payload('new')
        synced = []
        save = self.recovery.save
        sync = self.recovery.sync_directory

        def sync_directory(path):
            sync(path)
            synced.append(path)

        def save_checkpoint():
            if self.recovery.state['pending'] is None:
                self.assertIn(self.app, synced)
                self.assertIn(self.prefix, synced)
            save()

        with patch.object(trial, 'prefix_processes', return_value=[]), \
                patch.object(self.recovery, 'sync_directory', side_effect=sync_directory), \
                patch.object(self.recovery, 'save', side_effect=save_checkpoint):
            self.recovery.rollback()
        self.assert_old()
