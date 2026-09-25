"""Real native text client against owned protocol fixtures; no IME/UU input."""
import ctypes
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TextReconnectTests(unittest.TestCase):
    def test_missing_late_and_replaced_endpoint_without_replaying_failed_text(self):
        with tempfile.TemporaryDirectory(prefix='uurb-text-reconnect-') as name:
            parent = Path(name); library = parent / 'client.so'; endpoint = parent / 'ime.sock'
            subprocess.run(['gcc', '-shared', '-fPIC', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror',
                str(ROOT / 'src/native_text_client.c'), '-o', str(library)],
                check=True, capture_output=True, timeout=15)
            native = ctypes.CDLL(str(library))
            native.uurb_native_text_send.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
            native.uurb_native_text_send.restype = ctypes.c_int

            def send(text):
                return native.uurb_native_text_send(bytes(endpoint), text, len(text))

            self.assertEqual(send(b'failed-before-start'), 0)
            for expected, acknowledge in [(b'new-after-start', True), (b'new-lost-ack', False), (b'new-after-restart', True)]:
                received = []; failures = []
                with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
                    listener.bind(str(endpoint)); endpoint.chmod(0o600)
                    # Bound-but-not-listening is also not a ready text service.
                    self.assertEqual(send(b'failed-before-listen'), 0)
                    listener.listen(4); listener.settimeout(0.2)

                    def server():
                        try:
                            client, _ = listener.accept()
                            with client:
                                client.settimeout(1)
                                request = client.recv(16384)
                                magic, version, sequence, size = struct.unpack('<4I', request[:16])
                                if (magic, version, sequence, size) != (0x54525555, 1, 1, len(expected)):
                                    raise ValueError('Invalid owned request header')
                                received.append(request[16:])
                                if acknowledge:
                                    client.send(struct.pack('<4I', magic, version, sequence, 0))
                            try:
                                extra, _ = listener.accept()
                                extra.close()
                                raise ValueError('Unexpected automatic replay')
                            except TimeoutError:
                                pass
                        except BaseException as error:
                            failures.append(type(error).__name__)

                    worker = threading.Thread(target=server)
                    worker.start()
                    try:
                        result = send(expected)
                    finally:
                        worker.join(timeout=3)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(failures, [])
                    self.assertEqual(received, [expected])
                    self.assertEqual(result, int(acknowledge))
                # Leave a stale inode briefly; next request must fail promptly.
                self.assertEqual(send(b'failed-while-down'), 0)
                endpoint.unlink()


if __name__ == '__main__':
    unittest.main()
