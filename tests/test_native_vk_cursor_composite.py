"""Explicit opt-in, real NVIDIA fixture; never opens a screen or UU session."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NativeVkCursorCompositeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('UURB_TEST_GPU') == '1', 'Explicit UURB_TEST_GPU=1 required')
    def test_synthetic_gpu_pixels_cursor_only_updates_and_no_extra_uploads(self):
        subprocess.run(['bash', str(ROOT / 'scripts/build-native-cursor-shaders.sh')],
                       check=True, capture_output=True, timeout=20)
        with tempfile.TemporaryDirectory(prefix='uurb-cursor-gpu-') as temporary:
            executable = str(Path(temporary) / 'cursor')
            subprocess.run(['gcc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                '-I', str(ROOT / 'src'), '-I', str(ROOT / 'build/native-presenter/cursor-shaders'),
                str(ROOT / 'tests/probes/native_vk_cursor_composite.c'),
                str(ROOT / 'src/native_vk_cursor_composite.c'), '-o', executable, '-lvulkan'],
                check=True, capture_output=True, timeout=20)
            validation = ROOT / 'build/portal-recovery/validation/usr'
            env = dict(os.environ, VK_LAYER_PATH=str(validation / 'share/vulkan/explicit_layer.d'),
                       LD_LIBRARY_PATH=str(validation / 'lib/x86_64-linux-gnu'))
            result = subprocess.run([executable], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr[-6000:])
            report = json.loads(result.stdout)
            self.assertEqual(report, dict(nvidia_gpu=True, synthetic_frames_verified=10,
                cursor_uploads=3, validation_errors=0, desktop_opened=False, uu_phone_tested=False))


if __name__ == '__main__':
    unittest.main()
