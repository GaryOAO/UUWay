import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    spec = importlib.util.spec_from_file_location('native_text_service', ROOT / 'scripts/uu-native-text-service.py')
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)
finally:
    sys.path.pop(0)


class TextServiceTests(unittest.TestCase):
    def test_system_python_daemon_is_adopted_across_python_versions(self):
        with tempfile.TemporaryDirectory() as name:
            endpoint, restore = Path(name) / 'text.sock', Path(name) / 'restore.json'
            script = Path(name) / 'daemon.py'
            script.write_text('import socket, sys, time\n'
                              's = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)\n'
                              's.bind(sys.argv[2])\n'
                              'import os; os.chmod(sys.argv[2], 0o600)\n'
                              's.listen(1)\nprint("ready", flush=True)\ntime.sleep(30)\n')
            child = subprocess.Popen(['/usr/bin/python3', str(script), '--socket', str(endpoint),
                                      '--restore-state', str(restore)], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                with patch.object(service, 'DAEMON', script):
                    identity = service.live_daemon(endpoint, restore)
                self.assertEqual(identity[0], child.pid)
                os.close(identity[1])
            finally:
                child.terminate()
                child.wait(timeout=5)
                child.stdout.close()

    def test_stale_owned_socket_is_reclaimed_not_timed_out_live_process(self):
        with tempfile.TemporaryDirectory() as name:
            endpoint = Path(name) / 'text.sock'
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as old:
                old.bind(str(endpoint)); endpoint.chmod(0o600)
            self.assertIsNone(service.live_daemon(endpoint, Path(name) / 'restore.json'))
            self.assertFalse(endpoint.exists())

    def test_child_between_bind_and_listen_keeps_its_socket(self):
        with tempfile.TemporaryDirectory() as name:
            endpoint = Path(name) / 'text.sock'
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as child:
                child.bind(str(endpoint)); endpoint.chmod(0o600)
                self.assertIsNone(service.live_daemon(endpoint, Path(name) / 'restore.json', reclaim_stale=False))
                self.assertTrue(endpoint.exists())

    def test_unrelated_live_listener_is_neither_adopted_nor_removed(self):
        with tempfile.TemporaryDirectory() as name:
            endpoint = Path(name) / 'text.sock'
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as other:
                other.bind(str(endpoint)); endpoint.chmod(0o600); other.listen(1)
                with self.assertRaises(ValueError):
                    service.live_daemon(endpoint, Path(name) / 'restore.json')
                self.assertTrue(endpoint.exists())

    def test_non_socket_is_preserved(self):
        with tempfile.TemporaryDirectory() as name:
            endpoint = Path(name) / 'text.sock'; endpoint.touch(mode=0o600)
            with self.assertRaises(ValueError):
                service.live_daemon(endpoint, Path(name) / 'restore.json')
            self.assertTrue(endpoint.is_file())


if __name__ == '__main__':
    unittest.main()
