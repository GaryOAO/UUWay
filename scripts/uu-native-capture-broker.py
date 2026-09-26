#!/usr/bin/python3
"""On-demand authorized Wayland GPU source. No UU login, RDP or live DLL edits.

Each accepted same-UID connection owns one producer and one allocation lifetime.
Never reuses an inherited consumer FD or old frames across UU recreation.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('capture_runtime_state', ROOT / 'scripts/native_runtime_state.py')
state_tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(state_tools)


class DisplayReset:
    """One request retires at most its old allocation, never its successor."""
    def __init__(self):
        self.pending = None
        self.last_request = 0

    def request(self, path, generation, active, now):
        value = state_tools.read_private(path, max_bytes=512)
        if (not isinstance(value, dict) or
                set(value) != {'version', 'width', 'height', 'after_ns', 'requested_ns', 'require_fresh'} or
                any(type(value[k]) is not int for k in ('version', 'width', 'height', 'after_ns', 'requested_ns')) or
                value['version'] != 1 or type(value['require_fresh']) is not bool or
                any(not 2 <= value[k] <= 4096 or value[k] % 2 for k in ('width', 'height')) or
                not 0 < value['after_ns'] <= value['requested_ns'] <= now or
                now - value['requested_ns'] > 5_000_000_000):
            raise ValueError('Invalid display reset request')
        if value['requested_ns'] <= self.last_request:
            return
        self.last_request = value['requested_ns']
        self.pending = (value, generation) if active else None

    def decision(self, generation, started_ns, accepted, now):
        if self.pending is None:
            return None
        value, target_generation = self.pending
        fresh = started_ns >= value['after_ns']
        if generation != target_generation:
            result = 'superseded'
        elif (accepted is not None and all(accepted[k] == value[k] for k in ('width', 'height')) and
              (not value['require_fresh'] or fresh)):
            result = 'already_recovered'
        elif now - value['requested_ns'] >= (12 if fresh and accepted is None else 2) * 1_000_000_000:
            result = 'reset'
        else:
            return None
        self.pending = None
        return result


class FirstFrameStatus:
    """One bounded status per producer; never reads the producer's raw logs."""
    def __init__(self):
        self.reader, self.writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
        self.reader.setblocking(False)

    def poll(self):
        try:
            packet, _, flags, _ = self.reader.recvmsg(32)
        except BlockingIOError:
            return None
        if flags or len(packet) != 32:
            raise ValueError('Invalid bounded capture status')
        magic, version, width, height, sequence, accepted_ns = struct.unpack('<4I2Q', packet)
        age = time.monotonic_ns() - accepted_ns
        if (magic != 0x53525555 or version != 1 or sequence != 1 or
                not 2 <= width <= 8192 or not 2 <= height <= 8192 or not 0 <= age <= 5_000_000_000):
            raise ValueError('Invalid or stale first-frame status')
        return dict(width=width, height=height, sequence=sequence, accepted_ns=accepted_ns)

    def close(self):
        self.reader.close()
        self.writer.close()


def process_start_ticks(pid):
    # The comm field may contain spaces and ')'. Never export it or argv/env.
    value = (Path('/proc') / str(pid) / 'stat').read_text()
    return int(value[value.rindex(')') + 2:].split()[19])


class CursorState:
    """Bounded private snapshot, owned by this broker and never an account file."""
    SIZE = 64 + 384 * 384 * 4

    def __init__(self, path, video_embedded=False, video_composited=False):
        if video_embedded and video_composited:
            raise ValueError('Conflicting cursor video policies')
        validate_endpoint(path)
        self.path = path
        self.video_embedded = video_embedded
        self.video_composited = video_composited
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        self.identity = os.fstat(self.fd)
        try:
            os.ftruncate(self.fd, self.SIZE)
            self.reset(0)
        except BaseException:
            self.close()
            raise

    def reset(self, generation):
        # Caller has reaped the previous writer. Keep readers from accepting a
        # partially reset header, including after a writer died mid-commit.
        current = os.pread(self.fd, 4, 8)
        sequence = ((struct.unpack('<I', current)[0] | 1) + 2) & 0xffffffff
        if os.pwrite(self.fd, struct.pack('<I', sequence), 8) != 4:
            raise RuntimeError('Cursor reset failed')
        header = struct.pack('<16I', 0x43525555, 1, sequence, generation,
                             *([0] * 9), 2 if self.video_composited else int(self.video_embedded), 0, 0)
        if os.pwrite(self.fd, header, 0) != len(header) or os.pwrite(
                self.fd, struct.pack('<I', (sequence + 1) & 0xffffffff), 8) != 4:
            raise RuntimeError('Cursor reset failed')

    def close(self):
        try:
            self.reset(0)
        finally:
            os.close(self.fd)
            try:
                current = self.path.lstat()
                if (current.st_dev, current.st_ino) == (self.identity.st_dev, self.identity.st_ino):
                    self.path.unlink()
            except FileNotFoundError:
                pass


