from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CUDA = Path('/usr/local/cuda/include')


@unittest.skipUnless(shutil.which('gcc') and (CUDA / 'cuda.h').exists(), 'CUDA headers and gcc required')
class CudaFrameCopyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.binary = str(Path(cls.directory.name) / 'copy-test')
        subprocess.run(['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(CUDA),
                        '-I', str(ROOT / 'src'), str(ROOT / 'src/native_cuda_frame_copy.c'),
                        str(ROOT / 'tests/probes/cuda_frame_copy_lifecycle.c'), '-o', cls.binary],
                       check=True, capture_output=True, timeout=20)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def check_case(self, case, expected=0):
        result = subprocess.run([self.binary, case], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, expected, case + '\n' + result.stderr)

    def test_persistent_array_copy_and_context_restoration(self):
        self.check_case('success')

    def test_invalid_arguments_consume_descriptors(self):
        for case in ('same-fd', 'missing-fd', 'odd', 'small', 'large', 'size', 'oversize', 'uuid', 'null-uuid'):
            with self.subTest(case=case):
                self.check_case('invalid-' + case)

    def test_each_partial_initialization_failure_releases_resources(self):
        for case in ('init', 'count', 'device', 'uuid', 'context', 'import1', 'import2', 'map1', 'map2',
                     'level1', 'level2', 'stream', 'event'):
            with self.subTest(case=case):
                self.check_case(case)

    def test_pre_submission_context_error_is_recoverable(self):
        self.check_case('frame-push')

    def test_uncertain_gpu_completion_exits_worker_without_reuse(self):
        for case in ('copy', 'record', 'query', 'timeout'):
            with self.subTest(case=case):
                self.check_case('frame-' + case, 4)


if __name__ == '__main__':
    unittest.main()
