import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('capture_encode_probe', ROOT / 'scripts/probe-wayland-portal.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
BINARY = ROOT / 'build/native-presenter/uu-pipewire-native-probe'


def color_image(swap=False):
    pixels = bytearray(192 * 108 * 3)
    for x0, y0, channel in [(10, 10, 1 if swap else 0), (90, 10, 0 if swap else 1), (10, 60, 2)]:
        for y in range(y0, y0 + 20):
            for x in range(x0, x0 + 30):
                pixels[(y * 192 + x) * 3 + channel] = 240
    return pixels


class CaptureEncodeValidationTests(unittest.TestCase):
    def test_color_centroids_and_invalid_inputs(self):
        pixels = color_image()
        self.assertEqual(probe.color_centroid(pixels, 0), (24.5, 19.5))
        self.assertEqual(probe.color_centroid(pixels, 1), (104.5, 19.5))
        self.assertEqual(probe.color_centroid(pixels, 2), (24.5, 69.5))
        for value, channel in [(b'', 0), (pixels, -1), (pixels, 3), (None, 0)]:
            with self.subTest(channel=channel), self.assertRaises(ValueError):
                probe.color_centroid(value, channel)
        with self.assertRaisesRegex(RuntimeError, 'missing'):
            probe.color_centroid(bytes(len(pixels)), 0)

    def validate(self, *, codec='h264', swap=False, frames=12, decoded_frames=12, distinct=12, statistics=12, warmup=False):
        report = dict(codec=codec, encoded=True, width=1920, height=1080, frames=frames)
        info = dict(streams=[dict(codec_name=codec, width=1920, height=1080, nb_read_frames=str(decoded_frames))])
        responses = [json.dumps(info),
                     'lavfi.signalstats.YMIN=16\nlavfi.signalstats.YMAX=235\n' * statistics,
                     bytes(color_image(swap)),
                     '# header\n' + ''.join(f'0, {i}, {i}, 1, 10, {i % distinct:032x}\n' for i in range(decoded_frames))]
        with tempfile.TemporaryFile() as stream:
            stream.write(b'test-compressed-placeholder')
            stream.flush()
            with patch.object(probe.subprocess, 'run', side_effect=[subprocess.CompletedProcess([], 0, out) for out in responses]) as run:
                result = probe.validate_encoded_capture(stream, report, color_fixture=True, fixture_warmup=warmup)
                self.assertEqual('-ss' in run.call_args_list[2].args[0], warmup)
                for call in run.call_args_list:
                    self.assertTrue(call.kwargs['check'])
                    self.assertEqual(call.kwargs['timeout'], 20)
                    self.assertEqual(call.kwargs['pass_fds'], (stream.fileno(),))
                return result

    def test_both_codec_reports_require_pixels_and_animation(self):
        for codec in ['h264', 'hevc']:
            with self.subTest(codec=codec):
                result = self.validate(codec=codec)
                self.assertTrue(result['color_fixture_verified'])
                self.assertEqual(result['distinct_decoded_frames'], 12)
                self.assertFalse(result['persistent_recording'])

    def test_fixture_warmup_still_requires_valid_pixels_and_motion(self):
        self.assertTrue(self.validate(warmup=True)['color_fixture_verified'])
        with self.assertRaisesRegex(RuntimeError, 'colors or orientation'):
            self.validate(warmup=True, swap=True)
        with self.assertRaisesRegex(RuntimeError, 'frozen'):
            self.validate(warmup=True, distinct=1)

    def test_channel_swap_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'colors or orientation'):
            self.validate(swap=True)

    def test_frozen_animation_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'frozen'):
            self.validate(distinct=1)

    def test_missing_frames_and_statistics_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'count/geometry'):
            self.validate(decoded_frames=11)
        with self.assertRaisesRegex(RuntimeError, 'statistics missing'):
            self.validate(statistics=11)

    def test_invalid_worker_report_and_size_fail_before_decoder(self):
        base = dict(codec='h264', encoded=True, width=1920, height=1080, frames=1)
        with tempfile.TemporaryFile() as stream, patch.object(probe.subprocess, 'run') as run:
            for changes in [dict(codec='av1'), dict(encoded=False), dict(frames=0), dict(width=None), dict(frames=True), {}]:
                with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                    probe.validate_encoded_capture(stream, dict(base, **changes))
            stream.truncate(64 * 1024 * 1024 + 1)
            with self.assertRaisesRegex(RuntimeError, 'size outside bound'):
                probe.validate_encoded_capture(stream, base)
            run.assert_not_called()

    def test_color_option_requires_encoding_before_portal(self):
        result = subprocess.run(['/usr/bin/python3', str(ROOT / 'scripts/probe-wayland-portal.py'), '--check-color-fixture'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('Portal CreateSession:', result.stderr)

    @unittest.skipUnless(BINARY.is_file(), 'Build native probe first')
    def test_encoder_rejects_unsafe_output_fds_before_capture(self):
        with socket.socket() as portal, tempfile.TemporaryFile() as writable:
            # A valid inherited FD passes argument validation but is never used:
            # every output below must fail before connecting to PipeWire/Vulkan.
            readonly = os.open('/proc/self/fd/' + str(writable.fileno()), os.O_RDONLY)
            try:
                for output in [str(portal.fileno()), str(readonly), '1', '999999', 'not-a-fd']:
                    with self.subTest(output=output):
                        result = subprocess.run([str(BINARY), str(portal.fileno()), '1', '--encode-h264', output],
                            pass_fds=(portal.fileno(), readonly), capture_output=True, text=True, timeout=5)
                        self.assertEqual(result.returncode, 2)
                        self.assertIn('writable regular output FD', result.stderr)
            finally:
                os.close(readonly)


if __name__ == '__main__':
    unittest.main()
