import importlib.util
import copy
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('gpu_probe', ROOT / 'tests/probes/gpu_interop_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class GpuInteropProbeTests(unittest.TestCase):
    def test_pattern_validation_checks_every_quadrant(self):
        pixels = bytearray()
        for y in range(8):
            for x in range(8):
                pixels.extend(((255, 0, 0) if y < 4 else (0, 0, 255)) if x < 4
                              else ((0, 255, 0) if y < 4 else (255, 255, 255)))
        self.assertEqual(len(probe.validate_pixels(pixels, 8, 8, 'pattern')), 5)
        for x, y in [(1, 1), (7, 1), (5, 5), (1, 7), (7, 7)]:
            broken = pixels.copy()
            broken[(y * 8 + x) * 3:(y * 8 + x) * 3 + 3] = bytes(3)
            with self.assertRaisesRegex(RuntimeError, 'decoded incorrectly'):
                probe.validate_pixels(broken, 8, 8, 'pattern')

    def test_black_successful_bitstream_is_not_accepted(self):
        with self.assertRaisesRegex(RuntimeError, 'decoded incorrectly'):
            probe.validate_pixels(bytes(8 * 8 * 3), 8, 8, 'clear')

    def test_truncated_decoded_image_is_not_accepted(self):
        with self.assertRaisesRegex(RuntimeError, 'Incomplete'):
            probe.validate_pixels(bytes(3), 8, 8, 'pattern')

    def test_lossy_red_is_accepted_with_bounded_tolerance(self):
        self.assertEqual(probe.validate_pixels(bytes((253, 0, 0)) * 64, 8, 8, 'clear'),
                         [(253, 0, 0)] * 5)
        with self.assertRaises(RuntimeError):
            probe.validate_pixels(bytes((230, 0, 0)) * 64, 8, 8, 'clear')

    def test_gpu_source_path_has_no_raw_cpu_readback_or_upload(self):
        source = (ROOT / 'src/uu_gpu_interop_probe.c').read_text()
        source += (ROOT / 'src/uu_dxvk_gpu_interop.h').read_text()
        source += (ROOT / 'src/uu_d3d11_encode_session.c').read_text()
        native = (ROOT / 'src/native_cuda_encode_session.c').read_text()
        adapter = (ROOT / 'src/uu_d3d11_frame_adapter.c').read_text()
        for forbidden in ['D3D11_USAGE_STAGING', 'D3D11_CPU_ACCESS_READ',
                          'ID3D11DeviceContext_Map', 'UpdateSubresource',
                          'cuMemcpyDtoH', 'cuMemcpy2D', 'CU_MEMORYTYPE_HOST']:
            self.assertNotIn(forbidden, source + native + adapter)
        self.assertIn('VK_QUEUE_FAMILY_EXTERNAL', source)
        self.assertIn('id.deviceUUID', source)
        self.assertIn('NV_ENC_INPUT_RESOURCE_TYPE_CUDAARRAY', native)

    def test_adapter_submission_reuses_recorded_work_and_preserves_state(self):
        adapter = (ROOT / 'src/uu_d3d11_frame_adapter.c').read_text()
        submit = adapter.split('HRESULT uurb_d3d11_frame_adapter_submit(', 1)[1]
        self.assertIn('ID3D11DeviceContext_ExecuteCommandList(immediate, s->commands, TRUE)', submit)
        self.assertIn('D3D11_DEVICE_CONTEXT_IMMEDIATE', submit)
        self.assertIn('same_device', submit)
        for forbidden in ['D3DCompile', 'CreateTexture2D', 'CreateDeferredContext',
                          'FinishCommandList', 'calloc', 'GetData', 'Flush(']:
            self.assertNotIn(forbidden, submit)

    def test_native_encoding_resolves_driver_without_export_interposition(self):
        native = (ROOT / 'src/native_cuda_encode_session.c').read_text()
        self.assertIn('pthread_once(&driver_once, load_driver)', native)
        self.assertIn('dlsym(library, "NvEncodeAPICreateInstance")', native)
        self.assertIn('dlsym(library, "NvEncodeAPIGetMaxSupportedVersion")', native)
        self.assertNotIn('NV(NvEncodeAPI', native)
        build = (ROOT / 'scripts/build-gpu-interop-probe.sh').read_text()
        self.assertIn('nvenc_symbol_collision.o', build)
        self.assertNotIn('-lnvidia-encode', build)

    def test_nv12_spatial_validation_rejects_flips_and_plane_misplacement(self):
        pixels = bytearray()
        for y in range(8):
            for x in range(8):
                pixels.extend(((255, 0, 0) if y < 4 else (0, 0, 255)) if x < 4
                              else ((0, 255, 0) if y < 4 else (255, 255, 255)))
        probe.validate_pixels(pixels, 8, 8, 'nv12_pattern')
        flipped = b''.join(pixels[y*24:(y+1)*24] for y in reversed(range(8)))
        with self.assertRaisesRegex(RuntimeError, 'decoded incorrectly'):
            probe.validate_pixels(flipped, 8, 8, 'nv12_pattern')
        with self.assertRaisesRegex(RuntimeError, 'decoded incorrectly'):
            probe.validate_pixels(bytes((255, 0, 0)) * 64, 8, 8, 'nv12_pattern')

    def test_sequence_rejects_stale_and_reordered_frames(self):
        for frame in range(64):
            color = ((frame & 3) * 85, ((frame >> 2) & 3) * 85, ((frame >> 4) & 3) * 85)
            pixels = bytes(color) * 64
            probe.validate_pixels(pixels, 8, 8, 'sequence', frame)
            with self.assertRaisesRegex(RuntimeError, 'decoded incorrectly'):
                probe.validate_pixels(pixels, 8, 8, 'sequence', (frame + 1) % 64)

    def test_nv12_validation_checks_both_planes_and_rgb_conversion(self):
        for frame in range(64):
            color = ((frame & 3) * 85, ((frame >> 2) & 3) * 85, ((frame >> 4) & 3) * 85)
            probe.validate_pixels(bytes(color) * 64, 8, 8, 'nv12', frame)
            r, g, b = [value / 255 for value in color]
            planes = bytes((round(16 + 219 * (0.2126*r + 0.7152*g + 0.0722*b)),
                            round(128 + 224 * (-0.114572*r - 0.385428*g + 0.5*b)),
                            round(128 + 224 * (0.5*r - 0.454153*g - 0.045847*b))))
            probe.validate_pixels(planes * 64, 8, 8, 'nv12_planes', frame)
        with self.assertRaisesRegex(RuntimeError, 'decoded incorrectly'):
            probe.validate_pixels(bytes((16, 128, 128)) * 64, 8, 8, 'nv12_planes', 1)

    @staticmethod
    def metadata():
        return [dict(codec=codec, frame=i, timestamp=1000+i, bytes=100,
                     idr=i in {0, 2, 3}, reconfigured=i == 3)
                for codec in ['h264', 'hevc'] for i in range(6)]

    def test_session_metadata_checks_timestamps_idrs_and_reconfigure(self):
        entries = self.metadata()
        probe.validate_metadata(entries, 6)
        for index, field, value in [(1, 'frame', 0), (1, 'timestamp', 1000),
                                    (0, 'bytes', 0), (2, 'idr', False),
                                    (3, 'reconfigured', False)]:
            broken = copy.deepcopy(entries)
            broken[index][field] = value
            with self.assertRaises(RuntimeError):
                probe.validate_metadata(broken, 6)
        with self.assertRaises(RuntimeError):
            probe.validate_metadata(entries[:-1], 6)
        with self.assertRaises(RuntimeError):
            probe.validate_metadata(entries + [dict(entries[0], codec='unknown')], 6)

    def test_session_restores_cuda_context_and_disallows_reordering(self):
        native = (ROOT / 'src/native_cuda_encode_session.c').read_text()
        self.assertIn('cuCtxPushCurrent', native)
        self.assertIn('cuCtxPopCurrent', native)
        self.assertIn('timestamp <= s->last_timestamp', native)
        reference = (ROOT / 'src/native_nvenc_reference_config.h').read_text()
        self.assertIn('uurb_nvenc_reference_config(', native)
        self.assertIn('(*config).frameIntervalP = 1', reference)
        self.assertIn('(*config).rcParams.enableLookahead = 0', reference)
        self.assertIn('nvEncReconfigureEncoder', native)


if __name__ == '__main__':
    unittest.main()
