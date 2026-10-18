"""uuway setup/doctor/uninstall: each step is idempotent and touches only what it owns."""
from argparse import Namespace
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    spec = importlib.util.spec_from_file_location("uuway_cli", ROOT / "scripts/uuway_cli.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
finally:
    sys.path.pop(0)

OLD_PROXY = b"MZ-old-terminal-proxy"
NEW_PROXY = b"MZ-new-terminal-proxy"
WINE_PLACEHOLDER = b"MZ Wine builtin DLL powershell placeholder"


def completed(argv, code=0, out=""):
    return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")


class CliCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.checkout = self.root / "checkout"
        (self.checkout / "build/helpers").mkdir(parents=True)
        for name in cli.HELPERS:
            (self.checkout / "build/helpers" / name).write_bytes(NEW_PROXY if name == "uu-terminal-proxy.exe"
                                                                   else b"new-" + name.encode())
        for override in (patch.object(Path, "home", return_value=self.home),
                         patch.object(cli, "ROOT", self.checkout)):
            override.start()
            self.addCleanup(override.stop)
        self.prefix = self.home / ".local/share/wineprefixes/uu-remote"
        self.commands = []
        self.active = set()

    def context(self, **options):
        args = Namespace(dry_run=False, yes=True, prefix=self.prefix, **options)
        ctx = cli.Context(args)
        self.assertEqual(ctx.prefix, self.prefix)
        return ctx

    def record_commands(self, ctx):
        """Replace command execution; systemctl is-active answers from self.active."""
        def run(argv, **kwargs):
            self.commands.append([str(part) for part in argv])
            return completed(argv)

        def probe(argv, **kwargs):
            argv = [str(part) for part in argv]
            if argv[:3] == ["systemctl", "--user", "is-active"]:
                unit = argv[-1].removesuffix(".service")
                return completed(argv, 0 if unit in self.active else 3)
            if argv[0] == "pgrep":
                return completed(argv, 1)
            return completed(argv, 127)
        ctx.run = run
        ctx.probe = probe
        return ctx

    def quiet(self, function, *args, **kwargs):
        with redirect_stdout(io.StringIO()) as output:
            result = function(*args, **kwargs)
        return result, output.getvalue()

    def make_prefix(self, with_uu=True):
        bin_dir = self.prefix / "drive_c/Program Files/Netease/GameViewer/bin"
        bin_dir.mkdir(parents=True)
        if with_uu:
            (bin_dir / "GameViewerServer.exe").write_bytes(b"MZ server")
        (self.prefix / "drive_c/windows/system32/WindowsPowerShell/v1.0").mkdir(parents=True)
        return bin_dir


class HelperSyncTests(CliCase):
    def test_fresh_install_puts_the_terminal_proxy_beside_uu(self):
        bin_dir = self.make_prefix()
        ctx = self.context()
        self.assertFalse(cli.helpers_done(ctx))
        self.quiet(cli.helpers_run, ctx)
        self.assertEqual((bin_dir / "powershell.exe").read_bytes(), NEW_PROXY)
        for name in cli.HELPERS:
            installed = self.prefix / "compat" / name
            self.assertEqual(installed.read_bytes(), (self.checkout / "build/helpers" / name).read_bytes())
            self.assertEqual(installed.stat().st_mode & 0o777, 0o755)
        self.assertTrue(cli.helpers_done(ctx))

    def test_previous_proxy_and_conpty_are_upgraded_wherever_they_were_installed(self):
        bin_dir = self.make_prefix()
        compat = self.prefix / "compat"
        compat.mkdir()
        mux = self.prefix / "drive_c/windows/system32/WindowsPowerShell/v1.0/powershell.exe"
        (compat / "uu-terminal-proxy.exe").write_bytes(OLD_PROXY)
        (compat / "uu-conpty.dll").write_bytes(b"old-conpty")
        for target in (bin_dir / "powershell.exe", mux):
            target.write_bytes(OLD_PROXY)
        (bin_dir / "conpty.dll").write_bytes(b"old-conpty")
        self.quiet(cli.helpers_run, self.context())
        self.assertEqual((bin_dir / "powershell.exe").read_bytes(), NEW_PROXY)
        self.assertEqual(mux.read_bytes(), NEW_PROXY)
        self.assertEqual((bin_dir / "conpty.dll").read_bytes(), b"new-uu-conpty.dll")

    def test_files_that_are_not_ours_are_left_alone(self):
        bin_dir = self.make_prefix()
        compat = self.prefix / "compat"
        compat.mkdir()
        mux = self.prefix / "drive_c/windows/system32/WindowsPowerShell/v1.0/powershell.exe"
        (compat / "uu-terminal-proxy.exe").write_bytes(OLD_PROXY)
        (bin_dir / "powershell.exe").write_bytes(b"vendor powershell")
        (bin_dir / "conpty.dll").write_bytes(b"microsoft conpty")
        mux.write_bytes(WINE_PLACEHOLDER)
        _, output = self.quiet(cli.helpers_run, self.context())
        self.assertEqual((bin_dir / "powershell.exe").read_bytes(), b"vendor powershell")
        self.assertEqual((bin_dir / "conpty.dll").read_bytes(), b"microsoft conpty")
        self.assertEqual(mux.read_bytes(), WINE_PLACEHOLDER)
        self.assertIn("powershell.exe", output)

    def test_rerun_after_stopping_between_targets_and_compat_converges(self):
        bin_dir = self.make_prefix()
        compat = self.prefix / "compat"
        compat.mkdir()
        (compat / "uu-terminal-proxy.exe").write_bytes(OLD_PROXY)  # compat is replaced last
        (bin_dir / "powershell.exe").write_bytes(NEW_PROXY)        # targets were already upgraded
        self.quiet(cli.helpers_run, self.context())
        self.assertEqual((compat / "uu-terminal-proxy.exe").read_bytes(), NEW_PROXY)
        self.assertEqual((bin_dir / "powershell.exe").read_bytes(), NEW_PROXY)

    def test_repeat_is_a_no_op_and_dry_run_writes_nothing(self):
        self.make_prefix()
        ctx = self.context()
        self.quiet(cli.helpers_run, ctx)
        self.assertTrue(cli.helpers_done(ctx))
        snapshot = sorted(path for path in self.prefix.rglob("*") if path.is_file())
        dry = self.context()
        dry.dry_run = True
        self.quiet(cli.helpers_run, dry)
        self.assertEqual(snapshot, sorted(path for path in self.prefix.rglob("*") if path.is_file()))

    def test_missing_helper_in_the_package_is_reported(self):
        self.make_prefix()
        (self.checkout / "build/helpers/uu-conpty.dll").unlink()
        with self.assertRaises(cli.SetupError):
            self.quiet(cli.helpers_run, self.context())


class RuntimeStepTests(CliCase):
    def test_same_api_runtime_upgrade_also_reloads_display_fixes(self):
        old, new = self.home / 'old-runtime', self.home / '.local/share/uuway/releases/abc'
        for bundle in (old, new):
            bundle.mkdir(parents=True)
            (bundle / 'manifest.json').write_text(json.dumps({'contract': {'virtual_display_emulation': True}}))
        ctx = self.run_runtime(config={'bundle': str(old)}, bridge_active=True)
        self.assertTrue(ctx.restart_display)

    def test_packaging_failure_preserves_the_running_bridge(self):
        ctx = self.record_commands(self.context())
        self.active.add('uu-native-bridge')
        with patch.object(cli, 'package_bundle', side_effect=cli.SetupError('invalid runtime')):
            with self.assertRaises(cli.SetupError):
                self.quiet(cli.runtime_run, ctx)
        self.assertFalse(any('stop' in command for command in self.commands))

    def test_upgrade_to_virtual_display_api_restarts_only_display_before_bridge(self):
        bundle = self.home / '.local/share/uuway/releases/abc'
        bundle.mkdir(parents=True)
        (bundle / 'manifest.json').write_text(json.dumps({'contract': {'virtual_display_emulation': True}}))
        ctx = self.run_runtime(config={'bundle': str(self.home / 'old-runtime'), 'cursor_mode': 'composited'},
                               bridge_active=True)
        self.assertTrue(ctx.restart_display)
        with patch.object(cli.time, 'sleep'):
            self.quiet(cli.start_run, ctx)
        restarts = [command[-1] for command in self.commands if 'restart' in command]
        self.assertEqual(restarts, ['uu-native-display.service', 'uu-native-bridge.service'])
        self.assertFalse(ctx.restart_display)

    def run_runtime(self, config=None, bridge_active=False, text_backend=None):
        ctx = self.record_commands(self.context())
        if config:
            ctx.config_dir.mkdir(mode=0o700, parents=True)
            ctx.runtime_config.write_text(json.dumps(config))
        if text_backend:
            ctx.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            (ctx.config_dir / "text-backend.json").write_text(text_backend)
        if bridge_active:
            self.active.add("uu-native-bridge")
        bundle = self.home / ".local/share/uuway/releases/abc"
        with patch.object(cli, "package_bundle", return_value=bundle):
            self.quiet(cli.runtime_run, ctx)
        return ctx

    def install_command(self):
        return next(c for c in self.commands if c[1].endswith("install-uu-native-service.py"))

    def test_fresh_install_chooses_cursor_and_display_policy(self):
        self.run_runtime()
        command = self.install_command()
        self.assertEqual(command[command.index("--cursor-mode") + 1], "metadata")
        self.assertIn("--preserve-display-session", command)

    def test_existing_install_keeps_what_the_console_saved(self):
        self.run_runtime(config={"cursor_mode": "composited"})
        command = self.install_command()
        self.assertNotIn("--cursor-mode", command)
        self.assertNotIn("--preserve-display-session", command)

    def test_bridge_is_stopped_before_files_change_and_units_reload_after(self):
        self.run_runtime(bridge_active=True)
        kinds = [next((tag for tag in ("stop", "install-uu", "configure-uu", "daemon-reload") if tag in " ".join(c)),
                      None) for c in self.commands]
        self.assertEqual([k for k in kinds if k], ["stop", "install-uu", "configure-uu", "daemon-reload"])

    def stage_existing_install(self):
        """A working install as install.sh leaves it: units, config, helpers, proxies, menu entry."""
        ctx = self.context()
        ctx.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        ctx.runtime_config.write_text(json.dumps({"cursor_mode": "composited"}))
        (ctx.config_dir / "text-backend.json").write_text('{"version":1,"backend":"fcitx"}')
        ctx.unit_dir.mkdir(parents=True, exist_ok=True)
        for name in cli.SERVICES:
            (ctx.unit_dir / f"{name}.service").write_text(cli.SERVICE_MARKER + f"\n# {name}\n")
        bin_dir = self.make_prefix()
        compat = self.prefix / "compat"
        compat.mkdir()
        for name in cli.HELPERS:
            (compat / name).write_bytes(b"old-" + name.encode())
        (compat / "uu-terminal-proxy.exe").write_bytes(OLD_PROXY)
        (bin_dir / "powershell.exe").write_bytes(OLD_PROXY)
        (bin_dir / "conpty.dll").write_bytes(b"old-uu-conpty.dll")
        (self.prefix / "drive_c/windows/system32/WindowsPowerShell/v1.0/powershell.exe").write_bytes(OLD_PROXY)
        desktop = self.home / ".local/share/applications/uuway.desktop"
        desktop.parent.mkdir(parents=True, exist_ok=True)
        desktop.write_text(cli.DESKTOP_MARKER + "\n[Desktop Entry]\n")
        return ctx

    @staticmethod
    def snapshot(root):
        found = {}
        for path in sorted(root.rglob("*")):
            relative = str(path.relative_to(root))
            if path.is_file() and "backup-" not in relative and ".local/share/uuway" not in relative:
                found[relative] = path.read_bytes()
        return found

    def test_a_working_install_is_backed_up_in_full_before_the_first_change(self):
        ctx = self.record_commands(self.stage_existing_install())
        before = self.snapshot(self.home)
        self.quiet(cli.helpers_run, ctx)  # the first step that replaces anything
        backups = sorted(ctx.state_dir.glob("backup-*"))
        self.assertEqual(len(backups), 1)
        saved = self.snapshot(backups[0] / "home")
        self.assertEqual(saved, {name: before[name] for name in saved})
        for expected in (".config/uurb/native-runtime.json", ".config/uurb/text-backend.json",
                         ".local/share/applications/uuway.desktop",
                         *(f".config/systemd/user/{name}.service" for name in cli.SERVICES),
                         *(f".local/share/wineprefixes/uu-remote/compat/{name}" for name in cli.HELPERS),
                         ".local/share/wineprefixes/uu-remote/drive_c/Program Files/Netease/GameViewer/bin/powershell.exe",
                         ".local/share/wineprefixes/uu-remote/drive_c/Program Files/Netease/GameViewer/bin/conpty.dll",
                         ".local/share/wineprefixes/uu-remote/drive_c/windows/system32/WindowsPowerShell/v1.0/powershell.exe"):
            self.assertIn(expected, saved)
        # what was backed up is what was there before, not what the step wrote
        self.assertEqual(saved[".local/share/wineprefixes/uu-remote/compat/uu-terminal-proxy.exe"], OLD_PROXY)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o700)
        with patch.object(cli, "package_bundle", return_value=self.home / "bundle"):
            self.quiet(cli.runtime_run, ctx)
        self.assertEqual(len(list(ctx.state_dir.glob("backup-*"))), 1, "one backup per run, not one per step")

    def test_the_documented_restore_command_really_restores_everything(self):
        ctx = self.record_commands(self.stage_existing_install())
        before = self.snapshot(self.home)
        self.quiet(cli.helpers_run, ctx)
        with patch.object(cli, "package_bundle", return_value=self.home / "bundle"):
            self.quiet(cli.runtime_run, ctx)
        backup = next(ctx.state_dir.glob("backup-*"))
        instructions = (backup / "RESTORE.txt").read_text()
        self.assertIn("cp -a home/. ~/", instructions)
        self.assertIn("systemctl --user daemon-reload", instructions)
        restored = self.root / "restored-home"
        restored.mkdir()
        subprocess.run(["cp", "-a", "home/.", str(restored) + "/"], cwd=backup, check=True)
        after = self.snapshot(restored)
        self.assertEqual(after, {name: before[name] for name in after})
        self.assertEqual(after[".local/share/wineprefixes/uu-remote/compat/uu-terminal-proxy.exe"], OLD_PROXY)

    def test_a_fresh_install_and_a_dry_run_make_no_backup(self):
        self.run_runtime()
        self.assertEqual(list(self.state_dir_of().glob("backup-*")), [])
        ctx = self.stage_existing_install()
        ctx.dry_run = True
        self.quiet(cli.ensure_backup, ctx)
        self.assertEqual(list(ctx.state_dir.glob("backup-*")), [])

    def test_steps_that_change_nothing_do_not_pile_up_backups(self):
        ctx = self.record_commands(self.stage_existing_install())
        self.quiet(cli.helpers_run, ctx)
        self.quiet(cli.helpers_run, self.record_commands(self.context()))  # a later, separate run
        self.assertEqual(len(list(ctx.state_dir.glob("backup-*"))), 2)  # one per run that changed something
        fresh = self.record_commands(self.context())
        self.assertTrue(cli.helpers_done(fresh))  # and a run that finds everything done has no step to back up

    def state_dir_of(self):
        return self.home / ".local/state/uurb"

    def test_install_arguments_name_the_users_own_paths(self):
        ctx = self.run_runtime()
        command = self.install_command()
        pairs = dict(zip(command[2::2], command[3::2]))
        self.assertEqual(pairs["--prefix"], str(ctx.prefix))
        self.assertEqual(pairs["--restore-state"], str(ctx.state_dir / "portal-probe.json"))
        self.assertEqual(pairs["--text-socket"], str(ctx.state_dir / "text.sock"))
        self.assertTrue(pairs["--bundle"].startswith(str(self.home)))

    def test_text_backend_is_portal_unless_fcitx_and_its_addon_are_both_present(self):
        ctx = self.run_runtime()
        self.assertEqual(json.loads((ctx.config_dir / "text-backend.json").read_text())["backend"], "portal")

    def test_existing_text_backend_choice_is_not_overwritten(self):
        ctx = self.run_runtime(text_backend='{"version":1,"backend":"fcitx"}')
        self.assertEqual(json.loads((ctx.config_dir / "text-backend.json").read_text())["backend"], "fcitx")

    def test_a_new_runtime_requires_restarting_the_bridge(self):
        ctx = self.run_runtime()
        self.assertTrue(ctx.restart_bridge)
        self.active.update(cli.SERVICES)
        self.assertFalse(cli.start_done(ctx))