def event(name, **values):
    print(json.dumps(dict(event=name, **values)), flush=True)


def validate_endpoint(path):
    if not path.is_absolute() or not 1 < len(os.fsencode(path)) < 108:
        raise ValueError('Expected a bounded absolute Unix socket path')
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Socket parent must be a real owner-only directory')
    if path.exists() or path.is_symlink():
        raise ValueError('Refusing to replace any existing endpoint')


def stop_producer(process):
    if process is None:
        return
    # The Portal wrapper can exit while its native GPU child is still alive.
    # Every producer is started in its own session: stop that owned group even
    # when the wrapper's return code is already available.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # Reaping the wrapper alone does not prove its child exited. SIGKILL
        # closes the bounded allocation lifetime for any remaining descendant.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def cleanup(producer, connection, log, listener, path, bound, previous):
    """Attempt every owned cleanup even when one operation fails; no raw logs."""
    failures = []

    def attempt(name, operation):
        try:
            operation()
        except Exception:
            failures.append(name)

    attempt('stop_producer', lambda: stop_producer(producer))
    for name, resource in (('connection', connection), ('log', log), ('listener', listener)):
        if resource is not None:
            attempt('close_' + name, resource.close)

    def remove_endpoint():
        try:
            current = path.lstat()
            if bound and (current.st_dev, current.st_ino) == (bound.st_dev, bound.st_ino):
                path.unlink()
        except FileNotFoundError:
            pass

    attempt('remove_endpoint', remove_endpoint)
    for sig, handler in previous.items():
        attempt('restore_signal', lambda sig=sig, handler=handler: signal.signal(sig, handler))
    if failures:
        event('cleanup_failed', operations=failures)
        raise RuntimeError('Native capture broker cleanup incomplete')


def producer_failure(log):
    """Only our fixed scalar failure record, never raw diagnostic text."""
    if log is None:
        return None
    size = os.fstat(log.fileno()).st_size
    data = os.pread(log.fileno(), min(size, 8192), max(0, size - 8192))
    result = None
    for line in data.splitlines():
        prefix = b'UURB_GPU_FAILURE '
        if not line.startswith(prefix) or len(line) > 256:
            continue
        try:
            value = json.loads(line[len(prefix):])
            if (set(value) == {'stage', 'sequence', 'wait_ms'} and
                    value['stage'] in ('send', 'receive') and
                    type(value['sequence']) is int and 1 <= value['sequence'] <= (1 << 64) - 1 and
                    type(value['wait_ms']) is int and -1 <= value['wait_ms'] <= 60000):
                result = value
        except (ValueError, TypeError):
            continue
    return result


def producer_command(restore_state, channel, duration, cursor_mode, cursor_fd=None, status_fd=None, capture_binary=None):
    if cursor_mode not in ('embedded', 'hidden', 'metadata', 'composited'):
        raise ValueError('Unsupported capture cursor policy')
    if (cursor_mode in ('metadata', 'composited')) != (cursor_fd is not None):
        raise ValueError('Metadata mode requires its private cursor channel')
    command = ['/usr/bin/python3', str(ROOT / 'scripts/probe-wayland-portal.py'),
            '--restore-state', str(restore_state), '--authorization-timeout', '10',
            '--gpu-relay-fd', str(channel), '--duration-seconds', str(duration),
            '--cursor-mode', cursor_mode]
    if cursor_fd is not None:
        # This native trial is scoped to Mutter DisplayConfig / one monitor.
        command.extend(['--cursor-backend', 'mutter'])
        command.extend(['--cursor-state-fd', str(cursor_fd)])
    if status_fd is not None:
        if status_fd < 3 or status_fd in (channel, cursor_fd):
            raise ValueError('Capture status requires a separate descriptor')
        command.extend(['--capture-status-fd', str(status_fd)])
    if capture_binary is not None:
        if not capture_binary.is_absolute():
            raise ValueError('Expected an absolute version-pinned capture binary')
        command.extend(['--capture-binary', str(capture_binary)])
    return command


