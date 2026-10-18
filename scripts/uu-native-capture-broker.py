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


class DesktopCursor:
    """Merge monitor-local snapshots into USER32's one desktop-wide cursor.

    UU polls GetCursorInfo independently of DXGI and its video tracks. Keep
    the legacy file alive and publish the visible stream here, including its
    desktop origin. Source serials are local to each producer, so assign a
    new serial when switching sources, even if both say shape_serial=1.
    """
    HEADER = struct.Struct('<9I4i3I')

    def __init__(self, target):
        self.target = target
        self.source = None
        self.shape_key = None
        self.shape_serial = 0
        self.last = None
        self.reports = 0

    def update(self, sources):
        candidates = []
        unstable = False
        for index, (state, monitor) in sources.items():
            if state is None:
                continue
            raw = os.pread(state.fd, self.HEADER.size, 0)
            if len(raw) != self.HEADER.size:
                unstable = True
                continue
            header = list(self.HEADER.unpack(raw))
            if header[2] & 1:
                unstable = True
                continue
            if (header[0:2] != [0x43525555, 1] or not header[3] or
                    header[4] != 1 or header[5] not in (0, 1)):
                continue
            # Visibility is authoritative. A hidden cursor may legitimately
            # have no bitmap, but its position still belongs to this desktop.
            if header[5] and (not header[6] or not 1 <= header[7] <= 384 or not 1 <= header[8] <= 384 or
                    not 0 <= header[9] < header[7] or not 0 <= header[10] < header[8]):
                continue
            key = (index, header[3], header[6]) if header[5] else self.shape_key
            pixels = None
            if header[5] and key != self.shape_key:
                size = header[7] * header[8] * 4
                pixels = os.pread(state.fd, size, 64)
                if len(pixels) != size:
                    continue
            if os.pread(state.fd, self.HEADER.size, 0) != raw:
                unstable = True
                continue
            candidates.append((header[5], index == self.source, index, header, pixels, monitor, key))
        if not candidates:
            if unstable:
                return
            if self.last is not None:
                self.target.reset(1)
                self.last = self.shape_key = self.source = None
            return
        _, _, index, header, pixels, monitor, key = max(candidates, key=lambda item: item[:3])
        header[11] += monitor.get('x', 0)
        header[12] += monitor.get('y', 0)
        if not all(-(1 << 31) <= value < (1 << 31) for value in header[11:13]):
            return
        if key != self.shape_key:
            self.shape_serial = (self.shape_serial + 1) & 0xffffffff or 1
        header[3] = 1
        header[6] = self.shape_serial
        header[13] = 2 if self.target.video_composited else int(self.target.video_embedded)
        header[14:] = [0, 0]
        # Source seqlocks are unrelated. Use only the target's write sequence.
        header[2] = 0
        value = tuple(header)
        if value == self.last and key == self.shape_key:
            return
        sequence = (struct.unpack('<I', os.pread(self.target.fd, 4, 8))[0] | 1) & 0xffffffff
        os.pwrite(self.target.fd, struct.pack('<I', sequence), 8)
        if pixels is not None:
            os.pwrite(self.target.fd, pixels, 64)
        header[2] = sequence
        os.pwrite(self.target.fd, self.HEADER.pack(*header), 0)
        os.pwrite(self.target.fd, struct.pack('<I', (sequence + 1) & 0xffffffff), 8)
        switched = index != self.source
        visibility_changed = self.last is None or value[5] != self.last[5]
        self.source, self.shape_key, self.last = index, key, value
        if switched or visibility_changed or self.reports < 8:
            event('desktop_cursor_published', screen_id=index, visible=header[5],
                  x=header[11], y=header[12], shape_serial=self.shape_serial)
            self.reports += 1


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


def save_debug_log(log, path):
    """Opt-in bounded producer stderr for field diagnostics; never content."""
    if os.environ.get('UURB_CAPTURE_DEBUG_LOG') != '1' or log is None:
        return
    try:
        size = os.fstat(log.fileno()).st_size
        data = os.pread(log.fileno(), min(size, 65536), max(0, size - 65536))
        target = path.parent / 'capture.last.log'
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError:
        pass


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


def producer_command(restore_state, channel, duration, cursor_mode, cursor_fd=None, status_fd=None,
                     capture_binary=None, connector=None):
    if cursor_mode not in ('embedded', 'hidden', 'metadata', 'composited'):
        raise ValueError('Unsupported capture cursor policy')
    if (cursor_mode in ('metadata', 'composited')) != (cursor_fd is not None):
        raise ValueError('Metadata mode requires its private cursor channel')
    if connector is not None:
        return direct_command(connector, channel, duration, cursor_mode, capture_binary,
                              cursor_fd, status_fd)
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