class StartStepTests(CliCase):
    def test_explicit_restart_is_not_skipped_when_every_service_is_active(self):
        self.active.update(cli.SERVICES)
        self.assertTrue(cli.start_done(self.record_commands(self.context())))
        for options in ({'restart_display': True}, {'restart_services': True}):
            with self.subTest(options=options):
                self.assertFalse(cli.start_done(self.record_commands(self.context(**options))))

    def start(self, **options):
        ctx = self.record_commands(self.context(**options))
        ctx.restart_bridge = True
        self.active.add("uu-native-bridge")
        with patch.object(cli.time, "sleep"):
            self.quiet(cli.start_run, ctx)
        return ctx

    def restarts(self):
        return [c for c in self.commands if "restart" in c]

    def test_only_the_bridge_is_restarted_by_default(self):
        self.start()
        self.assertEqual([c[-1] for c in self.restarts()], ["uu-native-bridge.service"])

    def test_restart_services_includes_text_and_display(self):
        self.start(restart_services=True)
        flat = " ".join(" ".join(c) for c in self.restarts())
        self.assertIn("uu-native-text.service", flat)
        self.assertIn("uu-native-display.service", flat)

    def test_a_bridge_that_does_not_come_up_is_an_error(self):
        ctx = self.record_commands(self.context())
        ctx.restart_bridge = True
        with patch.object(cli.time, "sleep"), self.assertRaises(cli.SetupError):
            self.quiet(cli.start_run, ctx)


class PrefixStepTests(CliCase):
    def test_signed_in_ignores_the_guest_profile(self):
        ctx = self.context()
        settings = self.prefix / "drive_c/users/u/AppData/Local/GameViewer"
        settings.mkdir(parents=True)
        (settings / "setting_guest_1.ini").write_text("")
        self.assertFalse(cli.signed_in(ctx))
        (settings / "setting_12345.ini").write_text("")
        self.assertTrue(cli.signed_in(ctx))

    def test_powershell_override_is_read_from_the_dll_overrides_section_only(self):
        ctx = self.context()
        self.prefix.mkdir(parents=True)
        registry = self.prefix / "user.reg"
        registry.write_text('[Software\\\\Other]\n"powershell.exe"="native"\n\n'
                            '[Software\\\\Wine\\\\DllOverrides] 1700000000\n"ntdll"="builtin"\n')
        self.assertFalse(cli.powershell_native(ctx))
        registry.write_text('[Software\\\\Wine\\\\DllOverrides] 1700000000\n"ntdll"="builtin"\n'
                            '"powershell.exe"="native"\n\n[Software\\\\Wine\\\\X]\n')
        self.assertTrue(cli.powershell_native(ctx))

    def test_a_fresh_prefix_is_made_private_for_the_service_that_requires_it(self):
        ctx = self.record_commands(self.context(installer=self.root / "UURemote_Setup.exe"))
        (self.root / "UURemote_Setup.exe").write_bytes(b"MZ")
        server = ctx.server_exe

        def run(argv, **kwargs):
            argv = [str(part) for part in argv]
            self.commands.append(argv)
            if argv[0].endswith("wineboot"):
                ctx.prefix.mkdir(parents=True, mode=0o755)
                os.chmod(ctx.prefix, 0o755)
            elif argv[-1] == "/S":
                server.parent.mkdir(parents=True)
                server.write_bytes(b"MZ")
            return completed(argv)
        ctx.run = run
        ctx.wait_for_user = lambda what: self.commands.append(["wait", what])
        ctx.args.login = False
        with patch.object(cli, "signed_in", return_value=True), patch.object(cli.subprocess, "Popen") as login:
            self.quiet(cli.prefix_run, ctx)
        login.assert_called_once()  # a freshly installed UU always needs its login window
        self.assertEqual(ctx.prefix.stat().st_mode & 0o777, 0o700)
        boot = next(c for c in self.commands if c[0].endswith("wineboot"))
        self.assertIsNotNone(boot)

    def test_missing_installer_is_a_clear_error(self):
        ctx = self.record_commands(self.context(installer=self.root / "nope.exe"))
        with self.assertRaises(cli.SetupError):
            self.quiet(cli.prefix_run, ctx)


