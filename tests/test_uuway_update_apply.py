"""The root side of an update: install, prove the machine is back, and roll back when it is not."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("uuway_update_apply", ROOT / "scripts/uuway_update_apply.py")
apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply)

UID = os.getuid()
SERVER = "C:\\Program Files\\Netease\\GameViewer\\bin\\GameViewerServer.exe"
SERVICE = "C:\\Program Files\\Netease\\GameViewer\\GameViewerService.exe"
# A UU server socket (inode 555) that is established to a public address, as /proc/net/tcp prints it.
TCP = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
       "   0: 0100007F:0035 00000000:0000 0A 00000000:00000000 00:00000000 00000000   101        0 111 1\n"
       "   1: E301A8C0:9C40 7962BA2A:01BB 01 00000000:00000000 00:00000000 00000000  1000        0 555 1\n")


def completed(argv, code=0, out="", err=""):
    return subprocess.CompletedProcess(argv, code, stdout=out, stderr=err)


def make_proc(proc, *, server=True, socket_state="01", other_uid_only=False):
    shutil.rmtree(proc, ignore_errors=True)
    (proc / "net").mkdir(parents=True)
    (proc / "net/tcp").write_text(TCP.replace(" 01 00000000:00000000 00:00000000 00000000  1000",
                                              f" {socket_state} 00000000:00000000 00:00000000 00000000  1000"))
    (proc / "net/tcp6").write_text("  sl  local_address rem_address st\n")
    for pid, argv0 in ((100, SERVER), (101, SERVICE), (200, "/usr/bin/zsh")):
        if not server and pid in (100, 101):
            continue
        (proc / str(pid) / "fd").mkdir(parents=True)
        arguments = [argv0] + (["-c", "echo GameViewerServer.exe"] if pid == 200 else [])
        (proc / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in arguments) + b"\0")
        (proc / str(pid) / "stat").write_text(f"{pid} (process) S 1")
        if pid == 100:
            os.symlink("socket:[555]", proc / "100/fd/3")
            os.symlink("socket:[999]", proc / "100/fd/4")
            os.symlink("/dev/null", proc / "100/fd/5")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class HealthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.proc = Path(directory.name) / "proc"
        make_proc(self.proc)

    def test_only_the_executable_name_counts_as_a_uu_process(self):
        counts = apply.count_uu_processes(self.proc, UID)
        self.assertEqual(counts, {"GameViewerServer.exe": 1, "GameViewerService.exe": 1})  # the zsh is not UU

    def test_another_users_processes_do_not_count(self):
        self.assertEqual(apply.count_uu_processes(self.proc, UID + 1),
                         {"GameViewerServer.exe": 0, "GameViewerService.exe": 0})

    def test_an_established_connection_to_a_public_address_means_connected(self):
        self.assertIs(apply.uu_connected(self.proc, UID), True)

    def test_exited_leader_with_live_workers_is_counted_once_and_remains_connected(self):
        server = self.proc / '100'
        for tid in (102, 103):
            task = server / 'task' / str(tid)
            (task / 'fd').mkdir(parents=True)
            (task / 'stat').write_text(f'{tid} (worker) S 1')
            (task / 'cmdline').write_bytes(SERVER.encode() + b'\0')
            (task / 'fd/3').symlink_to('socket:[555]')
        (server / 'stat').write_text('100 (GameViewerServe) Z 1')
        (server / 'cmdline').write_bytes(b'')
        shutil.rmtree(server / 'fd')
        self.assertEqual(apply.count_uu_processes(self.proc, UID)['GameViewerServer.exe'], 1)
        self.assertIs(apply.uu_connected(self.proc, UID), True)
        self.assertEqual(apply.count_uu_processes(self.proc, UID + 1)['GameViewerServer.exe'], 0)
        for task in (server / 'task').iterdir():
            (task / 'stat').write_text(f'{task.name} (worker) Z 1')
        self.assertEqual(apply.count_uu_processes(self.proc, UID)['GameViewerServer.exe'], 0)
        self.assertIsNone(apply.uu_connected(self.proc, UID))

    def test_disappearing_thread_does_not_hide_a_surviving_worker(self):
        server = self.proc / '100'
        (server / 'stat').write_text('100 (GameViewerServe) Z 1')
        (server / 'cmdline').write_bytes(b'')
        (server / 'task/102').mkdir(parents=True)
        task = server / 'task/103'
        task.mkdir()
        (task / 'stat').write_text('103 (worker) S 1')
        (task / 'cmdline').write_bytes(SERVER.encode() + b'\0')
        self.assertEqual(apply.count_uu_processes(self.proc, UID)['GameViewerServer.exe'], 1)

    def test_closed_or_local_connections_do_not(self):
        make_proc(self.proc, socket_state="08")  # CLOSE_WAIT
        self.assertIs(apply.uu_connected(self.proc, UID), False)
        make_proc(self.proc)
        (self.proc / "net/tcp").write_text(TCP.replace("7962BA2A:01BB", "0100007F:01BB"))  # to 127.0.0.1
        self.assertIs(apply.uu_connected(self.proc, UID), False)

    def test_no_server_means_unknown(self):
        make_proc(self.proc, server=False)
        self.assertIsNone(apply.uu_connected(self.proc, UID))

    def test_loopback_addresses(self):
        for address in ("0100007F:1", "0200007F:1", "00000000000000000000000001000000:1",
                        "0000000000000000FFFF00000100007F:1"):
            self.assertTrue(apply._loopback(address), address)
        for address in ("7962BA2A:1", "E301A8C0:1", "00000000000000000000000000000000:1",
                        "20010DB8000000000000000000000001:1"):
            self.assertFalse(apply._loopback(address), address)

    def test_judge_is_relative_to_the_baseline(self):
        units = {name: dict(active=True, restarts=0, entered=1) for name in apply.SERVICES}
        baseline = dict(units=units, processes={"GameViewerServer.exe": 1, "GameViewerService.exe": 1}, connected=True)
        good = dict(units=units, processes=dict(baseline["processes"]), connected=True)
        self.assertEqual(apply.judge(baseline, good, network_up=True), [])
        down = {**good, "units": {**units, "uu-native-bridge": dict(active=False, restarts=0, entered=1)}}
        self.assertEqual(apply.judge(baseline, down, network_up=True), ["uu-native-bridge 没有在运行"])
        gone = {**good, "processes": {"GameViewerServer.exe": 0, "GameViewerService.exe": 1}}
        self.assertEqual(apply.judge(baseline, gone, network_up=True), ["GameViewerServer.exe 没有在运行"])
        offline = {**good, "connected": False}
        self.assertEqual(apply.judge(baseline, offline, network_up=True), ["UU 没有连上服务器"])
        self.assertEqual(apply.judge(baseline, offline, network_up=False), [])  # a cable is not the update's fault
        never_connected = {**baseline, "connected": False}
        self.assertEqual(apply.judge(never_connected, offline, network_up=True), [])  # it was not before either

    def test_the_baseline_must_be_healthy_for_an_update_to_start(self):
        units = {name: dict(active=True, restarts=0, entered=1) for name in apply.SERVICES}
        good = dict(units=units, processes={"GameViewerServer.exe": 1}, connected=True)
        self.assertEqual(apply.baseline_problems(good), [])
        bad = dict(units={**units, "uu-native-text": dict(active=False, restarts=0, entered=1)},
                   processes={"GameViewerServer.exe": 0}, connected=None)
        self.assertEqual(len(apply.baseline_problems(bad)), 2)


class Machine:
    """A pretend Ubuntu: dpkg, apt, the user's systemd manager and their desktop session."""

    def __init__(self, test):
        self.test = test
        self.version = "1.2.0"
        self.snapshot_version = self.version
        self.restored_runtime = None
        self.commands = []
        self.session = True
        self.audit = ""
        self.simulation = "Inst uuway [1.2.0] (1.2.1 now [amd64])\nConf uuway (1.2.1 now [amd64])\n"
        self.apt_fails = {}          # version -> exit code, for an install of that version
        self.apt_leaves_half = False  # a failed install still changes the version
        self.refresh_rc = {}         # version installed at the time -> exit code of `uuway refresh`
        self.bad_versions = set()    # versions whose bridge does not come up after refresh
        self.bridge_up = True
        self.restarts = 0
        self.after_show = None
        self.session_returns_after = 0  # show-environment calls that fail before the session is there
        self.session_timeline = []   # what each successive "is the session there?" question answers
        self.rebooted = False

    # -- the runner the code under test calls
    def __call__(self, argv, **kwargs):
        argv = [str(part) for part in argv]
        self.commands.append(argv)
        program = argv[0]
        if program == "dpkg-query" and "-W" in argv:
            return completed(argv, 0, self.version)
        if program == "dpkg" and argv[1] == "--audit":
            return completed(argv, 0, self.audit)
        if program == "apt-get" and argv[1] == "-s":
            return completed(argv, 0, self.simulation)
        if program == "apt-get" and "install" in argv:
            version = Path(argv[-1]).name.split("_")[1]
            if '--allow-downgrades' not in argv:
                self.snapshot_version = self.version
                self.restored_runtime = None
            if version in self.apt_fails:
                if self.apt_leaves_half:
                    self.version = version
                return completed(argv, self.apt_fails[version], "", "E: dpkg was interrupted")
            self.version = version
            self.bridge_up = version not in self.bad_versions
            return completed(argv, 0)
        if program == "ip":
            return completed(argv, 0, "default via 192.168.1.1 dev eth0\n")
        if program == "systemctl" and "reboot" in argv:
            self.rebooted = True
            return completed(argv)
        if program == "timeout" and "runuser" in argv:
            inner = argv[argv.index("env") + 1:]
            while inner and "=" in inner[0] and not inner[0].startswith("/"):
                inner = inner[1:]
            return self.as_user(inner)
        return completed(argv)

    def as_user(self, inner):
        if inner[:3] == ['/usr/bin/python3', '-c', apply.RESTORE_HOME_FILES]:
            self.restored_runtime = self.snapshot_version
            return completed(inner)
        if inner[:3] == ["systemctl", "--user", "show-environment"]:
            if self.session and self.session_returns_after <= 0:
                return completed(inner, 0, "XDG_SESSION_TYPE=wayland\nXDG_CURRENT_DESKTOP=ubuntu:GNOME\n"
                                           "WAYLAND_DISPLAY=wayland-0\nDISPLAY=:0\nPATH=/x\n")
            self.session_returns_after -= 1
            return completed(inner, 1)
        if inner[:4] == ["systemctl", "--user", "is-active", "--quiet"]:
            if self.session_timeline:
                self.session = self.session_timeline.pop(0)
            return completed(inner, 0 if self.session else 3)
        if inner[:3] == ["systemctl", "--user", "show"]:
            if self.after_show:
                self.after_show()
            blocks = []
            for name in apply.SERVICES:
                active = self.session and (self.bridge_up or name != "uu-native-bridge")
                blocks.append(f"NRestarts={self.restarts}\nId={name}.service\n"
                              f"ActiveState={'active' if active else 'failed'}\n"
                              f"SubState={'running' if active else 'failed'}\n"
                              f"ActiveEnterTimestampMonotonic={500 + self.restarts}\n")
            return completed(inner, 0, "\n".join(blocks))
        if inner[0] == "/usr/bin/uuway" and inner[1] == "refresh":
            return completed(inner, self.refresh_rc.get(self.version, 0))
        if inner[:3] == ["systemctl", "--user", "restart"]:
            if "uu-native-bridge.service" in inner:
                self.bridge_up = (self.restored_runtime or self.version) not in self.bad_versions
            return completed(inner)
        return completed(inner)

    # -- helpers for the assertions
    def ran(self, *words):
        return [argv for argv in self.commands if all(word in argv for word in words)]

    def index(self, *words):
        for position, argv in enumerate(self.commands):
            if all(word in argv for word in words):
                return position
        self.test.fail(f"{words} was never run")


class UpdateCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "home/alice"
        self.home.mkdir(parents=True)
        self.snapshot = self.root / "snapshot"
        (self.snapshot / "home/.config/uurb").mkdir(parents=True)
        (self.root / "run/user" / str(UID)).mkdir(parents=True)
        (self.root / "run/user" / str(UID) / "bus").touch()
        (self.root / "etc").mkdir()
        self.layout = apply.Layout(self.root / "state", resume_unit=self.root / "etc/uuway-update-resume.service",
                                   proc=self.root / "proc", runtime=self.root / "run/user",
                                   dpkg_info=self.root / "info", root=self.root / "system")
        make_proc(self.layout.proc)
        self.machine = Machine(self)
        self.clock = Clock()
        self.logs = []
        units = {name: dict(active=True, restarts=0, entered=500) for name in apply.SERVICES}
        self.baseline = dict(units=units, processes={"GameViewerServer.exe": 1, "GameViewerService.exe": 1},
                             connected=True)
        self.stage = self.root / "stage"
        self.stage.mkdir()
        for name, data in (("SHA256SUMS", b"sums"), ("uuway_1.2.1_amd64.deb", b"new package")):
            (self.stage / name).write_bytes(data)

        def repack(job, version):
            built = Path(tempfile.mkdtemp(prefix="repack-", dir=self.layout.work)) / apply.deb_name(version)
            built.write_bytes(b"previous package")
            return built, hashlib.sha256(b"previous package").hexdigest()

        def verify(work, tag, installed, *, run):
            self.verified = (tag, installed)
            deb = work / apply.deb_name(tag[1:])
            return apply.Release(tag[1:], deb, hashlib.sha256(deb.read_bytes()).hexdigest())

        self.repack = repack
        self.verify = verify
        self.verified = None
        for patcher in (patch.object(apply, "repack_installed", repack),):
            patcher.start()
            self.addCleanup(patcher.stop)

    def job(self, **options):
        options.setdefault("tag", "v1.2.1")
        options.setdefault("snapshot", str(self.snapshot))
        return apply.Job(self.layout, user="alice", uid=UID, home=str(self.home), prefix=str(self.home / "prefix"),
                         baseline=self.baseline, run=self.machine, check_run=self.machine, clock=self.clock.now,
                         sleep=self.clock.sleep, log=self.logs.append, **options)

    def update(self, stage=None, **options):
        job = self.job(**options)
        outcome = apply.run_update(job, stage or self.stage, verify=self.verify)
        return outcome, job

    def stage_for(self, version):
        stage = self.root / f"stage-{version}"
        stage.mkdir()
        for name, data in (("SHA256SUMS", b"sums"), (f"uuway_{version}_amd64.deb", b"pkg " + version.encode())):
            (stage / name).write_bytes(data)
        return stage

    def result(self):
        return json.loads(self.layout.result.read_text())

    def journal(self):
        return json.loads(self.layout.journal.read_text())

    def resume_armed(self):
        return self.layout.resume_unit.exists()


