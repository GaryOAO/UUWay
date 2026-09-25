import importlib.util
import copy
import os
import socket
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('native_trial', Path(__file__).resolve().parents[1] / 'scripts/uu-native-trial.py')
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


class CleanupTests(unittest.TestCase):
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

    def test_recovery_refuses_live_prefix_without_running_cleanup(self):
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


if __name__ == '__main__':
    unittest.main()