class EnvironmentTests(CliCase):
    def check(self, env, nvidia=0, euid=1000):
        ctx = self.context()
        ctx.probe = lambda argv, **kwargs: completed(argv, nvidia, "GPU 0: NVIDIA GeForce RTX 3090 (UUID: x)\n")
        with patch.dict(os.environ, env, clear=True), patch.object(cli.os, "geteuid", return_value=euid):
            return self.quiet(cli.check_environment, ctx)

    wayland = {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}

    def test_accepts_gnome_wayland_with_nvidia(self):
        self.check(self.wayland)

    def test_refuses_root_and_sudo(self):
        with self.assertRaises(cli.SetupError):
            self.check(self.wayland, euid=0)
        with self.assertRaises(cli.SetupError):
            self.check(dict(self.wayland, SUDO_USER="henu"))

    def test_refuses_x11_and_other_desktops(self):
        with self.assertRaises(cli.SetupError):
            self.check({"XDG_SESSION_TYPE": "x11", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"})
        with self.assertRaises(cli.SetupError):
            self.check({"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "KDE"})

    def test_refuses_a_machine_without_a_usable_nvidia_driver(self):
        with self.assertRaises(cli.SetupError):
            self.check(self.wayland, nvidia=9)


class FcitxStepTests(CliCase):
    def test_user_level_addon_points_at_the_installed_library_without_extension(self):
        library = self.checkout / "fcitx5/libuurb-native-ime.so"
        library.parent.mkdir()
        library.write_bytes(b"\x7fELF")
        ctx = self.context()
        with patch.object(cli.shutil, "which", return_value="/usr/bin/fcitx5"):
            self.assertFalse(cli.fcitx_done(ctx))
            self.quiet(cli.fcitx_run, ctx)
            self.assertTrue(cli.fcitx_done(ctx))
        text = cli.fcitx_conf_path(ctx).read_text()
        self.assertTrue(text.startswith(cli.FCITX_MARKER))
        self.assertIn(f"Library={library.with_suffix('')}\n", text)
        self.assertIn("Enabled=True", text)

    def test_nothing_to_do_without_fcitx_or_without_the_library(self):
        ctx = self.context()
        with patch.object(cli.shutil, "which", return_value=None):
            self.assertTrue(cli.fcitx_done(ctx))
        with patch.object(cli.shutil, "which", return_value="/usr/bin/fcitx5"):
            self.assertTrue(cli.fcitx_done(ctx))  # the checkout has no add-on library


class LauncherAndUninstallTests(CliCase):
    def desktop(self, text):
        path = self.home / ".local/share/applications/uuway.desktop"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_only_the_console_installers_old_menu_entry_is_removed(self):
        ctx = self.record_commands(self.context())
        mine = self.desktop(cli.DESKTOP_MARKER + "\n[Desktop Entry]\n")
        with patch("native_launcher_migration.retire_legacy_launchers", return_value={"changed": 0}):
            self.assertFalse(cli.launchers_done(ctx))
            self.quiet(cli.launchers_run, ctx)
        self.assertFalse(mine.exists())
        theirs = self.desktop("[Desktop Entry]\nName=mine\n")
        with patch("native_launcher_migration.retire_legacy_launchers", return_value={"changed": 0}):
            self.assertTrue(cli.launchers_done(ctx))
            self.quiet(cli.launchers_run, ctx)
        self.assertTrue(theirs.exists())

    def test_uninstall_removes_marked_files_only_and_keeps_the_wine_prefix(self):
        ctx = self.context(purge=True)
        units = self.home / ".config/systemd/user"
        units.mkdir(parents=True)
        (units / "uu-native-bridge.service").write_text(cli.SERVICE_MARKER + "\n[Service]\n")
        (units / "uu-native-text.service").write_text("[Service]\n# somebody else's unit\n")
        self.desktop(cli.DESKTOP_MARKER + "\n")
        ctx.config_dir.mkdir(mode=0o700, parents=True)
        (ctx.config_dir / "native-runtime.json").write_text("{}")
        self.make_prefix()
        record = self.record_commands(ctx)
        args = Namespace(dry_run=False, yes=True, prefix=self.prefix, purge=True)
        with patch.object(cli, "Context", return_value=record):
            self.quiet(cli.command_uninstall, args)
        self.assertFalse((units / "uu-native-bridge.service").exists())
        self.assertTrue((units / "uu-native-text.service").exists())
        self.assertFalse((self.home / ".local/share/applications/uuway.desktop").exists())
        self.assertFalse(ctx.config_dir.exists())
        self.assertTrue(self.prefix.is_dir())
        self.assertTrue(any("disable" in c for c in self.commands))


class StepSelectionTests(CliCase):
    def test_packaged_refresh_retires_source_menu_but_source_refresh_keeps_it(self):
        for packaged in (True, False):
            with self.subTest(packaged=packaged):
                seen = []
                with patch.object(cli, 'ROOT', self.root), \
                        patch.object(cli, 'DEB_ROOT', self.root if packaged else self.root / 'package'), \
                        patch.object(cli, 'check_environment'), patch.object(cli, 'unit_active', return_value=False), \
                        patch.object(cli, 'run_steps', side_effect=lambda ctx, only, force: seen.append(only)):
                    args = cli.build_parser().parse_args(['refresh', '--yes', '--prefix', str(self.prefix)])
                    self.quiet(cli.command_refresh, args)
                self.assertEqual('launchers' in seen[0], packaged)

    def test_unknown_steps_are_rejected_with_the_valid_names(self):
        with self.assertRaises(cli.SetupError) as raised:
            cli.parse_steps("wine,nonsense")
        self.assertIn("runtime", str(raised.exception))

    def test_completed_steps_are_skipped_unless_forced_and_failures_name_the_step(self):
        calls = []

        def make(name, done, error=None):
            def run(ctx):
                calls.append(name)
                if error:
                    raise cli.SetupError(error)
            return (name, name, lambda ctx: done, run)

        steps = (make("one", True), make("two", False), make("three", True))
        with patch.object(cli, "STEPS", steps):
            self.quiet(cli.run_steps, self.context(), set(), set())
            self.assertEqual(calls, ["two"])
            calls.clear()
            self.quiet(cli.run_steps, self.context(), {"three"}, {"three"})
            self.assertEqual(calls, ["three"])
        with patch.object(cli, "STEPS", (make("bad", False, "boom"),)):
            with self.assertRaises(cli.SetupError) as raised:
                self.quiet(cli.run_steps, self.context(), set(), set())
        self.assertIn("uuway setup --only bad", str(raised.exception))

    def test_system_flag_limits_setup_to_the_privileged_steps(self):
        seen = {}
        with patch.object(cli, "check_environment"), \
                patch.object(cli, "run_steps", side_effect=lambda ctx, only, force: seen.update(only=only)):
            args = cli.build_parser().parse_args(["setup", "--system", "--dry-run"])
            self.quiet(cli.command_setup, args)
        self.assertEqual(seen["only"], {"wine", "udev"})

    def run_wine(self, installed, pinned=False, sources=True):
        ctx = self.record_commands(self.context())
        pin = self.root / "uuway-wine"
        if pinned:
            pin.write_text("pin")
        # Not installed yet: the first look finds nothing, the check after the apt commands finds 11.0.
        versions = ["wine-11.0"] * 3 if installed else [None, "wine-11.0", "wine-11.0"]
        listing = [Path("/etc/apt/sources.list.d/winehq-noble.sources")] if sources else []
        with patch.object(cli, "WINE_PIN", pin), patch.object(cli, "wine_version", side_effect=versions), \
                patch.object(Path, "glob", return_value=iter(listing)):
            self.quiet(cli.wine_run, ctx)
        return " | ".join(" ".join(c) for c in self.commands)

    def test_a_fresh_wine_install_pins_the_series_before_apt_can_pick_another(self):
        commands = self.run_wine(installed=False)
        self.assertIn("winehq-stable=11.0.*", commands)
        self.assertLess(commands.index("uuway-wine"), commands.index("apt-get install"))
        self.assertIn("config/uuway-wine.preferences", commands)

    def test_an_existing_winehq_source_is_never_overwritten(self):
        self.assertNotIn("winehq-noble.sources", self.run_wine(installed=False, sources=True))
        self.commands.clear()
        self.assertIn("winehq-noble.sources", self.run_wine(installed=False, sources=False))

    def test_wine_that_is_already_11_0_only_gets_the_pin(self):
        commands = self.run_wine(installed=True)
        self.assertIn("uuway-wine", commands)
        self.assertNotIn("apt-get", commands)

    def test_the_wine_step_is_done_only_with_both_the_version_and_the_pin(self):
        ctx = self.context()
        pin = self.root / "uuway-wine"
        with patch.object(cli, "WINE_PIN", pin), patch.object(cli, "wine_version", return_value="wine-11.0"):
            self.assertFalse(cli.wine_done(ctx))
            pin.write_text("pin")
            self.assertTrue(cli.wine_done(ctx))
        with patch.object(cli, "WINE_PIN", pin), patch.object(cli, "wine_version", return_value="wine-10.0"):
            self.assertFalse(cli.wine_done(ctx))


class ElevationTests(CliCase):
    """A shell reached through UU's terminal or SSH has DISPLAY set, but nobody watches that desktop."""

    BRIDGE_CGROUP = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/uu-native-bridge.service\n"
    DESKTOP_CGROUP = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-terminal.scope\n"

    def elevate(self, *, environment, cgroup, pkexec=True, terminal=False, **options):
        cgroup_file = self.root / "cgroup"
        cgroup_file.write_text(cgroup)
        which = "/usr/bin/pkexec" if pkexec else None
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(cli, "CGROUP_FILE", cgroup_file), \
                patch.object(cli.shutil, "which", return_value=which), \
                patch.object(cli.sys.stdin, "isatty", return_value=terminal):
            return self.context().elevate(["true"], **options)

    def test_a_local_desktop_session_still_gets_the_polkit_dialog(self):
        self.assertEqual(self.elevate(environment={"WAYLAND_DISPLAY": "wayland-0"}, cgroup=self.DESKTOP_CGROUP),
                         ["pkexec", "true"])

    def test_the_uu_terminal_asks_for_sudo_in_the_terminal_instead(self):
        environment = {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}
        self.assertEqual(self.elevate(environment=environment, cgroup=self.BRIDGE_CGROUP), ["sudo", "true"])

    def test_ssh_asks_for_sudo_even_with_a_forwarded_display(self):
        for variable in ("SSH_CONNECTION", "SSH_TTY"):
            environment = {"DISPLAY": "localhost:10.0", variable: "x"}
            self.assertEqual(self.elevate(environment=environment, cgroup=self.DESKTOP_CGROUP), ["sudo", "true"], variable)

    def test_no_desktop_or_no_pkexec_means_sudo(self):
        self.assertEqual(self.elevate(environment={}, cgroup=self.DESKTOP_CGROUP), ["sudo", "true"])
        self.assertEqual(self.elevate(environment={"DISPLAY": ":0"}, cgroup=self.DESKTOP_CGROUP, pkexec=False),
                         ["sudo", "true"])

    def test_an_unreadable_cgroup_file_does_not_hide_the_dialog(self):
        with patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True), \
                patch.object(cli, "CGROUP_FILE", self.root / "missing"), \
                patch.object(cli.shutil, "which", return_value="/usr/bin/pkexec"):
            self.assertEqual(self.context().elevate(["true"]), ["pkexec", "true"])

    def test_terminal_first_prefers_sudo_whenever_someone_can_type(self):
        environment = {"WAYLAND_DISPLAY": "wayland-0"}
        self.assertEqual(self.elevate(environment=environment, cgroup=self.DESKTOP_CGROUP, terminal=True,
                                      terminal_first=True), ["sudo", "true"])
        self.assertEqual(self.elevate(environment=environment, cgroup=self.DESKTOP_CGROUP, terminal=False,
                                      terminal_first=True), ["pkexec", "true"])

    def test_the_prompts_say_which_kind_of_authorization_to_expect(self):
        with patch.object(cli.Context, "elevate", return_value=["pkexec"]):
            self.assertIn("授权窗口", self.context().elevation_hint())
        with patch.object(cli.Context, "elevate", return_value=["sudo"]):
            self.assertIn("sudo 密码", self.context().elevation_hint())


ORIGINAL_FOLLOW = cli.follow_progress  # the real one; UpdateCase replaces it for the other tests


class FakeUpdateFetcher:
    """GitHub, as far as `uuway update` can tell."""

    def __init__(self):
        self.calls = []
        self.tag = "v1.2.1"
        self.etag = '"e1"'
        self.not_modified = False
        self.failure = None

    def get(self, url, limit, headers=None):
        self.calls.append(("get", url, dict(headers or {})))
        if self.failure:
            raise self.failure
        if self.not_modified and (headers or {}).get("If-None-Match"):
            raise cli.uuway_update.NotModified()
        body = json.dumps(dict(tag_name=self.tag, draft=False, prerelease=False,
                               published_at="2026-10-08T01:02:03Z")).encode()
        return body, {"etag": self.etag}

    def save(self, url, path, limit):
        self.calls.append(("save", url, limit))
        name = url.rsplit("/", 1)[1]
        Path(path).write_bytes(b"deb" * 1000 if name.endswith(".deb") else b"x" * 10)


def dotted(version):
    return tuple(int(part) for part in version.split("."))


class UpdateCase(CliCase):
    HEALTHY = dict(units={name: dict(active=True, restarts=0, entered=1) for name in ("uu-native-display",
                   "uu-native-text", "uu-native-bridge")}, processes={"GameViewerServer.exe": 1}, connected=True)

    def setUp(self):
        super().setUp()
        self.state = self.root / "varstate"
        self.state.mkdir()
        self.cgroup = self.root / "cgroup"
        self.cgroup.write_text("0::/user.slice/user-1000.slice/app.slice/app-gnome-terminal.scope\n")
        self.fetcher = FakeUpdateFetcher()
        self.installed = "1.2.0"
        self.update_running = False
        self.resume_running = False  # the boot-time unit is a oneshot: while it runs it is "activating"
        self.audit = ""
        self.recovery_layer = True
        self.progress = []
        self.baseline = dict(self.HEALTHY)

        def check_release(stage, tag, installed, *, run):
            self.checked = (tag, installed)
            deb = Path(stage) / f"uuway_{tag[1:]}_amd64.deb"
            return cli.update_checks.Release(tag[1:], deb, "0" * 64)
        self.checked = None
        self.early_checks = []

        def check_checksums(stage, tag):
            self.early_checks.append((Path(stage), tag))
            return "0" * 64
        for override in (patch.object(cli, "DEB_ROOT", self.checkout.resolve()),
                         patch.object(cli, "UPDATE_STATE", self.state),
                         patch.object(cli, "CGROUP_FILE", self.cgroup),
                         patch.object(cli, "new_fetcher", lambda: self.fetcher),
                         patch.object(cli, "health_baseline", lambda ctx: self.baseline),
                         patch.object(cli, "timed_state", lambda ctx: (self.recovery_layer, cli.getpass.getuser())),
                         patch.object(cli, "follow_progress", self.follow),
                         patch.object(cli.update_checks, "check_checksums", check_checksums),
                         patch.object(cli.update_checks, "check_release", check_release)):
            override.start()
            self.addCleanup(override.stop)

    def follow(self, ctx, started, already, limit=0):
        self.followed = True
        return 0

    def api_calls(self):
        """The requests to GitHub's API (as opposed to the downloads)."""
        return [call for call in self.fetcher.calls if call[1] == cli.uuway_update.API_URL]

    def update_context(self, action="install", yes=True, dry_run=False, reboot=False, source=None, tag=None):
        args = Namespace(dry_run=dry_run, yes=yes, prefix=self.prefix, action=action, reboot_if_stuck=reboot,
                         source=source, tag=tag)
        ctx = cli.Context(args)
        self.commands = []
        self.followed = False

        def run(argv, **kwargs):
            self.commands.append([str(part) for part in argv])
            return completed(argv)

        def probe(argv, **kwargs):
            argv = [str(part) for part in argv]
            if argv[:2] == ["dpkg-query", "-W"]:
                return completed(argv, 0 if self.installed else 1, self.installed or "")
            if argv[:2] == ["dpkg", "--compare-versions"]:
                return completed(argv, 0 if dotted(argv[2]) > dotted(argv[4]) else 1)
            if argv[:2] == ["systemctl", "is-active"] and "--user" not in argv:
                state = {"uuway-update": "active" if self.update_running else "inactive",
                         "uuway-update-resume": "activating" if self.resume_running else "inactive"}[argv[-1]]
                return completed(argv, 0 if state == "active" else 3, state + "\n")
            if argv[:2] == ["dpkg", "--audit"]:
                return completed(argv, 0, self.audit)
            return completed(argv, 127)
        ctx.run, ctx.probe = run, probe
        return ctx, args

    def install(self, **options):
        ctx, args = self.update_context(**options)
        with patch.object(cli.sys.stdin, "isatty", return_value=True):
            return ctx, args, self.quiet(cli.update_install, ctx, args)

    def launched(self):
        return [argv for argv in self.commands if "systemd-run" in argv]


class UpdateCheckTests(UpdateCase):
    def test_gui_check_json_is_structured_and_does_not_start_an_install(self):
        ctx,args=self.update_context('check');args.json=True
        code,output=self.quiet(cli.update_check,ctx,args)
        value=json.loads(output)
        self.assertTrue(value['available'])
        self.assertTrue(value['installable'])
        self.assertEqual(value['latest'],'1.2.1')
        self.assertEqual(self.commands,[])

    def check(self):
        ctx, args = self.update_context("check")
        return ctx, self.quiet(cli.update_check, ctx, args)

    def test_a_newer_release_is_announced_with_how_to_install_it(self):
        ctx, (code, output) = self.check()
        self.assertEqual(code, 0)
        self.assertIn("有新版本", output)
        self.assertIn("1.2.1", output)
        self.assertIn("uuway update install", output)
        self.assertEqual(self.commands, [])  # a check never touches the system

    def test_the_answer_is_cached_privately_for_doctor(self):
        ctx, _ = self.check()
        cache = ctx.state_dir / "update.json"
        self.assertEqual(json.loads(cache.read_text())["version"], "1.2.1")
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)

    def test_no_newer_release_is_said_plainly(self):
        self.fetcher.tag = "v1.2.0"
        _, (code, output) = self.check()
        self.assertIn("已是最新", output)
        self.assertNotIn("uuway update install", output)

    def test_a_second_check_sends_the_etag_and_a_304_uses_the_cache(self):
        self.check()
        self.fetcher.not_modified = True
        _, (code, output) = self.check()
        self.assertEqual(self.api_calls()[-1][2], {"If-None-Match": '"e1"'})
        self.assertIn("有新版本", output)

    def test_a_source_checkout_cannot_compare_versions_and_says_so(self):
        self.installed = None
        _, (code, output) = self.check()
        self.assertEqual(code, 0)
        self.assertIn("1.2.1", output)
        self.assertIn("不是用安装包装的", output)

    def test_a_network_failure_is_an_error_with_a_reason(self):
        self.fetcher.failure = cli.uuway_update.FetchError("无法连接 GitHub：no route")
        ctx, args = self.update_context("check")
        with self.assertRaisesRegex(cli.SetupError, "无法连接 GitHub"):
            self.quiet(cli.update_check, ctx, args)

    def test_a_tampered_cache_is_ignored(self):
        ctx, _ = self.check()
        (ctx.state_dir / "update.json").write_text(json.dumps(dict(tag="v9.9.9", version="1.0.0", etag='"x"')))
        self.check()
        self.assertEqual(self.api_calls()[-1][2], {})  # no etag was trusted from it


class UpdateRefusalTests(UpdateCase):
    def refused(self, pattern, **options):
        ctx, args = self.update_context(**options)
        with patch.object(cli.sys.stdin, "isatty", return_value=True), \
                self.assertRaisesRegex(cli.SetupError, pattern):
            self.quiet(cli.update_install, ctx, args)
        self.assertEqual(self.launched(), [])
        return ctx

    def test_only_a_package_install_can_be_updated(self):
        self.installed = None
        self.refused("只支持用安装包")

    def test_a_development_build_cannot(self):
        self.installed = "0.0.0~git1234567"
        self.refused("开发构建")

    def test_a_running_update_blocks_another(self):
        self.update_running = True
        self.refused("已经有一个更新在进行")

    def test_an_update_that_is_being_resumed_after_a_reboot_blocks_another(self):
        self.resume_running = True
        self.refused("已经有一个更新在进行")

    def test_a_broken_dpkg_state_blocks_it(self):
        self.audit = "The following packages are only half configured:\n uuway\n"
        self.refused("dpkg 处于未完成状态")

    def test_an_unhealthy_machine_is_not_updated(self):
        self.baseline = dict(units={**self.HEALTHY["units"], "uu-native-bridge": dict(active=False, restarts=0, entered=1)},
                             processes={"GameViewerServer.exe": 0}, connected=None)
        ctx = self.refused("不太正常")
        self.assertFalse((ctx.state_dir / "update").exists())  # not even downloaded

    def test_nothing_newer_means_nothing_to_do(self):
        self.fetcher.tag = "v1.2.0"
        ctx, args, (code, output) = self.install()
        self.assertEqual(code, 0)
        self.assertIn("已是最新", output)
        self.assertEqual(self.launched(), [])
        self.assertFalse([call for call in self.fetcher.calls if call[0] == "save"])

    def test_a_release_that_fails_verification_is_not_installed(self):
        def refuse(*args, **kwargs):
            raise cli.update_checks.VerifyError("哈希校验失败。")
        with patch.object(cli.update_checks, "check_release", refuse):
            self.refused("哈希校验失败")

    def test_bad_checksums_are_found_before_the_deb_is_downloaded(self):
        def refuse(*args, **kwargs):
            raise cli.update_checks.VerifyError("SHA256SUMS 里没有这个版本的校验和。")
        with patch.object(cli.update_checks, "check_checksums", refuse):
            self.refused("校验和")
        saved = [call[1].rsplit("/", 1)[1] for call in self.fetcher.calls if call[0] == "save"]
        self.assertEqual(saved, ["SHA256SUMS"])  # the megabytes were never fetched

    def test_the_early_check_gets_the_checksums_and_the_tag(self):
        self.install()
        ((stage, tag),) = self.early_checks
        self.assertEqual(tag, "v1.2.1")
        self.assertEqual(stage.name, "1.2.1")

    def test_a_download_failure_is_reported(self):
        def broken(url, path, limit):
            raise cli.uuway_update.FetchError("下载超时，已放弃。")
        self.fetcher.save = broken
        self.refused("下载超时")

    def test_too_little_disk_space_stops_it_before_anything_is_installed(self):
        usage = lambda path: Namespace(free=1 << 20)
        with patch.object(cli.shutil, "disk_usage", usage):
            self.refused("磁盘空间不够")

    def test_a_directory_that_does_not_exist_yet_is_checked_on_the_disk_it_will_be_made_on(self):
        asked = []

        def usage(path):
            asked.append(Path(path))
            return Namespace(free=10 << 30)
        deb = self.root / "x.deb"
        deb.write_bytes(b"x" * 100)
        with patch.object(cli.shutil, "disk_usage", usage):
            cli.check_disk_space(deb, self.root / "not/made/yet")
        self.assertIn(self.root, asked)
        self.assertTrue(all(path.exists() for path in asked))

    def test_the_disk_that_holds_the_downloads_is_checked_too(self):
        def usage(path):
            return Namespace(free=(10 << 30) if str(path) in ("/var/lib", "/usr") else 1 << 20)
        with patch.object(cli.shutil, "disk_usage", usage):
            self.refused("磁盘空间不够")

    def test_a_non_interactive_run_without_yes_cannot_confirm(self):
        ctx, args = self.update_context(yes=False)
        with patch.object(cli.sys.stdin, "isatty", return_value=False), \
                self.assertRaisesRegex(cli.SetupError, "不是交互终端"):
            self.quiet(cli.update_install, ctx, args)
        self.assertEqual(self.launched(), [])

    def test_declining_the_confirmation_changes_nothing(self):
        ctx, args = self.update_context(yes=False)
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value=""):
            code, output = self.quiet(cli.update_install, ctx, args)
        self.assertEqual(code, 1)
        self.assertIn("已取消", output)
        self.assertEqual(self.launched(), [])


class HealthBaselineTests(CliCase):
    def test_the_baseline_is_read_as_the_user_through_the_probe(self):
        asked = []
        blocks = "\n".join(f"NRestarts=1\nId={name}.service\nActiveState=active\nSubState=running\n"
                           "ActiveEnterTimestampMonotonic=77\n" for name in cli.SERVICES)

        def probe(argv, **kwargs):
            asked.append([str(part) for part in argv])
            return completed(argv, 0, blocks)
        ctx = self.context()
        ctx.probe = probe
        with patch.object(cli.update_checks, "count_uu_processes", return_value={"GameViewerServer.exe": 1}) as count, \
                patch.object(cli.update_checks, "uu_connected", return_value=True) as connected:
            baseline = cli.health_baseline(ctx)
        self.assertEqual(asked[0][:3], ["systemctl", "--user", "show"])  # no runuser: this already is the user
        self.assertNotIn("runuser", asked[0])
        self.assertEqual(count.call_args[0][1], os.getuid())
        self.assertEqual(connected.call_args[0][1], os.getuid())
        self.assertTrue(all(unit["active"] and unit["restarts"] == 1 for unit in baseline["units"].values()))
        self.assertEqual((baseline["processes"], baseline["connected"]), ({"GameViewerServer.exe": 1}, True))
        self.assertEqual(cli.update_checks.baseline_problems(baseline), [])


class UpdateRecoveryLayerTests(UpdateCase):
    """An update that crashes the desktop must not strand a machine at its login screen."""

    def test_without_the_recovery_layer_yes_alone_refuses(self):
        self.recovery_layer = False
        ctx, args = self.update_context(yes=True)
        with patch.object(cli.sys.stdin, "isatty", return_value=True), \
                self.assertRaisesRegex(cli.SetupError, "--yes 或非交互运行不会替你冒这个险"):
            self.quiet(cli.update_install, ctx, args)
        self.assertEqual(self.launched(), [])

    def test_interactively_it_can_be_accepted_knowingly(self):
        self.recovery_layer = False
        ctx, args = self.update_context(yes=False)
        answers = iter(["y", "y"])  # carry on despite the warning; then confirm the update
        with patch.object(cli.sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", side_effect=lambda prompt: next(answers)):
            code, output = self.quiet(cli.update_install, ctx, args)
        self.assertIn("uuway autologin on", output)
        self.assertEqual(len(self.launched()), 1)

    def test_interactively_it_can_be_declined(self):
        self.recovery_layer = False
        ctx, args = self.update_context(yes=False)
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value="n"), \
                self.assertRaisesRegex(cli.SetupError, "已取消"):
            self.quiet(cli.update_install, ctx, args)
        self.assertEqual(self.launched(), [])


class UpdateLaunchTests(UpdateCase):
    def test_the_one_root_command_is_a_detached_unit_with_everything_it_needs(self):
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}), \
                patch.object(cli.shutil, "which", return_value="/usr/bin/pkexec"):
            ctx, args, (code, output) = self.install(reboot=True)
        self.assertEqual(code, 0)
        (command,) = self.launched()
        self.assertEqual(command[0], "sudo")  # someone can type here, so ask here, not on an empty desktop
        unit = command[command.index("systemd-run"):]
        self.assertIn("--unit=uuway-update", unit)
        self.assertIn("--collect", unit)
        self.assertNotIn("--pipe", unit)   # these would tie it to this terminal
        self.assertNotIn("--wait", unit)
        self.assertEqual(unit[unit.index("-E") + 1], "DEBIAN_FRONTEND=noninteractive")
        self.assertIn("NEEDRESTART_SUSPEND=1", unit)
        self.assertIn("--what=shutdown:sleep", unit)
        self.assertIn("--mode=block", unit)
        script = unit[unit.index("/usr/bin/python3"):]
        self.assertEqual(script[:4], ["/usr/bin/python3", "-I", "-B", str(self.checkout.resolve() / "scripts/uuway_update_apply.py")])
        arguments = dict(zip(script[5::2], script[6::2]))
        self.assertEqual(script[4], "apply")
        self.assertEqual(arguments["--tag"], "v1.2.1")
        self.assertEqual(arguments["--user"], cli.getpass.getuser())
        self.assertEqual(arguments["--uid"], str(os.getuid()))
        self.assertEqual(arguments["--home"], str(self.home))
        self.assertEqual(arguments["--prefix"], str(self.prefix))
        self.assertIn("--reboot-if-stuck", script)
        stage = Path(arguments["--stage"])
        self.assertEqual(stage, ctx.state_dir / "update/1.2.1")
        self.assertEqual(sorted(path.name for path in stage.iterdir()),
                         ["SHA256SUMS", "baseline.json", "uuway_1.2.1_amd64.deb"])
        baseline = json.loads((stage / "baseline.json").read_text())
        self.assertEqual(baseline["processes"], {"GameViewerServer.exe": 1})
        self.assertEqual(Path(arguments["--baseline"]), stage / "baseline.json")
        self.assertEqual((stage / "baseline.json").stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.followed)

    def test_the_cli_itself_never_installs_anything(self):
        self.install()
        for argv in self.commands:
            self.assertNotIn(argv[-1][-4:], (".deb",), argv)
            self.assertNotIn("apt-get", argv)
            self.assertNotIn("dpkg", argv)

    def test_no_reboot_flag_by_default(self):
        self.install()
        self.assertNotIn("--reboot-if-stuck", self.launched()[0])

    def test_the_user_files_are_snapshotted_before_the_launch(self):
        self.stage_snapshot_inputs()
        ctx, args, _ = self.install()
        backups = sorted(ctx.state_dir.glob("backup-*"))
        self.assertEqual(len(backups), 1)
        script = self.launched()[0]
        self.assertEqual(script[script.index("--snapshot") + 1], str(backups[0]))
        self.assertTrue((backups[0] / "home/.config/uurb/native-runtime.json").is_file())
        self.assertTrue((backups[0] / "home/.config/uurb/extra-setting.json").is_file())  # the whole config directory

    def stage_snapshot_inputs(self):
        config = self.home / ".config/uurb"
        config.mkdir(parents=True)
        (config / "native-runtime.json").write_text("{}")
        (config / "extra-setting.json").write_text("{}")

    def test_the_prompt_says_how_the_connection_will_behave(self):
        _, _, (code, output) = self.install()
        self.assertIn("远程连接会断开", output)
        self.assertNotIn("UU 的远程终端里", output)

    def test_inside_the_uu_terminal_the_warning_is_stronger(self):
        self.cgroup.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/uu-native-bridge.service\n")
        _, _, (code, output) = self.install()
        self.assertIn("你正在 UU 的远程终端里", output)
        self.assertIn("uuway update status", output)

    def test_the_reboot_flag_is_announced(self):
        _, _, (code, output) = self.install(reboot=True)
        self.assertIn("重启这台机器一次", output)

    def test_dry_run_downloads_and_changes_nothing_and_shows_the_command(self):
        ctx, args = self.update_context(dry_run=True)
        before = sorted(path for path in self.home.rglob("*"))
        with patch.object(cli.sys.stdin, "isatty", return_value=True):
            code, output = self.quiet(cli.update_install, ctx, args)
        self.assertEqual(code, 0)
        self.assertIn("systemd-run", output)
        self.assertIn("uuway_update_apply.py", output)
        self.assertEqual(self.commands, [])
        self.assertEqual(sorted(self.home.rglob("*")), before)
        self.assertFalse([call for call in self.fetcher.calls if call[0] == "save"])

    def test_the_progress_is_followed_after_the_launch(self):
        self.install()
        self.assertTrue(self.followed)


