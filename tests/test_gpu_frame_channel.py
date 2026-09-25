import array
import ctypes as C
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('channel_probe', ROOT / 'tests/probes/gpu_frame_channel_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class Message(C.Structure):
    _fields_ = [(field, C.c_uint32) for field in ('magic', 'version', 'kind', 'width', 'height', 'reserved')] + [
        (field, C.c_uint64) for field in ('sequence', 'timestamp', 'allocation_bytes', 'ready_ns')] + [('uuid', C.c_ubyte * 16)] + [
        (field, C.c_uint32) for field in ('rate_num', 'rate_den', 'max_rate_num', 'max_rate_den')]


class Publisher(C.Structure):
    _fields_ = [('socket_fd', C.c_int), ('failed', C.c_int), ('sequence', C.c_uint64), ('timestamp', C.c_uint64)] + [
        (field, C.c_uint32) for field in ('rate_num', 'rate_den', 'max_rate_num', 'max_rate_den')]


@unittest.skipUnless(shutil.which('gcc'), 'C compiler required')
class GpuFrameChannelTests(unittest.TestCase):
    def test_source_rate_evidence_is_not_fixed_60_or_measured_fps(self):
        for rate, maximum, nominal in [([17, 1], [0, 0], [17, 1]),
                ([60000, 1001], [120, 1], [60000, 1001]),
                ([0, 1], [60000, 1001], [60000, 1001]),
                ([0, 0], [120, 1], [0, 1]), ([0, 1], [0, 0], [0, 1])]:
            source = dict(negotiated_framerate=rate, negotiated_max_framerate=maximum, actual_fps=12)
            probe.validate_source_rate(source, dict(capture_nominal_rate=nominal))
            for incorrect in ([60, 1], [12, 1]):
                with self.assertRaises(RuntimeError):
                    probe.validate_source_rate(source, dict(capture_nominal_rate=incorrect))
        for invalid in (None, [17, 0], [0, 2], [True, 1], [-1, 1], [1]):
            with self.assertRaises(RuntimeError):
                probe.validate_source_rate(dict(negotiated_framerate=invalid, negotiated_max_framerate=[0, 0]), {})

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        binary = Path(cls.directory.name) / 'channel.so'
        subprocess.run(['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(ROOT / 'src/native_gpu_frame_channel.c'), '-o', str(binary)],
                       check=True, capture_output=True, timeout=15)
        cls.lib = C.CDLL(str(binary))
        cls.lib.uurb_gpu_message_valid.argtypes = [C.POINTER(Message), C.c_int]
        cls.lib.uurb_gpu_channel_check.argtypes = [C.c_int]
        cls.lib.uurb_gpu_channel_connect.argtypes = [C.c_char_p]
        cls.lib.uurb_gpu_channel_send.argtypes = [C.c_int, C.POINTER(Message), C.c_int, C.c_uint]
        cls.lib.uurb_gpu_channel_receive.argtypes = [C.c_int, C.POINTER(Message), C.POINTER(C.c_int), C.c_uint]
        cls.lib.uurb_gpu_publish.argtypes = [C.POINTER(Publisher), C.c_int, C.c_uint64,
                                          C.POINTER(C.c_ubyte), C.c_uint32, C.c_uint32, C.c_uint64]
        cls.lib.uurb_gpu_publish_end.argtypes = [C.POINTER(Publisher)]

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    @staticmethod
    def message(kind=1, sequence=1):
        m = Message(magic=0x47525555, version=3, kind=kind, sequence=sequence)
        if kind == 1:
            m.width, m.height, m.allocation_bytes, m.timestamp = 640, 360, 1048576, 1000
            m.ready_ns = 1234
            m.uuid[0] = 1
        return m

    def test_fixed_abi_and_frame_metadata_constraints(self):
        self.assertEqual(C.sizeof(Message), 88)
        self.assertEqual(Message.ready_ns.offset, 48)
        self.assertEqual(Message.uuid.offset, 56)
        self.assertEqual(Message.rate_num.offset, 72)
        self.assertEqual(self.lib.uurb_gpu_message_valid(self.message(), 1), 1)
        self.assertEqual(self.lib.uurb_gpu_message_valid(self.message(sequence=2), 0), 1)
        for field, value in [('magic', 0), ('version', 1), ('ready_ns', 0), ('kind', 9), ('width', 0),
                             ('height', 361), ('width', 8192), ('allocation_bytes', 100),
                             ('allocation_bytes', 2**31), ('reserved', 1), ('sequence', 0)]:
            m = self.message()
            setattr(m, field, value)
            with self.subTest(field=field):
                self.assertEqual(self.lib.uurb_gpu_message_valid(m, 1), 0)
        m = self.message()
        m.uuid[0] = 0
        self.assertEqual(self.lib.uurb_gpu_message_valid(m, 1), 0)

    def test_negotiated_rates_and_control_packet_refusals(self):
        for num, den, maximum, max_den in [(0, 0, 0, 0), (0, 1, 17, 1),
                                         (60000, 1001, 0, 0), (0, 1, 60000, 1001)]:
            m = self.message()
            m.rate_num, m.rate_den, m.max_rate_num, m.max_rate_den = num, den, maximum, max_den
            self.assertEqual(self.lib.uurb_gpu_message_valid(m, 1), 1)
        for field in ('rate_num', 'max_rate_num', 'rate_den', 'max_rate_den'):
            m = self.message(); setattr(m, field, 2)
            self.assertEqual(self.lib.uurb_gpu_message_valid(m, 1), 0)
            for kind in (2, 3):
                m = self.message(kind); setattr(m, field, 1)
                self.assertEqual(self.lib.uurb_gpu_message_valid(m, 0), 0)
        m = self.message(); m.version = 2
        self.assertEqual(self.lib.uurb_gpu_message_valid(m, 1), 0)

    def test_only_first_frame_carries_a_descriptor(self):
        for sequence, descriptor in [(1, 0), (2, 1)]:
            self.assertEqual(self.lib.uurb_gpu_message_valid(self.message(sequence=sequence), descriptor), 0)
        for kind in (2, 3):
            m = self.message(kind)
            self.assertEqual(self.lib.uurb_gpu_message_valid(m, 0), 1)
            self.assertEqual(self.lib.uurb_gpu_message_valid(m, 1), 0)
            m.width = 640
            self.assertEqual(self.lib.uurb_gpu_message_valid(m, 0), 0)

    def test_fresh_endpoint_is_private_connected_and_close_on_exec(self):
        with tempfile.TemporaryDirectory(prefix='uurb-endpoint-') as directory:
            path = Path(directory) / 'capture.sock'
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
                listener.bind(str(path)); path.chmod(0o600); listener.listen(2)
                for _ in range(2):
                    fd = self.lib.uurb_gpu_channel_connect(os.fsencode(path))
                    self.assertGreaterEqual(fd, 3)
                    try:
                        self.assertFalse(os.get_inheritable(fd))
                        self.assertEqual(self.lib.uurb_gpu_channel_check(fd), 0)
                        peer, _ = listener.accept()
                        peer.close()
                    finally:
                        os.close(fd)
                path.chmod(0o666)
                self.assertEqual(self.lib.uurb_gpu_channel_connect(os.fsencode(path)), -1)
                path.chmod(0o600); Path(directory).chmod(0o755)
                self.assertEqual(self.lib.uurb_gpu_channel_connect(os.fsencode(path)), -1)
                Path(directory).chmod(0o700)

    def test_private_seqpacket_socket_required(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with left, right, tempfile.TemporaryFile() as regular, socket.socket() as internet:
            self.assertEqual(self.lib.uurb_gpu_channel_check(left.fileno()), 0)
            for fd in (-1, regular.fileno(), internet.fileno()):
                self.assertNotEqual(self.lib.uurb_gpu_channel_check(fd), 0)
        left, right = socket.socketpair()
        with left, right:
            self.assertNotEqual(self.lib.uurb_gpu_channel_check(left.fileno()), 0)

    def test_descriptor_roundtrip_is_cloexec_and_not_pixels(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with left, right, tempfile.TemporaryFile() as allocation:
            message = self.message()
            self.assertEqual(self.lib.uurb_gpu_channel_send(left.fileno(), message, allocation.fileno(), 100), 0)
            output, fd = Message(), C.c_int(-1)
            self.assertEqual(self.lib.uurb_gpu_channel_receive(right.fileno(), output, fd, 100), 0)
            try:
                self.assertEqual(bytes(output), bytes(message))
                self.assertFalse(os.get_inheritable(fd.value))
                self.assertEqual(os.fstat(fd.value).st_ino, os.fstat(allocation.fileno()).st_ino)
            finally:
                os.close(fd.value)

    def test_bad_packets_close_all_received_descriptors_and_preserve_output(self):
        for payload, descriptor_count in [(bytes(72), 1), (bytes(self.message())[:-1], 1),
                                          (bytes(self.message()) + b'x', 1), (bytes(self.message()), 2),
                                          (bytes(self.message()), 8)]:
            left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            with left, right, tempfile.TemporaryFile() as allocation:
                baseline = len(os.listdir('/proc/self/fd'))
                rights = array.array('i', [allocation.fileno()] * descriptor_count)
                left.sendmsg([payload], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)])
                output, fd = self.message(kind=3, sequence=123), C.c_int(456)
                before = bytes(output)
                self.assertNotEqual(self.lib.uurb_gpu_channel_receive(right.fileno(), output, fd, 100), 0)
                self.assertEqual((bytes(output), fd.value), (before, 456))
                self.assertEqual(len(os.listdir('/proc/self/fd')), baseline)

    def test_timeout_and_disconnect_preserve_caller_output(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with left, right:
            output, fd = self.message(), C.c_int(456)
            before = bytes(output)
            for disconnected in (False, True):
                if disconnected:
                    left.close()
                self.assertNotEqual(self.lib.uurb_gpu_channel_receive(right.fileno(), output, fd, 0), 0)
                self.assertEqual((bytes(output), fd.value), (before, 456))

    def test_wrong_ack_poisons_publisher(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with left, right, tempfile.TemporaryFile() as allocation:
            ack = self.message(kind=2, sequence=2)
            self.assertEqual(self.lib.uurb_gpu_channel_send(right.fileno(), ack, -1, 100), 0)
            publisher = Publisher(socket_fd=left.fileno(), rate_den=1, max_rate_num=60000, max_rate_den=1001)
            frame = self.message()
            self.assertNotEqual(self.lib.uurb_gpu_publish(publisher, allocation.fileno(), frame.allocation_bytes,
                                frame.uuid, frame.width, frame.height, frame.timestamp), 0)
            self.assertEqual(publisher.failed, 1)
            self.assertEqual(publisher.sequence, 0)
            self.assertNotEqual(self.lib.uurb_gpu_publish(publisher, allocation.fileno(), frame.allocation_bytes,
                                frame.uuid, frame.width, frame.height, frame.timestamp), 0)

    def test_acknowledged_frames_share_one_fd_and_end_is_terminal(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with left, right, tempfile.TemporaryFile() as allocation:
            publisher = Publisher(socket_fd=left.fileno(), rate_den=1, max_rate_num=60000, max_rate_den=1001)
            frame = self.message()
            for sequence in (1, 2):
                # Test-only queued acknowledgments avoid threads; the real GPU
                # receiver acknowledges only after its GPU fence/release.
                ack = self.message(kind=2, sequence=sequence)
                self.assertEqual(self.lib.uurb_gpu_channel_send(right.fileno(), ack, -1, 100), 0)
                self.assertEqual(self.lib.uurb_gpu_publish(publisher, allocation.fileno(), frame.allocation_bytes,
                                 frame.uuid, frame.width, frame.height, frame.timestamp + sequence), 0)
                received, fd = Message(), C.c_int(-1)
                self.assertEqual(self.lib.uurb_gpu_channel_receive(right.fileno(), received, fd, 100), 0)
                try:
                    self.assertEqual(received.sequence, sequence)
                    self.assertGreater(received.ready_ns, 0)
                    self.assertEqual((received.rate_num, received.rate_den, received.max_rate_num, received.max_rate_den),
                                     (0, 1, 60000, 1001))
                    self.assertEqual(fd.value >= 0, sequence == 1)
                finally:
                    if fd.value >= 0:
                        os.close(fd.value)
            self.assertEqual(self.lib.uurb_gpu_publish_end(publisher), 0)
            end, fd = Message(), C.c_int(-1)
            self.assertEqual(self.lib.uurb_gpu_channel_receive(right.fileno(), end, fd, 100), 0)
            self.assertEqual((end.kind, end.sequence, fd.value), (3, 3, -1))
            self.assertNotEqual(self.lib.uurb_gpu_publish_end(publisher), 0)
            self.assertEqual(publisher.failed, 1)
            self.assertEqual(publisher.sequence, 2)
            self.assertNotEqual(self.lib.uurb_gpu_publish(publisher, allocation.fileno(), frame.allocation_bytes,
                                frame.uuid, frame.width, frame.height, frame.timestamp), 0)


class GpuRelayProbeValidationTests(unittest.TestCase):
    def test_dxvk_entrypoint_requires_actual_hook_and_matching_geometry(self):
        with self.assertRaises(ValueError):
            probe.run('h264', Path('/not-used'), pe_child=True)
        evidence = dict(windows_pe_client=True, dxgi_duplication_interface=True, metadata_and_lease_checks=True,
                        qpc_timestamp_checks=True, terminal_access_lost_checked=True, encode_timestamp_source='gpu-ready-qpc',
                        dxvk_creation_hook=True, capture_constructor='duplicate1', output_geometry_matches_capture=True,
                        hook_negative_checks=True)
        probe.validate_dxvk_evidence(evidence, 'duplicate1')
        probe.validate_dxvk_evidence(dict(evidence, capture_constructor='legacy'), 'legacy')
        for field in ('dxvk_creation_hook', 'capture_constructor', 'output_geometry_matches_capture', 'hook_negative_checks'):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                probe.validate_dxvk_evidence(dict(evidence, **{field: False}), 'duplicate1')
        with self.assertRaises(RuntimeError):
            probe.validate_dxvk_evidence(evidence, 'legacy')
        with self.assertRaises(ValueError):
            probe.run('h264', Path('/not-used'), dxvk_entrypoint='unsupported')

    def test_pe_evidence_cannot_be_substituted_by_winelib_or_source_pts(self):
        evidence = dict(windows_pe_client=True, dxgi_duplication_interface=True, metadata_and_lease_checks=True,
                        qpc_timestamp_checks=True, terminal_access_lost_checked=True, encode_timestamp_source='gpu-ready-qpc')
        probe.validate_pe_evidence(evidence)
        for field in evidence:
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                probe.validate_pe_evidence(dict(evidence, **{field: False}))
        with self.assertRaises(RuntimeError):
            probe.validate_pe_evidence(dict(evidence, encode_timestamp_source='pipewire-pts'))

    def test_diagnostic_tail_is_bounded(self):
        with tempfile.TemporaryFile() as log:
            log.write(b'x' * 20000 + b'end')
            log.flush()
            tail = probe.log_tail(log)
            self.assertEqual(len(tail), 6000)
            self.assertTrue(tail.endswith('end'))

    def startup(self, code):
        process = subprocess.Popen([sys.executable, '-u', '-c', code], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(probe.stop, process)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stderr.close)
        return process

    def test_ready_ignores_startup_noise_and_drains_stderr(self):
        process = self.startup("import os; os.write(1, b'Wine startup\\n'); "
                               "os.write(2, b'x' * 200000); os.write(1, b'READY\\n{}\\n')")
        remainder, errors = probe.wait_for_ready(process, 'READY', 5)
        output, _ = process.communicate(timeout=5)
        self.assertEqual(remainder + output, '{}\n')
        self.assertLessEqual(len(errors), 3500)

    def test_ready_accepts_split_marker(self):
        process = self.startup("import os,time; os.write(1, b'REA'); time.sleep(.05); os.write(1, b'DY\\n')")
        probe.wait_for_ready(process, 'READY', 5)
        process.communicate(timeout=5)

    def test_ready_reports_early_exit(self):
        process = self.startup("import sys; print('startup failed', file=sys.stderr)")
        with self.assertRaisesRegex(RuntimeError, 'startup failed'):
            probe.wait_for_ready(process, 'READY', 5)
        process.communicate(timeout=5)

    def test_ready_timeout_is_bounded(self):
        process = self.startup('import time; time.sleep(5)')
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'did not reach READY'):
            probe.wait_for_ready(process, 'READY', .1)
        self.assertLess(time.monotonic() - started, 2)
        probe.stop(process)

    def test_reports_must_agree_and_identify_distinct_roles(self):
        producer = dict(gpu_relayed=True, encoded=False, pixels_mapped=False, dmabuf_only=True,
                        frames=480, width=1920, height=1080)
        consumer = dict(gpu_channel_received=True, encoded=True, frames=480, width=1920, height=1080)
        probe.validate_pair(producer, consumer)
        for changes in [dict(frames=479), dict(width=1280), dict(gpu_channel_received=False)]:
            with self.assertRaises(RuntimeError):
                probe.validate_pair(producer, dict(consumer, **changes))
        with self.assertRaises(RuntimeError):
            probe.validate_pair(dict(producer, encoded=True), consumer)

    def test_invalid_codec_rejected_before_capture(self):
        with self.assertRaises(ValueError):
            probe.run('av1', Path('/not-used'))

    def test_invalid_channel_rejected_before_portal(self):
        result = subprocess.run(['/usr/bin/python3', str(ROOT / 'scripts/probe-wayland-portal.py'),
                                 '--gpu-relay-fd', '0'], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('Portal CreateSession:', result.stderr)


if __name__ == '__main__':
    unittest.main()
