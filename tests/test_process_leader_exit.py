"""Exercise the real Linux /proc behavior behind Wine's exited initial thread."""
import importlib.util
import os
from pathlib import Path
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@unittest.skipUnless(shutil.which('gcc') and Path('/proc/self/task').is_dir(), 'Linux and gcc required')
class ExitedLeaderTests(unittest.TestCase):
    def test_runtime_health_and_frame_ownership_use_surviving_threads(self):
        trial = module('thread_trial', 'uu-native-trial.py')
        update = module('thread_update', 'uuway_update_apply.py')
        confirmation = module('thread_confirmation', 'native_display_confirmation.py')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / 'GameViewerServer.exe'
            prefix = root / 'prefix'
            subprocess.run(['gcc', '-Wall', '-Wextra', '-Werror', '-pthread',
                            str(ROOT / 'tests/probes/process_leader_exit.c'), '-o', str(binary)],
                           check=True, capture_output=True, timeout=30)
            child = subprocess.Popen([str(binary)], env=dict(os.environ, WINEPREFIX=str(prefix)),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            try:
                self.assertTrue(select.select([child.stdout], [], [], 5)[0])
                self.assertEqual(child.stdout.readline(), b'ready\n')
                process = Path('/proc') / str(child.pid)
                deadline = time.monotonic() + 5
                while (process / 'stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z':
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                self.assertEqual((process / 'cmdline').read_bytes(), b'')
                self.assertIsNone(child.poll())  # the process still has a live worker
                for helper in (trial.state_tools.live_process_task, update.live_process_task):
                    task = helper(process)
                    self.assertNotEqual(task, process)
                    self.assertIn(b'WINEPREFIX=' + os.fsencode(prefix), (task / 'environ').read_bytes())
                    self.assertTrue((task / 'fd/0').is_symlink())
                with patch.object(trial.subprocess, 'check_output',
                                  return_value=f'{child.pid} GameViewerServe\n'):
                    self.assertTrue(trial.uu_server_alive(prefix))
                    self.assertFalse(trial.uu_server_alive(root / 'other-prefix'))
                    self.assertEqual(len(trial.prefix_processes(prefix)), 1)
                proc = root / 'proc'
                proc.mkdir()
                (proc / str(child.pid)).symlink_to(process, target_is_directory=True)
                self.assertEqual(update.count_uu_processes(proc, os.getuid())['GameViewerServer.exe'], 1)
                evidence = dict(peer_pid=child.pid, peer_start_ticks=confirmation.process_start_ticks(child.pid))
                self.assertTrue(confirmation.owned_consumer(evidence, prefix, binary, binary))
                self.assertFalse(confirmation.owned_consumer(evidence, root / 'other-prefix', binary, binary))
                child.communicate(timeout=5)
                with self.assertRaises(ProcessLookupError):
                    trial.state_tools.live_process_task(process)
                self.assertEqual(update.count_uu_processes(proc, os.getuid())['GameViewerServer.exe'], 0)
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)
