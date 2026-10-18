import os
import importlib.util
from pathlib import Path
import shutil
import subprocess
import struct
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WINE = Path('/opt/wine-stable/bin')
trial_spec = importlib.util.spec_from_file_location('cursor_trial', ROOT / 'scripts/uu-native-trial.py')
trial = importlib.util.module_from_spec(trial_spec)
trial_spec.loader.exec_module(trial)


@unittest.skipUnless((WINE / 'wine').exists() and shutil.which('x86_64-w64-mingw32-gcc')
                     and shutil.which('xvfb-run'), 'Wine, MinGW and Xvfb required')
class BootstrapEnvironmentTests(unittest.TestCase):
    def test_merged_snapshot_becomes_a_real_user32_cursor(self):
        spec = importlib.util.spec_from_file_location('cursor_broker', ROOT / 'scripts/uu-native-capture-broker.py')
        broker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(broker)
        with tempfile.TemporaryDirectory(prefix='uurb-user32-cursor-') as temporary:
            directory = Path(temporary)
            target = broker.CursorState(directory / 'cursor.state')
            source = broker.CursorState(directory / 'cursor.1.state')
            try:
                header = (0x43525555, 1, 10, 2, 1, 1, 1, 2, 2, 1, 1, 321, 200, 0, 0, 0)
                os.pwrite(source.fd, struct.pack('<9I4i3I', *header), 0)
                os.pwrite(source.fd, bytes([128, 0, 0, 255]) * 4, 64)
                broker.DesktopCursor(target).update({1: (source, dict(x=1920, y=-100))})
                executable = directory / 'cursor.exe'
                subprocess.run(['x86_64-w64-mingw32-gcc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                                '-municode', '-I', str(ROOT / 'src'),
                                str(ROOT / 'tests/probes/native_cursor_user32.c'),
                                '-luser32', '-lgdi32', '-o', str(executable)],
                               check=True, capture_output=True, timeout=30)
                env = dict(os.environ, WINEPREFIX=str(directory / 'wine'), WINEDEBUG='-all',
                           WINEDLLOVERRIDES='mscoree,mshtml=', UURB_CURSOR_PRESENTATION='client')
                trial.cursor_environment(env, directory, 'metadata')
                env.pop('WINEDLLPATH', None)
                try:
                    with (directory / 'probe.log').open('w') as log:
                        result = subprocess.run(['xvfb-run', '-a', str(WINE / 'wine'), str(executable),
                                                'Z:' + str(target.path).replace('/', '\\'), '2241', '100'],
                                                cwd=directory, env=env, stdout=log, stderr=log, timeout=45)
                    output = (directory / 'probe.log').read_text()
                    self.assertEqual(result.returncode, 0, output[-6000:])
                    self.assertIn('USER32 real cursor handle and desktop position passed', output)
                finally:
                    subprocess.run([str(WINE / 'wineserver'), '-k'], env=env, capture_output=True, timeout=10)
            finally:
                source.close()
                target.close()

    def test_all_modules_retain_real_cursor_metadata_with_client_presentation(self):
        spec = importlib.util.spec_from_file_location('cursor_broker', ROOT / 'scripts/uu-native-capture-broker.py')
        broker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(broker)
        with tempfile.TemporaryDirectory(prefix='uurb-cursor-channels-') as temporary:
            directory = Path(temporary)
            executable = directory / 'channels.exe'
            subprocess.run(['x86_64-w64-mingw32-gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-municode',
                            str(ROOT / 'tests/probes/native_cursor_channels.c'), '-luser32', '-lgdi32', '-lws2_32',
                            '-o', str(executable)], check=True, capture_output=True, timeout=30)
            subprocess.run(['x86_64-w64-mingw32-gcc', '-O2', '-Wall', '-Wextra', '-Werror', '-shared',
                            str(ROOT / 'tests/probes/native_cursor_capture_module.c'), '-luser32',
                            '-o', str(directory / 'streamer.dll')], check=True, capture_output=True, timeout=30)
            shutil.copy2(directory / 'streamer.dll', directory / 'ordinary.dll')
            state = broker.CursorState(directory / 'cursor.state')
            env = dict(os.environ, WINEPREFIX=str(directory / 'wine'), WINEDEBUG='-all',
                       WINEDLLOVERRIDES='mscoree,mshtml=')
            env.pop('WINEDLLPATH', None)
            try:
                for client_policy in (False, True):
                    with self.subTest(client_policy=client_policy):
                        header = (0x43525555, 1, 10, 1, 1, 1, 1, 2, 2, 1, 1, 2241, -100, 0, 0, 0)
                        os.pwrite(state.fd, struct.pack('<9I4i3I', *header), 0)
                        os.pwrite(state.fd, bytes([128, 0, 0, 255]) * 4, 64)
                        env.pop('UURB_CURSOR_PRESENTATION', None)
                        if client_policy:
                            # The production launcher must expose complete
                            # metadata to streamer.dll, including when a stale
                            # presentation override was inherited.
                            env['UURB_CURSOR_PRESENTATION'] = 'stale'
                            trial.cursor_environment(env, directory, 'metadata')
                        with (directory / 'probe.log').open('w') as log:
                            result = subprocess.run(['xvfb-run', '-a', str(WINE / 'wine'), str(executable),
                                'Z:' + str(state.path).replace('/', '\\'), '2241', '-100', str(int(client_policy))],
                                cwd=directory, env=env, stdout=log, stderr=log, timeout=45)
                        output = (directory / 'probe.log').read_text()
                        # Each xvfb-run owns a different X server. Retire this
                        # prefix's Wine server before starting the next case.
                        subprocess.run([str(WINE / 'wineserver'), '-k'], env=env, capture_output=True, timeout=10)
                        subprocess.run([str(WINE / 'wineserver'), '-w'], env=env, capture_output=True, timeout=10)
                        self.assertEqual(result.returncode, 0, output[-6000:])
                        self.assertIn('USER32 client/capture imports, dynamic queries and hide/restore passed', output)
            finally:
                subprocess.run([str(WINE / 'wineserver'), '-k'], env=env, capture_output=True, timeout=10)
                state.close()

    def test_fresh_service_environment_and_owned_cleanup(self):
        with tempfile.TemporaryDirectory(prefix='uurb-bootstrap-env-') as temporary:
            directory = Path(temporary)
            executable = directory / 'environment.exe'
            subprocess.run(['x86_64-w64-mingw32-gcc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-municode', str(ROOT / 'tests/probes/native_bootstrap_environment.c'),
                            '-ladvapi32', '-luserenv', '-o', str(executable)],
                           check=True, capture_output=True, timeout=30)
            env = dict(os.environ, WINEPREFIX=str(directory / 'wine'), WINEDEBUG='-all',
                       WINEDLLOVERRIDES='mscoree,mshtml=')
            env.pop('WINEDLLPATH', None)
            for name in list(env):
                if name.startswith(('UURB_', 'UU_INPUT_')):
                    env.pop(name)
            try:
                with (directory / 'probe.log').open('w') as log:
                    result = subprocess.run(['xvfb-run', '-a', str(WINE / 'wine'), str(executable)],
                                            cwd=directory, env=env, stdout=log, stderr=log, timeout=45)
                output = (directory / 'probe.log').read_text()
                self.assertEqual(result.returncode, 0, output[-6000:])
                self.assertIn('BOOTSTRAP fresh environment and owned cleanup passed', output)
            finally:
                subprocess.run([str(WINE / 'wineserver'), '-k'], env=env, capture_output=True, timeout=10)


if __name__ == '__main__':
    unittest.main()
