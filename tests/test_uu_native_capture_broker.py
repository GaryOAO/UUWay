import importlib.util
import os
import struct
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, call, patch


spec = importlib.util.spec_from_file_location('capture_broker',
    Path(__file__).resolve().parents[1] / 'scripts/uu-native-capture-broker.py')
broker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broker)


class BrokerCleanupTests(unittest.TestCase):
    def test_target_reset_is_generation_fenced_and_waits_for_pending_first_ack(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'reset.json'
            value = dict(version=1, width=1680, height=1050, after_ns=1_000_000_000,
                         requested_ns=2_000_000_000, require_fresh=False)
            broker.state_tools.write_private(path, value)
            reset = broker.DisplayReset()
            reset.request(path, 2, True, 2_000_000_000)
            self.assertIsNone(reset.decision(2, 1_500_000_000, None, 5_000_000_000))
            self.assertEqual(reset.decision(2, 1_500_000_000, dict(width=1680,height=1050), 5_100_000_000), 'already_recovered')
            self.assertIsNone(reset.decision(2, 1_500_000_000, None, 20_000_000_000))
            # Replayed signals cannot invalidate the recovered allocation.
            reset.request(path, 2, True, 6_000_000_000)
            self.assertIsNone(reset.pending)
            for active, current, expected in ((True, 3, 'superseded'), (False, 2, None)):
                reset = broker.DisplayReset(); reset.request(path, 2, active, 2_000_000_000)
                self.assertEqual(reset.decision(current, 1, None, 20_000_000_000), expected)
            reset = broker.DisplayReset(); reset.request(path, 2, True, 2_000_000_000)
            self.assertIsNone(reset.decision(2, 1, None, 3_900_000_000))
            self.assertEqual(reset.decision(2, 1, None, 4_000_000_000), 'reset')
            # Even a new allocation with no usable ACK has a bounded fallback.
            reset = broker.DisplayReset(); reset.request(path, 2, True, 2_000_000_000)
            self.assertEqual(reset.decision(2, 1_500_000_000, None, 14_000_000_000), 'reset')

    def test_same_size_reset_requires_fresh_generation_and_rejects_invalid_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'reset.json'
            value = dict(version=1,width=1680,height=1050,after_ns=10,requested_ns=20,require_fresh=True)
            broker.state_tools.write_private(path, value)
            reset = broker.DisplayReset(); reset.request(path, 1, True, 20)
            self.assertIsNone(reset.decision(1, 1, dict(width=1680,height=1050), 21))
            self.assertEqual(reset.decision(1, 1, dict(width=1680,height=1050), 2_000_000_020), 'reset')
            for change in ({'width':True}, {'height':1051}, {'after_ns':21}, {'requested_ns':22},
                           {'require_fresh':1}, {'extra':'private'}, {'version':2}):
                broker.state_tools.write_private(path, dict(value, **change))
                with self.assertRaises(ValueError): broker.DisplayReset().request(path, 1, True, 21)

    def test_real_late_target_reset_does_not_kill_new_capture_with_pending_ack(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='uurb-capture-reset-test-',dir='/tmp') as temporary:
            directory=Path(temporary);endpoint=directory/'capture.sock'
            broker.state_tools.write_private(directory/'restore.json', {})
            with tempfile.TemporaryFile() as output:
                process=subprocess.Popen(['/usr/bin/python3',str(root/'tests/probes/native_capture_reset_fixture.py'),
                                          str(directory),'--targeted'],stdout=output,stderr=output)
                try:
                    deadline=time.monotonic()+4
                    while not endpoint.exists() and process.poll() is None and time.monotonic()<deadline:
                        time.sleep(0.01)
                    self.assertIsNone(process.poll());self.assertTrue(endpoint.exists())
                    before=time.monotonic_ns()
                    with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as old:
                        old.settimeout(4);old.connect(str(endpoint))
                        self.assertEqual(old.recv(64),b'UURB_FIXTURE_READY')
                    time.sleep(0.4)
                    with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as peer:
                        peer.settimeout(4);peer.connect(str(endpoint))
                        self.assertEqual(peer.recv(64),b'UURB_FIXTURE_READY')
                        broker.state_tools.write_private(directory/'reset.json', dict(version=1,width=1680,height=1050,
                            after_ns=before,requested_ns=time.monotonic_ns(),require_fresh=False))
                        process.send_signal(signal.SIGUSR1)
                        time.sleep(2.3) # New source is still starting when the delayed request arrives.
                        peer.send(b'ack');self.assertEqual(peer.recv(64),b'UURB_FIXTURE_ALIVE')
                        time.sleep(0.4)
                        process.send_signal(signal.SIGUSR1) # Duplicate notification is harmless.
                        peer.send(b'ping');self.assertEqual(peer.recv(64),b'UURB_FIXTURE_ALIVE')
                    self.assertIsNone(process.poll())
                finally:
                    process.terminate()
                    try:process.wait(timeout=8)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
                output.seek(0);records=output.read()
                self.assertIn(b'"result": "already_recovered"',records)
                self.assertNotIn(b'"display_reset": true',records)
                self.assertEqual(process.returncode,0)

    def test_only_bounded_own_gpu_failure_fields_are_exported(self):
        import json
        good = dict(stage='receive', sequence=2, wait_ms=2001)
        with tempfile.TemporaryFile() as log:
            log.write(b'PRIVATE_SENTINEL unrelated log\nUURB_GPU_FAILURE ' + json.dumps(good).encode() + b'\n')
            log.flush()
            self.assertEqual(broker.producer_failure(log), good)
        for value in (dict(good, payload='PRIVATE_SENTINEL'), dict(good, stage='unknown'),
                      dict(good, sequence=True), dict(good, wait_ms=60001), [], None):
            with tempfile.TemporaryFile() as log:
                log.write(b'UURB_GPU_FAILURE ' + json.dumps(value).encode() + b'\n');log.flush()
                self.assertIsNone(broker.producer_failure(log))

    def test_driver_mismatch_names_both_versions_only_when_they_differ(self):
        proprietary = 'NVRM version: NVIDIA UNIX x86_64 Kernel Module  580.95.05  Tue Sep 23 10:11:16 UTC 2025\n'
        open_module = 'NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  580.95.05  Release Build\n'
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            proc, libraries = directory / 'version', directory / 'lib'
            libraries.mkdir()
            for name in ('libnvidia-encode.so', 'libnvidia-encode.so.1', 'libnvidia-encode.so.580.178.04'):
                (libraries / name).touch()
            check = lambda: broker.state_tools.nvidia_driver_mismatch(proc, libraries)
            self.assertIsNone(check())  # no driver loaded
            for text in (proprietary, open_module):
                proc.write_text(text)
                self.assertEqual(check(), dict(kernel_module='580.95.05', userspace='580.178.04'))
            (libraries / 'libnvidia-encode.so.580.95.05').touch()
            self.assertIsNone(check())  # the loaded module's libraries are still installed
            for path in libraries.iterdir():
                path.unlink()
            self.assertIsNone(check())  # no user-space encoder at all is a different problem

    def test_driver_mismatch_is_reported_only_for_a_producer_without_first_frame(self):
        source = (Path(broker.__file__)).read_text()
        check = source.index('state_tools.nvidia_driver_mismatch() if accepted_status is None else None')
        self.assertLess(source.index("detail = producer_failure(log)"), check)
        self.assertLess(check, source.index("event('gpu_driver_mismatch'"))
        self.assertLess(source.index("event('gpu_driver_mismatch'"), source.index("event('capture_ended'"))

    def test_managed_producer_is_explicit_and_not_a_build_directory_default(self):
        binary = Path('/private/release/capture/uu-pipewire-native-probe')
        command = broker.producer_command(Path('/private/state.json'), 7, 0, 'embedded', capture_binary=binary)
        self.assertEqual(command[-2:], ['--capture-binary', str(binary)])
        with self.assertRaises(ValueError):
            broker.producer_command(Path('/private/state.json'), 7, 0, 'embedded', capture_binary=Path('build/probe'))

    def test_real_reset_retires_consumer_without_restarting_listener(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='uurb-capture-reset-test-',dir='/tmp') as temporary:
            directory=Path(temporary);endpoint=directory/'capture.sock'
            (directory/'restore.json').write_text('{}');(directory/'restore.json').chmod(0o600)
            with tempfile.TemporaryFile() as output:
                process=subprocess.Popen(['/usr/bin/python3',str(root/'tests/probes/native_capture_reset_fixture.py'),str(directory)],
                    stdout=output,stderr=output)
                try:
                    deadline=time.monotonic()+4
                    while not endpoint.exists() and process.poll() is None and time.monotonic()<deadline:
                        time.sleep(0.01)
                    self.assertIsNone(process.poll());self.assertTrue(endpoint.exists())
                    inode=endpoint.stat().st_ino
                    for iteration in range(3):
                        if iteration: process.send_signal(signal.SIGUSR1) # Idle reset must not poison next generation.
                        with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as peer:
                            peer.settimeout(4);peer.connect(str(endpoint))
                            self.assertEqual(peer.recv(64),b'UURB_FIXTURE_READY')
                            process.send_signal(signal.SIGUSR1)
                            self.assertEqual(peer.recv(64),b'')
                        self.assertIsNone(process.poll())
                        self.assertEqual(endpoint.stat().st_ino,inode)
                finally:
                    process.terminate()
                    try:process.wait(timeout=8)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
                self.assertEqual(process.returncode,0)
                self.assertFalse(endpoint.exists())

    def test_first_frame_status_is_bounded_one_shot_and_scalar(self):
        status = broker.FirstFrameStatus()
        try:
            self.assertIsNone(status.poll())
            accepted = time.monotonic_ns()
            status.writer.send(struct.pack('<4I2Q', 0x53525555, 1, 1920, 1080, 1, accepted))
            self.assertEqual(status.poll(), dict(width=1920, height=1080, sequence=1, accepted_ns=accepted))
            self.assertIsNone(status.poll())
        finally:
            status.close()

    def test_first_frame_rejects_malformed_stale_and_truncated_packets(self):
        now = time.monotonic_ns()
        packets = [b'', b'x' * 33, struct.pack('<4I2Q', 0, 1, 1920, 1080, 1, now),
                   struct.pack('<4I2Q', 0x53525555, 1, 1920, 1080, 2, now),
                   struct.pack('<4I2Q', 0x53525555, 1, 0, 1080, 1, now),
                   struct.pack('<4I2Q', 0x53525555, 1, 1920, 1080, 1, now - 6_000_000_000)]
        for packet in packets:
            status = broker.FirstFrameStatus()
            try:
                status.writer.send(packet)
                with self.assertRaises(ValueError):
                    status.poll()
            finally:
                status.close()

    def test_embedded_cursor_state_suppresses_only_separate_sprite(self):
        with tempfile.TemporaryDirectory() as temporary:
            cursor = broker.CursorState(Path(temporary) / 'cursor.state', video_embedded=True)
            try:
                for generation in (0, 7, 0):
                    cursor.reset(generation)
                    header = struct.unpack('<16I', os.pread(cursor.fd, 64, 0))
                    self.assertEqual(header[13:], (1, 0, 0))
                    self.assertEqual(header[4:6], (0, 0))
            finally:
                cursor.close()

    def test_composited_cursor_keeps_metadata_writer_and_distinct_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'cursor.state'
            cursor = broker.CursorState(path, video_composited=True)
            try:
                for generation in (0, 7, 0):
                    cursor.reset(generation)
                    header = struct.unpack('<16I', os.pread(cursor.fd, 64, 0))
                    self.assertEqual(header[13:], (2, 0, 0))
                    self.assertEqual(header[4:6], (0, 0))
            finally:
                cursor.close()
            with self.assertRaises(ValueError):
                broker.CursorState(path, video_composited=True, video_embedded=True)
            self.assertFalse(path.exists())
        command = broker.producer_command(Path('/private/state.json'), 7, 0, 'composited', 8)
        self.assertEqual(command[command.index('--cursor-mode') + 1], 'composited')
        self.assertEqual(command[-2:], ['--cursor-state-fd', '8'])
        with self.assertRaises(ValueError):
            broker.producer_command(Path('/private/state.json'), 7, 0, 'composited')

    def test_status_descriptor_is_separate_and_forwarded(self):
        command = broker.producer_command(Path('/private/state.json'), 7, 0, 'metadata', 8, 9)
        self.assertEqual(command[-2:], ['--capture-status-fd', '9'])
        for fd in (1, 7, 8):
            with self.assertRaises(ValueError):
                broker.producer_command(Path('/private/state.json'), 7, 0, 'metadata', 8, fd)

    def test_continuous_capture_reaches_portal_without_hourly_cutoff(self):
        command = broker.producer_command(Path('/private/state.json'), 7, 0, 'metadata', 8)
        self.assertEqual(command[command.index('--duration-seconds') + 1], '0')

    def test_each_new_producer_receives_the_selected_cursor_policy(self):
        for policy in ('embedded', 'hidden'):
            command = broker.producer_command(Path('/private/state.json'), 7, 3600, policy)
            self.assertEqual(command[-2:], ['--cursor-mode', policy])
            self.assertEqual(command[command.index('--gpu-relay-fd') + 1], '7')
        with self.assertRaises(ValueError):
            broker.producer_command(Path('/private/state.json'), 7, 3600, 'metadata')
        command = broker.producer_command(Path('/private/state.json'), 7, 3600, 'metadata', 8)
        self.assertEqual(command[-2:], ['--cursor-state-fd', '8'])
        with self.assertRaises(ValueError):
            broker.producer_command(Path('/private/state.json'), 7, 3600, 'hidden', 8)

    def test_cursor_state_generation_reset_and_owned_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'cursor.state'
            cursor = broker.CursorState(path)
            self.assertEqual(path.stat().st_size, 64 + 384 * 384 * 4)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            cursor.reset(7)
            header = struct.unpack('<16I', os.pread(cursor.fd, 64, 0))
            self.assertEqual(header[:2], (0x43525555, 1))
            self.assertEqual(header[2] & 1, 0)
            self.assertEqual(header[3], 7)
            self.assertFalse(any(header[4:]))
            # A killed producer may leave an odd sequence; recovery commits an
            # inactive fresh generation instead of exposing half a bitmap.
            os.pwrite(cursor.fd, struct.pack('<I', 99), 8)
            cursor.reset(8)
            header = struct.unpack('<16I', os.pread(cursor.fd, 64, 0))
            self.assertEqual(header[2] & 1, 0)
            self.assertEqual(header[3], 8)
            cursor.close()
            self.assertFalse(path.exists())

    def test_cursor_state_refuses_existing_and_preserves_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'cursor.state'
            cursor = broker.CursorState(path)
            with self.assertRaises(ValueError):
                broker.CursorState(path)
            path.rename(Path(temporary) / 'original')
            path.touch(mode=0o600)
            cursor.close()
            self.assertTrue(path.exists())

    def test_exited_wrapper_still_stops_owned_gpu_group(self):
        process = Mock(pid=12345, returncode=1)
        process.poll.return_value = 1
        with patch.object(broker.os, 'killpg') as kill:
            broker.stop_producer(process)
        self.assertEqual(kill.call_args_list,
            [call(12345, signal.SIGTERM), call(12345, signal.SIGKILL)])

    def test_timeout_escalates_and_reaps(self):
        process = Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired('owned-producer', 5), -9]
        with patch.object(broker.os, 'killpg') as kill:
            broker.stop_producer(process)
        self.assertEqual(kill.call_args_list[-1], call(12345, signal.SIGKILL))
        self.assertEqual(process.wait.call_count, 2)

    def test_already_gone_group_is_harmless(self):
        process = Mock(pid=12345)
        with patch.object(broker.os, 'killpg', side_effect=ProcessLookupError):
            broker.stop_producer(process)
        self.assertEqual(process.wait.call_count, 2)

    def test_stop_failure_does_not_skip_socket_or_signal_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'capture.sock'
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            listener.bind(str(path))
            bound = path.lstat()
            connection, log = Mock(), Mock()
            with patch.object(broker, 'stop_producer', side_effect=TimeoutError), \
                    patch.object(broker, 'event') as event, patch.object(broker.signal, 'signal') as restore:
                with self.assertRaisesRegex(RuntimeError, 'cleanup incomplete'):
                    broker.cleanup(Mock(), connection, log, listener, path, bound,
                        {signal.SIGTERM: signal.SIG_DFL})
            connection.close.assert_called_once()
            log.close.assert_called_once()
            self.assertEqual(listener.fileno(), -1)
            self.assertFalse(path.exists())
            restore.assert_called_once_with(signal.SIGTERM, signal.SIG_DFL)
            event.assert_called_once_with('cleanup_failed', operations=['stop_producer'])

    def test_replacement_endpoint_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'capture.sock'
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as original, \
                    socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as replacement:
                original.bind(str(path))
                bound = path.lstat()
                path.rename(Path(temporary) / 'old.sock')
                replacement.bind(str(path))
                broker.cleanup(None, None, None, original, path, bound, {})
                self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
