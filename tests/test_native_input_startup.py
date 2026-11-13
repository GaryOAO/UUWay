"""Deterministic checks for the Windows bootstrap's actual input hook loop."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NativeInputStartupTests(unittest.TestCase):
    def test_startup_retry_and_liveness_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'startup'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            str(ROOT / 'tests/probes/native_input_startup.c'), '-o', str(executable)],
                           check=True, capture_output=True, timeout=30)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('native input startup checks passed', result.stdout)
