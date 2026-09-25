import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('nvenc_encode_probe', ROOT / 'tests/probes/nvenc_encode_probe.py')
probe = importlib.util.module_from_spec(spec)
with patch.object(sys, 'path', [str(ROOT / 'tests/probes'), *sys.path]):
    spec.loader.exec_module(probe)


class NvencEncodeProbeTests(unittest.TestCase):
    def test_standalone_headers_match_all_decoded_parameter_sets(self):
        for codec, kinds in [('h264', (7, 8)), ('hevc', (32, 33, 34))]:
            units = [b'\x00\x00\x00\x01' + bytes([kind if codec == 'h264' else kind << 1, 1, 123]) for kind in kinds]
            headers = b''.join(units)
            frame = b'\x00\x00\x01' + bytes([5 if codec == 'h264' else 19 << 1, 1, 55])
            result = probe.validate_sequence_headers(headers, headers + frame, codec)
            self.assertEqual(result['nal_types'], list(kinds))
            for invalid in (b'', headers[2:], units[0], headers[:-1] + b'X'):
                with self.subTest(codec=codec, invalid=invalid), self.assertRaises(RuntimeError):
                    probe.validate_sequence_headers(invalid, headers + frame, codec)

    @staticmethod
    def packets():
        return [dict(codec=codec, frame=i, timestamp=1000+i, bytes=100, idr=i in (0, 21, 32))
                for codec in ('h264', 'hevc') for i in range(64)]

    @staticmethod
    def stream(codec, rows):
        return dict(streams=[dict(codec_name=codec, width=640, height=360, color_space='bt709',
                    color_transfer='bt709', color_primaries='bt709', color_range='tv')],
                    frames=[dict(pict_type='I' if row['idr'] else 'P', key_frame=int(row['idr'])) for row in rows])

    def test_all_frames_and_both_codecs_are_required(self):
        packets = self.packets()
        for codec in ('h264', 'hevc'):
            self.assertEqual(len(probe.validate_packets(packets, codec)), 64)
        for broken in (packets[:-1], packets + [packets[0]], packets[:64] * 2):
            with self.assertRaises(RuntimeError):
                probe.validate_packets(broken, 'h264')
        packets[64]['codec'] = 'av1'
        with self.assertRaises(RuntimeError):
            probe.validate_packets(packets, 'h264')

    def test_packet_corruption_is_rejected(self):
        for field, value in [('frame', 0), ('timestamp', 1000), ('bytes', 0), ('bytes', -1),
                             ('bytes', 4*1024*1024+1), ('bytes', 1.5), ('idr', 'false')]:
            packets = self.packets()
            packets[1][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                probe.validate_packets(packets, 'h264')

    def test_initial_forced_and_bitrate_change_idrs_are_required(self):
        for index in (0, 21, 32):
            packets = self.packets()
            packets[index]['idr'] = False
            with self.assertRaisesRegex(RuntimeError, 'IDR'):
                probe.validate_packets(packets, 'h264')

    def test_wrong_source_and_codec_are_rejected_before_launch(self):
        with self.assertRaises(ValueError):
            probe.run('p010')
        with self.assertRaises(ValueError):
            probe.validate_packets(self.packets(), 'av1')
        with self.assertRaises(ValueError):
            probe.validate_packets([], 'h264', frames=2)
        with self.assertRaises(ValueError):
            probe.run('nv12', input_bind='unknown')

    def test_real_initialization_and_bounded_sequence_export_are_wired(self):
        source = (ROOT / 'src/uu_nvenc_encode.c').read_text()
        init = source.split('static NVENCSTATUS NVENCAPI encode_initialize(', 1)[1].split('static NVENCSTATUS NVENCAPI register_resource(', 1)[0]
        self.assertLess(init.index('uurb_d3d11_encoder_initialize('), init.index('s->token = token'))
        self.assertIn('api->nvEncGetSequenceParams = sequence_params;', source)
        native = (ROOT / 'src/native_cuda_encode_session.c').read_text()
        self.assertIn('s->config = *requested->encodeConfig;', native)
        self.assertIn('s->init.encodeConfig = &s->config;', native)
        self.assertIn('.inBufferSize = sizeof(buffer)', native)
        self.assertIn('if (written > capacity) return NV_ENC_ERR_NOT_ENOUGH_BUFFER;', native)

    def test_non_sampleable_inputs_use_only_a_retained_gpu_copy(self):
        source = (ROOT / 'src/uu_d3d11_frame_adapter.c').read_text()
        self.assertIn('if (!(src.BindFlags & D3D11_BIND_SHADER_RESOURCE))', source)
        self.assertIn('copy.BindFlags = D3D11_BIND_SHADER_RESOURCE;', source)
        self.assertIn('ID3D11DeviceContext_CopyResource(deferred,', source)
        self.assertIn('ID3D11DeviceContext_FinishCommandList(deferred, FALSE, &s->commands)', source)

    def test_decoder_must_confirm_geometry_and_color_contract(self):
        rows = probe.validate_packets(self.packets(), 'h264')
        info = self.stream('h264', rows)
        probe.validate_stream_info(info, 'h264', rows)
        for field, value in [('width', 1280), ('height', 720), ('color_space', 'smpte170m'),
                             ('color_range', 'pc'), ('codec_name', 'hevc')]:
            broken = copy.deepcopy(info)
            broken['streams'][0][field] = value
            with self.assertRaises(RuntimeError):
                probe.validate_stream_info(broken, 'h264', rows)

    def test_decoder_must_confirm_all_pictures_and_idrs(self):
        rows = probe.validate_packets(self.packets(), 'hevc')
        info = self.stream('hevc', rows)
        for field, value in [('pict_type', 'B'), ('pict_type', '?'), ('key_frame', 0)]:
            broken = copy.deepcopy(info)
            broken['frames'][0][field] = value
            with self.assertRaises(RuntimeError):
                probe.validate_stream_info(broken, 'hevc', rows)
        info['frames'].pop()
        with self.assertRaises(RuntimeError):
            probe.validate_stream_info(info, 'hevc', rows)

    def test_probe_stays_isolated_and_not_named_as_production_dll(self):
        build = (ROOT / 'scripts/build-nvenc-encode-probe.sh').read_text()
        self.assertIn('uurb-nvenc-encode-loader.dll', build)
        self.assertNotIn('"$stage/nvEncodeAPI64.dll"', build)
        runner = (ROOT / 'tests/probes/nvenc_encode_probe.py').read_text()
        self.assertIn('TemporaryDirectory(', runner)
        self.assertIn("WINEPREFIX=str(directory / 'wine')", runner)
        self.assertIn("'xvfb-run'", runner)
        self.assertIn("binary.read(2) != b'MZ'", runner)

    def test_backend_never_uses_raw_cpu_readback_or_upload(self):
        source = (ROOT / 'src/uu_nvenc_encode.c').read_text()
        source += (ROOT / 'src/uu_nvenc_encode_probe.c').read_text()
        source += (ROOT / 'tests/probes/d3d11_nv12_fixture.h').read_text()
        for forbidden in ('D3D11_USAGE_STAGING', 'D3D11_CPU_ACCESS_READ', 'ID3D11DeviceContext_Map',
                          'UpdateSubresource', 'cuMemcpyDtoH', 'cuMemcpy2D', 'CU_MEMORYTYPE_HOST'):
            self.assertNotIn(forbidden, source)
        self.assertIn('memcpy(output->bytes, packet.data, packet.size)', source)
        self.assertIn('PACKET_LIMIT', source)
        self.assertIn('!ZERO_FIELD(params, reserved1)', source)


if __name__ == '__main__':
    unittest.main()
