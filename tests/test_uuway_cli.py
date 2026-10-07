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
