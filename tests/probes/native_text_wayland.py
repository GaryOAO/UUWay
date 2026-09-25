#!/usr/bin/python3
"""Paste known fixture text into an owned Wayland TextView, never other apps.

Checks prior text clipboard equality without logging its content or hash.
Requires the separately authorized text daemon to remain alive afterwards.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import secrets
import pwd
import threading
import time
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

TEXT = 'UU 原生输入：中文🙂\n第二行\tABC'


def trial_environment(trial_state):
    wine_environment = None
    root = Path(__file__).resolve().parents[2]
    if trial_state:
        spec = importlib.util.spec_from_file_location('text_trial', root / 'scripts/uu-native-trial.py')
        trial = importlib.util.module_from_spec(spec); spec.loader.exec_module(trial)
        trial.private_directory(trial_state)
        journal = json.loads((trial_state / 'journal.json').read_text())
        if journal.get('native_text_requested') is not True:
            raise RuntimeError('Expected an explicitly text-enabled native trial')
        for process in trial.prefix_processes(Path(journal['prefix'])):
            path = Path('/proc') / str(process['pid'])
            try:
                if str(trial_state / 'bootstrap.exe') not in (path / 'maps').read_text():
                    continue
                values = dict(v.split(b'=', 1) for v in (path / 'environ').read_bytes().split(b'\0') if b'=' in v)
                wine_environment = dict(os.environ, WINEPREFIX=journal['prefix'], WINEDEBUG='-all')
                for key in ('DISPLAY', 'XAUTHORITY'):
                    wine_environment[key] = values[key.encode()].decode()
                break
            except (OSError, KeyError):
                pass
        if wine_environment is None:
            raise RuntimeError('Matching live trial not found')
    return wine_environment


class OwnedFocusPointer:
    """Short-lived independent helper; click only after owned-surface motion."""
    def __init__(self, root):
        self.directory = tempfile.TemporaryDirectory(prefix='uurb-text-focus-')
        self.process = None
        self.connection = None
        self.sequence = 0
        try:
            token = secrets.token_hex(32).encode()
            path = Path(self.directory.name) / 'token'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(token)
            self.process = subprocess.Popen([str(root / 'build/native-presenter/uu-native-input'),
                '--user', pwd.getpwuid(os.getuid()).pw_name, '--token-file', str(path),
                '--seconds', '15', '--single-output'], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True)
            import select
            if not select.select([self.process.stdout], [], [], 3)[0]:
                raise RuntimeError('Focus pointer startup timeout')
            line = self.process.stdout.readline()
            if not line.startswith('UURB_NATIVE_INPUT '):
                raise RuntimeError('Focus pointer unavailable')
            ready = json.loads(line.split(' ', 1)[1])
            if ready.get('event') != 'ready' or ready.get('uid') != os.getuid():
                raise RuntimeError('Focus pointer identity mismatch')
            self.connection = socket.create_connection(('127.0.0.1', ready['port']), timeout=1)
            self.connection.sendall(struct.pack('<II64s', 0x58315255, 3, token))
            self.reply(0, 1)
        except BaseException:
            self.close()
            raise

    def reply(self, sequence, count):
        data = b''
        while len(data) < 16:
            part = self.connection.recv(16 - len(data))
            if not part:
                raise RuntimeError('Focus pointer closed')
            data += part
        if data != struct.pack('<4I', 0x58315255, sequence, count, 0):
            raise RuntimeError('Focus pointer rejected gesture')

    def send(self, records):
        self.sequence += 1
        self.connection.sendall(struct.pack('<4I', 0x58315255, self.sequence, len(records), 0) +
                                b''.join(struct.pack('<IIiiIHH', 2, flags, x, y, 0, 0, 0)
                                         for flags, x, y in records))
        self.reply(self.sequence, len(records))

    def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=3)
            self.process.stdout.close()
        self.directory.cleanup()


def run(endpoint, trial_state=None, focus_pointer=False):
    wine_environment = trial_environment(trial_state)
    root = Path(__file__).resolve().parents[2]
    display = Gdk.Display.get_default()
    if focus_pointer and (not display or display.get_n_monitors() != 1 or
                          not display.get_name().startswith('wayland-')):
        raise RuntimeError('Owned focus check requires one Wayland output')
    pointer = OwnedFocusPointer(root) if focus_pointer else None
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    previous = None
    window = Gtk.Window(title='UU native Unicode text check — temporary')
    window.set_decorated(False); window.fullscreen()
    editor = Gtk.TextView(); window.add(editor)
    status = dict(sent=False, accepted=False, reply=False, text_matches=False, clipboard_preserved=False)
    started = time.monotonic()
    worker = None
    replied_at = None
    broker_diagnostic = {'responded': False, 'stage': 'not_requested', 'error': 0}
    focus = {'moved': False, 'clicked': False, 'ctrl_down': False, 'v_down': False,
             'v_has_ctrl': False, 'unexpected_modifier': False}

    def motion(widget, event):
        # Motion must arrive on our actual editor at the requested point.
        # Recheck pointer ownership immediately before clicking; no blind click.
        if not pointer or not focus['moved'] or focus['clicked'] or status['sent']:
            return False
        if abs(event.x - editor.get_allocated_width() / 2) > 5 or abs(event.y - editor.get_allocated_height() / 2) > 5:
            return False
        device = display.get_default_seat().get_pointer()
        surface, _, _ = device.get_window_at_position()
        if not surface or surface.get_toplevel() != window.get_window():
            return False
        focus['clicked'] = True
        pointer.send([(2, 0, 0), (4, 0, 0)])
        return False

    def key(widget, event):
        # Export only expected-chord booleans, never arbitrary user key values.
        if not status['sent']:
            return False
        if event.keyval == Gdk.KEY_Control_L:
            focus['ctrl_down'] = True
        if event.keyval in (Gdk.KEY_v, Gdk.KEY_V):
            focus['v_down'] = True
            focus['v_has_ctrl'] = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
            focus['unexpected_modifier'] = bool(event.state & (Gdk.ModifierType.SHIFT_MASK |
                Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.META_MASK))
        return False

    def send():
        try:
            if wine_environment is not None:
                result = subprocess.run(['/opt/wine-stable/bin/wine', str(root / 'build/native-presenter/uu-native-input-send.exe'),
                    'unicode-fixture'], env=wine_environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=7)
                import re
                for line in result.stdout.splitlines():
                    match = re.fullmatch(r'UURB_INPUT_PROBE stage=(wait|open|response)(?: transport=([01]) count=([0-9]+))? error=([0-9]+)', line)
                    if match:
                        broker_diagnostic.update(stage=match[1], error=int(match[4]),
                                                 responded=match[1] == 'response' and match[2] == '1')
                status['accepted'] = result.returncode == 0
                return
            data = TEXT.encode('utf-8')
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
                connection.settimeout(5)
                connection.connect(str(endpoint))
                connection.sendall(struct.pack('<4I', 0x54525555, 1, 1, len(data)) + data)
                response = connection.recv(16)
                status['accepted'] = response == struct.pack('<4I', 0x54525555, 1, 1, 0)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        finally:
            status['reply'] = True

    def tick():
        nonlocal worker, previous, replied_at
        if pointer and not focus['moved'] and time.monotonic() - started > 0.6:
            focus['moved'] = True
            pointer.send([(0x8001, 16384, 16384), (0x8001, 32768, 32768)])
        if not status['sent'] and time.monotonic() - started > 1:
            if window.is_active() and editor.has_focus():
                # Wayland clipboard offers become available to this client
                # only after keyboard focus. A pre-map read is not a baseline.
                previous = clipboard.wait_for_text()
                status['sent'] = True
                worker = threading.Thread(target=send, daemon=True); worker.start()
        if status['reply']:
            if replied_at is None:
                replied_at = time.monotonic()
            if time.monotonic() - replied_at < 0.1:
                return True  # Allow the restored Wayland data offer to arrive.
            buffer = editor.get_buffer()
            status['text_matches'] = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == TEXT
            # The daemon ACK follows clipboard restoration. Only equality is
            # exported; neither clipboard contents nor hashes leave this test.
            status['clipboard_preserved'] = clipboard.wait_for_text() == previous
            window.destroy(); return False
        if time.monotonic() - started > 10:
            window.destroy(); return False
        return True

    window.connect('destroy', Gtk.main_quit)
    editor.add_events(Gdk.EventMask.POINTER_MOTION_MASK)
    editor.connect('motion-notify-event', motion)
    editor.connect('key-press-event', key)
    window.show_all(); editor.grab_focus()
    source = GLib.timeout_add(25, tick)
    try:
        Gtk.main()
    finally:
        if GLib.MainContext.default().find_source_by_id(source):
            GLib.source_remove(source)
        if worker:
            worker.join(timeout=6)
        if pointer:
            pointer.close()
    print(json.dumps(dict(status, windows_broker_requested=wine_environment is not None,
        windows_broker_tested=broker_diagnostic['responded'], broker_diagnostic=broker_diagnostic,
        owned_focus=focus, uu_controller_tested=False)))
    if not all(status.values()):
        raise RuntimeError('Owned native text fixture failed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('--trial-state', type=Path)
    parser.add_argument('--focus-pointer', action='store_true', help='Focus only a verified owned Wayland editor')
    args = parser.parse_args()
    run(args.socket, args.trial_state, args.focus_pointer)
