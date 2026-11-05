"""A shared Fedora package selects native FFmpeg components without user choices."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('platform_files', ROOT / 'scripts/uuway_platform.py')
platform = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform)


class FedoraBundleTests(unittest.TestCase):
    def test_only_ffmpeg_components_switch_on_fedora45(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compat = root / 'build/compat/fedora45'
            compat.mkdir(parents=True)
            for name in platform.FEDORA_AMD_VARIANTS:
                (compat / name).write_bytes(b'variant')
                for version in ('44', '45'):
                    selected = platform.backend_file(root, 'amd', name, dict(ID='fedora', VERSION_ID=version))
                    self.assertEqual(selected == compat / name, version == '45')
                self.assertNotEqual(platform.backend_file(root, 'nvidia', name,
                    dict(ID='fedora', VERSION_ID='45')), compat / name)
            self.assertEqual(platform.backend_file(root, 'amd', 'uu-native-input',
                dict(ID='fedora', VERSION_ID='45')), root / 'build/backends/amd/native-presenter/uu-native-input')


if __name__ == '__main__':
    unittest.main()
