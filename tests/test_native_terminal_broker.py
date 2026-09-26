import os
import re
from pathlib import Path
import signal
import socket
import struct
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAGIC = 0x55555242
VERSION = 1
TOKEN_LENGTH = 64


class NativeTerminalBrokerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = Path(tempfile.mkdtemp(prefix="uurb-terminal-broker-build-"))
        cls.executable = cls.build / "uu-terminal-bridge"
        subprocess.run(
            ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
             "-I", str(ROOT / "src"), "-o", str(cls.executable),
             str(ROOT / "src" / "uu_terminal_bridge.c"), "-lutil"],
            check=True,
            cwd=ROOT,
        )

    @classmethod
    def tearDownClass(cls):
        for path in cls.build.iterdir():
            path.unlink()
        cls.build.rmdir()

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="uurb-terminal-broker-"))
        self.directory.chmod(0o700)
        self.token = "0123456789abcdef" * 4
        self.token_file = self.directory / "token"
        self.token_file.write_text(self.token + "\n")
        self.token_file.chmod(0o600)
        self.ready = self.directory / "ready"
        self.process = None

    def tearDown(self):
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            self.process.wait(timeout=3)
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()

    def start(self, token_file=True, environment_token=None):
        environment = os.environ.copy()
        environment.pop("UURB_TERMINAL_BRIDGE_TOKEN", None)
        arguments = [str(self.executable), "--ready-file", str(self.ready)]
        if token_file:
            arguments.extend(("--token-file", str(self.token_file)))
        if environment_token is not None:
            environment["UURB_TERMINAL_BRIDGE_TOKEN"] = environment_token
        self.process = subprocess.Popen(
            arguments,
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 2
        while not self.ready.exists() and self.process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if self.process.poll() is not None:
            self.fail("terminal broker exited before publishing ready file")
        self.assertTrue(self.ready.exists())
        port = int(self.ready.read_text())
        self.assertGreater(port, 0)
        return port

    def children(self):
        path = Path("/proc") / str(self.process.pid) / "task" / str(self.process.pid) / "children"
        try:
            return path.read_text().split()
        except FileNotFoundError:
            return []

    @staticmethod
    def hello():
        return struct.pack("!IHHHH", MAGIC, VERSION, TOKEN_LENGTH, 80, 24)

    def test_token_file_rejects_bad_client_without_starting_shell(self):
        port = self.start()
        with socket.create_connection(("127.0.0.1", port), timeout=1) as client:
            client.sendall(self.hello() + ("f" * TOKEN_LENGTH).encode())
            client.settimeout(1)
            self.assertEqual(client.recv(1), b"")
        deadline = time.monotonic() + 1
        while self.children() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.children())

    def test_partial_handshake_is_interruptible_and_does_not_start_shell(self):
        port = self.start()
        client = socket.create_connection(("127.0.0.1", port), timeout=1)
        client.sendall(self.hello()[:3])
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=2)
        client.close()
        self.assertFalse(self.ready.exists())

    def test_legacy_environment_token_remains_supported(self):
        port = self.start(token_file=False, environment_token=self.token)
        self.assertTrue(self.ready.exists())
        self.process.send_signal(signal.SIGTERM)
        self.process.wait(timeout=2)
        self.assertFalse(self.ready.exists())

    def test_existing_ready_file_is_not_replaced_or_removed(self):
        self.ready.write_text("unrelated\n")
        self.ready.chmod(0o600)
        environment = os.environ.copy()
        environment.pop("UURB_TERMINAL_BRIDGE_TOKEN", None)
        result = subprocess.run(
            [str(self.executable), "--ready-file", str(self.ready),
             "--token-file", str(self.token_file)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=2,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.ready.read_text(), "unrelated\n")

    def test_invalid_token_file_is_rejected_before_listener_or_ready_file(self):
        self.token_file.write_text("not-a-token\n")
        self.token_file.chmod(0o600)
        environment = os.environ.copy()
        environment.pop("UURB_TERMINAL_BRIDGE_TOKEN", None)
        result = subprocess.run(
            [str(self.executable), "--ready-file", str(self.ready),
             "--token-file", str(self.token_file)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=2,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.ready.exists())


if __name__ == "__main__":
    unittest.main()


class PersistentTerminalSessionTests(NativeTerminalBrokerTests):
    """Version 2: named sessions outlive viewers, like dtach."""

    ATTACH = 1
    ANCHOR = 2

    def connect(self, port, role, name, columns=80, rows=24):
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        encoded = name.encode()
        client.sendall(struct.pack("!IHHHH", MAGIC, 2, TOKEN_LENGTH, columns, rows) +
                       self.token.encode() + struct.pack("!BB", role, len(encoded)) + encoded)
        accepted = client.recv(1)
        if accepted != b"\x06":
            client.close()
            return None
        return client

    @staticmethod
    def send_input(client, data):
        client.sendall(struct.pack("!B3xI", 1, len(data)) + data)

    @staticmethod
    def read_until(client, marker, timeout=5):
        received = b""
        deadline = time.monotonic() + timeout
        while marker not in received and time.monotonic() < deadline:
            client.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                chunk = client.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            received += chunk
        return received

    def shell_pid(self, client):
        # The quotes keep the echoed command line from matching the output.
        self.send_input(client, b'echo "SHELL""-PID-$$-END"\n')
        output = b""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            output += self.read_until(client, b"-END", 0.5)
            match = re.search(rb"SHELL-PID-(\d+)-END", output)
            if match:
                return match.group(1)
        self.fail("shell did not report its pid")

    def closed(self, client, timeout=5):
        client.settimeout(timeout)
        try:
            while client.recv(65536):
                pass
        except socket.timeout:
            return False
        except OSError:
            pass
        return True

    def test_viewer_returns_to_the_same_shell_while_anchored(self):
        port = self.start()
        first = self.connect(port, self.ATTACH, "session1")
        self.assertIsNotNone(first)
        pid = self.shell_pid(first)
        anchor = self.connect(port, self.ANCHOR, "session1")
        self.assertIsNotNone(anchor)
        first.close()
        time.sleep(0.3)
        second = self.connect(port, self.ATTACH, "session1", 100, 30)
        self.assertIsNotNone(second)
        self.assertEqual(self.shell_pid(second), pid)
        self.send_input(second, b"stty size\n")
        self.assertIn(b"30 100", self.read_until(second, b"30 100"))
        anchor.close()
        self.assertTrue(self.closed(second))
        second.close()

    def test_sessions_are_independent(self):
        port = self.start()
        first = self.connect(port, self.ATTACH, "session1")
        second = self.connect(port, self.ATTACH, "session2")
        self.assertNotEqual(self.shell_pid(first), self.shell_pid(second))
        first.close()
        second.close()

    def test_unanchored_session_ends_with_its_viewer(self):
        port = self.start()
        first = self.connect(port, self.ATTACH, "session1")
        self.shell_pid(first)
        first.close()
        time.sleep(0.5)
        self.assertIsNone(self.connect(port, self.ANCHOR, "session1"))

    def test_anchor_without_session_is_rejected(self):
        port = self.start()
        self.assertIsNone(self.connect(port, self.ANCHOR, "session9"))

    def test_shell_exit_releases_anchor_and_viewer(self):
        port = self.start()
        viewer = self.connect(port, self.ATTACH, "session1")
        anchor = self.connect(port, self.ANCHOR, "session1")
        self.shell_pid(viewer)
        self.send_input(viewer, b"exit\n")
        self.assertTrue(self.closed(anchor))
        self.assertTrue(self.closed(viewer))
        anchor.close()
        viewer.close()

    def test_invalid_session_names_are_rejected(self):
        port = self.start()
        for name in ("", ".hidden", "a/b", "x" * 65):
            self.assertIsNone(self.connect(port, self.ATTACH, name), name)
