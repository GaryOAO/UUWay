"""Supervisor lifecycle tests; no real UU, Wine or desktop workers are started."""
import contextlib
import importlib.util
import itertools
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stop_service', ROOT / 'scripts/uu-native-service.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class ServiceLifecycleTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, worker):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = dict(schema=1, prefix=str(root / 'prefix'), bundle=str(root / 'bundle'),
                          restore_state=str(root / 'restore.json'), state_parent=str(root / 'state'),
                          text_socket=str(root / 'state/text.sock'), cursor_mode='composited')
            path = root / 'native-runtime.json'
            service.trial.state_tools.write_private(path, config)
            handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
            with patch.object(service.trial.bundle_tools, 'verify', return_value={}), \
                    patch.object(service.trial, 'run', side_effect=worker) as launch, \
                    patch.object(service.trial, 'event') as event, \
                    patch.object(service.trial.state_tools, 'notify') as notify:
                yield path, launch, event, notify
            self.assertEqual(handlers, {sig: signal.getsignal(sig) for sig in handlers})

    def test_stop_and_remap_same_iteration_never_restarts(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            def worker(*args, **kwargs):
                self.assertFalse(kwargs['stop_requested']())
                signal.raise_signal(sig)
                self.assertTrue(kwargs['stop_requested']())
                raise RuntimeError(service.DISPLAY_REMAP_FAILURE)

            with self.subTest(sig=sig), self.fixture(worker) as (path, launch, event, notify):
                service.run(path)
                self.assertEqual(launch.call_count, 1)
                event.assert_not_called()
                notify.assert_called_once_with('STOPPING=1\nSTATUS=UU native service stopped')

    def test_stop_during_reconnect_delay_never_starts_second_trial(self):
        with self.fixture(RuntimeError(service.DISPLAY_REMAP_FAILURE)) as (path, launch, event, notify), \
                patch.object(service.time, 'sleep', side_effect=lambda seconds: signal.raise_signal(signal.SIGTERM)):
            service.run(path)
            self.assertEqual(launch.call_count, 1)

    def test_remap_budget_is_bounded_and_only_service_finally_announces_shutdown(self):
        with self.fixture(RuntimeError(service.DISPLAY_REMAP_FAILURE)) as (path, launch, event, notify), \
                patch.object(service.time, 'monotonic', side_effect=itertools.count(0, .5)), \
                patch.object(service.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, service.DISPLAY_REMAP_FAILURE):
                service.run(path)
            self.assertEqual(launch.call_count, service.DISPLAY_REMAP_LIMIT + 1)
            self.assertEqual(event.call_count, service.DISPLAY_REMAP_LIMIT)
            notify.assert_called_once_with('STOPPING=1\nSTATUS=UU native service stopped')

    def test_stop_does_not_hide_cleanup_failure(self):
        def worker(*args, **kwargs):
            signal.raise_signal(signal.SIGTERM)
            raise RuntimeError('cleanup failed fixture')

        with self.fixture(worker) as (path, launch, event, notify):
            with self.assertRaisesRegex(RuntimeError, 'cleanup failed fixture'):
                service.run(path)
            self.assertEqual(launch.call_count, 1)
            event.assert_not_called()

    def test_cancelled_trial_does_not_touch_prefix(self):
        with patch.object(service.trial, 'private_directory') as private:
            service.trial.run(None, None, None, None, None, stop_requested=lambda: True)
            private.assert_not_called()

    def test_managed_trial_cleanup_does_not_notify_systemd_stopping(self):
        trial = service.trial
        for managed in (False, True):
            with self.subTest(managed=managed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                app = root / 'drive_c/Program Files/Netease/GameViewer'
                (app / 'bin').mkdir(parents=True)
                for name in ('GameViewer.exe', 'GameViewerService.exe', 'bin/GameViewerServer.exe'):
                    (app / name).touch()
                (root / 'system.reg').touch()
                with patch.object(trial.bundle_tools, 'verify', return_value={'native_capture_producer_included': True}), \
                        patch.object(trial.subprocess, 'run') as command, \
                        patch.object(trial, 'prefix_processes', return_value=[]), \
                        patch.object(trial.fcntl, 'flock', side_effect=RuntimeError('fixture lock failure')), \
                        patch.object(trial, 'cleanup_trial', return_value=[]), \
                        patch.object(trial.state_tools, 'notify') as notify:
                    command.return_value.returncode = 1
                    with self.assertRaisesRegex(RuntimeError, 'fixture lock failure'):
                        trial.run(root, root / 'bundle', root / 'restore.json', root, 10,
                                  stop_requested=(lambda: False) if managed else None)
                    self.assertEqual('STOPPING=1' in notify.call_args.args[0], not managed)

    def test_managed_worker_failure_is_logged_and_slow_cleanup_keeps_watchdog_fed(self):
        trial = service.trial
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / 'drive_c/Program Files/Netease/GameViewer'
            (app / 'bin').mkdir(parents=True)
            for name in ('GameViewer.exe', 'GameViewerService.exe', 'bin/GameViewerServer.exe'):
                (app / name).touch()
            (root / 'system.reg').touch()
            with patch.object(trial.bundle_tools, 'verify', return_value={'native_capture_producer_included': True}), \
                    patch.object(trial.subprocess, 'run') as command, \
                    patch.object(trial, 'prefix_processes', return_value=[]), \
                    patch.object(trial.fcntl, 'flock', side_effect=subprocess.TimeoutExpired('fixture', 5)), \
                    patch.object(trial, 'cleanup_trial', side_effect=lambda *a, **k: time.sleep(0.3) or []), \
                    patch.object(trial, 'CLEANUP_HEARTBEAT_INTERVAL', 0.02), \
                    patch.object(trial, 'event') as event, \
                    patch.object(trial.state_tools, 'notify') as notify:
                command.return_value.returncode = 1
                with self.assertRaises(subprocess.TimeoutExpired):
                    trial.run(root, root / 'bundle', root / 'restore.json', root, None, input_access='user',
                              stop_requested=lambda: False)
                event.assert_called_once_with('native_worker_failed', error_type='TimeoutExpired')
                beats = [call.args[0] for call in notify.call_args_list]
                self.assertTrue(beats[0].startswith('WATCHDOG=1\nSTATUS=Cleaning up'))
                self.assertGreaterEqual(beats[1:].count('WATCHDOG=1'), 3)
                self.assertNotIn('STOPPING=1', ''.join(beats))


if __name__ == '__main__':
    unittest.main()


class TerminalMuxProxyTests(unittest.TestCase):
    def bridge(self, root, current):
        compat = root / 'prefix/compat'
        compat.mkdir(parents=True)
        (compat / 'uu-terminal-proxy.exe').write_bytes(b'MZ proxy')
        bridge = service.TerminalBridge(root / 'prefix', root / 'state')
        bridge.mux_proxy.parent.mkdir(parents=True)
        if current is not None:
            bridge.mux_proxy.write_bytes(current)
        return bridge

    def test_wine_placeholder_is_backed_up_and_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            placeholder = b'MZ' + b'\0' * 62 + b'Wine builtin DLL' + b'\0' * 64
            bridge = self.bridge(Path(temporary), placeholder)
            self.assertTrue(bridge._install_mux_proxy())
            self.assertEqual(bridge.mux_proxy.read_bytes(), b'MZ proxy')
            self.assertEqual(bridge.mux_placeholder.read_bytes(), placeholder)
            self.assertTrue(bridge._install_mux_proxy())
            self.assertEqual(bridge.mux_placeholder.read_bytes(), placeholder)

    def test_unknown_powershell_is_left_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.bridge(Path(temporary), b'MZ real powershell')
            self.assertFalse(bridge._install_mux_proxy())
            self.assertEqual(bridge.mux_proxy.read_bytes(), b'MZ real powershell')
            self.assertFalse(bridge.mux_placeholder.exists())

    def test_missing_powershell_is_not_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.bridge(Path(temporary), None)
            self.assertFalse(bridge._install_mux_proxy())
            self.assertFalse(bridge.mux_proxy.exists())


class TerminalConptyShimTests(unittest.TestCase):
    def bridge(self, root, current):
        compat = root / 'prefix/compat'
        compat.mkdir(parents=True)
        (compat / 'uu-conpty.dll').write_bytes(b'MZ shim')
        bridge = service.TerminalBridge(root / 'prefix', root / 'state')
        bridge.conpty.parent.mkdir(parents=True)
        if current is not None:
            bridge.conpty.write_bytes(current)
        return bridge

    def test_microsoft_conpty_is_kept_and_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            vendor = b'MZ ConptyCreatePseudoConsoleAsUser'
            bridge = self.bridge(Path(temporary), vendor)
            self.assertTrue(bridge._install_conpty_shim())
            self.assertEqual(bridge.conpty.read_bytes(), b'MZ shim')
            self.assertEqual(bridge.conpty_vendor.read_bytes(), vendor)
            self.assertTrue(bridge._install_conpty_shim())

    def test_unknown_conpty_is_left_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.bridge(Path(temporary), b'MZ something else')
            self.assertFalse(bridge._install_conpty_shim())
            self.assertEqual(bridge.conpty.read_bytes(), b'MZ something else')

    def test_missing_conpty_is_not_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.bridge(Path(temporary), None)
            self.assertFalse(bridge._install_conpty_shim())
            self.assertFalse(bridge.conpty.exists())


class ClipboardBridgeTests(unittest.TestCase):
    def test_missing_helper_is_optional(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = service.ClipboardBridge(Path(temporary) / 'prefix', Path(temporary) / 'state')
            self.assertFalse(bridge.start())
            bridge.stop()

    def test_helper_is_started_and_stopped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / 'prefix/compat/uu-clipboard-bridge'
            helper.parent.mkdir(parents=True)
            helper.write_text('#!/bin/sh\nexec sleep 30\n')
            helper.chmod(0o755)
            bridge = service.ClipboardBridge(root / 'prefix', root / 'state')
            self.assertTrue(bridge.start())
            process = bridge.process
            self.assertIsNone(process.poll())
            bridge.stop()
            self.assertIsNotNone(process.poll())
            self.assertIsNone(bridge.process)


class DownloadPathMappingTests(unittest.TestCase):
    def prefix(self, root):
        app = root / 'prefix/drive_c/Program Files/Netease/GameViewer'
        app.mkdir(parents=True)
        return root / 'prefix', app

    def test_maps_gameviewer_receive_directory_to_linux_downloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix, app = self.prefix(root)
            destination = root / 'Downloads'
            mapping = service.DownloadPathMapping(prefix, destination)

            self.assertTrue(mapping.apply())
            self.assertTrue(destination.is_dir())
            self.assertTrue(mapping.source.is_symlink())
            self.assertEqual(mapping.source.resolve(), destination.resolve())

    def test_existing_wine_directory_is_preserved_before_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix, app = self.prefix(root)
            source = app / 'Download'
            source.mkdir()
            (source / 'old.bin').write_bytes(b'old')
            destination = root / 'Downloads'

            self.assertTrue(service.DownloadPathMapping(prefix, destination).apply())
            self.assertEqual((source.parent / 'Download.uurb-wine' / 'old.bin').read_bytes(), b'old')
            self.assertTrue(source.is_symlink())
            self.assertEqual(source.resolve(), destination.resolve())

    def test_conflicting_file_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix, app = self.prefix(root)
            source = app / 'Download'
            source.write_bytes(b'custom')

            self.assertFalse(service.DownloadPathMapping(prefix, root / 'Downloads').apply())
            self.assertEqual(source.read_bytes(), b'custom')

    def test_private_custom_download_directory_is_used(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix, app = self.prefix(root)
            config = root / 'config'
            config.mkdir(mode=0o700)
            custom = root / 'received'
            custom.mkdir()
            service.trial.state_tools.write_private(config / 'download-directory.json',
                                                    {'version': 1, 'path': str(custom)})
            mapping = service.DownloadPathMapping(prefix, config_directory=config)
            self.assertEqual(mapping.destination, custom.resolve())
            self.assertTrue(mapping.apply())
            self.assertEqual(mapping.source.resolve(), custom.resolve())


class DesktopImageMappingTests(unittest.TestCase):
    def test_default_penguin_is_written_to_wine_desktop_registry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / 'prefix'
            prefix.mkdir()
            (prefix / 'system.reg').write_text('fixture')
            calls = []

            def run(command, **kwargs):
                calls.append((command, kwargs))
                return type('Result', (), {'returncode': 0})()

            self.assertTrue(service.DesktopImageMapping(
                prefix, root / 'config', runner=run).apply())
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[0][0][1:4], ['reg', 'add', r'HKCU\Control Panel\Desktop'])
            self.assertTrue(any('uuway-penguin.bmp' in value for value in calls[0][0]))
            self.assertEqual(calls[0][1]['env']['WINEPREFIX'], str(prefix))

    def test_custom_image_overrides_the_penguin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / 'prefix'
            prefix.mkdir()
            (prefix / 'system.reg').write_text('fixture')
            config = root / 'config'
            config.mkdir(mode=0o700)
            image = root / 'custom.png'
            image.write_bytes(b'fixture image')
            image.chmod(0o600)
            service.trial.state_tools.write_private(config / 'desktop-image.json',
                                                    {'version': 1, 'path': str(image)})
            commands = []

            def run(command, **kwargs):
                commands.append(command)
                return type('Result', (), {'returncode': 0})()

            self.assertTrue(service.DesktopImageMapping(prefix, config, runner=run).apply())
            self.assertTrue(any('custom.png' in value for value in commands[0]))
