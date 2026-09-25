import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WINE = Path('/opt/wine-stable/bin')
HEADERS = ROOT / 'build/gpu-relay/wine-dev/usr/include/wine'


@unittest.skipUnless((WINE / 'winegcc').exists() and HEADERS.exists() and shutil.which('xvfb-run'),
                     'Pinned Wine compiler/headers and Xvfb required')
class DxgiDuplicationStateTests(unittest.TestCase):
    def test_real_com_state_machine_and_fd_lifetimes(self):
        with tempfile.TemporaryDirectory(prefix='uurb-dxgi-state-') as temporary:
            directory = Path(temporary)
            channel = directory / 'channel.o'
            subprocess.run(['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-fPIC', '-c',
                            str(ROOT / 'src/native_gpu_frame_channel.c'), '-o', str(channel)],
                           capture_output=True, check=True, timeout=20)
            executable = directory / 'state.exe'
            build = subprocess.run([str(WINE / 'winegcc'), '-m64', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-I', str(ROOT / 'src'), '-I', str(HEADERS), '-I', str(HEADERS / 'wine/windows'),
                            str(ROOT / 'src/uu_dxgi_duplication.c'),
                            str(ROOT / 'tests/probes/dxgi_duplication_state.c'), str(channel),
                            '-ldxguid', '-luuid', '-o', str(executable)],
                           capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stderr[-6000:])
            env = dict(os.environ, WINEPREFIX=str(directory / 'wine'), WINELOADER=str(WINE / 'wine'),
                       WINEDEBUG='-all', WINEDLLOVERRIDES='mscoree,mshtml=')
            env.pop('WINEDLLPATH', None)
            process = None
            try:
                with (directory / 'probe.log').open('w') as log:
                    process = subprocess.Popen(['xvfb-run', '-a', '--server-args=-screen 0 640x360x24 -nolisten tcp',
                                                str(executable)], env=env, cwd=directory, stdout=log, stderr=log,
                                               start_new_session=True)
                    code = process.wait(timeout=45)
                output = (directory / 'probe.log').read_text()
                self.assertEqual(code, 0, output[-6000:])
                for case in ('success', 'future-pts', 'pending-close', 'held-close', 'open-failure', 'update-failure',
                             'rate-17', 'rate-fraction', 'rate-variable', 'rate-absent-max', 'rate-change', 'max-rate-change',
                             'geometry', 'uuid', 'allocation', 'sequence', 'timestamp', 'ready-time', 'future-ready',
                             'end', 'disconnect', 'malformed'):
                    self.assertEqual(output.count('STATE ' + case + ' passed'), 3, output[-6000:])
                self.assertIn('DXGI deterministic state checks passed;', output)
            finally:
                if process is not None and process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                subprocess.run([str(WINE / 'wineserver'), '-k'], env=env, capture_output=True, timeout=10)


if __name__ == '__main__':
    unittest.main()