def run(path, restore_state, duration=3600, cursor_mode='embedded', cursor_path=None, capture_binary=None,
        display_reset_state=None):
    validate_endpoint(path)
    if not 0 <= duration <= 3600:
        raise ValueError('Capture lifetime must be 0 (supervised) or from 1 to 3600 seconds')
    producer_command(restore_state, 3, duration, cursor_mode,
                     4 if cursor_path is not None and cursor_mode in ('metadata', 'composited') else None, capture_binary=capture_binary)
    info = restore_state.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('An existing private authorized restore-state file is required')
    stopping = False
    reset_pending = False
    display_reset = DisplayReset()
    accepted_status = None
    started_ns = 0

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True

    def request_reset(signum, frame):
        nonlocal reset_pending
        reset_pending = True  # Cleanup belongs to the normal loop, not a signal handler.

    previous = {s: signal.signal(s, request_stop) for s in (signal.SIGTERM, signal.SIGINT)}
    previous[signal.SIGUSR1] = signal.signal(signal.SIGUSR1, request_reset)
    producer = connection = log = None
    generation = 0
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
    bound = None
    cursor = None
    status_channel = None
    peer_pid = peer_start = None
    try:
        if cursor_path is not None:
            cursor = CursorState(cursor_path, video_embedded=cursor_mode == 'embedded',
                                 video_composited=cursor_mode == 'composited')
        listener.bind(str(path))
        os.chmod(path, 0o600)
        bound = path.lstat()
        listener.listen(2)
        listener.setblocking(False)
        event('ready')
        while not stopping:
            resetting = reset_pending
            reset_pending = False
            if resetting and display_reset_state is not None:
                resetting = False
                try:
                    display_reset.request(display_reset_state, generation, producer is not None, time.monotonic_ns())
                except (OSError, ValueError):
                    event('display_reset_request_rejected')
            if producer is not None:
                # Drain the actual first GPU ACK before deciding to invalidate.
                # A delayed supervisor observation can arrive after UU has
                # already opened the correct-size replacement allocation.
                if status_channel:
                    try:
                        status = status_channel.poll()
                        if status is not None:
                            if process_start_ticks(peer_pid) != peer_start:
                                raise ValueError('Capture consumer identity changed')
                            accepted_status = status
                            event('capture_frame_accepted', generation=generation, peer_pid=peer_pid,
                                  peer_start_ticks=peer_start, **status)
                            status_channel.close()
                            status_channel = None
                    except (OSError, ValueError):
                        event('capture_status_unavailable', generation=generation)
                        status_channel.close()
                        status_channel = None
                decision = display_reset.decision(generation, started_ns, accepted_status, time.monotonic_ns())
                if decision:
                    event('display_reset_decision', generation=generation, result=decision)
                    resetting = resetting or decision == 'reset'
                poller = select.poll()
                poller.register(connection, select.POLLHUP | select.POLLERR | select.POLLRDHUP)
                peer_gone = bool(poller.poll(0))
                code = producer.poll()
                if code is not None or peer_gone or resetting:
                    stop_producer(producer)
                    detail = producer_failure(log)
                    if detail:
                        event('capture_gpu_failure', generation=generation, **detail)
                    mismatch = state_tools.nvidia_driver_mismatch() if accepted_status is None else None
                    if mismatch:
                        # UU then shows only its own Wine screen; a reboot loads the new module.
                        event('gpu_driver_mismatch', generation=generation, **mismatch)
                    if cursor:
                        cursor.reset(0)
                    event('capture_ended', generation=generation, peer_disconnected=peer_gone,
                          producer_exit=producer.returncode, display_reset=resetting)
                    connection.close(); log.close()
                    producer = connection = log = None
                    if status_channel:
                        status_channel.close()
                        status_channel = None
            readable, _, _ = select.select([listener], [], [], 0.25)
            if not readable:
                continue
            candidate, _ = listener.accept()
            candidate_pid, uid, _ = struct.unpack('3i', candidate.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.geteuid() or producer is not None:
                candidate.close()
                event('connection_rejected', busy=producer is not None)
                continue
            try:
                peer_start = process_start_ticks(candidate_pid)
            except (OSError, ValueError):
                candidate.close()
                continue
            peer_pid = candidate_pid
            connection = candidate
            # A reset received while select() was idle belongs to the old
            # (absent) generation, not the consumer we are about to start.
            reset_pending = False
            generation += 1
            started_ns = time.monotonic_ns()
            accepted_status = None
            if cursor:
                cursor.reset(generation)
            log = tempfile.TemporaryFile()
            status_channel = FirstFrameStatus()
            cursor_fd = cursor.fd if cursor and cursor_mode in ('metadata', 'composited') else None
            inherited = (connection.fileno(), status_channel.writer.fileno()) + ((cursor_fd,) if cursor_fd is not None else ())
            producer = subprocess.Popen(producer_command(restore_state, connection.fileno(), duration, cursor_mode,
                cursor_fd, status_channel.writer.fileno(), capture_binary), pass_fds=inherited,
                stdout=log, stderr=log, start_new_session=True)
            status_channel.writer.close()
            event('capture_started', generation=generation, cursor_mode=cursor_mode)
    finally:
        try:
            cleanup(producer, connection, log, listener, path, bound, previous)
        finally:
            if status_channel:
                status_channel.close()
            if cursor:
                cursor.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('--restore-state', type=Path, required=True)
    parser.add_argument('--duration-seconds', type=int, default=3600)
    parser.add_argument('--cursor-mode', choices=['embedded', 'hidden', 'metadata', 'composited'], default='embedded')
    parser.add_argument('--cursor-state', type=Path)
    parser.add_argument('--capture-binary', type=Path)
    parser.add_argument('--display-reset-state', type=Path)
    args = parser.parse_args()
    run(args.socket, args.restore_state, args.duration_seconds, args.cursor_mode, args.cursor_state, args.capture_binary,
        args.display_reset_state)
