#!/usr/bin/python3
"""Same-UID native display RPC and independent rollback timer. No input/text."""
import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import struct
import time
from native_display_config import MutterDisplay
from native_display_guardian import DisplayGuardian
from native_runtime_state import read_private, notify


def run(config_path):
    config = read_private(config_path)
    parent = Path(config['state_parent'])
    info = parent.lstat()
    if not parent.is_absolute() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Invalid private display service directory')
    endpoint = parent / 'display.sock'
    lock = os.open(parent / 'display.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    listener = None
    bound = None
    previous = {}
    stopping = False
    guardian = None

    def stop(*unused):
        nonlocal stopping
        stopping = True

    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if endpoint.exists() or endpoint.is_symlink():
            old = endpoint.lstat()
            if not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.getuid() or old.st_mode & 0o077:
                raise ValueError('Invalid existing display endpoint')
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as probe:
                probe.settimeout(1)
                try:
                    probe.connect(str(endpoint))
                except OSError as error:
                    if error.errno != errno.ECONNREFUSED:
                        raise
                else:
                    raise RuntimeError('Another display listener is alive')
            current = endpoint.lstat()
            if (current.st_dev, current.st_ino) != (old.st_dev, old.st_ino):
                raise RuntimeError('Display endpoint changed during recovery')
            endpoint.unlink()
        guardian = DisplayGuardian(MutterDisplay(), parent / 'display-transaction.json')
        guardian.expire()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
        listener.bind(str(endpoint)); endpoint.chmod(0o600); bound = endpoint.lstat()
        listener.listen(4)
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, stop)
        print(json.dumps(dict(event='native_display_ready', same_uid_only=True, uu_controller_acceptance_verified=False)), flush=True)
        notify('READY=1\nSTATUS=Native display rollback guardian ready')
        heartbeat = 0
        last_expiry = None
        while not stopping:
            try:
                expired = guardian.expire()
                if expired and expired != last_expiry:
                    print(json.dumps(dict(event='display_deadline', result=expired)), flush=True)
                last_expiry = expired or last_expiry
            except Exception as error:
                if last_expiry != 'failed':
                    print(json.dumps(dict(event='display_rollback_failed', error_type=type(error).__name__)), flush=True)
                last_expiry = 'failed'
            if time.monotonic() >= heartbeat:
                notify('WATCHDOG=1'); heartbeat = time.monotonic() + 5
            if not select.select([listener], [], [], 0.25)[0]:
                continue
            with listener.accept()[0] as client:
                client.settimeout(0.5)
                _, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    continue
                op = 'invalid'
                try:
                    packet, _, flags, _ = client.recvmsg(2048)
                    if flags or not packet:
                        raise ValueError('Invalid bounded display packet')
                    request = json.loads(packet)
                    if isinstance(request, dict) and request.get('op') in ('inspect', 'verify', 'apply', 'confirm', 'rollback'):
                        op = request['op']
                    result = dict(ok=True, **guardian.request(request))
                except Exception as error:
                    result = dict(ok=False, error_type=type(error).__name__)
                encoded = json.dumps(result, allow_nan=False).encode()
                if len(encoded) > 65536:
                    encoded = b'{"ok":false,"error_type":"ReplyBoundExceeded"}'
                try:
                    client.sendall(encoded)
                except OSError:
                    pass  # Never replay a possibly completed display change.
                if op not in ('inspect', 'invalid'):
                    print(json.dumps(dict(event='display_request', operation=op, accepted=result['ok'])), flush=True)
    finally:
        notify('STOPPING=1')
        if guardian is not None and guardian.transaction.pending is not None:
            guardian.deadline = 0
            try:
                guardian.expire()
            except Exception:
                pass  # Durable pending record remains for the next instance.
        if listener is not None:
            listener.close()
        if bound:
            try:
                info = endpoint.lstat()
                if (info.st_dev, info.st_ino) == (bound.st_dev, bound.st_ino):
                    endpoint.unlink()
            except FileNotFoundError:
                pass
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        os.close(lock)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    try:
        run(parser.parse_args().config)
    except Exception as error:
        print(json.dumps(dict(event='native_display_failed', error_type=type(error).__name__)), flush=True)
        raise SystemExit(1)
