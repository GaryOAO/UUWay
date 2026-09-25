#!/usr/bin/python3
"""Windows broker -> native input -> owned Wayland event-window acceptance.

Requires an already running input-enabled trial; never starts/stops the prefix
or injects a DLL into a vendor process. Test gestures only, no account access.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('trial', ROOT / 'scripts/uu-native-trial.py')
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


def run(directory):
    trial.private_directory(directory)
    journal = json.loads((directory / 'journal.json').read_text())
    if journal.get('native_input_requested') is not True:
        raise RuntimeError('Expected an existing native-input trial')
    prefix = Path(journal['prefix'])
    environment = None
    for proc in trial.prefix_processes(prefix):
        path = Path('/proc') / str(proc['pid'])
        try:
            if str(directory / 'bootstrap.exe') not in (path / 'maps').read_text():
                continue
            fields = dict(v.split(b'=', 1) for v in (path / 'environ').read_bytes().split(b'\0') if b'=' in v)
            environment = dict(os.environ, WINEPREFIX=str(prefix), WINEDEBUG='-all,err+module')
            for name in ('DISPLAY', 'XAUTHORITY'):
                environment[name] = fields[name.encode()].decode()
            break
        except (OSError, KeyError):
            continue
    if environment is None or trial.native_topology() != journal['initial_topology']:
        raise RuntimeError('Matching live trial/display was not found')
    if not any(v == dict(event='native_input_hook_ready', code=0) for v in trial.boot_events(directory / 'wine.log')):
        raise RuntimeError('The real UU input hook has not initialized')
    report = dict(windows_broker=True, motion_quarter=False, motion_center=False, click=False,
                  key_down=False, key_up=False, uu_remote_session_tested=False)
    state = dict(phase=0)
    window = Gtk.Window(title='UU Windows-to-Wayland input check — temporary')
    window.set_decorated(False); window.fullscreen()
    area = Gtk.DrawingArea(); area.set_can_focus(True)
    area.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_PRESS_MASK |
                   Gdk.EventMask.KEY_PRESS_MASK | Gdk.EventMask.KEY_RELEASE_MASK)
    window.add(area)

    def issue(*gesture):
        subprocess.run([trial.WINE, str(ROOT / 'build/native-presenter/uu-native-input-send.exe'), *map(str, gesture)],
            env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=True)

    def motion(widget, event):
        target = 0.25 if state['phase'] == 1 else 0.5
        if abs(event.x - area.get_allocated_width() * target) < 5 and abs(event.y - area.get_allocated_height() * target) < 5:
            report['motion_quarter' if state['phase'] == 1 else 'motion_center'] = True
        return True

    def button(widget, event):
        if event.button == 1:
            report['click'] = True; area.grab_focus()
        return True

    def key(widget, event):
        if event.keyval == Gdk.KEY_F8:
            report['key_down' if event.type == Gdk.EventType.KEY_PRESS else 'key_up'] = True
        return True

    def tick():
        try:
            if state['phase'] == 0 and area.get_allocated_width() > 100:
                state['phase'] = 1; issue('move', 16384, 16384)
            elif state['phase'] == 1 and report['motion_quarter']:
                state['phase'] = 2; issue('move', 32768, 32768)
            elif state['phase'] == 2 and report['motion_center']:
                state['phase'] = 3; issue('click')
            elif state['phase'] == 3 and report['click'] and window.is_active() and area.has_focus():
                state['phase'] = 4; issue('f8')
            elif state['phase'] == 4 and report['key_up']:
                issue('unsupported-text')
                report['unsupported_text_rejected'] = True
                window.destroy(); return False
        except (subprocess.SubprocessError, OSError):
            report['transport_error'] = True
            window.destroy(); return False
        return True

    area.connect('draw', lambda w, c: (c.set_source_rgb(0.1, 0.2, 0.15), c.paint(), False)[-1])
    area.connect('motion-notify-event', motion); area.connect('button-press-event', button)
    area.connect('key-press-event', key); area.connect('key-release-event', key)
    window.connect('destroy', Gtk.main_quit)
    window.show_all(); window.present()
    GLib.timeout_add(250, tick)
    GLib.timeout_add_seconds(12, lambda: (window.destroy(), False)[1])
    Gtk.main()
    report['passed'] = all(report.get(k) for k in ('motion_quarter', 'motion_center', 'click',
                                                 'key_down', 'key_up', 'unsupported_text_rejected'))
    print(json.dumps(report), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trial', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.trial.resolve()))
