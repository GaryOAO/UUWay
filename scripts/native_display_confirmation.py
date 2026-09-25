"""Fresh owned-UU frame evidence for opt-in display confirmation.

No framebuffer access or vendor-log export. ACK means allocation acceptance,
not hardware encoding completion or remote decoding/presentation.
"""
import json
import os
from pathlib import Path
import re
import select
import socket
import stat
import struct
import time


def process_start_ticks(pid):
    value = (Path('/proc') / str(pid) / 'stat').read_text()
    return int(value[value.rindex(')') + 2:].split()[19])


def mapped_files_match(pid, paths):
    expected = set()
    for path in paths:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return False
        expected.add((os.major(info.st_dev), os.minor(info.st_dev), info.st_ino))
    with (Path('/proc') / str(pid) / 'maps').open('rb') as source:
        data = source.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        return False
    for line in data.splitlines():
        fields = line.split(None, 5)
        if len(fields) < 5:
            return False
        major, minor = fields[3].split(b':')
        expected.discard((int(major, 16), int(minor, 16), int(fields[4])))
    return not expected


def owned_consumer(evidence, prefix, server, capture_dll):
    pid = evidence['peer_pid']
    root = Path('/proc') / str(pid)
    if root.stat().st_uid != os.getuid() or process_start_ticks(pid) != evidence['peer_start_ticks']:
        return False
    with (root / 'environ').open('rb') as source:
        environment = source.read(2 * 1024 * 1024 + 1)
    if len(environment) > 2 * 1024 * 1024 or os.fsencode('WINEPREFIX=' + str(prefix)) not in environment.split(b'\0'):
        return False
    return mapped_files_match(pid, (server, capture_dll)) and process_start_ticks(pid) == evidence['peer_start_ticks']


def confirmation_request(record, topology, evidence, now_ns):
    if not isinstance(record, dict) or record.get('version') != 1 or record.get('confirmation_policy') != 'gpu_frame':
        return None
    pending = record.get('pending')
    identifier = record.get('transaction')
    if not isinstance(pending, dict) or not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier):
        return None
    observed, target = pending.get('observed'), pending.get('target')
    if not isinstance(observed, dict) or not isinstance(target, dict) or pending.get('confirmed') is not False:
        return None
    if not 0 <= now_ns - evidence['accepted_ns'] <= 5_000_000_000:
        return None
    if (len(topology['modes']) != 1 or len(topology['layout']) != 1 or
            topology['generation'] != observed.get('serial') or observed.get('mode') != target.get('mode') or
            observed.get('scale') != target.get('scale')):
        return None
    layout, mode = topology['layout'][0], topology['modes'][0]
    if (layout != dict(x=0, y=0, scale=observed['scale'], transform=0, outputs=1) or
            any(evidence[key] != mode[key] or evidence[key] != target.get(key) for key in ('width', 'height'))):
        return None
    return dict(version=1, op='confirm', transaction=identifier, serial=topology['generation'])


def confirm_rpc(endpoint, request):
    for path, directory in ((endpoint.parent, True), (endpoint, False)):
        info = path.lstat()
        valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISSOCK(info.st_mode)
        if not valid_type or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Invalid private confirmation endpoint')
    # One bounded attempt. A lost reply must never cause replay.
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC) as client:
        client.settimeout(1)
        client.connect(str(endpoint))
        _, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid():
            raise ValueError('Unexpected confirmation peer')
        client.sendall(json.dumps(request, allow_nan=False).encode('ascii'))
        packet, _, flags, _ = client.recvmsg(1024)
        if flags or not packet:
            raise ValueError('Invalid bounded confirmation response')
        return json.loads(packet) == dict(ok=True, confirmed=True)


class DisplayConfirmation:
    def __init__(self, directory, prefix, server, bundle, topology, read_private):
        self.directory, self.prefix, self.server = directory, prefix, server
        self.capture_dll = bundle / 'app/bin/dxgi.dll'
        self.topology, self.read_private = topology, read_private
        self.position = 0
        self.fragment = b''
        self.dropping = False
        self.generation = None
        self.evidence = None
        self.accepted_after_ns = 0

    def reset_for_topology(self, topology, after_ns=None):
        # Old accepted-frame records must not confirm a new mode, including
        # forced resets with unchanged pixel size. Start at the current tail
        # before requesting video recreation; require a new capture_started.
        # For changed pixels only, a bounded replay can recover the actual new
        # ACK that raced ahead of the topology poll. Never reuse an ACK from
        # before the last topology observation or a same-size/DPI reset.
        replay = (type(after_ns) is int and after_ns > 0 and
                  self.topology.get('modes') != topology.get('modes'))
        self.accepted_after_ns = after_ns if replay else 0
        with (self.directory / 'capture.log').open('rb') as source:
            size = os.fstat(source.fileno()).st_size
            self.position = max(0, size - 65536) if replay else size
            if self.position:
                source.seek(self.position - 1)
                self.dropping = source.read(1) != b'\n'
            else:
                self.dropping = False
        self.fragment = b''
        self.generation = self.evidence = None
        self.topology = topology

    def consume(self):
        # Work per supervisor tick is bounded, even with a malformed log line.
        with (self.directory / 'capture.log').open('rb') as source:
            if os.fstat(source.fileno()).st_size < self.position:
                self.position = 0; self.fragment = b''; self.dropping = False
                self.generation = self.evidence = None
            source.seek(self.position)
            data = source.read(65536)
            self.position = source.tell()
        parts = (self.fragment + data).split(b'\n')
        self.fragment = parts.pop()
        for line in parts:
            if self.dropping:
                self.dropping = False
                continue
            if len(line) > 4096:
                continue
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(value, dict):
                continue
            event = value.get('event')
            if event == 'capture_started':
                generation = value.get('generation')
                self.generation = generation if type(generation) is int and generation > 0 else None
                self.evidence = None
            elif event == 'capture_ended' and value.get('generation') == self.generation:
                self.generation = self.evidence = None
            elif event == 'capture_frame_accepted':
                keys = {'generation', 'peer_pid', 'peer_start_ticks', 'width', 'height', 'sequence', 'accepted_ns'}
                if (set(value) != keys | {'event'} or any(type(value[k]) is not int or value[k] <= 0 for k in keys) or
                        value['generation'] != self.generation or value['sequence'] != 1 or
                        value['accepted_ns'] < self.accepted_after_ns or
                        not 2 <= value['width'] <= 4096 or not 2 <= value['height'] <= 4096):
                    continue
                self.evidence = value
        if len(self.fragment) > 4096:
            self.fragment = b''
            self.dropping = True

    def tick(self, ready, endpoint):
        self.consume()
        if not ready or not self.evidence:
            return None
        evidence, self.evidence = self.evidence, None
        try:
            record = self.read_private(endpoint.parent / 'display-transaction.json')
            request = confirmation_request(record, self.topology, evidence, time.monotonic_ns())
            if request is None:
                return None
            # Keep the process identity pinned for this one RPC. An already-dead
            # process, PID reuse, local fixture or other release cannot qualify.
            pidfd = os.pidfd_open(evidence['peer_pid'])
            try:
                if select.select([pidfd], [], [], 0)[0] or not owned_consumer(
                        evidence, self.prefix, self.server, self.capture_dll):
                    return 'frame_consumer_not_owned'
                if select.select([pidfd], [], [], 0)[0]:
                    return 'frame_consumer_exited'
                return 'gpu_frame_confirmed' if confirm_rpc(endpoint, request) else 'confirmation_rejected'
            finally:
                os.close(pidfd)
        except (OSError, ValueError, KeyError, TypeError):
            return 'confirmation_unavailable'
