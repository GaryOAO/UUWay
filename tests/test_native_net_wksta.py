import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(Path('/opt/wine-stable/bin/wine').exists() and
                     shutil.which('x86_64-w64-mingw32-gcc') and shutil.which('xvfb-run'),
                     'Wine, MinGW and Xvfb required')
class NetWkstaTests(unittest.TestCase):
    def test_real_wine_os_gate_and_passthrough(self):
        with tempfile.TemporaryDirectory(prefix='uurb-net-wksta-') as temporary:
            directory = Path(temporary)
            executable = directory / 'gate.exe'
            subprocess.run(['x86_64-w64-mingw32-gcc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                '-I', str(ROOT / 'src'), str(ROOT / 'tests/probes/native_net_wksta.c'),
                '-luser32', '-lnetapi32', '-o', str(executable)],
                check=True, capture_output=True, timeout=30)
            env = dict(os.environ, WINEPREFIX=str(directory / 'wine'), WINEDEBUG='-all',
                       WINEDLLOVERRIDES='mscoree,mshtml=')
            env.pop('WINEDLLPATH', None)
            try:
                with (directory / 'probe.log').open('w') as log:
                    result = subprocess.run(['xvfb-run', '-a', '/opt/wine-stable/bin/wine', str(executable)],
                        cwd=directory, env=env, stdout=log, stderr=log, timeout=45)
                output = (directory / 'probe.log').read_text()
                self.assertEqual(result.returncode, 0, output)
                self.assertIn('UU Super Screen OS gate and NetAPI ownership passed', output)
            finally:
                subprocess.run(['/opt/wine-stable/bin/wineserver', '-k'], env=env,
                               capture_output=True, timeout=10)
