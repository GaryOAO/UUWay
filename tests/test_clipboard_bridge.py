"""Clipboard bridge between two private Xvfb servers standing in for UU's
display and the desktop's Xwayland. Wine itself is not needed: xclip plays the
applications, and a raw Xlib client plays an owner that never answers."""
import ctypes
import ctypes.util
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
import urllib.parse


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
        # Stands in for Wine running uu-clipboard-files.exe: saves one file
        # into the Z:\ directory it is given, after the delay in a "delay" file.
        (cls.build / "uu-clipboard-files.exe").write_bytes(b"")
        cls.wine = cls.build / "wine"
        cls.wine.write_text("#!/bin/sh\n"
                            "directory=$(printf '%s' \"$2\" | sed 's/^Z://; s|\\\\|/|g')\n"
                            "sleep \"$(cat \"$XDG_CACHE_HOME/delay\" 2>/dev/null || echo 0)\"\n"
                            "printf 'phone bytes' > \"$directory/phone file.pdf\"\n")
        cls.wine.chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.build)

    def setUp(self):
        self.environment = {key: value for key, value in os.environ.items() if key != "XAUTHORITY"}
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        # No session bus: transfer notifications must not reach a real desktop.
        self.environment.update(WINELOADER=str(self.wine), WINEPREFIX=self.cache.name,
                                XDG_CACHE_HOME=self.cache.name,
                                DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent",
                                UURB_CLIPBOARD_KEEP_SECONDS="2")
        self.stale = Path(self.cache.name) / "uurb-clipboard" / "files-stale"
        self.stale.mkdir(parents=True)
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
                       input=text if isinstance(text, bytes) else text.encode(), env=self.environment,
                       check=True,
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

    def test_gnome_file_list_also_reaches_uu_as_uris(self):
        self.copy(self.desktop, "copy\nfile:///home/fixture/a%20b.txt\nfile:///home/fixture/c",
                  "x-special/gnome-copied-files")
        self.wait_for(lambda: self.paste(self.uu, "text/uri-list") is not None,
                      "file list did not reach UU as text/uri-list")
        self.assertEqual(self.paste(self.uu, "text/uri-list"),
                         "file:///home/fixture/a%20b.txt\r\nfile:///home/fixture/c\r\n")

    @staticmethod
    def descriptor(*names):
        """FILEGROUPDESCRIPTORW: item count, then 592-byte descriptors with the
        UTF-16 name at byte 72."""
        data = struct.pack("<I", len(names))
        for name in names:
            encoded = name.encode("utf-16-le")
            data += bytes(72) + encoded + bytes(520 - len(encoded))
        return data

    def paste_files(self):
        """Paste as a file manager does; returns the listed paths."""
        listing = self.paste(self.desktop, "x-special/gnome-copied-files")
        action, *uris = listing.split("\n")
        self.assertEqual(action, "copy")
        return [Path(urllib.parse.unquote(uri[len("file://"):])) for uri in uris]

    def offer_phone_file(self):
        self.copy(self.uu, self.descriptor("phone file.pdf"), "FileGroupDescriptorW")
        self.wait_for(lambda: b"offered uu->desktop" in self.read_log(), "phone file was not offered")

    def test_phone_files_are_saved_only_when_pasted(self):
        self.offer_phone_file()
        self.assertEqual(self.paste(self.desktop, "TARGETS").split(),
                         ["TARGETS", "TIMESTAMP", "x-special/gnome-copied-files", "text/uri-list"])
        # Clipboard managers read text; there is none, and nothing is fetched.
        self.assertIsNone(self.paste(self.desktop))
        time.sleep(0.5)
        self.assertNotIn(b"saving files", self.read_log())
        [path] = self.paste_files()
        self.assertEqual(path.name, "phone file.pdf")
        self.assertEqual(path.read_bytes(), b"phone bytes")
        self.assertEqual(self.paste(self.desktop, "text/uri-list"), path.as_uri() + "\r\n")

    def test_paste_waits_for_phone_files_being_saved(self):
        (Path(self.cache.name) / "delay").write_text("1")
        self.offer_phone_file()
        started = time.monotonic()
        [path] = self.paste_files()
        self.assertGreater(time.monotonic() - started, 0.5)
        self.assertEqual(path.read_bytes(), b"phone bytes")

    def test_copying_the_same_files_again_keeps_saving(self):
        (Path(self.cache.name) / "delay").write_text("1")
        self.offer_phone_file()
        paste = subprocess.Popen(["xclip", "-display", self.desktop, "-selection", "clipboard", "-o",
                                  "-t", "x-special/gnome-copied-files"], env=self.environment,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.wait_for(lambda: b"saving files" in self.read_log(), "paste did not start saving")
        self.copy(self.uu, self.descriptor("phone file.pdf"), "FileGroupDescriptorW")
        self.wait_for(lambda: b"same files copied again" in self.read_log(), "same copy was not recognised")
        self.assertIn(b"phone%20file.pdf", paste.communicate(timeout=5)[0])
        self.assertNotIn(b"abandoned", self.read_log())

    def test_unused_saved_files_are_removed_and_saved_again(self):
        self.offer_phone_file()
        [path] = self.paste_files()
        self.assertTrue(path.exists())
        self.wait_for(lambda: not path.exists(), "unused saved file was kept", seconds=5)
        self.assertEqual(self.paste_files(), [path])
        self.assertEqual(path.read_bytes(), b"phone bytes")

    def test_saved_files_of_an_earlier_bridge_are_removed(self):
        self.assertFalse(self.stale.exists())

    def test_unreadable_file_list_is_not_offered(self):
        self.copy(self.uu, "descriptor", "FileGroupDescriptorW")
        self.wait_for(lambda: b"unsupported files" in self.read_log(), "bad file list was not refused")
        self.assertFalse(self.bridge_owns(self.desktop))

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