class UpdateSourceTests(UpdateCase):
    """The rehearsal path: a release laid out by hand, no network, the same checks."""

    def staged(self, name="rehearsal"):
        stage = self.root / name
        stage.mkdir()
        for file in ("SHA256SUMS", "uuway_1.2.1_amd64.deb"):
            (stage / file).write_bytes(b"x" * 10)
        return stage

    def install_from(self, stage, tag="v1.2.1", **options):
        ctx, args = self.update_context(source=stage, tag=tag, **options)
        with patch.object(cli.sys.stdin, "isatty", return_value=True):
            return ctx, args, self.quiet(cli.update_install, ctx, args)

    def test_it_never_goes_online_and_stages_the_given_directory(self):
        stage = self.staged()
        ctx, args, (code, output) = self.install_from(stage)
        self.assertEqual(code, 0)
        self.assertEqual(self.fetcher.calls, [])
        (command,) = self.launched()
        script = command[command.index("/usr/bin/python3"):]
        self.assertEqual(script[script.index("--stage") + 1], str(stage.resolve()))
        self.assertEqual(script[script.index("--tag") + 1], "v1.2.1")
        self.assertTrue((stage / "baseline.json").is_file())
        self.assertIn("已读取并校验", output)

    def test_it_is_checked_exactly_like_a_download(self):
        def refuse(*args, **kwargs):
            raise cli.update_checks.VerifyError("哈希校验失败。")
        stage = self.staged()
        ctx, args = self.update_context(source=stage, tag="v1.2.1")
        with patch.object(cli.update_checks, "check_release", refuse), \
                patch.object(cli.sys.stdin, "isatty", return_value=True), \
                self.assertRaisesRegex(cli.SetupError, "哈希校验失败"):
            self.quiet(cli.update_install, ctx, args)
        self.assertEqual(self.launched(), [])

    def test_the_tag_is_given_to_the_check_with_the_installed_version(self):
        self.install_from(self.staged())
        self.assertEqual(self.checked, ("v1.2.1", "1.2.0"))

    def test_a_tag_is_required_and_must_be_plain(self):
        for tag in (None, "", "v1.2", "1.2.1", "v1.2.1/../x"):
            ctx, args = self.update_context(source=self.staged(f"s{abs(hash(tag))}"), tag=tag)
            with self.assertRaisesRegex(cli.SetupError, "--tag"):
                self.quiet(cli.update_install, ctx, args)

    def test_a_version_that_is_not_newer_is_refused(self):
        ctx, args = self.update_context(source=self.staged(), tag="v1.2.0")
        with self.assertRaisesRegex(cli.SetupError, "不比已安装的"):
            self.quiet(cli.update_install, ctx, args)

    def test_the_directory_it_was_given_is_never_deleted(self):
        ctx, args = self.update_context(source=self.root / "x", tag="v1.2.1")
        inside = ctx.state_dir / "update" / "1.2.1"  # where downloads normally go
        inside.mkdir(parents=True)
        for file in ("SHA256SUMS", "uuway_1.2.1_amd64.deb"):
            (inside / file).write_bytes(b"x")
        ctx, args, _ = self.install_from(inside)
        self.assertTrue((inside / "uuway_1.2.1_amd64.deb").is_file())

    def test_a_dry_run_with_a_source_does_not_go_online_either(self):
        ctx, args, (code, output) = self.install_from(self.staged(), dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.fetcher.calls, [])
        self.assertEqual(self.launched(), [])

    def test_the_option_is_not_advertised(self):
        update = cli.build_parser()._subparsers._group_actions[0].choices["update"]
        self.assertNotIn("--source", update.format_help())
        self.assertNotIn("--tag", update.format_help())
        self.assertIsNotNone(cli.build_parser().parse_args(["update", "install", "--source", "/x", "--tag", "v1.2.1"]).source)