class SuccessfulUpdateTests(UpdateCase):
    def test_a_healthy_update_installs_refreshes_and_reports_success(self):
        outcome, job = self.update()
        self.assertEqual(outcome, "success")
        self.assertEqual(self.machine.version, "1.2.1")
        self.assertEqual(self.result()["outcome"], "success")
        self.assertEqual((self.result()["from_version"], self.result()["to_version"]), ("1.2.0", "1.2.1"))
        self.assertEqual(self.journal()["phase"], "done")
        self.assertFalse(self.resume_armed())  # the boot-time resume is removed again
        self.assertEqual(self.verified, ("v1.2.1", "1.2.0"))

    def test_the_order_of_the_steps(self):
        self.update()
        simulate = self.machine.index("apt-get", "-s")
        install = self.machine.index("apt-get", "install", "-y", "--no-install-recommends", "-o")
        refresh = self.machine.index("/usr/bin/uuway", "refresh")
        self.assertLess(simulate, install)
        self.assertLess(install, refresh)

    def test_apt_installs_the_verified_copy_without_recommends(self):
        seen = []
        original = Machine.__call__

        def look(machine, argv, **kwargs):
            if argv[0] == "apt-get" and "-s" not in argv and "install" in argv:
                deb = Path(argv[-1])  # the file has to be right at the moment apt reads it
                seen.append((list(argv), deb.parent, deb.read_bytes(), deb.stat().st_mode & 0o777))
            return original(machine, argv, **kwargs)
        self.machine.__class__ = type("Looking", (Machine,), {"__call__": look})
        self.update()
        (argv, parent, content, mode), = seen
        self.assertIn("--no-install-recommends", argv)
        self.assertEqual(parent, self.layout.new)  # not the user's download
        self.assertEqual((content, mode), (b"new package", 0o644))

    def test_the_staged_copy_does_not_outlive_the_update(self):
        self.update()
        self.assertEqual(list(self.layout.new.iterdir()), [])
        self.setUp()
        self.machine.bad_versions.add("1.2.1")
        self.update()
        self.assertEqual(list(self.layout.new.iterdir()), [])  # nor a rolled-back one

    def test_the_refresh_runs_as_the_user_with_the_desktop_environment(self):
        self.update()
        refresh = self.machine.ran("/usr/bin/uuway", "refresh")[0]
        self.assertEqual(refresh[:2], ["timeout", "900"])
        self.assertIn("runuser", refresh)
        self.assertIn("--yes", refresh)
        self.assertNotIn("--restart-services", refresh)
        for variable in ("XDG_SESSION_TYPE=wayland", "XDG_CURRENT_DESKTOP=ubuntu:GNOME", f"HOME={self.home}",
                         f"XDG_RUNTIME_DIR=/run/user/{UID}"):
            self.assertIn(variable, refresh)
        self.assertNotIn("PATH=/x", refresh)  # only the graphical variables are handed on

    def test_the_resume_unit_is_in_place_while_the_package_is_being_replaced(self):
        seen = []
        original = self.machine.__call__

        def spy(argv, **kwargs):
            if argv[0] == "apt-get" and "-s" not in argv and "install" in argv:
                seen.append(self.resume_armed())
            return original(argv, **kwargs)
        self.machine = Machine(self)
        self.machine.__class__ = type("Spy", (Machine,), {"__call__": lambda me, argv, **kw: spy(argv, **kw)})
        self.update()
        self.assertEqual(seen, [True])

    def test_a_disconnected_network_is_noted_not_blamed(self):
        original = self.machine.__call__
        make_proc(self.layout.proc, socket_state="08")  # UU is not connected ...

        def offline(argv, **kwargs):
            if argv[0] == "ip":
                return completed(argv, 0, "")  # ... and there is no default route
            return original(argv, **kwargs)
        self.machine.__class__ = type("Offline", (Machine,), {"__call__": lambda me, argv, **kw: offline(argv, **kw)})
        outcome, _ = self.update()
        self.assertEqual(outcome, "success")
        self.assertIn("网络不通", self.result()["message"])


