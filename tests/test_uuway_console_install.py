"""Immutable console releases include the artwork displayed by the UI."""
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    spec = importlib.util.spec_from_file_location("console_installer", ROOT / "scripts/install-uu-settings.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
finally:
    sys.path.pop(0)


class ConsoleInstallTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        for name, data in (("SOURCE", b"#!/usr/bin/python3\nprint('console')\n"),
                           ("PENGUIN", b"fixture-bitmap"), ("ICON", b"<svg/>")):
            source = self.root / name
            source.write_bytes(data)
            override = patch.object(installer, name, source)
            override.start(); self.addCleanup(override.stop)
        for override in (patch.object(Path, "home", return_value=self.home),
                         patch.object(installer, "retire_legacy_launchers", return_value={"changed": 0})):
            override.start(); self.addCleanup(override.stop)

    def install(self):
        with redirect_stdout(io.StringIO()):
            installer.run()
        entry = (self.home / ".local/share/applications/uuway.desktop").read_text()
        icon = next(line[5:] for line in entry.splitlines() if line.startswith("Icon="))
        return Path(icon).parent.parent

    def test_artwork_only_update_installs_a_new_complete_release(self):
        first = self.install()
        self.assertEqual(first, self.install())
        installer.PENGUIN.write_bytes(b"updated-fixture-bitmap")
        second = self.install()
        self.assertNotEqual(first, second)
        self.assertEqual((first / "assets/uuway-penguin.bmp").read_bytes(), b"fixture-bitmap")
        self.assertEqual((second / "assets/uuway-penguin.bmp").read_bytes(), b"updated-fixture-bitmap")
        self.assertEqual((second / "uuway-console").read_bytes(), installer.SOURCE.read_bytes())

    def test_modified_installed_artwork_is_not_silently_reused(self):
        release = self.install()
        (release / "assets/uuway-icon.svg").write_text("changed")
        with self.assertRaisesRegex(ValueError, "Changed UUWay console release"):
            self.install()


if __name__ == "__main__":
    unittest.main()