class UpdateFollowTests(UpdateCase):
    def test_the_progress_lines_are_shown_until_the_result_arrives(self):
        ctx, _ = self.update_context()
        (self.state / "progress.log").write_text("10:00:00 older update\n")
        started = time.time()
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 1:
                (self.state / "progress.log").write_text("10:00:00 older update\n10:05:00 开始更新\n10:06:00 结果\n")
                cli.write_atomic(self.state / "last-result.json", json.dumps(
                    dict(outcome="success", message="好了", finished=started + 5, from_version="1.2.0",
                         to_version="1.2.1")).encode(), 0o644)
        with patch.object(cli.time, "sleep", sleep):
            code, output = self.quiet(ORIGINAL_FOLLOW, ctx, started, 1)
        self.assertEqual(code, 0)
        self.assertIn("开始更新", output)
        self.assertNotIn("older update", output)  # only what this update wrote
        self.assertIn("更新成功", output)

    def test_an_update_that_died_without_a_result_is_noticed_not_waited_for(self):
        ctx, _ = self.update_context()
        started = time.time()
        sleeps = []
        with patch.object(cli.time, "sleep", sleeps.append):
            code, output = self.quiet(ORIGINAL_FOLLOW, ctx, started, 0)
        self.assertEqual(code, 1)
        self.assertIn("没有留下结果", output)
        self.assertIn("journalctl -u uuway-update", output)
        self.assertLess(len(sleeps), 10)  # it did not sit there for 25 minutes

    def test_a_running_update_is_waited_for(self):
        ctx, _ = self.update_context()
        self.update_running = True
        started = time.time()
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 6:  # long enough that an idle check would have given up
                cli.write_atomic(self.state / "last-result.json", json.dumps(
                    dict(outcome="success", message="好了", finished=started + 1, from_version="1.2.0",
                         to_version="1.2.1")).encode(), 0o644)
        with patch.object(cli.time, "sleep", sleep):
            code, output = self.quiet(ORIGINAL_FOLLOW, ctx, started, 0)
        self.assertEqual(code, 0)

    def test_the_resume_unit_counts_as_running_too(self):
        ctx, _ = self.update_context()
        self.resume_running = True
        started = time.time()
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 6:
                cli.write_atomic(self.state / "last-result.json", json.dumps(
                    dict(outcome="success", message="好了", finished=started + 1, from_version="1.2.0",
                         to_version="1.2.1")).encode(), 0o644)
        with patch.object(cli.time, "sleep", sleep):
            self.assertEqual(self.quiet(ORIGINAL_FOLLOW, ctx, started, 0)[0], 0)

    def test_giving_up_waiting_is_not_success(self):
        ctx, _ = self.update_context()
        self.update_running = True  # alive, but it never produces a result
        clock = iter(range(0, 10 ** 6, 400))
        with patch.object(cli.time, "sleep", lambda seconds: None), patch.object(cli.time, "monotonic", lambda: next(clock)):
            code, output = self.quiet(ORIGINAL_FOLLOW, ctx, time.time(), 0, limit=1000)
        self.assertEqual(code, 1)
        self.assertIn("uuway update status", output)

    def test_a_deliberate_rollback_that_worked_is_a_success(self):
        ctx, _ = self.update_context()
        started = time.time()
        cli.write_atomic(self.state / "last-result.json", json.dumps(
            dict(outcome="rolled_back", manual=True, message="按要求手动回滚；已回滚，机器恢复正常。", finished=started + 1,
                 from_version="1.2.0", to_version="1.2.1", rolled_back_to="1.2.0")).encode(), 0o644)
        code, output = self.quiet(ORIGINAL_FOLLOW, ctx, started, 0)
        self.assertEqual(code, 0)
        self.assertIn("已回滚到 1.2.0", output)
        self.assertNotIn("更新没有成功", output)

    def test_a_failed_update_makes_the_command_fail(self):
        ctx, _ = self.update_context()
        started = time.time()
        cli.write_atomic(self.state / "last-result.json", json.dumps(
            dict(outcome="rolled_back", message="不正常", finished=started + 1, from_version="1.2.0",
                 to_version="1.2.1")).encode(), 0o644)
        code, output = self.quiet(ORIGINAL_FOLLOW, ctx, started, 0)
        self.assertEqual(code, 1)
        self.assertIn("已自动回滚", output)


