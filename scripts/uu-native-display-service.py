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
from native_display_guardian import DisplayRouter
from native_runtime_state import read_private, notify
from native_virtual_display import VirtualDisplays, VirtualSelectedDisplay, recover_layout


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
    virtual = None

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
        recover_layout(config)
        backend = MutterDisplay()
        manifest = read_private(Path(config['bundle']) / 'manifest.json')
        virtual = VirtualDisplays(backend, Path(config['bundle']) / 'capture/uu-pipewire-native-probe',
                                  enabled=manifest.get('contract', {}).get('virtual_display_emulation') is True)
        virtual.recovery_path = parent / 'virtual-layout-recovery.json'
        guardian = DisplayRouter(backend, parent,
            selected_factory=lambda connector: VirtualSelectedDisplay(connector, virtual))
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
        inspect_failure = None
        inspect_failure_at = 0
        while not stopping:
            virtual.pump()
            virtual.poll_lifetime(allow_idle=not any(item.transaction.pending is not None
                                                    for item in guardian.guardians.values()))
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
                pid, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    continue
                op = 'invalid'
                try:
                    # 1,024 binary bytes can occupy over 4 KiB as a JSON byte
                    # array. Keep this bounded without truncating mode lists.
                    packet, _, flags, _ = client.recvmsg(8192)
                    if flags or not packet:
                        raise ValueError('Invalid bounded display packet')
                    request = json.loads(packet)
                    if isinstance(request, dict) and request.get('op') in ('inspect', 'verify', 'apply', 'confirm', 'rollback',
                            'virtual-control', 'virtual-inspect', 'virtual-create', 'virtual-remove', 'virtual-release', 'virtual-session', 'virtual-output'):
                        op = request['op']
                    if op.startswith('virtual-'):
                        if type(request.get('version')) is not int or request['version'] != 1:
                            raise ValueError('Invalid virtual display request version')
                        # Virtual output ownership is independent of a pending
                        # physical mode confirmation. UU probes and removes an
                        # IDD output during that window, so never serialize
                        # this lifecycle against the physical transaction.
                        if op == 'virtual-control' and set(request) == {'version', 'op', 'code', 'payload'}:
                            value = virtual.ioctl(request['code'], request['payload'])
                            if request['code'] == 0x30c004:
                                virtual.bind_screen_owner(struct.unpack_from('<I', bytes(request['payload']))[0], pid)
                        elif op == 'virtual-inspect' and set(request) == {'version', 'op'}:
                            value = virtual.status()
                        elif op == 'virtual-create' and set(request) == {'version', 'op', 'slot', 'modes'}:
                            screen = virtual.create(request['slot'], request['modes'])
                            value = dict(created=True, slot=screen['slot'], connector=screen['connector'])
                        elif op == 'virtual-remove' and set(request) == {'version', 'op', 'slot'}:
                            value = virtual.remove(request['slot'])
                        elif op == 'virtual-release' and set(request) == {'version', 'op'}:
                            value = virtual.release_all()
                        elif op == 'virtual-session' and set(request) == {'version', 'op', 'tracks'}:
                            value = virtual.session_state(pid, request['tracks'])
                        elif op == 'virtual-output' and set(request) == {'version', 'op', 'connector', 'enabled'}:
                            value = virtual.set_output(request['connector'], request['enabled'])
                        else:
                            raise ValueError('Unexpected virtual display fields')
                        result = dict(ok=True, **value)
                    else:
                        result = dict(ok=True, **guardian.request(request))
                        if op == 'inspect':
                            result['virtual_displays'] = virtual.status()
                            inspect_failure = None
                except Exception as error:
                    result = dict(ok=False, error_type=type(error).__name__)
                    if op == 'inspect':
                        failure = (type(error).__name__, str(error)[:200])
                        now = time.monotonic()
                        if failure != inspect_failure or now - inspect_failure_at >= 5:
                            print(json.dumps(dict(event='display_inspect_failed', error_type=failure[0],
                                                  reason=failure[1])), flush=True)
                            inspect_failure, inspect_failure_at = failure, now
                if op == 'virtual-control' and request.get('code') != 0:
                    payload = request.get('payload') if isinstance(request, dict) else None
                    first = sum(int(payload[index]) << (8 * index) for index in range(min(4, len(payload)))) \
                        if isinstance(payload, list) else None
                    second = sum(int(payload[index]) << (8 * (index - 4)) for index in range(4, min(8, len(payload)))) \
                        if isinstance(payload, list) and len(payload) > 4 else None
                    print(json.dumps(dict(event='virtual_control', code=request.get('code'),
                                          bytes=len(payload) if isinstance(payload, list) else None,
                                          first=first, second=second,
                                          accepted=result.get('ok') is True,
                                          error_type=result.get('error_type'))), flush=True)
                encoded = json.dumps(result, allow_nan=False).encode()
                if len(encoded) > 65536:
                    encoded = b'{"ok":false,"error_type":"ReplyBoundExceeded"}'
                try:
                    client.sendall(encoded)
                except OSError:
                    pass  # Never replay a possibly completed display change.
                if op not in ('inspect', 'virtual-inspect', 'virtual-session', 'invalid') and not (op == 'virtual-control' and request.get('code') == 0):
                    print(json.dumps(dict(event='display_request', operation=op, accepted=result['ok'])), flush=True)
    finally:
        notify('STOPPING=1')
        if guardian is not None:
            try:
                guardian.stop()
            except Exception:
                pass  # Durable pending record remains for the next instance.
        if virtual is not None:
            virtual.close()
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
    parser.add_argument('--recover', action='store_true')
    try:
        arguments = parser.parse_args()
        if arguments.recover:
            recover_layout(read_private(arguments.config))
        else:
            run(arguments.config)
    except Exception as error:
        print(json.dumps(dict(event='native_display_failed', error_type=type(error).__name__)), flush=True)
        raise SystemExit(1)