def display_monitors(path):
    """Read the same monitor list the Win32 shim uses.  Bounded and same-UID."""
    if path is None:
        return []
    try:
        directory = path.parent
        info = directory.lstat()
        if (not path.is_absolute() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or
                info.st_mode & 0o077):
            return []
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC) as client:
            client.settimeout(2)
            client.connect(str(path))
            client.sendall(b'{"version":1,"op":"inspect"}')
            data = client.recv(65536)
        if not data or len(data) > 65536:
            return []
        value = json.loads(data)
        if not isinstance(value, dict) or value.get('ok') is not True:
            return []
        monitors = value.get('monitors')
        if not isinstance(monitors, list) or not 1 <= len(monitors) <= 8:
            return []
        result = []
        for index, item in enumerate(monitors):
            if (not isinstance(item, dict) or item.get('index') != index or
                    not isinstance(item.get('connector'), str) or not item['connector'] or
                    len(item['connector']) > 128 or not isinstance(item.get('gdi_name'), str) or
                    not item['gdi_name'].startswith('\\\\.\\DISPLAY') or
                    any(type(item.get(axis, 0)) is not int or not -65536 <= item.get(axis, 0) <= 65536
                        for axis in ('x', 'y'))):
                return []
            result.append(dict(index=index, connector=item['connector'],
                               gdi_name=item['gdi_name'], virtual=bool(item.get('virtual')),
                               x=item.get('x', 0), y=item.get('y', 0)))
        return result
    except (OSError, ValueError, json.JSONDecodeError):
        return []


def resolve_monitor(monitors, device):
    if not monitors:
        return None
    if not device:
        return monitors[0]
    for item in monitors:
        if item['gdi_name'].lower() == device.lower():
            return item
    return None


def virtual_session_state(endpoint, tracks):
    if endpoint is None:
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC) as client:
        client.settimeout(0.5)
        client.connect(str(endpoint))
        client.sendall(json.dumps(dict(version=1, op='virtual-session', tracks=tracks)).encode())
        reply = json.loads(client.recv(4096))
        if reply.get('ok') is not True:
            raise RuntimeError('Virtual session lifetime update rejected')


def transaction_requires_new_capture(parent, index, started_ns):
    name = 'display-transaction.json' if index == 0 else 'display-transaction.%d.json' % index
    try:
        record = state_tools.read_private(parent / name)
        pending = record.get('pending')
        return (record.get('output') == index and isinstance(pending, dict) and
                isinstance(pending.get('observed'), dict) and
                type(pending.get('requested_ns')) is int and pending['requested_ns'] > started_ns)
    except (OSError, ValueError):
        return False


def direct_command(connector, channel, duration, cursor_mode, capture_binary,
                   cursor_fd=None, status_fd=None):
    if not capture_binary:
        raise ValueError('Direct compositor capture requires a pinned producer')
    command = ['/usr/bin/python3', str(ROOT / 'scripts/mutter-direct-capture.py'),
               '--connector', connector, '--gpu-relay-fd', str(channel),
               '--duration-seconds', str(duration), '--cursor-mode', cursor_mode,
               '--capture-binary', str(capture_binary)]
    if cursor_fd is not None:
        command.extend(['--cursor-state-fd', str(cursor_fd)])
    if status_fd is not None:
        command.extend(['--capture-status-fd', str(status_fd)])
    return command


def read_capture_request(connection):
    """Return the requested GDI display, or '' for a legacy one-output peer."""
    connection.settimeout(2)
    try:
        data = connection.recv(1024)
    except (BlockingIOError, socket.timeout):
        return ''
    finally:
        connection.settimeout(None)
    if len(data) != 44:
        return ''
    magic, version, _ = struct.unpack_from('<III', data, 0)
    if magic != 0x43525555 or version != 1:
        return ''
    raw = data[12:44].split(b'\0', 1)[0]
    try:
        device = raw.decode('ascii')
    except UnicodeDecodeError:
        return ''
    return device if device.startswith('\\\\.\\DISPLAY') else ''