class UpdateStatusTests(UpdateCase):
    def test_gui_status_json_preserves_the_update_result(self):
        self.result(outcome='rolled_back')
        ctx,args=self.update_context('status');args.json=True
        code,output=self.quiet(cli.update_status,ctx,args)
        value=json.loads(output)
        self.assertFalse(value['busy'])
        self.assertEqual(value['result']['outcome'],'rolled_back')
    def result(self, **fields):
        data = dict(outcome="success", message="m", finished=time.time(), from_version="1.2.0", to_version="1.2.1")
        data.update(fields)
        cli.write_atomic(self.state / "last-result.json", json.dumps(data).encode(), 0o644)

    def status(self):
        ctx, args = self.update_context("status")
        return self.quiet(cli.update_status, ctx, args)

    def test_no_record(self):
        code, output = self.status()
        self.assertEqual(code, 0)
        self.assertIn("还没有更新记录", output)

    def test_each_outcome_is_described(self):
        for outcome, words, code in (("success", "更新成功", 0), ("rolled_back", "已自动回滚", 0),
                                     ("not_started", "系统没有任何改变", 0), ("rollback_failed", "回滚也没有成功", 1)):
            self.result(outcome=outcome)
            status, output = self.status()
            self.assertEqual(status, code, outcome)
            self.assertIn(words, output, outcome)

    def test_a_deliberate_rollback_is_not_described_as_a_failed_update(self):
        self.result(outcome="rolled_back", manual=True, rolled_back_to="1.2.0")
        code, output = self.status()
        self.assertEqual(code, 0)
        self.assertIn("已回滚到 1.2.0", output)
        self.assertNotIn("更新没有成功", output)

    def test_a_failed_rollback_points_at_the_recovery_steps(self):
        self.result(outcome="rollback_failed", recovery=str(self.state / "RECOVERY.txt"))
        _, output = self.status()
        self.assertIn("RECOVERY.txt", output)

    def test_a_running_update_shows_its_latest_progress(self):
        self.update_running = True
        (self.state / "progress.log").write_text("".join(f"10:00:{i:02d} 步骤 {i}\n" for i in range(30)))
        code, output = self.status()
        self.assertEqual(code, 0)
        self.assertIn("更新正在进行中", output)
        self.assertIn("步骤 29", output)
        self.assertNotIn("步骤 5\n", output)

    def test_a_garbled_result_file_is_not_a_crash(self):
        (self.state / "last-result.json").write_text("not json")
        code, output = self.status()
        self.assertEqual(code, 0)


class UpdateRollbackCommandTests(UpdateCase):
    def test_without_a_saved_old_version_there_is_nothing_to_go_back_to(self):
        ctx, args = self.update_context("rollback")
        with self.assertRaisesRegex(cli.SetupError, "没有可以回到的旧版本"):
            self.quiet(cli.update_rollback, ctx, args)

    def test_a_refused_attempt_after_the_update_does_not_take_the_way_back_away(self):
        previous = self.state / "previous/uuway_1.2.0_amd64.deb"
        previous.parent.mkdir()
        previous.write_bytes(b"x")
        cli.write_atomic(self.state / "last-result.json", json.dumps(
            dict(outcome="not_started", message="模拟安装失败", rollback_to=dict(deb=str(previous), version="1.2.0"),
                 from_version="1.2.1", to_version="1.2.2", finished=time.time())).encode(), 0o644)
        ctx, args = self.update_context("rollback")
        with patch.object(cli.sys.stdin, "isatty", return_value=True):
            code, output = self.quiet(cli.update_rollback, ctx, args)
        self.assertEqual(len(self.launched()), 1)
        self.assertIn("1.2.0", output)

    def test_a_result_that_names_no_way_back_is_refused(self):
        cli.write_atomic(self.state / "last-result.json", json.dumps(
            dict(outcome="not_started", rollback_to=None, finished=time.time())).encode(), 0o644)
        ctx, args = self.update_context("rollback")
        with self.assertRaisesRegex(cli.SetupError, "没有可以回到的旧版本"):
            self.quiet(cli.update_rollback, ctx, args)

    def test_rollback_launches_the_same_kind_of_detached_unit(self):
        previous = self.state / "previous/uuway_1.2.0_amd64.deb"
        previous.parent.mkdir()
        previous.write_bytes(b"x")
        cli.write_atomic(self.state / "last-result.json", json.dumps(
            dict(outcome="success", rollback_to=dict(deb=str(previous), version="1.2.0"), from_version="1.2.0",
                 to_version="1.2.1", finished=time.time())).encode(), 0o644)
        ctx, args = self.update_context("rollback")
        with patch.object(cli.sys.stdin, "isatty", return_value=True):
            code, output = self.quiet(cli.update_rollback, ctx, args)
        (command,) = self.launched()
        self.assertEqual(command[-1], "rollback")
        self.assertIn("--unit=uuway-update", command)
        self.assertIn("1.2.0", output)


