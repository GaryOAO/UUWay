"""Exercise the directory commit used by UU 4.42 -> 4.46 without Wine."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import call, patch

spec = importlib.util.spec_from_file_location('update_trial', Path(__file__).resolve().parents[1] / 'scripts/uu-native-trial.py')
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


class VendorUpdateTests(unittest.TestCase):
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
            self.assertEqual(trial.relocated_loader_aliases(aliases), [])
            old.symlink_to(app / 'bak', target_is_directory=True)
            self.assertEqual(trial.relocated_loader_aliases(aliases), [])

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