def run(path, restore_state, duration=3600, cursor_mode='embedded', cursor_path=None, capture_binary=None,
        display_reset_state=None, display_socket=None):
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
    active_monitor = None
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
        listener.listen(8)
        listener.setblocking(False)
        event('ready')
        while not stopping:
            resetting = reset_pending
            reset_pending = False
            if resetting and display_reset_state is not None and active_monitor in (None, 0):
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
                    save_debug_log(log, path)
                    connection.close(); log.close()
                    producer = connection = log = None
                    active_monitor = None
                    if status_channel:
                        status_channel.close()
                        status_channel = None
            readable, _, _ = select.select([listener], [], [], 0.25)
            if not readable:
                continue
            candidate, _ = listener.accept()
            candidate_pid, uid, _ = struct.unpack('3i', candidate.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.geteuid():
                candidate.close()
                event('connection_rejected', busy=False)
                continue
            device = read_capture_request(candidate)
            monitors = display_monitors(display_socket)
            monitor = resolve_monitor(monitors, device)
            if device and monitor is None:
                candidate.close()
                event('connection_rejected', unknown_output=True)
                continue
            requested = monitor['index'] if monitor else 0
            if producer is not None:
                if requested == active_monitor:
                    candidate.close()
                    event('connection_rejected', busy=True)
                    continue
                # A different output replaces the current producer.  UU opens
                # the new track before it releases the old one; refusing here
                # made switching displays fail.
                stop_producer(producer)
                detail = producer_failure(log)
                if detail:
                    event('capture_gpu_failure', generation=generation, **detail)
                if cursor:
                    cursor.reset(0)
                event('capture_ended', generation=generation, peer_disconnected=True,
                      producer_exit=producer.returncode, display_reset=False, reason='switched')
                save_debug_log(log, path)
                connection.close(); log.close()
                producer = connection = log = None
                active_monitor = None
                if status_channel:
                    status_channel.close()
                    status_channel = None
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
            active_monitor = requested
            accepted_status = None
            if cursor:
                cursor.reset(generation)
            log = tempfile.TemporaryFile()
            status_channel = FirstFrameStatus()
            cursor_fd = cursor.fd if cursor and cursor_mode in ('metadata', 'composited') else None
            inherited = (connection.fileno(), status_channel.writer.fileno()) + ((cursor_fd,) if cursor_fd is not None else ())
            connector = monitor['connector'] if monitor else None
            producer = subprocess.Popen(producer_command(restore_state, connection.fileno(), duration, cursor_mode,
                cursor_fd, status_channel.writer.fileno(), capture_binary, connector), pass_fds=inherited,
                stdout=log, stderr=log, start_new_session=True)
            status_channel.writer.close()
            event('capture_started', generation=generation, cursor_mode=cursor_mode, screen_id=requested)
    finally:
        try:
            cleanup(producer, connection, log, listener, path, bound, previous)
        finally:
            if status_channel:
                status_channel.close()
            if cursor:
                cursor.close()


def run_multi(path, restore_state, duration=3600, cursor_mode='embedded', cursor_path=None, capture_binary=None,
              display_reset_state=None, display_socket=None):
    """One concurrent producer per display.

    UU opens a video track for each screen it displays.  Replacing the active
    producer when another screen connected made both tracks take turns and
    flicker, so every screen keeps its own channel, cursor snapshot and
    first-frame status. Each selected output has its own display transaction.
    """
    validate_endpoint(path)
    if not 0 <= duration <= 3600:
        raise ValueError('Capture lifetime must be 0 (supervised) or from 1 to 3600 seconds')
    producer_command(restore_state, 3, duration, cursor_mode,
                     4 if cursor_path is not None and cursor_mode in ('metadata', 'composited') else None,
                     capture_binary=capture_binary)
    info = restore_state.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('An existing private authorized restore-state file is required')
    cursor_directory = cursor_path.parent if cursor_path is not None else None
    stopping = False
    reset_pending = False
    display_reset = DisplayReset()
    sessions = {}
    generation = 0

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True

    def request_reset(signum, frame):
        nonlocal reset_pending
        reset_pending = True

    previous = {s: signal.signal(s, request_stop) for s in (signal.SIGTERM, signal.SIGINT)}
    previous[signal.SIGUSR1] = signal.signal(signal.SIGUSR1, request_reset)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
    bound = None

    def remove_cursor_file(candidate):
        try:
            item = candidate.lstat()
            if (stat.S_ISREG(item.st_mode) and item.st_uid == os.geteuid() and
                    not item.st_mode & 0o077):
                candidate.unlink()
        except FileNotFoundError:
            pass

    legacy_cursor = None
    desktop_cursor = None
    monitor_refresh_at = 0
    transaction_check_at = 0
    session_update_at = 0
    cursor_states = {}

    def close_session(index, peer_gone=False, resetting=False, reason=None):
        session = sessions.pop(index, None)
        if session is None:
            return
        stop_producer(session['producer'])
        detail = producer_failure(session['log'])
        if detail:
            event('capture_gpu_failure', generation=session['generation'], screen_id=index, **detail)
        mismatch = state_tools.nvidia_driver_mismatch() if session['accepted'] is None else None
        if mismatch:
            event('gpu_driver_mismatch', generation=session['generation'], screen_id=index, **mismatch)
        if session['cursor']:
            # Keep the pre-created file: the DXGI backend opens it once when
            # the duplicator is created and never reopens it.
            try:
                session['cursor'].reset(1)
            except Exception:
                pass
        if session['status']:
            session['status'].close()
        save_debug_log(session['log'], path)
        session['connection'].close()
        session['log'].close()
        extra = {'reason': reason} if reason else {}
        event('capture_ended', generation=session['generation'], screen_id=index,
              peer_disconnected=peer_gone, producer_exit=session['producer'].returncode,
              display_reset=resetting, **extra)

    try:
        if cursor_path is not None:
            # The input bridge maps the legacy single cursor path at UU
            # startup; each capture session uses its own cursor.<n>.state.
            remove_cursor_file(cursor_path)
            legacy_cursor = CursorState(cursor_path, video_embedded=cursor_mode == 'embedded',
                                        video_composited=cursor_mode == 'composited')
            legacy_cursor.reset(1)
            if cursor_mode in ('metadata', 'composited'):
                desktop_cursor = DesktopCursor(legacy_cursor)
        if cursor_directory is not None and cursor_mode in ('metadata', 'composited'):
            for item in display_monitors(display_socket):
                index = int(item['gdi_name'][11:]) - 1
                candidate = cursor_directory / ('cursor.%d.state' % index)
                remove_cursor_file(candidate)
                try:
                    state = CursorState(candidate, video_embedded=False,
                                        video_composited=cursor_mode == 'composited')
                    state.reset(1)
                    cursor_states[index] = state
                except Exception:
                    event('cursor_state_unavailable', screen_id=index)
        listener.bind(str(path))
        os.chmod(path, 0o600)
        bound = path.lstat()
        listener.listen(8)
        listener.setblocking(False)
        event('ready')
        while not stopping:
            if time.monotonic() >= session_update_at:
                try:
                    virtual_session_state(display_socket, len(sessions))
                except (OSError, ValueError, RuntimeError):
                    event('virtual_session_update_unavailable')
                session_update_at = time.monotonic() + 0.5
            if time.monotonic() >= monitor_refresh_at:
                refreshed = {item['index']: item for item in display_monitors(display_socket)}
                for index, session in list(sessions.items()):
                    monitor = refreshed.get(index)
                    if monitor and session['monitor'] and monitor['connector'] == session['monitor']['connector']:
                        session['monitor'] = monitor
                    elif session['monitor']:
                        close_session(index, resetting=True, reason='output_topology_changed')
                monitor_refresh_at = time.monotonic() + 0.5
            if reset_pending:
                reset_pending = False
                origin = sessions.get(0)
                if origin is not None and display_reset_state is not None:
                    try:
                        display_reset.request(display_reset_state, origin['generation'], True,
                                              time.monotonic_ns())
                    except (OSError, ValueError):
                        event('display_reset_request_rejected')
            for index in list(sessions):
                session = sessions.get(index)
                if session is None:
                    continue
                if session['status']:
                    try:
                        status = session['status'].poll()
                        if status is not None:
                            if process_start_ticks(session['peer_pid']) != session['peer_start']:
                                raise ValueError('Capture consumer identity changed')
                            session['accepted'] = status
                            event('capture_frame_accepted', generation=session['generation'],
                                  screen_id=index, peer_pid=session['peer_pid'],
                                  peer_start_ticks=session['peer_start'],
                                  capture_started_ns=session['started_ns'], **status)
                            session['status'].close()
                            session['status'] = None
                    except (OSError, ValueError):
                        event('capture_status_unavailable', generation=session['generation'], screen_id=index)
                        session['status'].close()
                        session['status'] = None
                resetting = False
                if index == 0 and display_reset_state is not None:
                    decision = display_reset.decision(session['generation'], session['started_ns'],
                                                      session['accepted'], time.monotonic_ns())
                    if decision:
                        event('display_reset_decision', generation=session['generation'],
                              screen_id=index, result=decision)
                        resetting = decision == 'reset'
                poller = select.poll()
                poller.register(session['connection'], select.POLLHUP | select.POLLERR | select.POLLRDHUP)
                peer_gone = bool(poller.poll(0))
                code = session['producer'].poll()
                if code is not None or peer_gone or resetting:
                    close_session(index, peer_gone=peer_gone, resetting=resetting)
            if display_socket is not None and time.monotonic() >= transaction_check_at:
                for index, session in list(sessions.items()):
                    if transaction_requires_new_capture(display_socket.parent, index, session['started_ns']):
                        close_session(index, resetting=True, reason='selected_display_changed')
                transaction_check_at = time.monotonic() + 0.1
            if desktop_cursor:
                desktop_cursor.update({index: (session['cursor'], session['monitor'] or {})
                                       for index, session in sessions.items()})
            readable, _, _ = select.select([listener], [], [], 0.008 if desktop_cursor else 0.25)
            if not readable:
                continue
            candidate, _ = listener.accept()
            candidate_pid, uid, _ = struct.unpack('3i', candidate.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.geteuid():
                candidate.close()
                event('connection_rejected', busy=False)
                continue
            device = read_capture_request(candidate)
            monitor = resolve_monitor(display_monitors(display_socket), device)
            if device and monitor is None:
                candidate.close()
                event('connection_rejected', unknown_output=True)
                continue
            requested = monitor['index'] if monitor else 0
            if requested in sessions:
                close_session(requested, peer_gone=True, reason='replaced')
            try:
                peer_start = process_start_ticks(candidate_pid)
            except (OSError, ValueError):
                candidate.close()
                continue
            generation += 1
            cursor_index = int(monitor['gdi_name'][11:]) - 1 if monitor else 0
            cursor = cursor_states.get(cursor_index)
            if cursor is None and cursor_directory is not None and cursor_mode in ('metadata', 'composited'):
                cursor_file = cursor_directory / ('cursor.%d.state' % cursor_index)
                remove_cursor_file(cursor_file)
                try:
                    cursor = CursorState(cursor_file, video_embedded=False,
                                         video_composited=cursor_mode == 'composited')
                    cursor.reset(generation)
                    cursor_states[cursor_index] = cursor
                except Exception:
                    event('cursor_state_unavailable', screen_id=requested)
                    if cursor:
                        cursor.close()
                    cursor = None
            elif cursor is not None:
                try:
                    cursor.reset(generation)
                except Exception:
                    event('cursor_state_unavailable', screen_id=requested)
                    cursor = None
            status_channel = FirstFrameStatus()
            log = tempfile.TemporaryFile()
            cursor_fd = cursor.fd if cursor else None
            inherited = ((candidate.fileno(), status_channel.writer.fileno()) +
                         ((cursor_fd,) if cursor_fd is not None else ()))
            # Resolve every track, including DISPLAY1, by connector. A portal
            # restore token is tied to the previous screen configuration and
            # may request consent again after a mode change. Retrying it here
            # left the primary track waiting for a dialog while secondary
            # tracks already used explicit Mutter sources.
            connector = monitor['connector'] if monitor else None
            try:
                producer = subprocess.Popen(producer_command(restore_state, candidate.fileno(), duration,
                    cursor_mode, cursor_fd, status_channel.writer.fileno(), capture_binary, connector),
                    pass_fds=inherited, stdout=log, stderr=log, start_new_session=True)
            except Exception:
                status_channel.close()
                log.close()
                candidate.close()
                if cursor:
                    cursor.close()
                event('producer_start_failed', screen_id=requested)
                continue
            status_channel.writer.close()
            sessions[requested] = dict(producer=producer, connection=candidate, log=log,
                status=status_channel, peer_pid=candidate_pid, peer_start=peer_start,
                generation=generation, started_ns=time.monotonic_ns(), accepted=None,
                cursor=cursor, monitor=monitor)
            event('capture_started', generation=generation, cursor_mode=cursor_mode, screen_id=requested)
    finally:
        for index in list(sessions):
            close_session(index, reason='stopping')
        listener.close()
        if bound:
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (bound.st_dev, bound.st_ino):
                    path.unlink()
            except FileNotFoundError:
                pass
        for state in cursor_states.values():
            state.close()
        if legacy_cursor:
            legacy_cursor.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('--restore-state', type=Path, required=True)
    parser.add_argument('--duration-seconds', type=int, default=3600)
    parser.add_argument('--cursor-mode', choices=['embedded', 'hidden', 'metadata', 'composited'], default='embedded')
    parser.add_argument('--cursor-state', type=Path)
    parser.add_argument('--capture-binary', type=Path)
    parser.add_argument('--display-reset-state', type=Path)
    parser.add_argument('--display-socket', type=Path)
    args = parser.parse_args()
    if args.display_socket is not None:
        run_multi(args.socket, args.restore_state, args.duration_seconds, args.cursor_mode, args.cursor_state,
                  args.capture_binary, args.display_reset_state, args.display_socket)
    else:
        run(args.socket, args.restore_state, args.duration_seconds, args.cursor_mode, args.cursor_state,
            args.capture_binary, args.display_reset_state)