class UpdateDoctorTests(UpdateCase):
    def doctor(self):
        args = Namespace(prefix=self.prefix, json=True)
        with patch.object(cli.Context, "probe", lambda self_, argv, **kw: self.update_context()[0].probe(argv)):
            _, output = self.quiet(cli.command_doctor, args)
        return {item["name"]: item for item in json.loads(output)}

    def test_a_cached_newer_release_is_mentioned_without_going_online(self):
        ctx, _ = self.update_context()
        cli.write_atomic(ctx.state_dir / "update.json", json.dumps(
            dict(tag="v1.2.1", version="1.2.1", checked_at=time.time(), etag='"e"')).encode(), 0o600)
        found = self.doctor()
        self.assertEqual(found["有新版本 1.2.1"]["level"], "warn")
        self.assertIn("uuway update", found["有新版本 1.2.1"]["detail"])
        self.assertEqual(self.fetcher.calls, [])  # doctor stays offline

    def test_a_cached_release_that_is_not_newer_is_not_mentioned(self):
        ctx, _ = self.update_context()
        cli.write_atomic(ctx.state_dir / "update.json", json.dumps(
            dict(tag="v1.2.0", version="1.2.0", checked_at=time.time())).encode(), 0o600)
        self.assertFalse([name for name in self.doctor() if name.startswith("有新版本")])

    def test_a_recent_rollback_is_mentioned_and_an_old_one_is_not(self):
        cli.write_atomic(self.state / "last-result.json", json.dumps(
            dict(outcome="rolled_back", message="不正常", finished=time.time() - 86400)).encode(), 0o644)
        self.assertIn("上次更新没有成功，已自动回滚", self.doctor())
        cli.write_atomic(self.state / "last-result.json", json.dumps(
            dict(outcome="rolled_back", message="不正常", finished=time.time() - 30 * 86400)).encode(), 0o644)
        self.assertNotIn("上次更新没有成功，已自动回滚", self.doctor())

    def test_a_rollback_somebody_asked_for_is_not_a_warning(self):
        cli.write_atomic(self.state / "last-result.json", json.dumps(
            dict(outcome="rolled_back", manual=True, message="按要求手动回滚", finished=time.time())).encode(), 0o644)
        self.assertFalse([name for name in self.doctor() if "上次更新" in name])

    def test_a_failed_rollback_points_at_the_recovery_file(self):
        cli.write_atomic(self.state / "last-result.json", json.dumps(
            dict(outcome="rollback_failed", finished=time.time(), recovery=str(self.state / "RECOVERY.txt"))).encode(), 0o644)
        found = self.doctor()
        self.assertIn("RECOVERY.txt", found["上次更新失败，自动回滚也没有成功"]["detail"])

    def test_a_successful_update_is_not_a_warning(self):
        cli.write_atomic(self.state / "last-result.json", json.dumps(
            dict(outcome="success", finished=time.time())).encode(), 0o644)
        self.assertFalse([name for name in self.doctor() if "上次更新" in name])


class InterruptTests(unittest.TestCase):
    def interrupted(self, *argv):
        def raise_interrupt(args):
            raise KeyboardInterrupt
        with patch.object(cli, "build_parser") as parser:
            parser.return_value.parse_args.return_value = Namespace(command=argv[0], handler=raise_interrupt)
            stderr = io.StringIO()
            with patch.object(cli.sys, "stderr", stderr):
                code = cli.main(list(argv))
        return code, stderr.getvalue()

    def test_interrupting_an_update_says_it_carries_on(self):
        code, message = self.interrupted("update")
        self.assertEqual(code, 130)
        self.assertIn("uuway update status", message)
        self.assertNotIn("uuway setup", message)

    def test_setup_keeps_its_own_message(self):
        code, message = self.interrupted("setup")
        self.assertEqual(code, 130)
        self.assertIn("uuway setup", message)


class UpdateParserTests(unittest.TestCase):
    def test_check_is_the_default_and_the_actions_exist(self):
        parser = cli.build_parser()
        self.assertEqual(parser.parse_args(["update"]).action, "check")
        for action in ("check", "install", "status", "rollback"):
            args = parser.parse_args(["update", action])
            self.assertTrue(callable(args.handler))
            self.assertEqual(args.action, action)
        self.assertTrue(parser.parse_args(["update", "install", "--reboot-if-stuck"]).reboot_if_stuck)
        self.assertFalse(parser.parse_args(["update", "install"]).reboot_if_stuck)

    def test_root_is_refused(self):
        with patch.object(cli.os, "geteuid", return_value=0), self.assertRaisesRegex(cli.SetupError, "普通用户"):
            cli.command_update(cli.build_parser().parse_args(["update"]))


class DryRunTests(CliCase):
    def test_dry_run_setup_changes_nothing_on_disk(self):
        args = cli.build_parser().parse_args(["setup", "--dry-run", "--yes"])
        rule = self.root / "rule"
        rule.write_text("# udev rule\n")
        # Whatever this machine has installed must not change which steps the plan walks through.
        with patch.object(cli, "check_environment"), patch.object(cli, "unit_active", return_value=False), \
                patch.object(cli, "SHIPPED_RULE", rule), patch.object(cli, "LEGACY_RULE", self.root / "none"), \
                patch.object(cli, "uinput_usable", return_value=False):
            result, output = self.quiet(cli.command_setup, args)
        self.assertEqual(result, 0)
        self.assertIn("$ ", output)
        self.assertEqual(list(self.home.rglob("*")), [])

    def test_every_command_has_a_handler_and_help(self):
        parser = cli.build_parser()
        for command in ("setup", "refresh", "doctor", "uninstall"):
            self.assertTrue(callable(parser.parse_args([command]).handler))
        for action in ("on", "off", "status"):
            self.assertTrue(callable(parser.parse_args(["autologin", action]).handler))


UBUNTU_GDM = """# GDM configuration storage

[daemon]
# Uncomment the line below to force the login screen to use Xorg
#WaylandEnable=false
WaylandEnable=true

#  AutomaticLoginEnable = true
#  AutomaticLogin = user1

[security]

[xdmcp]
"""


