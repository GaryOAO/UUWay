import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('migration', ROOT / 'scripts/native_launcher_migration.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class MigrationTests(unittest.TestCase):
    def setup_home(self, home):
        config = home / '.config/uurb'
        migration.safe_tree(home, config)
        (config / 'native-runtime.json').write_text('{}')
        (config / 'native-runtime.json').chmod(0o600)
        menu = home / '.local/share/applications'
        migration.safe_tree(home, menu)
        return menu

    def test_no_native_installation_is_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.assertEqual(migration.retire_legacy_launchers(home)['changed'], 0)
            self.assertEqual(list(home.iterdir()), [])

    def test_recognized_launcher_backed_up_and_migration_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            menu = self.setup_home(home)
            original = '[Desktop Entry]\nExec=' + str(home / '.local/bin/uu-remote') + ' open\n'
            path = menu / 'uu-remote.desktop'
            path.write_text(original)
            path.chmod(0o644)
            result = migration.retire_legacy_launchers(home)
            self.assertEqual(result['changed'], 5)
            backup = Path(result['backup']) / path.relative_to(home)
            self.assertEqual(backup.read_text(), original)
            self.assertEqual(path.read_text(), migration.RETIRED)
            self.assertEqual(migration.retire_legacy_launchers(home)['changed'], 0)
            for unit in migration.UNITS:
                guard = home / '.config/systemd/user' / (unit + '.service.d/99-uurb-native-guard.conf')
                self.assertEqual(guard.read_text(), migration.GUARD)

    def test_unrelated_entry_prevents_all_file_replacements(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            menu = self.setup_home(home)
            path = menu / 'uu-remote.desktop'
            original = '[Desktop Entry]\nExec=/usr/bin/true\n'
            path.write_text(original)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                migration.retire_legacy_launchers(home)
            self.assertEqual(path.read_text(), original)
            self.assertFalse((home / '.config/systemd').exists())

    def test_parent_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            home = Path(temp)
            menu = self.setup_home(home)
            (menu / 'wine').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                migration.retire_legacy_launchers(home)
            self.assertEqual(list(Path(outside).iterdir()), [])
            self.assertFalse((menu / 'uu-remote.desktop').exists())

    def test_regenerated_vendor_shortcut_is_retired_on_reinstall(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.setup_home(home)
            migration.retire_legacy_launchers(home)
            path = home / '.local/share/applications/wine/Programs/UU远程.desktop'
            prefix = home / '.local/share/wineprefixes/uu-remote'
            vendor = ('[Desktop Entry]\nName=UU远程\nExec=env "WINEPREFIX=' + str(prefix) + '" wine "UU.lnk"\n'
                      'StartupWMClass=gameviewer.exe\nPath=' + str(prefix / 'drive_c/Program Files/Netease/GameViewer') + '\n')
            path.write_text(vendor)
            result = migration.retire_legacy_launchers(home)
            self.assertEqual(result['changed'], 1)
            self.assertEqual(path.read_text(), migration.RETIRED)
            self.assertEqual((Path(result['backup']) / path.relative_to(home)).read_text(), vendor)


if __name__ == '__main__':
    unittest.main()
