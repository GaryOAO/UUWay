"""SendInput → evdev translation used by the native input worker."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NativeInputTranslateTests(unittest.TestCase):
    def test_translation_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'translate'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT / 'src'),
                            '-o', str(executable), str(ROOT / 'tests/probes/native_input_translate.c')],
                           check=True, capture_output=True, timeout=60)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('translation checks passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