class AutologinTests(CliCase):
    def setUp(self):
        super().setUp()
        self.gdm = self.root / "custom.conf"
        self.gdm.write_text(UBUNTU_GDM)
        override = patch.object(cli, "GDM_CONFIGS", (self.gdm,))
        override.start()
        self.addCleanup(override.stop)
        override = patch.object(cli.getpass, "getuser", return_value="alice")
        override.start()
        self.addCleanup(override.stop)
        self.staged = []

    def recording_context(self, **options):
        """Commands are only recorded, but the staged config is read where root would read it."""
        ctx = self.context(**options)
        self.record_commands(ctx)
        record = ctx.run

        def run(argv, **kwargs):
            argv = [str(part) for part in argv]
            if "install" in argv and Path(argv[-2]).exists():  # a dry run stages nothing
                self.staged.append(Path(argv[-2]).read_text())
            return record(argv, **kwargs)
        ctx.run = run
        return ctx

    def test_reading_the_daemon_section_only_and_ignoring_comments(self):
        self.assertEqual(cli.gdm_autologin(UBUNTU_GDM), (False, None))
        self.assertEqual(cli.gdm_autologin("[daemon]\nAutomaticLoginEnable=True\nAutomaticLogin=alice\n"), (True, "alice"))
        self.assertEqual(cli.gdm_autologin("[daemon]\nAutomaticLoginEnable=false\nAutomaticLogin=alice\n"), (False, "alice"))
        self.assertEqual(cli.gdm_autologin("[security]\nAutomaticLoginEnable=true\nAutomaticLogin=bob\n"), (False, None))

    def test_enabling_adds_two_lines_and_keeps_everything_else(self):
        text = cli.gdm_set_autologin(UBUNTU_GDM, "alice", True)
        self.assertEqual(cli.gdm_autologin(text), (True, "alice"))
        self.assertEqual(text.replace("AutomaticLoginEnable=true\nAutomaticLogin=alice\n", "", 1), UBUNTU_GDM)
        self.assertEqual(cli.gdm_set_autologin(text, "alice", True), text)  # idempotent

    def test_enabling_replaces_existing_values_without_duplicates(self):
        text = cli.gdm_set_autologin("[daemon]\nAutomaticLoginEnable=False\nAutomaticLogin=bob\nWaylandEnable=true\n", "alice", True)
        self.assertEqual(text.count("AutomaticLogin"), 2)
        self.assertEqual(cli.gdm_autologin(text), (True, "alice"))
        self.assertIn("WaylandEnable=true", text)

    def test_a_config_without_a_daemon_section_gets_one(self):
        for original in ("", "[security]\nDisallowTCP=true", "[security]\nDisallowTCP=true\n"):
            text = cli.gdm_set_autologin(original, "alice", True)
            self.assertEqual(cli.gdm_autologin(text), (True, "alice"))
            self.assertIn("DisallowTCP=true" if original else "[daemon]", text)

    def test_disabling_removes_only_the_two_keys(self):
        enabled = cli.gdm_set_autologin(UBUNTU_GDM, "alice", True)
        self.assertEqual(cli.gdm_set_autologin(enabled, "alice", False), UBUNTU_GDM)
        other_section = "[daemon]\nAutomaticLogin=alice\n[custom]\nAutomaticLogin=keep\n"
        self.assertEqual(cli.gdm_set_autologin(other_section, "alice", False), "[daemon]\n[custom]\nAutomaticLogin=keep\n")

    def test_the_recovery_layer_is_independent_of_the_boot_login(self):
        both = cli.gdm_set_timed_login(cli.gdm_set_autologin(UBUNTU_GDM, "alice", True), "alice", True)
        self.assertEqual(cli.gdm_timed_login(both), (True, "alice"))
        self.assertIn(f"TimedLoginDelay={cli.RECOVERY_DELAY}", both)
        self.assertEqual(cli.gdm_set_timed_login(both, "alice", True), both)  # idempotent
        only_boot = cli.gdm_set_timed_login(both, "alice", False)
        self.assertEqual((cli.gdm_autologin(only_boot), cli.gdm_timed_login(only_boot)), ((True, "alice"), (False, None)))
        self.assertEqual(cli.gdm_set_autologin(only_boot, "alice", False), UBUNTU_GDM)
        self.assertEqual(cli.gdm_timed_login("[daemon]\n#TimedLoginEnable=true\nTimedLoginEnable=false\n"), (False, None))
        self.assertEqual(cli.gdm_set_timed_login("[daemon]", "alice", True).count("[daemon]\n"), 1)  # header without newline

    def test_on_backs_up_then_installs_the_edited_config_as_root(self):
        ctx = self.recording_context(only="autologin")
        _, output = self.quiet(cli.autologin_run, ctx)
        copy, install = self.commands
        self.assertEqual(copy[1:3], ["cp", "-p"])
        self.assertEqual(copy[3], str(self.gdm))
        self.assertIn(".before-uuway-", copy[4])
        self.assertEqual(install[1:8], ["install", "-m", "0644", "-o", "root", "-g", "root"])
        self.assertEqual(install[-1], str(self.gdm))
        self.assertEqual(cli.gdm_autologin(self.staged[0]), (True, "alice"))
        self.assertEqual(cli.gdm_timed_login(self.staged[0]), (True, "alice"))  # crash / logout recovery too
        self.assertFalse((ctx.state_dir / "gdm-custom.conf.new").exists())
        record = json.loads((ctx.state_dir / "autologin.json").read_text())
        self.assertEqual((record["user"], record["boot"], record["recovery"]), ("alice", True, True))
        self.assertIn("钥匙环", output)  # the cost is shown before anything is asked
        self.assertIn(f"{cli.RECOVERY_DELAY} 秒", output)

    def test_yes_alone_never_turns_it_on(self):
        ctx = self.recording_context()  # a plain `uuway setup --yes`
        _, output = self.quiet(cli.autologin_run, ctx)
        self.assertEqual(self.commands, [])
        self.assertFalse((ctx.state_dir / "autologin.json").exists())
        self.assertIn("uuway autologin on", output)

    def test_a_declined_prompt_is_remembered_and_not_asked_again(self):
        ctx = self.recording_context()
        ctx.assume_yes = False
        with patch.object(cli.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value="n"):
            self.quiet(cli.autologin_run, ctx)
        self.assertEqual(self.commands, [])
        self.assertTrue(cli.autologin_done(ctx))

    def test_another_accounts_autologin_is_never_changed(self):
        self.gdm.write_text("[daemon]\nAutomaticLoginEnable=true\nAutomaticLogin=bob\n")
        ctx = self.recording_context(only="autologin")
        self.quiet(cli.autologin_run, ctx)
        self.assertEqual(self.commands, [])
        self.assertIn("bob", self.gdm.read_text())

    def test_an_owner_set_boot_login_is_kept_and_only_the_recovery_layer_is_added_and_removed(self):
        owner = "[daemon]\nAutomaticLoginEnable=True\nAutomaticLogin=alice\n"  # set by the owner, not by uuway
        self.gdm.write_text(owner)
        ctx = self.recording_context(only="autologin")
        self.quiet(cli.autologin_run, ctx)
        staged = self.staged[0]
        self.assertEqual(cli.gdm_autologin(staged), (True, "alice"))
        self.assertEqual(cli.gdm_timed_login(staged), (True, "alice"))
        self.assertIn("AutomaticLoginEnable=True\n", staged)  # the owner's own line is not rewritten
        record = json.loads((ctx.state_dir / "autologin.json").read_text())
        self.assertEqual((record["boot"], record["recovery"]), (False, True))
        self.gdm.write_text(staged)
        self.quiet(cli.autologin_off, ctx)
        self.assertEqual(self.staged[1], owner)  # only the recovery lines went away

    def test_everything_already_enabled_is_a_no_op_and_off_will_not_touch_it(self):
        both = "[daemon]\nAutomaticLoginEnable=true\nAutomaticLogin=alice\nTimedLoginEnable=true\nTimedLogin=alice\nTimedLoginDelay=30\n"
        self.gdm.write_text(both)
        ctx = self.recording_context(only="autologin")
        _, output = self.quiet(cli.autologin_run, ctx)
        self.assertEqual(self.commands, [])
        self.assertIn("已经为 alice 开启", output)
        _, output = self.quiet(cli.autologin_off, ctx)
        self.assertEqual(self.commands, [])
        self.assertIn("不是 UUWay 开启的", output)
        self.assertEqual(self.gdm.read_text(), both)

    def test_a_timed_login_for_another_account_is_left_alone(self):
        self.gdm.write_text("[daemon]\nTimedLoginEnable=true\nTimedLogin=bob\nTimedLoginDelay=5\n")
        ctx = self.recording_context(only="autologin")
        self.quiet(cli.autologin_run, ctx)
        staged = self.staged[0]
        self.assertEqual(cli.gdm_autologin(staged), (True, "alice"))  # the boot login is still added
        self.assertEqual(cli.gdm_timed_login(staged), (True, "bob"))
        self.assertIn("TimedLoginDelay=5", staged)
        self.assertFalse(json.loads((ctx.state_dir / "autologin.json").read_text())["recovery"])

    def test_a_1_1_0_record_only_ever_covered_the_boot_login(self):
        recorded = "[daemon]\nAutomaticLoginEnable=true\nAutomaticLogin=alice\nTimedLoginEnable=true\nTimedLogin=alice\nTimedLoginDelay=7\n"
        self.gdm.write_text(recorded)
        ctx = self.recording_context()
        ctx.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (ctx.state_dir / "autologin.json").write_text(json.dumps(dict(version=1, user="alice", config=str(self.gdm), backup="x")))
        self.quiet(cli.autologin_off, ctx)
        self.assertEqual(cli.gdm_autologin(self.staged[0]), (False, None))
        self.assertEqual(cli.gdm_timed_login(self.staged[0]), (True, "alice"))  # set by the owner: kept

    def test_off_restores_what_on_changed_and_forgets_the_record(self):
        ctx = self.recording_context(only="autologin")
        self.quiet(cli.autologin_run, ctx)
        self.gdm.write_text(self.staged[0])  # what root's install would have written
        self.commands.clear()
        self.quiet(cli.autologin_off, ctx)
        self.assertEqual(self.staged[1], UBUNTU_GDM)
        self.assertFalse((ctx.state_dir / "autologin.json").exists())

    def test_dry_run_and_other_login_managers_change_nothing(self):
        ctx = self.recording_context(only="autologin")
        ctx.dry_run = True
        self.quiet(cli.autologin_run, ctx)
        self.assertFalse((ctx.state_dir / "autologin.json").exists())
        self.assertFalse((ctx.state_dir / "gdm-custom.conf.new").exists())
        with patch.object(cli, "GDM_CONFIGS", (self.root / "absent.conf",)):
            self.assertTrue(cli.autologin_done(ctx))  # no GDM: nothing to ask
            self.assertIsNone(cli.autologin_state(ctx))

    def test_the_step_is_optional_and_not_part_of_the_system_flag(self):
        self.assertEqual(cli.STEP_NAMES[-1], "autologin")
        self.assertNotIn("autologin", {"wine", "udev"})
        self.assertIn("{'wine', 'udev'}", (ROOT / "scripts/uuway_cli.py").read_text())

    def test_doctor_names_the_consequence_of_each_missing_layer(self):
        def results():
            args = Namespace(prefix=self.prefix, json=True)
            with patch.object(cli.Context, "probe", lambda self, argv, **kw: completed(argv, 127)):
                _, output = self.quiet(cli.command_doctor, args)
            return [item for item in json.loads(output) if item["name"].startswith(("开机自动登录", "崩溃/注销后自动重新登录"))]
        (item,) = results()
        self.assertEqual(item["level"], "warn")
        self.assertIn("UU 才会上线", item["detail"])
        boot = cli.gdm_set_autologin(UBUNTU_GDM, "alice", True)
        self.gdm.write_text(boot)
        (item,) = results()
        self.assertEqual((item["level"], item["name"]), ("warn", "崩溃/注销后自动重新登录"))
        self.assertIn("UU 不会自动回来", item["detail"])
        self.gdm.write_text(cli.gdm_set_timed_login(boot, "alice", True))
        (item,) = results()
        self.assertEqual(item["level"], "ok")
        self.gdm.write_text(cli.gdm_set_autologin(UBUNTU_GDM, "bob", True))
        self.assertEqual(results()[0]["level"], "warn")  # enabled, but for someone else

    def test_status_reports_both_layers(self):
        self.gdm.write_text(cli.gdm_set_autologin(UBUNTU_GDM, "alice", True))
        _, output = self.quiet(cli.command_autologin, Namespace(action="status", prefix=self.prefix, dry_run=False, yes=True))
        self.assertIn("开机自动登录：已为 alice 开启", output)
        self.assertIn("崩溃/注销后自动重新登录：未开启", output)

    def test_restarting_the_services_warns_about_the_desktop_and_can_be_declined(self):
        def start(answer):
            ctx = self.recording_context(restart_services=True)
            ctx.assume_yes = False
            ctx.restart_bridge = True
            self.active.add("uu-native-bridge")
            with patch.object(cli.time, "sleep"), patch("builtins.input", return_value=answer):
                _, output = self.quiet(cli.start_run, ctx)
            return output
        output = start("n")
        restarted = [c[-1] for c in self.commands if "restart" in c]
        self.assertEqual(restarted, ["uu-native-bridge.service"])  # the bridge only
        self.assertIn("可能让 GNOME Shell 崩溃", output)
        self.assertIn("没有开启崩溃/注销后自动重新登录", output)
        self.commands.clear()
        self.gdm.write_text(cli.gdm_set_timed_login(UBUNTU_GDM, "alice", True))
        output = start("y")
        self.assertIn("uu-native-text.service", " ".join(" ".join(c) for c in self.commands if "restart" in c))
        self.assertIn("十几秒内桌面和 UU 会回来", output)


class DoctorTests(CliCase):
    def test_a_bare_home_reports_failures_and_exits_nonzero(self):
        args = Namespace(prefix=self.prefix, json=True)
        with patch.object(cli.Context, "probe", lambda self, argv, **kw: completed(argv, 127)):
            code, output = self.quiet(cli.command_doctor, args)
        self.assertEqual(code, 1)
        results = json.loads(output)
        failed = {item["name"] for item in results if item["level"] == "fail"}
        self.assertIn("服务配置", failed)
        self.assertIn("UU 已安装", failed)


if __name__ == "__main__":
    unittest.main()
