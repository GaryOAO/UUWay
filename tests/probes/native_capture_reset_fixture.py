#!/usr/bin/python3
"""Synthetic broker producer: private test socket only; never calls Portal."""
import importlib.util
from pathlib import Path
import socket
import struct
import sys
import time

if sys.argv[1]=='--producer':
    with socket.socket(fileno=int(sys.argv[2])) as peer:
        peer.send(b'UURB_FIXTURE_READY')
        while packet := peer.recv(64):
            if packet == b'ack':
                with socket.socket(fileno=int(sys.argv[3])) as status:
                    status.send(struct.pack('<4I2Q', 0x53525555, 1, 1680, 1050, 1, time.monotonic_ns()))
            peer.send(b'UURB_FIXTURE_ALIVE')
else:
    directory=Path(sys.argv[1]).resolve()
    if directory.parent!=Path('/tmp') or not directory.name.startswith('uurb-capture-reset-test-'):
        raise SystemExit('Not an isolated fixture directory')
    root=Path(__file__).resolve().parents[2]
    spec=importlib.util.spec_from_file_location('fixture_broker',root/'scripts/uu-native-capture-broker.py')
    broker=importlib.util.module_from_spec(spec);spec.loader.exec_module(broker)
    def command(state,channel,duration,cursor,cursor_fd=None,status_fd=None,capture_binary=None):
        return [sys.executable,str(Path(__file__).resolve()),'--producer',str(channel),str(status_fd)]
    broker.producer_command=command
    broker.run(directory/'capture.sock',directory/'restore.json',0,'embedded',
               display_reset_state=directory/'reset.json' if '--targeted' in sys.argv else None)
