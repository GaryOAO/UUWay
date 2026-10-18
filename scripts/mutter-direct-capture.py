#!/usr/bin/python3
"""Capture one compositor output through Mutter's ScreenCast D-Bus.

The installed desktop service selects the same explicit connector that its
display API exposes to UU. Initial setup still checks screen sharing through
the portal; reconnects and mode changes use the selected Mutter source so an
expired, one-use portal restore token cannot strand a remote connection at an
unattended consent dialog. The legacy single-output broker retains its portal
path. The capture worker receives a private PipeWire connection.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

from gi.repository import Gio, GLib

SC = 'org.gnome.Mutter.ScreenCast'
ROOT = '/org/gnome/Mutter/ScreenCast'
# Mutter's ScreenCast enum is 0=hidden, 1=embedded, 2=metadata; the portal's
# public values (1/2/4) are different and must not be forwarded directly.
CURSOR_MODES = {'hidden': 0, 'embedded': 1, 'metadata': 2, 'composited': 2}


def call(bus, path, interface, method, args=None):
    value = bus.call_sync(SC, path, interface, method, args, None,
                          Gio.DBusCallFlags.NO_AUTO_START, 5000, None)
    return value.unpack() if value else ()


def fail(message):
    print('mutter-direct-capture: ' + message, file=sys.stderr, flush=True)
    return 1


def producer_environment(args, base):
    environment = dict(base, UURB_CAPTURE_DURATION_SECONDS=str(args.duration_seconds))
    for key in ('UURB_CURSOR_STATE_FD', 'UURB_CAPTURE_STATUS_FD',
                'UURB_CURSOR_METADATA_BACKEND', 'UURB_CURSOR_COMPOSITE'):
        environment.pop(key, None)
    if args.cursor_mode in ('metadata', 'composited'):
        # Mutter uses cursor id=0 for "hidden", distinct from no update.
        environment['UURB_CURSOR_METADATA_BACKEND'] = 'mutter'
        environment['UURB_CURSOR_STATE_FD'] = str(args.cursor_state_fd)
    if args.cursor_mode == 'composited':
        environment['UURB_CURSOR_COMPOSITE'] = '1'
    if args.capture_status_fd >= 3:
        environment['UURB_CAPTURE_STATUS_FD'] = str(args.capture_status_fd)
    return environment


def stop_child(child):
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=2)


def run(args):
    if args.cursor_mode not in CURSOR_MODES:
        return fail('unsupported cursor mode')
    if args.cursor_mode in ('metadata', 'composited') and args.cursor_state_fd < 3:
        return fail('metadata cursor mode requires a state descriptor')
    if not args.capture_binary.is_absolute() or args.gpu_relay_fd < 3 or not 0 <= args.duration_seconds <= 3600:
        return fail('invalid capture binary, descriptor or duration')
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    session = subscription = child = None
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.terminate()

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        session = call(bus, ROOT, SC, 'CreateSession', GLib.Variant('(a{sv})', ({},)))[0]
        stream = call(bus, session, SC + '.Session', 'RecordMonitor',
                      GLib.Variant('(sa{sv})', (args.connector,
                          {'cursor-mode': GLib.Variant('u', CURSOR_MODES[args.cursor_mode])})))[0]
        nodes = []
        subscription = bus.signal_subscribe(SC, SC + '.Stream', 'PipeWireStreamAdded', stream, None,
            Gio.DBusSignalFlags.NONE,
            lambda connection, sender, path, interface, signal, params: nodes.append(params.unpack()[0]))
        call(bus, session, SC + '.Session', 'Start')
        deadline = time.monotonic() + 10
        while not nodes and not stopping and time.monotonic() < deadline:
            context = GLib.MainContext.default()
            while context.pending():
                context.iteration(False)
            time.sleep(0.01)
        if stopping:
            return 0
        if len(nodes) != 1:
            return fail('no unambiguous PipeWire source')
        runtime = Path(os.environ['XDG_RUNTIME_DIR']).resolve() / 'pipewire-0'
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC) as remote:
            remote.connect(str(runtime))
            command = [str(args.capture_binary), str(remote.fileno()), str(nodes[0]),
                       '--gpu-relay', str(args.gpu_relay_fd)]
            inherited = [remote.fileno(), args.gpu_relay_fd]
            environment = producer_environment(args, os.environ)
            if args.cursor_state_fd >= 3:
                inherited.append(args.cursor_state_fd)
            if args.capture_status_fd >= 3:
                inherited.append(args.capture_status_fd)
            print('UURB_CAPTURE_CURSOR_POLICY ' + json.dumps(dict(connector=args.connector,
                  cursor_mode=args.cursor_mode, mutter_cursor_mode=CURSOR_MODES[args.cursor_mode],
                  gpu_composite=args.cursor_mode == 'composited')), file=sys.stderr, flush=True)
            if stopping:
                return 0
            child = subprocess.Popen(command, env=environment, pass_fds=tuple(inherited))
            if stopping:
                child.terminate()
            return child.wait(timeout=None if args.duration_seconds == 0 else args.duration_seconds + 5)
    finally:
        try:
            stop_child(child)
        finally:
            if session is not None:
                try:
                    call(bus, session, SC + '.Session', 'Stop')
                except Exception:
                    pass
            if subscription is not None:
                bus.signal_unsubscribe(subscription)
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connector', required=True)
    parser.add_argument('--gpu-relay-fd', type=int, required=True)
    parser.add_argument('--duration-seconds', type=int, default=0)
    parser.add_argument('--cursor-mode', choices=sorted(CURSOR_MODES), default='hidden')
    parser.add_argument('--cursor-state-fd', type=int, default=-1)
    parser.add_argument('--capture-status-fd', type=int, default=-1)
    parser.add_argument('--capture-binary', type=Path, required=True)
    arguments = parser.parse_args()
    try:
        raise SystemExit(run(arguments))
    except Exception as error:
        raise SystemExit(fail(type(error).__name__))