class RefusedBeforeAnythingChangesTests(UpdateCase):
    def assert_nothing_installed(self):
        self.assertFalse([argv for argv in self.machine.ran("apt-get", "install") if "-s" not in argv])
        self.assertEqual(self.machine.version, "1.2.0")
        self.assertFalse(self.resume_armed())
        self.assertEqual(self.result()["outcome"], "not_started")

    def test_a_failed_verification_stops_everything(self):
        def refuse(*args, **kwargs):
            raise apply.VerifyError("哈希校验失败")
        outcome = apply.run_update(self.job(), self.stage, verify=refuse)
        self.assertEqual(outcome, "not_started")
        self.assert_nothing_installed()
        self.assertIn("哈希校验失败", self.result()["message"])
        self.assertFalse(self.machine.ran("dpkg-deb"))  # nothing was repacked either

    def test_no_installed_package_means_nothing_to_update(self):
        self.machine.version = None
        original = self.machine.__call__
        self.machine.__class__ = type("None_", (Machine,), {
            "__call__": lambda me, argv, **kw: completed(argv, 1) if argv[0] == "dpkg-query" else original(argv, **kw)})
        outcome, _ = self.update()
        self.assertEqual(outcome, "not_started")

    def test_without_a_copy_of_the_old_version_it_does_not_start(self):
        def cannot(job, version):
            raise apply.TransactionError("磁盘满了")
        with patch.object(apply, "repack_installed", cannot):
            outcome, _ = self.update()
        self.assertEqual(outcome, "not_started")
        self.assert_nothing_installed()
        self.assertIn("磁盘满了", self.result()["message"])

    def test_a_plan_that_touches_other_packages_is_refused(self):
        for simulation in ("Inst uuway [1.2.0] (1.2.1 now)\nInst fcitx5 (5.1 now)\n",
                           "Inst uuway [1.2.0] (1.2.1 now)\nRemv wine-stable [11.0]\n", "Conf uuway\n", ""):
            self.setUp()
            self.machine.simulation = simulation
            outcome, _ = self.update()
            self.assertEqual(outcome, "not_started", simulation)
            self.assert_nothing_installed()

    def test_an_apt_failure_that_changed_nothing_is_not_started(self):
        self.machine.apt_fails["1.2.1"] = 100
        outcome, _ = self.update()
        self.assertEqual(outcome, "not_started")
        self.assertEqual(self.machine.version, "1.2.0")
        self.assertEqual(self.result()["outcome"], "not_started")
        self.assertFalse(self.machine.ran("/usr/bin/uuway", "refresh"))

    def test_a_staged_symlink_is_not_followed(self):
        secret = self.root / "secret"
        secret.write_bytes(b"root only")
        (self.stage / "SHA256SUMS").unlink()
        os.symlink(secret, self.stage / "SHA256SUMS")
        outcome, _ = self.update()
        self.assertEqual(outcome, "not_started")
        self.assert_nothing_installed()

    def test_an_oversized_download_is_refused(self):
        with patch.object(apply, "SUMS_LIMIT", 2):
            outcome, _ = self.update()
        self.assertEqual(outcome, "not_started")
        self.assertIn("过大", self.result()["message"])


