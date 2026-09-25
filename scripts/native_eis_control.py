"""Private synchronous EIS worker transitions; no authorization or input replay.

Successful ACK is local teardown/new EI readiness, not compositor/application
acceptance. On any ambiguous exchange retire the worker and its owning lease;
never retry or pretend the prior input mapping is still usable.
"""
import array
import os
import socket
import struct

MAGIC = 0x45525555


def validate_socket(fd, kind):
    if type(fd) is not int or fd < 3:
        raise ValueError('Expected inherited private input descriptor')
    with socket.socket(fileno=os.dup(fd)) as peer:
        if peer.family != socket.AF_UNIX or peer.type != kind:
            raise ValueError('Unexpected input descriptor type')
        peer.getpeername()
        _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid():raise ValueError('Unexpected input descriptor UID')


class EisController:
    def __init__(self, connection, mapping):
        validate_socket(connection.fileno(), socket.SOCK_SEQPACKET)
        if not isinstance(mapping, str) or '\0' in mapping or not 0 < len(mapping.encode()) <= 4096:
            raise ValueError('Expected bounded EIS mapping')
        self.connection = connection
        self.mapping = mapping.encode()
        self.sequence = 0
        self.paused = False
        self.failed = False

    def _exchange(self, command, fd=None):
        if self.failed or self.sequence == 0xffffffff:
            raise RuntimeError('Input control outcome is unusable')
        if (command == 1 and self.paused) or (command == 2 and not self.paused):
            raise ValueError('Invalid input transition state')
        if command == 2:validate_socket(fd, socket.SOCK_STREAM)
        sequence = self.sequence + 1
        mapping = self.mapping if command == 2 else b''
        packet = struct.pack('<8I', MAGIC, 1, sequence, command, len(mapping), 0, 0, 0) + mapping
        descriptors = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [fd]))] if fd is not None else []
        previous_timeout = self.connection.gettimeout()
        self.failed = True  # Any send/ACK uncertainty consumes this controller.
        try:
            self.connection.settimeout(6)
            if self.connection.sendmsg([packet], descriptors) != len(packet):
                raise RuntimeError('Incomplete input transition command')
            reply, ancillary, flags, _ = self.connection.recvmsg(32)
            if flags or ancillary or len(reply) != 32:
                raise RuntimeError('Invalid input transition acknowledgement')
            magic, version, ack_sequence, ack_command, error, width, height, reserved = struct.unpack('<8I', reply)
            if (magic != MAGIC or version != 1 or ack_sequence != sequence or ack_command != command or error or reserved or
                    (command == 1 and (width or height)) or (command == 2 and (not width or not height))):
                raise RuntimeError('Mismatched input transition acknowledgement')
            self.sequence = sequence; self.paused = command == 1; self.failed = False
            return dict(logical_width=width, logical_height=height)
        finally:
            self.connection.settimeout(previous_timeout)

    def suspend(self):
        self._exchange(1)

    def resume(self, fd):
        return self._exchange(2, fd)
