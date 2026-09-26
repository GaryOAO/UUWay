"""Clipboard bridge between two private Xvfb servers standing in for UU's
display and the desktop's Xwayland. Wine itself is not needed: xclip plays the
applications, and a raw Xlib client plays an owner that never answers."""
import ctypes
import ctypes.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("Xvfb", "xclip", "xprop")


def start_xvfb(environment):
    read, write = os.pipe()
    server = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write), "-nolisten", "tcp", "-screen", "0", "64x64x24"],
        pass_fds=(write,), env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(write)
    with os.fdopen(read) as reader:
        number = reader.readline().strip()
    return server, ":" + number


class SilentOwner:
    """Takes CLIPBOARD and never answers a request, like a hung application."""

    def __init__(self, display):
        xlib = ctypes.CDLL(ctypes.util.find_library("X11"))
        xlib.XOpenDisplay.restype = ctypes.c_void_p
        xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        xlib.XDefaultRootWindow.restype = ctypes.c_ulong
        xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        xlib.XCreateSimpleWindow.restype = ctypes.c_ulong
        xlib.XCreateSimpleWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong] + [ctypes.c_int] * 4 + \
            [ctypes.c_uint, ctypes.c_ulong, ctypes.c_ulong]
        xlib.XInternAtom.restype = ctypes.c_ulong
        xlib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        xlib.XSetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        xlib.XFlush.argtypes = [ctypes.c_void_p]
        xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.xlib = xlib
        self.display = xlib.XOpenDisplay(display.encode())
        window = xlib.XCreateSimpleWindow(self.display, xlib.XDefaultRootWindow(self.display),
                                          0, 0, 1, 1, 0, 0, 0)
        xlib.XSetSelectionOwner(self.display, xlib.XInternAtom(self.display, b"CLIPBOARD", 0),
                                window, 0)
        xlib.XFlush(self.display)

    def close(self):
        self.xlib.XCloseDisplay(self.display)


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "needs Xvfb, xclip and xprop")
class ClipboardBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = Path(tempfile.mkdtemp(prefix="uurb-clipboard-build-"))
        cls.executable = cls.build / "uu-clipboard-bridge"
        subprocess.run(
            ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(cls.executable),
             str(ROOT / "src" / "uu_clipboard_bridge.c"), "-lX11", "-lXfixes"],
            check=True, cwd=ROOT)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.build)

    def setUp(self):
        self.environment = {key: value for key, value in os.environ.items() if key != "XAUTHORITY"}
        self.servers = []
        self.uu = self.start_display()
        self.desktop = self.start_display()
        self.log = tempfile.TemporaryFile()
        self.bridge = subprocess.Popen(
            [str(self.executable), self.uu, self.desktop], env=self.environment,
            stdout=subprocess.DEVNULL, stderr=self.log)
        self.addCleanup(self.stop)
        self.wait_for(lambda: b"bridge connected" in self.read_log(), "bridge did not connect")

    def start_display(self):
        server, display = start_xvfb(self.environment)
        self.servers.append(server)
        return display

    def stop(self):
        self.bridge.terminate()
        self.bridge.wait(timeout=5)
        for server in self.servers:
            server.terminate()
            server.wait(timeout=5)
        self.log.close()

    def read_log(self):
        self.log.seek(0)
        return self.log.read()

    def wait_for(self, predicate, message, seconds=3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail(message + "\n" + self.read_log().decode(errors="replace"))

    def copy(self, display, text, target="UTF8_STRING"):
        subprocess.run(["xclip", "-display", display, "-selection", "clipboard", "-i", "-t", target],
                       input=text.encode(), env=self.environment, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def paste(self, display, target="UTF8_STRING"):
        result = subprocess.run(["xclip", "-display", display, "-selection", "clipboard", "-o",
                                 "-t", target], env=self.environment, capture_output=True, timeout=3)
        return result.stdout.decode() if result.returncode == 0 else None

    def bridge_owns(self, display):
        # Only the bridge offers TIMESTAMP; xclip lists TARGETS and its one target.
        return "TIMESTAMP" in (self.paste(display, "TARGETS") or "")

    def test_copies_both_ways_without_echo(self):
        self.copy(self.desktop, "desktop copy")
        self.wait_for(lambda: self.paste(self.uu) == "desktop copy", "desktop copy did not reach UU")
        self.assertFalse(self.bridge_owns(self.desktop))
        # Wine takes UU's clipboard again with what it imported: an echo.
        self.copy(self.uu, "desktop copy")
        self.wait_for(lambda: b"unchanged uu->desktop" in self.read_log(), "echo was not recognised")
        self.assertFalse(self.bridge_owns(self.desktop))
        self.copy(self.uu, "phone copy")
        self.wait_for(lambda: self.paste(self.desktop) == "phone copy", "phone copy did not reach desktop")
        self.assertTrue(self.bridge_owns(self.desktop))

    def test_paste_in_progress_is_not_copied(self):
        self.copy(self.desktop, "typed text", "application/x-uurb-transient")
        self.wait_for(lambda: b"transient on desktop" in self.read_log(), "transient selection was copied")
        self.assertIsNone(self.paste(self.uu))

    def test_touches_uu_root_so_wine_flushes_its_selection(self):
        def touched(display):
            result = subprocess.run(["xprop", "-display", display, "-root", "UURB_CLIPBOARD_WAKE"],
                                    env=self.environment, capture_output=True, text=True)
            return "UURB_CLIPBOARD_WAKE(CARDINAL)" in result.stdout
        self.wait_for(lambda: touched(self.uu), "UU root property was never touched")
        self.assertFalse(touched(self.desktop))

    def test_keeps_serving_and_queueing_while_an_owner_hangs(self):
        self.copy(self.desktop, "desktop copy")
        self.wait_for(lambda: self.paste(self.uu) == "desktop copy", "desktop copy did not reach UU")
        silent = SilentOwner(self.desktop)
        self.addCleanup(silent.close)
        time.sleep(0.3)
        # The bridge now waits on the silent owner; what it offers UU is
        # still answered at once.
        started = time.monotonic()
        self.assertEqual(self.paste(self.uu), "desktop copy")
        self.assertLess(time.monotonic() - started, 1)
        # A copy made meanwhile is queued, not dropped, and wins afterwards.
        self.copy(self.desktop, "later desktop copy")
        self.wait_for(lambda: self.paste(self.uu) == "later desktop copy",
                      "copy made during a hung transfer was lost", seconds=8)


if __name__ == "__main__":
    unittest.main()
