"""Tests for the Python/GTK UUWay console's bounded file/config helpers."""
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("uuway_console", ROOT / "scripts/uuway_console.py")
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


class ConsoleConfigTests(unittest.TestCase):
    def test_atomic_private_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "config"
            path = parent / "settings.json"
            console._write_json(path, {"version": 1, "backend": "portal"})
            self.assertEqual(console._read_json(path)["backend"], "portal")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)

    def test_download_directory_uses_xdg_user_dirs_without_shell_expansion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config"
            config.mkdir()
            (config / "user-dirs.dirs").write_text('XDG_DOWNLOAD_DIR="$HOME/收件箱"\n')
            self.assertEqual(console._download_directory(root, config), root / "收件箱")

    def test_runtime_rejects_relative_or_extra_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config"
            config.mkdir(mode=0o700)
            with patch.object(console, "CONFIG_DIR", config):
                console._write_json(config / "native-runtime.json", {"schema": 1, "prefix": "relative"})
                self.assertIsNone(console._runtime())

    def test_desktop_image_falls_back_to_packaged_penguin(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config"
            config.mkdir(mode=0o700)
            with patch.object(console, "CONFIG_DIR", config):
                image, is_default = console._desktop_image()
            self.assertTrue(is_default)
            self.assertEqual(image.name, "uuway-penguin.bmp")
            self.assertTrue(image.is_file())

    def test_custom_download_directory_is_read_from_private_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config"
            config.mkdir(mode=0o700)
            target = root / "received"
            target.mkdir()
            with patch.object(console, "CONFIG_DIR", config):
                console._write_json(config / "download-directory.json", {"version": 1, "path": str(target)})
                self.assertEqual(console._configured_download_directory(), target.resolve())

    def test_desktop_file_uris_escape_paths_and_accept_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file_path = root / "桌面 文件.txt"
            file_path.write_text("send")
            folder = root / "文件夹"
            folder.mkdir()
            uris = console._desktop_file_uris([str(file_path), str(folder)])
            self.assertEqual(len(uris), 2)
            self.assertTrue(uris[0].startswith("file://"))
            self.assertIn("%E6%A1%8C%E9%9D%A2%20%E6%96%87%E4%BB%B6.txt", uris[0])
            self.assertTrue(uris[1].endswith("%E6%96%87%E4%BB%B6%E5%A4%B9"))

    def test_desktop_file_uris_reject_missing_or_special_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                console._desktop_file_uris([str(root / "missing")])
            with self.assertRaises(ValueError):
                console._desktop_file_uris([])


if __name__ == "__main__":
    unittest.main()
