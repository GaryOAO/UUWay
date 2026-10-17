"""Setup must validate real encoding and retain the upstream NVIDIA default."""
import contextlib
from argparse import Namespace
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import uuway_cli as cli


class AmdSetupTests(unittest.TestCase):
    def test_spatial_aq_allocates_bits_to_flat_regions_and_bounds_strength(self):
        source = r'''
#include "native_spatial_aq.h"
#include <assert.h>
int main(void) {
    uint8_t pixels[256 * 128]; int delta[32], strong[32];
    for (unsigned y = 0; y < 128; ++y) for (unsigned x = 0; x < 256; ++x)
        pixels[y * 256 + x] = x < 128 ? 128 : ((x / 4 + y / 4) & 1) * 255;
    uurb_spatial_aq(pixels, 256, 256, 128, 3, delta);
    uurb_spatial_aq(pixels, 256, 256, 128, 15, strong);
    assert(delta[0] < 0 && delta[7] > 0);
    assert(strong[0] < delta[0] && strong[7] > delta[7]);
    for (int i = 0; i < 32; ++i) assert(strong[i] >= -5 && strong[i] <= 5);
    for (unsigned i = 0; i < sizeof(pixels); ++i) pixels[i] = 128;
    uurb_spatial_aq(pixels, 256, 256, 128, 0, delta);
    for (int i = 0; i < 32; ++i) assert(delta[i] == 0);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'aq'
            subprocess.run(['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-x', 'c', '-',
                            '-I', str(ROOT / 'src'), '-o', str(binary)], input=source, text=True, check=True)
            subprocess.run([str(binary)], check=True)

    def check(self, marker, code):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'build').mkdir()
            if marker:
                (root / 'build/encoder-backend').write_text(marker)
            ctx = Mock()
            ctx.probe.return_value = subprocess.CompletedProcess([], code, stdout='GPU 0: NVIDIA', stderr='hardware failure')
            with patch.object(cli, 'ROOT', root), patch.dict(os.environ, XDG_SESSION_TYPE='wayland', XDG_CURRENT_DESKTOP='GNOME'), contextlib.redirect_stdout(io.StringIO()):
                if code:
                    with self.assertRaises(cli.SetupError):
                        cli.check_environment(ctx)
                else:
                    cli.check_environment(ctx)
            return ctx.probe.call_args.args[0]

    def test_amd_runs_encoder_instead_of_nvidia_smi(self):
        argv = self.check('amd-vaapi\n', 0)
        self.assertEqual(Path(argv[0]).name, 'uu-vaapi-probe')

    def test_amd_hardware_failure_is_fatal(self):
        argv = self.check('amd-vaapi\n', 1)
        self.assertEqual(Path(argv[0]).name, 'uu-vaapi-probe')

    def test_no_marker_retains_nvidia_check(self):
        self.assertEqual(self.check(None, 0), ['nvidia-smi', '-L'])

    def test_login_bootstraps_service_and_stages_companion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage = root / 'build/native-presenter'
            stage.mkdir(parents=True)
            for name in ('uu-native-bootstrap.exe', 'uu-native-winlogon.exe'):
                (stage / name).write_bytes(name.encode())
            ctx = Mock(dry_run=False, state_dir=root / 'state')
            ctx.wine_env.return_value = {'WINEPREFIX': str(root / 'prefix')}
            with patch.object(cli, 'ROOT', root), patch.object(cli, 'amd_backend_enabled', return_value=True), \
                    patch.object(cli, 'unit_active', return_value=False), patch.object(cli.subprocess, 'Popen') as launch:
                cli.open_uu(ctx)
            argv = launch.call_args.args[0]
            self.assertEqual(argv[2], '--login')
            self.assertEqual(launch.call_args.kwargs['env']['UURB_NATIVE_BOOTSTRAP'], '1')
            self.assertEqual((ctx.state_dir / 'login-support/winlogon.exe').read_bytes(), b'uu-native-winlogon.exe')

    def test_open_with_running_bridge_explains_isolation_without_launching(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = Mock(dry_run=False, state_dir=Path(directory))
            ctx.wine_env.return_value = {}
            with patch.object(cli, 'amd_backend_enabled', return_value=True), \
                    patch.object(cli, 'unit_active', return_value=True), patch.object(cli.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(cli.SetupError, 'setup --login'):
                    cli.open_uu(ctx)
            launch.assert_not_called()

    def test_setup_repairs_vendor_shortcut_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(Path, 'home', return_value=root), \
                    patch.object(cli, 'amd_backend_enabled', return_value=True):
                ctx = cli.Context(Namespace())
                shortcut = root / 'Desktop/UU远程.desktop'
                shortcut.parent.mkdir()
                original = ('[Desktop Entry]\nExec=wine launcher.exe\n'
                            'StartupWMClass=gameviewer.exe\n' + f'Path={ctx.app_dir}\n')
                shortcut.write_text(original)
                self.assertFalse(cli.launchers_done(ctx))
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.launchers_run(ctx)
                    cli.launchers_run(ctx)
                self.assertTrue(cli.launchers_done(ctx))
                self.assertIn('scripts/uuway-amd.sh" open', shortcut.read_text())
                self.assertEqual(shortcut.with_suffix('.desktop.before-amd').read_text(), original)

    def test_uninstall_restores_amd_shortcuts_even_without_backend_marker(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                with patch.object(Path, 'home', return_value=home):
                    args = Namespace(yes=True, purge=True, dry_run=dry_run)
                    ctx = cli.Context(args)
                paths = [home / 'Desktop/UU远程.desktop',
                         home / '.local/share/applications/wine/Programs/UU远程.desktop',
                         home / '.local/share/applications/uuway-amd.desktop']
                originals = {}
                for path in paths:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(cli.amd_launcher_text())
                    if path != paths[-1]:
                        original = '[Desktop Entry]\nExec=wine original.exe\n'
                        backup = path.with_suffix('.desktop.before-amd')
                        backup.write_text(original)
                        backup.chmod(0o750)
                        originals[path] = original
                ctx.prefix.mkdir(parents=True)
                with patch.object(cli, 'Context', return_value=ctx), \
                        patch.object(ctx, 'run'), patch.object(cli, 'amd_backend_enabled', return_value=False), \
                        contextlib.redirect_stdout(io.StringIO()):
                    cli.command_uninstall(args)
                for path in paths:
                    if dry_run:
                        self.assertEqual(path.read_text(), cli.amd_launcher_text())
                    elif path in originals:
                        self.assertEqual(path.read_text(), originals[path])
                        self.assertEqual(path.stat().st_mode & 0o777, 0o750)
                        self.assertFalse(path.with_suffix('.desktop.before-amd').exists())
                    else:
                        self.assertFalse(path.exists())
                self.assertTrue(ctx.prefix.is_dir())

    def test_uninstall_preserves_unmanaged_entries_and_symlinks(self):
        for kind in ('unmanaged', 'launcher_symlink', 'backup_symlink'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                with patch.object(Path, 'home', return_value=home):
                    ctx = cli.Context(Namespace())
                target = home / 'external.desktop'
                target.write_text(cli.amd_launcher_text())
                path = home / '.local/share/applications/uuway-amd.desktop'
                path.parent.mkdir(parents=True)
                if kind == 'launcher_symlink':
                    path.symlink_to(target)
                else:
                    path.write_text('user entry' if kind == 'unmanaged' else cli.amd_launcher_text())
                if kind == 'backup_symlink':
                    path.with_suffix('.desktop.before-amd').symlink_to(target)
                before = path.read_text()
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.remove_amd_launchers(ctx)
                self.assertEqual(path.read_text(), before)
                self.assertEqual(target.read_text(), cli.amd_launcher_text())
                self.assertEqual(path.is_symlink(), kind == 'launcher_symlink')


if __name__ == '__main__':
    unittest.main()