class RollbackTests(UpdateCase):
    def test_an_unhealthy_machine_is_rolled_back_and_checked_again(self):
        self.machine.bad_versions.add("1.2.1")
        outcome, _ = self.update()
        self.assertEqual(outcome, "rolled_back")
        self.assertEqual(self.machine.version, "1.2.0")
        self.assertEqual(self.result()["outcome"], "rolled_back")
        self.assertEqual(self.result()["rolled_back_to"], "1.2.0")
        self.assertFalse(self.resume_armed())
        self.assertTrue(self.machine.ran("--allow-downgrades"))

    def test_rollback_restarts_the_restored_runtime_without_refreshing_over_it(self):
        self.machine.bad_versions.add("1.2.1")
        self.update()
        downgrade = self.machine.index("--allow-downgrades")
        restore = self.machine.index("/usr/bin/python3", "-c", apply.RESTORE_HOME_FILES)
        refreshes = [i for i, argv in enumerate(self.machine.commands)
                     if "/usr/bin/uuway" in argv and "refresh" in argv]
        self.assertEqual(len(refreshes), 1)  # only the new version; keep the exact previous runtime
        self.assertLess(refreshes[0], downgrade)
        self.assertLess(downgrade, restore)
        self.assertLess(restore, self.machine.index("restart", "uu-native-bridge.service"))
        copy = self.machine.ran("/usr/bin/python3", "-c", apply.RESTORE_HOME_FILES)[0]
        self.assertIn(f"{self.snapshot}/home", copy)
        self.assertIn(str(self.home), copy)
        self.assertIn("runuser", copy)  # as the user, never as root

    def test_restoring_files_preserves_private_directories_and_replaces_a_running_helper(self):
        prefix = self.home / 'prefix'
        prefix.mkdir(mode=0o700)
        helper = prefix / 'helper'
        shutil.copy2('/bin/sleep', helper)
        process = subprocess.Popen([str(helper), '30'])
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        source = self.snapshot / 'home/prefix'
        source.mkdir(mode=0o775)
        (source / 'helper').write_bytes(b'restored helper')
        (prefix / 'unrelated').write_text('keep')
        result = subprocess.run(['/usr/bin/python3', '-c', apply.RESTORE_HOME_FILES,
                                 str(self.snapshot / 'home'), str(self.home)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(prefix.stat().st_mode & 0o777, 0o700)
        self.assertEqual(helper.read_bytes(), b'restored helper')
        self.assertEqual((prefix / 'unrelated').read_text(), 'keep')
        self.assertIsNone(process.poll())

    def test_a_failing_refresh_rolls_back(self):
        self.machine.refresh_rc["1.2.1"] = 1
        outcome, _ = self.update()
        self.assertEqual(outcome, "rolled_back")
        self.assertIn("refresh", self.result()["message"])

    def test_a_service_in_a_crash_loop_never_counts_as_healthy(self):
        def flap():
            self.machine.restarts += 1
        self.machine.after_show = flap
        outcome, _ = self.update()
        # it restarts at every look, so the 90 s of calm never happens, on either version
        self.assertEqual(outcome, "rollback_failed")
        self.assertTrue(self.machine.ran("--allow-downgrades"))

    def test_a_stable_machine_that_restarted_once_before_settling_is_fine(self):
        calls = []

        def once():
            calls.append(1)
            if len(calls) == 3:
                self.machine.restarts += 1
        self.machine.after_show = once
        outcome, _ = self.update()
        self.assertEqual(outcome, "success")

    def test_the_desktop_session_not_coming_back_rolls_back(self):
        original = self.machine.__call__

        def lose_session(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[0] == "apt-get" and "-s" not in argv and "--allow-downgrades" not in argv:
                self.machine.session = False
                self.machine.session_returns_after = 10 ** 6
            if "--allow-downgrades" in argv:
                self.machine.session = True
                self.machine.session_returns_after = 0
            return result
        self.machine.__class__ = type("Lost", (Machine,), {"__call__": lambda me, argv, **kw: lose_session(argv, **kw)})
        outcome, _ = self.update()
        self.assertEqual(outcome, "rolled_back")
        self.assertIn("桌面会话", self.result()["message"])

    def test_two_session_restarts_during_the_watch_count_as_a_failure(self):
        self.machine.session_timeline = [True, False, True, False]  # up, lost, back, lost again
        verdict = apply.wait_healthy(self.job(), self.baseline)
        self.assertFalse(verdict.healthy)
        self.assertIn("两次", verdict.reason)

    def test_a_service_that_dies_half_a_minute_after_starting_is_not_healthy(self):
        looks = []

        def die_late():
            looks.append(1)
            if len(looks) == 7:  # about 30 seconds in: far short of the 90 of calm that count
                self.machine.bridge_up = False
        self.machine.after_show = die_late
        verdict = apply.wait_healthy(self.job(), self.baseline)
        self.assertFalse(verdict.healthy)
        self.assertIn("uu-native-bridge", verdict.reason)

    def test_health_is_only_declared_after_the_full_calm_period(self):
        start = self.clock.now()
        verdict = apply.wait_healthy(self.job(), self.baseline)
        self.assertTrue(verdict.healthy)
        self.assertGreaterEqual(self.clock.now() - start, apply.SOAK)

    def test_one_session_restart_during_the_watch_is_survived_with_more_time(self):
        self.machine.session_timeline = [True, False, True]
        verdict = apply.wait_healthy(self.job(), self.baseline)
        self.assertTrue(verdict.healthy)

    def test_a_missing_old_package_falls_back_to_restoring_the_snapshot(self):
        self.machine.bad_versions.add("1.2.1")

        original = self.machine.__call__

        def vanish(argv, **kwargs):
            if argv[0] == "apt-get" and "-s" not in argv and "install" in argv and "--allow-downgrades" not in argv:
                for saved in self.layout.previous.glob("*.deb"):
                    saved.unlink()  # the saved copy is gone by the time it is needed
            return original(argv, **kwargs)
        self.machine.__class__ = type("Vanishing", (Machine,), {"__call__": lambda me, argv, **kw: vanish(argv, **kw)})
        outcome, _ = self.update()
        self.assertFalse(self.machine.ran("--allow-downgrades"))
        self.assertTrue(self.machine.ran("restart", "uu-native-bridge.service"))
        self.assertEqual(self.machine.version, "1.2.1")
        self.assertEqual(outcome, "rolled_back")
        self.assertIn("仍是新版本", self.result()["message"])

    def test_a_corrupt_old_package_is_not_installed(self):
        self.machine.bad_versions.add("1.2.1")

        def corrupt(job, version):
            built = Path(tempfile.mkdtemp(prefix="repack-", dir=self.layout.work)) / "uuway_1.2.0_amd64.deb"
            built.write_bytes(b"tampered")
            return built, hashlib.sha256(b"original").hexdigest()
        with patch.object(apply, "repack_installed", corrupt):
            self.update()
        self.assertFalse(self.machine.ran("--allow-downgrades"))


class RollbackTargetTests(UpdateCase):
    """The way back to the version before the last update is not thrown away by a later attempt."""

    def succeed_once(self):
        outcome, _ = self.update()
        self.assertEqual(outcome, "success")
        self.assertEqual(self.machine.version, "1.2.1")

    def assert_old_target_survives(self):
        saved = self.layout.previous / "uuway_1.2.0_amd64.deb"
        self.assertTrue(saved.is_file(), "the saved 1.2.0 is gone")
        result = self.result()
        self.assertEqual(result["outcome"], "not_started")
        self.assertEqual(result["rollback_to"]["version"], "1.2.0")
        self.assertEqual(result["rollback_to"]["deb"], str(saved))
        self.assertEqual(self.journal()["version"], "1.2.1")  # still the journal of the update that happened
        self.assertEqual(self.journal()["phase"], "done")

    def test_an_attempt_the_apt_simulation_refuses_keeps_it(self):
        self.succeed_once()
        self.machine.simulation = "Inst uuway [1.2.1] (1.2.2 now)\nInst fcitx5 (5.1 now)\n"
        outcome, _ = self.update(self.stage_for("1.2.2"), tag="v1.2.2")
        self.assertEqual(outcome, "not_started")
        self.assert_old_target_survives()

    def test_an_attempt_that_cannot_be_repacked_keeps_it(self):
        self.succeed_once()

        def cannot(job, version):
            raise apply.TransactionError("磁盘满了")
        with patch.object(apply, "repack_installed", cannot):
            outcome, _ = self.update(self.stage_for("1.2.2"), tag="v1.2.2")
        self.assertEqual(outcome, "not_started")
        self.assert_old_target_survives()

    def test_an_attempt_that_fails_verification_keeps_it(self):
        self.succeed_once()

        def refuse(*args, **kwargs):
            raise apply.VerifyError("哈希校验失败")
        outcome = apply.run_update(self.job(tag="v1.2.2"), self.stage_for("1.2.2"), verify=refuse)
        self.assertEqual(outcome, "not_started")
        self.assert_old_target_survives()

    def test_an_update_that_goes_ahead_replaces_it_with_the_version_it_replaced(self):
        self.succeed_once()
        outcome, _ = self.update(self.stage_for("1.2.2"), tag="v1.2.2")
        self.assertEqual(outcome, "success")
        self.assertEqual([path.name for path in self.layout.previous.iterdir()], ["uuway_1.2.1_amd64.deb"])
        self.assertEqual(self.result()["rollback_to"]["version"], "1.2.1")

    def test_a_first_attempt_that_is_refused_has_nothing_to_roll_back_to(self):
        def refuse(*args, **kwargs):
            raise apply.VerifyError("哈希校验失败")
        apply.run_update(self.job(), self.stage, verify=refuse)
        self.assertIsNone(self.result()["rollback_to"])
        self.assertFalse(self.layout.journal.exists())

    def test_a_result_that_names_a_missing_deb_is_not_carried_over(self):
        self.succeed_once()
        for saved in self.layout.previous.glob("*.deb"):
            saved.unlink()
        self.machine.simulation = ""
        self.update(self.stage_for("1.2.2"), tag="v1.2.2")
        self.assertIsNone(self.result()["rollback_to"])


class HealthDeadlineTests(UpdateCase):
    def test_a_machine_that_is_healthy_but_late_gets_the_time_it_needs_to_settle(self):
        started = self.clock.now()
        self.machine.bridge_up = False

        def comes_up_late():
            if self.clock.now() - started >= 250:
                self.machine.bridge_up = True
        self.machine.after_show = comes_up_late
        verdict = apply.wait_healthy(self.job(), self.baseline)
        self.assertTrue(verdict.healthy)  # healthy from 250 s, calm for 90 s more: past the 300 s mark
        self.assertGreater(self.clock.now() - started, apply.DEADLINE)

    def test_running_out_of_time_while_still_restarting_says_so(self):
        looks = []

        def restart_every_minute():
            looks.append(1)
            if len(looks) % 12 == 0:
                self.machine.restarts += 1  # it is up every time anybody looks, but the calm never lasts
        self.machine.after_show = restart_every_minute
        verdict = apply.wait_healthy(self.job(), self.baseline)
        self.assertFalse(verdict.healthy)
        self.assertNotEqual(verdict.reason, "")
        self.assertIn("90", verdict.reason)


class ManualRollbackTests(UpdateCase):
    def committed(self):
        self.update()
        self.assertEqual(self.machine.version, "1.2.1")
        self.logs.clear()

    def manual(self, **options):
        journal = apply.Journal.load(self.layout.journal)
        job = apply.job_from_journal(self.layout, journal, run=self.machine, check_run=self.machine,
                                     clock=self.clock.now, sleep=self.clock.sleep, **options)
        return apply.run_manual_rollback(job)

    def test_it_is_recorded_as_a_deliberate_rollback_not_a_failed_update(self):
        self.committed()
        self.assertEqual(self.manual(), "rolled_back")
        result = self.result()
        self.assertTrue(result["manual"])
        self.assertEqual(self.machine.version, "1.2.0")
        self.assertNotIn("更新后", result["message"])

    def test_an_automatic_rollback_is_not_marked_manual(self):
        self.machine.bad_versions.add("1.2.1")
        self.update()
        self.assertFalse(self.result()["manual"])

    def test_it_has_the_same_protection_against_a_power_cut_as_an_update(self):
        self.committed()
        seen = []
        original = self.machine.__call__

        def spy(argv, **kwargs):
            if "--allow-downgrades" in argv:
                seen.append(self.resume_armed())  # armed while the package is being replaced
            return original(argv, **kwargs)
        self.machine.__class__ = type("Spy", (Machine,), {"__call__": lambda me, argv, **kw: spy(argv, **kw)})
        self.manual()
        self.assertEqual(seen, [True])
        self.assertFalse(self.resume_armed())  # and removed again afterwards

    def test_a_rollback_that_was_interrupted_is_picked_up_again_at_boot(self):
        self.committed()
        journal = apply.Journal.load(self.layout.journal)
        journal.update(phase="rolling_back", reason="按要求手动回滚")
        job = apply.job_from_journal(self.layout, journal, run=self.machine, check_run=self.machine,
                                     clock=self.clock.now, sleep=self.clock.sleep)
        self.assertEqual(apply.resume(job), "rolled_back")


class UnexpectedErrorTests(UpdateCase):
    def test_an_unexpected_error_after_the_install_still_ends_in_a_rollback_and_a_result(self):
        raised = []

        def explode_once():
            if not raised:
                raised.append(1)
                raise KeyError("something nobody expected")
        self.machine.after_show = explode_once
        job = self.job()
        outcome = apply.guarded(job, lambda: apply.run_update(job, self.stage, verify=self.verify))
        self.assertEqual(outcome, "rolled_back")
        self.assertEqual(self.machine.version, "1.2.0")
        self.assertIn("更新程序出错", self.result()["message"])
        self.assertFalse(self.resume_armed())

    def test_an_unexpected_error_before_the_install_changes_nothing_and_says_so(self):
        def explode(job, version):
            raise RuntimeError("something nobody expected")
        job = self.job()
        with patch.object(apply, "repack_installed", explode):
            outcome = apply.guarded(job, lambda: apply.run_update(job, self.stage, verify=self.verify))
        self.assertEqual(outcome, "not_started")
        self.assertEqual(self.machine.version, "1.2.0")
        self.assertFalse([argv for argv in self.machine.ran("apt-get", "install") if "-s" not in argv])
        self.assertEqual(self.result()["outcome"], "not_started")

    def test_an_error_inside_the_rollback_itself_is_recorded_as_a_failed_rollback(self):
        self.machine.bad_versions.add("1.2.1")

        def explode(*args, **kwargs):
            raise OSError("no space left on device")
        job = self.job()
        with patch.object(apply, "wait_healthy", side_effect=[apply.Verdict(False, "坏了", []), OSError("disk full")]):
            outcome = apply.guarded(job, lambda: apply.run_update(job, self.stage, verify=self.verify))
        self.assertEqual(outcome, "rollback_failed")
        self.assertEqual(self.result()["outcome"], "rollback_failed")
        self.assertTrue(self.layout.recovery.is_file())


class WhenTheRollbackFailsTests(UpdateCase):
    def break_everything(self):
        self.machine.bad_versions.update({"1.2.0", "1.2.1"})

    def test_it_says_how_to_recover_and_does_not_reboot_unasked(self):
        self.break_everything()
        outcome, _ = self.update()
        self.assertEqual(outcome, "rollback_failed")
        self.assertFalse(self.machine.rebooted)
        recovery = self.layout.recovery.read_text()
        for expected in ("dpkg --configure -a", "--allow-downgrades", str(self.layout.previous),
                         f"cp -a --remove-destination --no-preserve=mode {self.snapshot}/home/. ~/",
                         "systemctl --user restart uu-native-display uu-native-bridge"):
            self.assertIn(expected, recovery)
        self.assertEqual(self.result()["recovery"], str(self.layout.recovery))

    def test_without_a_snapshot_the_recovery_notes_do_not_tell_anyone_to_copy_a_directory(self):
        self.break_everything()
        self.update(snapshot="")
        recovery = self.layout.recovery.read_text()
        self.assertNotIn("cp -a", recovery)
        self.assertNotIn("/home/.", recovery)
        self.assertIn("没有更新前的快照", recovery)
        self.assertIn("uuway refresh --yes", recovery)

    def test_a_snapshot_path_with_spaces_is_quoted_in_the_recovery_notes(self):
        self.break_everything()
        spaced = self.root / "a snapshot"
        (spaced / "home").mkdir(parents=True)
        self.update(snapshot=str(spaced))
        self.assertIn(f"cp -a --remove-destination --no-preserve=mode '{spaced}/home/.' ~/",
                      self.layout.recovery.read_text())

    def test_it_reboots_once_when_asked_and_judges_again_afterwards(self):
        self.break_everything()
        outcome, job = self.update(reboot_if_stuck=True)
        self.assertEqual(outcome, "rebooting")
        self.assertTrue(self.machine.rebooted)
        self.assertEqual(self.machine.ran("systemctl", "reboot")[0][-1], "--ignore-inhibitors")  # our own lock
        self.assertEqual(self.journal()["phase"], "rebooted")
        self.assertTrue(self.resume_armed())  # the machine has to pick this up when it is back
        self.machine.bad_versions.clear()  # the reboot fixed it: the services start properly this time
        self.machine.bridge_up = True
        journal = apply.Journal.load(self.layout.journal)
        outcome = apply.resume(apply.job_from_journal(self.layout, journal, run=self.machine, check_run=self.machine,
                                                      clock=self.clock.now, sleep=self.clock.sleep))
        self.assertEqual(outcome, "rolled_back")
        self.assertFalse(self.resume_armed())
        self.assertEqual(len(self.machine.ran("systemctl", "reboot")), 1)

    def test_it_never_reboots_a_second_time(self):
        self.break_everything()
        self.update(reboot_if_stuck=True)
        journal = apply.Journal.load(self.layout.journal)
        job = apply.job_from_journal(self.layout, journal, run=self.machine, check_run=self.machine,
                                     clock=self.clock.now, sleep=self.clock.sleep)
        self.assertEqual(apply.resume(job), "rollback_failed")
        self.assertEqual(len(self.machine.ran("systemctl", "reboot")), 1)


class ResumeTests(UpdateCase):
    def resumed(self, **fields):
        base = dict(schema=1, phase="apt", tag="v1.2.1", version="1.2.1", from_version="1.2.0", user="alice",
                    uid=UID, home=str(self.home), prefix=str(self.home / "prefix"), snapshot=str(self.snapshot),
                    baseline=self.baseline, reboot_if_stuck=False,
                    previous_deb=str(self.layout.previous / "uuway_1.2.0_amd64.deb"),
                    previous_sha256=hashlib.sha256(b"previous package").hexdigest())
        base.update(fields)
        self.layout.prepare()
        (self.layout.previous / "uuway_1.2.0_amd64.deb").write_bytes(b"previous package")
        apply.write_json(self.layout.journal, base, 0o600)
        job = apply.job_from_journal(self.layout, apply.Journal.load(self.layout.journal), run=self.machine,
                                     check_run=self.machine, clock=self.clock.now, sleep=self.clock.sleep)
        return apply.resume(job)

    def test_an_interrupt_before_the_package_was_touched_changes_nothing(self):
        self.assertEqual(self.resumed(phase="apt"), "not_started")
        self.assertTrue(self.machine.ran("dpkg", "--configure", "-a"))  # dpkg is repaired first
        self.assertEqual(self.machine.version, "1.2.0")

    def test_an_interrupt_after_the_install_carries_on(self):
        self.machine.version = "1.2.1"
        self.assertEqual(self.resumed(phase="apt"), "success")
        self.assertTrue(self.machine.ran("/usr/bin/uuway", "refresh"))

    def test_an_interrupt_after_the_refresh_only_checks_again(self):
        self.machine.version = "1.2.1"
        self.assertEqual(self.resumed(phase="refreshed"), "success")

    def test_an_unrecognised_state_goes_back(self):
        self.machine.version = "1.1.9"
        self.assertEqual(self.resumed(phase="apt"), "rolled_back")

    def test_a_finished_update_has_nothing_to_resume(self):
        self.assertEqual(self.resumed(phase="done"), "nothing")
        self.assertFalse(self.resume_armed())


class RepackTests(unittest.TestCase):
    """The old version is rebuilt from the files on disk; this one runs the real dpkg-deb."""

    def test_the_installed_package_comes_back_as_a_deb_that_says_the_same(self):
        if not shutil.which("dpkg-deb"):
            self.skipTest("dpkg-deb is needed")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            system, info = base / "system", base / "info"
            (system / "usr/lib/uuway/scripts").mkdir(parents=True)
            (system / "usr/bin").mkdir(parents=True)
            info.mkdir()
            script = system / "usr/lib/uuway/scripts/uuway_cli.py"
            script.write_text("print('old')\n")
            script.chmod(0o755)
            os.symlink("../lib/uuway/scripts/uuway_cli.py", system / "usr/bin/uuway")
            (info / "uuway.list").write_text("/.\n/usr\n/usr/bin\n/usr/bin/uuway\n/usr/lib\n/usr/lib/uuway\n"
                                             "/usr/lib/uuway/scripts\n/usr/lib/uuway/scripts/uuway_cli.py\n"
                                             "/usr/lib/uuway/removed-by-the-admin\n")
            (info / "uuway.md5sums").write_text("d41d8cd98f00b204e9800998ecf8427e  usr/lib/uuway/scripts/uuway_cli.py\n")
            (info / "uuway.postinst").write_text("#!/bin/sh\nexit 0\n")
            status = ("Package: uuway\nStatus: install ok installed\nVersion: 1.2.0\nArchitecture: amd64\n"
                      "Maintainer: test <t@example.invalid>\nConfig-Version: 1.2.0\nDescription: test\n long line\n")

            readings = []

            def run(argv, **kwargs):
                if argv[:2] == ["dpkg-query", "-s"]:
                    return completed(argv, 0, status)
                if "--fsys-tarfile" in argv:
                    readings.append(kwargs)
                return apply.run_command(argv, **kwargs)
            layout = apply.Layout(base / "state", dpkg_info=info, root=system)
            layout.prepare()
            job = apply.Job(layout, user="alice", uid=UID, home=str(base), prefix=str(base), run=run)
            deb, digest = apply.repack_installed(job, "1.2.0")
            self.assertEqual(deb.parent.parent, layout.work)  # built in the working area, not yet the saved copy
            self.assertEqual(list(layout.previous.iterdir()), [])
            self.assertEqual(digest, apply.sha256_file(deb))
            (reading,) = readings  # the read-back check keeps the package's contents out of memory
            self.assertIs(reading["stdout"], subprocess.DEVNULL)
            self.assertFalse(reading.get("capture_output"))
            self.assertEqual(apply.deb_control(deb), {"Package": "uuway", "Version": "1.2.0", "Architecture": "amd64"})
            contents = subprocess.run(["dpkg-deb", "-c", str(deb)], capture_output=True, text=True, check=True).stdout
            self.assertIn("./usr/lib/uuway/scripts/uuway_cli.py", contents)
            self.assertIn("./usr/bin/uuway -> ../lib/uuway/scripts/uuway_cli.py", contents)
            self.assertNotIn("removed-by-the-admin", contents)
            control = subprocess.run(["dpkg-deb", "-f", str(deb)], capture_output=True, text=True, check=True).stdout
            self.assertNotIn("Status:", control)
            self.assertNotIn("Config-Version", control)
            self.assertIn(" long line", control)  # the multi-line description survives
            extracted = base / "out"
            subprocess.run(["dpkg-deb", "-e", str(deb), str(extracted)], check=True)
            self.assertTrue((extracted / "md5sums").is_file())
            self.assertTrue(os.access(extracted / "postinst", os.X_OK))
            saved = apply.commit_previous(layout, deb)
            self.assertEqual(saved, layout.previous / "uuway_1.2.0_amd64.deb")
            self.assertTrue(saved.is_file())
            self.assertFalse(deb.exists())  # moved, not copied
            self.assertEqual(saved.stat().st_mode & 0o777, 0o644)

    def test_repacking_leaves_the_saved_copy_of_the_last_update_alone(self):
        if not shutil.which("dpkg-deb"):
            self.skipTest("dpkg-deb is needed")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "info").mkdir()
            (base / "info/uuway.list").write_text("/.\n")
            layout = apply.Layout(base / "state", dpkg_info=base / "info", root=base / "system")
            layout.prepare()
            (layout.previous / "uuway_1.0.0_amd64.deb").write_bytes(b"older")
            status = "Package: uuway\nVersion: 1.2.0\nArchitecture: amd64\nMaintainer: t <t@e.invalid>\nDescription: x\n"
            job = apply.Job(layout, user="a", uid=UID, home=str(base), prefix=str(base),
                            run=lambda argv, **kw: completed(argv, 0, status) if argv[:2] == ["dpkg-query", "-s"]
                            else apply.run_command(argv, **kw))
            built, _ = apply.repack_installed(job, "1.2.0")
            self.assertEqual([p.name for p in layout.previous.iterdir()], ["uuway_1.0.0_amd64.deb"])
            apply.commit_previous(layout, built)  # committing adds to it; finishing the update prunes
            self.assertEqual(sorted(p.name for p in layout.previous.iterdir()),
                             ["uuway_1.0.0_amd64.deb", "uuway_1.2.0_amd64.deb"])


class SimulationTests(unittest.TestCase):
    def test_only_a_plain_upgrade_of_uuway_is_accepted(self):
        self.assertTrue(apply.simulation_is_safe("Inst uuway [1.2.0] (1.2.1 now [amd64])\nConf uuway (1.2.1 now)\n"))
        self.assertFalse(apply.simulation_is_safe("Inst uuway (1.2.1)\nInst libfoo (1)\n"))
        self.assertFalse(apply.simulation_is_safe("Inst uuway (1.2.1)\nRemv wine-stable [11.0]\n"))
        self.assertFalse(apply.simulation_is_safe("Inst uuway (1.2.1)\nPurg x [1]\n"))
        self.assertFalse(apply.simulation_is_safe("Inst uuway (1.2.1)\nInst uuway (1.2.1)\n"))
        self.assertFalse(apply.simulation_is_safe(""))


if __name__ == "__main__":
    unittest.main()
