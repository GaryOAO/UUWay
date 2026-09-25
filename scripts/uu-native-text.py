#!/usr/bin/python3
"""Local authorized text daemon, kept independent of UU/video reconnections.

SOCK_SEQPACKET messages contain bounded UTF-8, never shell commands. No text,
clipboard payload, authorization token or vendor log is printed or saved.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
from gi.repository import GLib
from native_text_portal import TextPortal

MAGIC = 0x54525555
VERSION = 1
HEADER = struct.Struct('<4I')
MAX_TEXT = 8192


def decode_request(packet):
    if len(packet) < HEADER.size:
        raise ValueError('Short text request')
    magic, version, sequence, size = HEADER.unpack_from(packet)
    if magic != MAGIC or version not in (1, 2) or not sequence or not 0 < size <= MAX_TEXT + (4 if version == 2 else 0) or len(packet) != HEADER.size + size:
        raise ValueError('Invalid text request')
    payload = packet[HEADER.size:]
    delete_before = 0
    if version == 2:
        if len(payload) <= 4:
            raise ValueError('Missing revision replacement')
        delete_before = struct.unpack_from('<I', payload)[0]
        if not 1 <= delete_before <= 2048:
            raise ValueError('Invalid revision deletion bound')
        payload = payload[4:]
    text = payload.decode('utf-8', errors='strict')
    if not text or len(text) > 2048 or '\0' in text:
        raise ValueError('Invalid bounded text')
    return sequence, text, delete_before


def private_parent(path):
    if not path.is_absolute():
        raise ValueError('Expected an absolute private endpoint path')
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
        raise ValueError('Expected a real owner-only parent directory')


def run(endpoint, restore_state, authorization_timeout):
    private_parent(endpoint); private_parent(restore_state)
    if endpoint.exists() or endpoint.is_symlink() or len(os.fsencode(endpoint)) >= 108:
        raise ValueError('Refusing an existing or overlong endpoint')
    if endpoint == restore_state or not 5 <= authorization_timeout <= 120:
        raise ValueError('Invalid text daemon configuration')
    portal = TextPortal(restore_state, authorization_timeout)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
    loop = GLib.MainLoop()
    clients = {}
    bound = None
    listening = None
    previous = {}

    def discard(connection):
        watch = clients.pop(connection, None)
        if watch and GLib.MainContext.default().find_source_by_id(watch):
            GLib.source_remove(watch)
        connection.close()

    def receive(connection, condition):
        # Remove the watch before Portal nested dispatch; one request only.
        watch = clients.pop(connection, None)
        if watch and GLib.MainContext.default().find_source_by_id(watch):
            GLib.source_remove(watch)
        sequence = 0
        error = 0x2001
        try:
            packet, _, flags, _ = connection.recvmsg(HEADER.size + MAX_TEXT + 4)
            if flags:
                raise ValueError('Truncated text packet')
            sequence, text, delete_before = decode_request(packet)
            error = 0x2003
            portal.commit(text, delete_before)
            error = 0
        except (ValueError, UnicodeError, RuntimeError, OSError, GLib.Error):
            pass
        finally:
            try:
                connection.send(HEADER.pack(MAGIC, VERSION, sequence, error))
            except OSError:
                pass
            connection.close()
        print(json.dumps({'event': 'text_transaction', 'accepted': error == 0, 'error': error,
                          'phase': portal.phase, 'insertion_witnessed': portal.witnessed}), flush=True)
        return False

    def accept(source, condition):
        connection, _ = listener.accept()
        _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.geteuid() or clients or portal.busy:
            connection.close()
            return True
        connection.setblocking(False)
        clients[connection] = GLib.io_add_watch(connection.fileno(), GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR,
            lambda fd, flags: receive(connection, flags))
        GLib.timeout_add(1000, lambda: (discard(connection), False)[1] if connection in clients else False)
        return True

    try:
        print(json.dumps({'event': 'text_authorization_start', 'screen_capture_requested': False}), flush=True)
        portal.start()
        listener.bind(str(endpoint)); os.chmod(endpoint, 0o600)
        bound = endpoint.lstat()
        listener.listen(2); listener.setblocking(False)
        listening = GLib.io_add_watch(listener.fileno(), GLib.IO_IN, accept)
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *unused: loop.quit())
        print(json.dumps({'event': 'text_ready', 'public_portal': True, 'screen_capture_requested': False,
                          'native_keyboard_granted': True, 'clipboard_granted': True,
                          'witnessed_revisions_available': portal.revisions is not None}), flush=True)
        GLib.timeout_add(250, lambda: (loop.quit(), False)[1] if portal.closed else True)
        loop.run()
    finally:
        for connection in list(clients):
            discard(connection)
        if listening and GLib.MainContext.default().find_source_by_id(listening):
            GLib.source_remove(listening)
        listener.close()
        portal.close()
        if bound:
            try:
                current = endpoint.lstat()
                if (current.st_dev, current.st_ino) == (bound.st_dev, bound.st_ino):
                    endpoint.unlink()
            except FileNotFoundError:
                pass
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('--restore-state', type=Path, required=True)
    parser.add_argument('--authorization-timeout', type=int, default=120)
    args = parser.parse_args()
    try:
        run(args.socket, args.restore_state, args.authorization_timeout)
    except Exception as error:
        known = {
            'Text Portal authorization timed out': 'authorization_timeout',
            'Text Portal authorization cancelled or denied': 'authorization_denied',
            'Expected only authorized keyboard and clipboard, no capture': 'unexpected_grant',
            'Public keyboard/clipboard Portal interfaces unavailable': 'interfaces_unavailable',
        }
        print(json.dumps({'event': 'text_failed', 'error_type': type(error).__name__,
                          'reason': known.get(str(error), 'other')}), flush=True)
        raise SystemExit(1)
