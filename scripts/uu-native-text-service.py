#!/usr/bin/python3
"""Supervise the owned text daemon, adopting a live instance without restarting.

Adoption preserves its in-memory clipboard ownership. pidfds scope all signals
to the exact validated process. Subsequent launches belong to this user unit.
Process recovery cannot recover arbitrary clipboard bytes after a crash.
"""
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
import subprocess
import time
from native_runtime_state import read_private, notify

DAEMON = Path(__file__).with_name('uu-native-text.py').resolve()


def live_daemon(endpoint, restore_state, reclaim_stale=True):
    try:
        info = endpoint.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Invalid private text endpoint')
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC) as peer:
        peer.settimeout(1)
        try:
            peer.connect(str(endpoint))
        except OSError as error:
            if error.errno != errno.ECONNREFUSED:
                raise
            if not reclaim_stale:
                return None  # Child may be between bind() and listen().
            current = endpoint.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise RuntimeError('Text endpoint changed during stale check')
            # Connection refused, not a timeout; the same owned socket has no
            # listener. The service lock prevents another supervisor racing.
            endpoint.unlink()
            return None
        pid, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid() or pid <= 1:
            raise ValueError('Unexpected text endpoint peer')
        pidfd = os.pidfd_open(pid, 0)
        try:
            proc = Path('/proc') / str(pid)
            command = (proc / 'cmdline').read_bytes().split(b'\0')
            command = [os.fsdecode(part) for part in command if part]
            if len(command) not in (6, 8) or Path(os.readlink(proc / 'exe')).name not in ('python3', 'python3.12'):
                raise ValueError('Unexpected text daemon executable')
            script = Path(command[1])
            if not script.is_absolute():
                script = Path(os.readlink(proc / 'cwd')) / script
            if script.resolve() != DAEMON:
                raise ValueError('Unexpected text daemon source')
            pairs = command[2:]
            options = dict(zip(pairs[::2], pairs[1::2]))
            if len(options) != len(pairs) // 2 or set(options) - {'--socket', '--restore-state', '--authorization-timeout'}:
                raise ValueError('Unexpected text daemon arguments')
            if options.get('--socket') != str(endpoint) or options.get('--restore-state') != str(restore_state):
                raise ValueError('Text daemon configuration mismatch')
            signal.pidfd_send_signal(pidfd, 0)
            return pid, pidfd
        except BaseException:
            os.close(pidfd)
            raise


def run(config_path):
    config = read_private(config_path)
    endpoint, restore_state = Path(config['text_socket']), Path(config['restore_state']).with_name('text-portal.json')
    if not endpoint.is_absolute() or not restore_state.is_absolute():
        raise ValueError('Expected absolute text service paths')
    info = endpoint.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Expected private text service directory')
    lock = os.open(endpoint.parent / 'text-supervisor.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    child = None
    identity = None
    stopping = False
    previous = {}

    def request_stop(*unused):
        nonlocal stopping
        stopping = True

    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, request_stop)
        identity = live_daemon(endpoint, restore_state)
        adopted = identity is not None
        if identity is None:
            env = dict(os.environ)
            for name in ('NOTIFY_SOCKET', 'WATCHDOG_PID', 'WATCHDOG_USEC'):
                env.pop(name, None)
            child = subprocess.Popen(['/usr/bin/python3', str(DAEMON), '--socket', str(endpoint),
                '--restore-state', str(restore_state), '--authorization-timeout', '120'],
                stdin=subprocess.DEVNULL, env=env)
            deadline = time.monotonic() + 125
            while identity is None and not stopping:
                code = child.poll()
                if code is not None:
                    print(json.dumps(dict(event='native_text_child_exit_before_ready', code=code)), flush=True)
                    raise RuntimeError('Text service did not become ready')
                if time.monotonic() >= deadline:
                    print(json.dumps(dict(event='native_text_child_ready_timeout')), flush=True)
                    raise RuntimeError('Text service did not become ready')
                identity = live_daemon(endpoint, restore_state, reclaim_stale=False)
                if identity is None:
                    time.sleep(0.1)
            if identity is not None and identity[0] != child.pid:
                os.close(identity[1]); identity = None
                raise RuntimeError('Another text daemon won the startup race')
        if stopping:
            return
        print(json.dumps(dict(event='native_text_supervised', adopted_existing=adopted, pid=identity[0],
                              clipboard_handover_performed=False)), flush=True)
        notify('READY=1\nSTATUS=Owned text daemon supervised; no clipboard content copied')
        heartbeat = 0
        while not stopping:
            if select.select([identity[1]], [], [], 0.25)[0]:
                raise RuntimeError('Owned text daemon exited')
            if time.monotonic() >= heartbeat:
                notify('WATCHDOG=1')
                heartbeat = time.monotonic() + 5
    finally:
        notify('STOPPING=1')
        if identity is not None:
            try:
                if stopping:
                    try:
                        signal.pidfd_send_signal(identity[1], signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    if not select.select([identity[1]], [], [], 15)[0]:
                        signal.pidfd_send_signal(identity[1], signal.SIGKILL)
            finally:
                os.close(identity[1])
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait(timeout=5)
            else:
                child.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        os.close(lock)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.config)
    except Exception as error:
        print(json.dumps(dict(event='native_text_supervision_failed', error_type=type(error).__name__)), flush=True)
        raise SystemExit(1)
