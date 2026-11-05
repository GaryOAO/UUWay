"""Native OTA assets must never select or install another distribution's package."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_update', ROOT / 'scripts/uuway_update_apply.py')
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


class NativeUpdateTests(unittest.TestCase):
    def stage(self, root, target):
        package = root / update.package_name('1.5.0', target)
        package.write_bytes(b'fixture')
        (root / 'SHA256SUMS').write_text(f'{hashlib.sha256(package.read_bytes()).hexdigest()}  {package.name}\n')
        return package

    def test_asset_names_remain_separate_and_legacy_ubuntu_is_unchanged(self):
        names = {target: update.package_name('1.5.0', target) for target in update.TARGETS}
        self.assertEqual(len(set(names.values())), 4)
        self.assertEqual(names['ubuntu'], update.deb_name('1.5.0'))
        self.assertTrue(names['debian-13'].endswith('_trixie_amd64.deb'))
        self.assertTrue(names['fedora'].endswith('.fedora.x86_64.rpm'))
        self.assertTrue(names['arch'].endswith('.pkg.tar.zst'))
        with self.assertRaises(update.VerifyError):
            update.package_name('1.5.0', '../unknown')

    def test_target_metadata_is_pinned_for_reboot_and_old_packages_default_to_ubuntu(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(update.installed_target(root), 'ubuntu')
            info = root / 'usr/lib/uuway/package-info.json'
            info.parent.mkdir(parents=True)
            info.write_text(json.dumps(dict(target='fedora')))
            self.assertEqual(update.installed_target(root), 'fedora')
            info.write_text(json.dumps(dict(target='unknown')))
            with self.assertRaises(update.VerifyError):
                update.installed_target(root)

    def test_rpm_rejects_wrong_distribution_architecture_version_and_obsoletes(self):
        metadata = ('Package: uuway\nVersion: 1.5.0\nArchitecture: x86_64\nTarget: fedora\n'
                    'Release: 1\nEpoch: 0\nObsoletes: \n')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.stage(root, 'fedora')
            def verify(text):
                return update.check_release(root, 'v1.5.0', '1.4.4', target='fedora',
                    run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, text, ''))
            self.assertEqual(verify(metadata).version, '1.5.0')
            for old, new in (('Target: fedora', 'Target: ubuntu'), ('x86_64', 'aarch64'),
                             ('Version: 1.5.0', 'Version: 1.4.4'), ('Release: 1', 'Release: 2'),
                             ('Epoch: 0', 'Epoch: 1'), ('Obsoletes: ', 'Obsoletes: other')):
                with self.subTest(new=new), self.assertRaises(update.VerifyError):
                    verify(metadata.replace(old, new))

    def test_arch_rejects_wrong_target_and_package_replacements(self):
        metadata = ('pkgname = uuway\npkgver = 1.5.0-1\narch = x86_64\n'
                    'provides = uuway-target-arch\n')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.stage(root, 'arch')
            def verify(text):
                return update.check_release(root, 'v1.5.0', '1.4.4', target='arch',
                    run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, text, ''))
            self.assertEqual(verify(metadata).version, '1.5.0')
            for text in (metadata.replace('uuway-target-arch', 'uuway-target-fedora'),
                         metadata + 'replaces = other\n', metadata + 'conflict = other\n',
                         metadata.replace('1.5.0-1', '1.5.0-2'), metadata.replace('x86_64', 'aarch64'),
                         metadata + 'pkgname = other\n'):
                with self.subTest(text=text), self.assertRaises(update.VerifyError):
                    verify(text)

    def test_missing_native_asset_cannot_fall_back_to_ubuntu(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.stage(root, 'ubuntu')
            for target in ('debian-13', 'fedora', 'arch'):
                with self.subTest(target=target), self.assertRaises(update.VerifyError):
                    update.check_checksums(root, 'v1.5.0', target=target)

    def test_native_versions_are_compared_without_dpkg(self):
        for target in ('fedora', 'arch'):
            self.assertTrue(update.version_is_newer('1.10.0', '1.9.9', target=target))
            self.assertFalse(update.version_is_newer('1.5.0', '1.5.0', target=target))
            with self.assertRaises(update.VerifyError):
                update.version_is_newer('1.5.0', '1.4.3~port1', target=target)

    def test_package_manager_commands_cannot_update_unrelated_packages(self):
        for target, tool in (('fedora', 'rpm'), ('arch', 'pacman')):
            job = type('Job', (), dict(target=target))()
            command = update.install_command(job, '/private/uuway-package', downgrade=True)
            self.assertEqual(command[0], tool)
            self.assertEqual(command[-1], '/private/uuway-package')
            self.assertNotIn('--nodeps', command)
            self.assertNotIn('--allowerasing', command)
            self.assertNotIn('--overwrite', command)


if __name__ == '__main__':
    unittest.main()
