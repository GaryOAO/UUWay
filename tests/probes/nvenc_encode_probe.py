#!/usr/bin/python3
"""Exercise standard Windows NVENC encoding in a disposable Wine prefix.
The PE client uses synthetic GPU textures. No desktop/UU account is accessed.
"""
import argparse
from fractions import Fraction
import json
import mmap
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile

from gpu_interop_probe import validate_pixels

ROOT = Path(__file__).resolve().parents[2]
STAGE = ROOT / 'build/native-presenter'


def validate_sequence_headers(headers, first_packets, codec):
    if codec not in ('h264', 'hevc') or not 0 < len(headers) <= 65536:
        raise RuntimeError('Invalid sequence header codec/size')
    wanted = {7, 8} if codec == 'h264' else {32, 33, 34}

    def parameter_sets(data):
        starts = list(re.finditer(rb'\x00\x00(?:\x00)?\x01', data))
        if not starts or starts[0].start() != 0:
            raise RuntimeError('Missing Annex B sequence prefix')
        result = {}
        for i, match in enumerate(starts):
            end = starts[i + 1].start() if i + 1 < len(starts) else len(data)
            unit = data[match.end():end].rstrip(b'\0')
            if not unit:
                raise RuntimeError('Empty sequence NAL unit')
            kind = unit[0] & 31 if codec == 'h264' else (unit[0] >> 1) & 63
            if kind in wanted and kind not in result:
                result[kind] = unit
        return result

    standalone, encoded = parameter_sets(headers), parameter_sets(first_packets)
    if set(standalone) != wanted or any(encoded.get(kind) != unit for kind, unit in standalone.items()):
        raise RuntimeError('Standalone sequence headers do not match the independently decoded stream')
    return dict(bytes=len(headers), nal_types=sorted(standalone), obtained_before_first_frame=True,
                matches_encoded_parameter_sets=True)


def validate_packets(packets, codec, frames=64):
    if codec not in ('h264', 'hevc') or frames < 33:
        raise ValueError('Expected a supported codec and at least 33 frames')
    if len(packets) != frames * 2 or any(row.get('codec') not in ('h264', 'hevc') for row in packets):
        raise RuntimeError('Unexpected total Windows packet count/codec')
    rows = [row for row in packets if row['codec'] == codec]
    if len(rows) != frames:
        raise RuntimeError('Missing Windows ABI encoded packets')
    for index, row in enumerate(rows):
        if (any(type(row.get(key)) is not int for key in ('frame', 'timestamp', 'bytes')) or
                type(row.get('idr')) is not bool or row['frame'] != index or
                row['timestamp'] != 1000 + index or not 0 < row['bytes'] <= 4 * 1024 * 1024):
            raise RuntimeError('Incorrect Windows packet order/timestamp/size')
    if not rows[0]['idr'] or not rows[21]['idr'] or not rows[32]['idr']:
        raise RuntimeError('Missing requested Windows IDR')
    return rows


def validate_stream_info(info, codec, rows):
    expected = dict(codec_name=codec, width=640, height=360, color_space='bt709',
                    color_transfer='bt709', color_primaries='bt709', color_range='tv')
    if info.get('streams') != [expected] or len(info.get('frames', [])) != len(rows):
        raise RuntimeError('Wrong codec, geometry, colorspace or decoded frame count')
    for frame, row in zip(info['frames'], rows):
        if frame.get('pict_type') not in ('I', 'P') or frame.get('key_frame') != int(row['idr']):
            raise RuntimeError('Wrong decoded picture type or IDR')


def run(source_format, missing_backend=False, input_bind='sampled', negotiated_rates=False):
    if source_format not in ('bgra', 'nv12'):
        raise ValueError('Expected bgra or nv12')
    if input_bind not in ('sampled', 'rtv', 'uav'):
        raise ValueError('Expected sampled, rtv or uav input bind')
    with tempfile.TemporaryDirectory(prefix='uurb-nvenc-encode-') as temporary:
        directory = Path(temporary)
        app = directory / 'app'
        app.mkdir()
        client = 'uu-nvenc-pe-query-probe.exe' if missing_backend else 'uu-nvenc-pe-encode-probe.exe'
        files = [client, 'uurb-nvenc-encode-loader.dll', 'd3d11.dll', 'dxgi.dll']
        if not missing_backend:
            files.append('uurb-nvenc-encode.dll.so')
        for name in files:
            shutil.copy2(STAGE / name, app / name)
        executable = app / client
        with executable.open('rb') as binary:
            if binary.read(2) != b'MZ':
                raise RuntimeError('Expected a real Windows PE executable')
        env = dict(os.environ, WINEPREFIX=str(directory / 'wine'), WINELOADER='/opt/wine-stable/bin/wine',
            WINEDEBUG='-all', WINEDLLOVERRIDES='mscoree,mshtml=;d3d11,dxgi=n;uurb-nvenc-encode-loader=n',
            DXVK_CONFIG_FILE=str(ROOT / 'config/dxvk-native.conf'), DXVK_LOG_PATH=str(directory))
        for name in ['DXVK_CONFIG', 'DXVK_FILTER_DEVICE_NAME', 'VK_INSTANCE_LAYERS', 'WINEDLLPATH']:
            env.pop(name, None)
        env.pop('UURB_NVENC_NEGOTIATION_PROBE', None)
        if negotiated_rates:
            env['UURB_NVENC_NEGOTIATION_PROBE'] = '1'
        try:
            output_directory = 'Z:' + str(directory).replace('/', '\\')
            arguments = ['uurb-nvenc-encode-loader.dll', '--missing-backend'] if missing_backend else [output_directory, source_format, input_bind]
            with (directory / 'probe.log').open('w') as log:
                process = subprocess.Popen(['xvfb-run', '-a', '--server-args=-screen 0 640x360x24 -nolisten tcp',
                    '/opt/wine-stable/bin/wine', str(executable), *arguments],
                    env=env, cwd=app, stdout=log, stderr=log, start_new_session=True)
                try:
                    code = process.wait(timeout=90)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=5)
            log = (directory / 'probe.log').read_text()
            marker = 'ABI missing backend checks passed;' if missing_backend else 'ENCODE ABI checks passed;'
            if code or marker not in log:
                raise RuntimeError(f'Windows encoder probe failed ({code}):\n' + log[-6500:])
            if missing_backend:
                return dict(windows_pe_client=True, missing_backend_rejected=True,
                            uu_session_tested=False, no_capability_advertised=True)
            packets = [json.loads(line.removeprefix('ENCODE ')) for line in log.splitlines() if line.startswith('ENCODE {')]
            if len(packets) != 128 or {row['codec'] for row in packets} != {'h264', 'hevc'}:
                raise RuntimeError('Unexpected total Windows packet count/codec')
            decoded = []
            for codec in ('h264', 'hevc'):
                if negotiated_rates:
                    for frame in (44, 52):
                        if log.splitlines().count(f'PRESET_SWITCH {codec} {frame} passed') != 1:
                            raise RuntimeError('Missing actual preset/tuning reconfiguration evidence')
                if log.splitlines().count('SEQUENCE_EX ' + codec + ' preinit_and_active_headers_match') != 1:
                    raise RuntimeError('Missing pre-initialization and active sequence-header checks')
                rows = validate_packets(packets, codec)
                if negotiated_rates and not all(rows[frame]['idr'] for frame in (44, 52)):
                    raise RuntimeError('Preset/tuning switch did not produce the requested IDR')
                stream = directory / (codec + '.bin')
                if stream.stat().st_size != sum(row['bytes'] for row in rows):
                    raise RuntimeError('Missing or extra compressed bytes')
                with stream.open('rb') as packets_file:
                    sequence_info = validate_sequence_headers((directory / (codec + '.sequence.bin')).read_bytes(),
                                                              packets_file.read(rows[0]['bytes']), codec)
                info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-f', codec,
                    '-show_entries', 'stream=codec_name,width,height,color_space,color_transfer,color_primaries,color_range:frame=key_frame,pict_type',
                    '-of', 'json', str(stream)], text=True, timeout=20))
                validate_stream_info(info, codec, rows)
                rate_segments = []
                if negotiated_rates:
                    with stream.open('rb') as source:
                        for start, stop, expected_rate in ((0, 32, '120/1' if codec == 'hevc' else '30000/1001'),
                                                           (32, 64, '60/1')):
                            source.seek(sum(row['bytes'] for row in rows[:start]))
                            encoded = source.read(sum(row['bytes'] for row in rows[start:stop]))
                            timing = subprocess.run(['ffmpeg', '-v', 'trace', '-f', codec, '-i', '-',
                                '-c:v', 'copy', '-bsf:v', 'trace_headers', '-f', 'null', '-'],
                                input=encoded, capture_output=True, check=True, timeout=10)
                            header_text = timing.stderr.decode(errors='replace')
                            prefix = 'vui_' if codec == 'hevc' else ''
                            ticks = set(map(int, re.findall(r'\b' + prefix + r'num_units_in_tick\s+[01]+\s+=\s+(\d+)', header_text)))
                            scales = set(map(int, re.findall(r'\b' + prefix + r'time_scale\s+[01]+\s+=\s+(\d+)', header_text)))
                            if len(ticks) != 1 or len(scales) != 1 or not next(iter(ticks)):
                                raise RuntimeError(f'Missing or inconsistent {codec} VUI timing fields')
                            # r_frame_rate is a demuxer guess (can be field rate).
                            # Progressive H.264 has two clock ticks per frame;
                            # HEVC VUI's units/time_scale specifies frame rate.
                            if codec == 'h264' and set(re.findall(r'\bframe_mbs_only_flag\s+[01]+\s+=\s+(\d+)', header_text)) != {'1'}:
                                raise RuntimeError('Expected progressive H.264 timing')
                            actual_rate = Fraction(next(iter(scales)), next(iter(ticks)) * (2 if codec == 'h264' else 1))
                            if actual_rate != Fraction(expected_rate):
                                raise RuntimeError(f'Encoded VUI frame-rate mismatch: {codec} frame {start}, expected {expected_rate}, got {actual_rate}')
                            rate_segments.append(dict(start_frame=start, end_frame=stop - 1, rate=str(actual_rate),
                                                      verified_from_vui=True))
                # Only synthetic test pixels, with a bounded temporary decode.
                with tempfile.TemporaryFile() as raw:
                    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', codec, '-i', str(stream),
                        '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                        stdout=raw, stderr=subprocess.PIPE, check=True, timeout=30)
                    frame_bytes = 640 * 360 * 3
                    if os.fstat(raw.fileno()).st_size != len(rows) * frame_bytes:
                        raise RuntimeError('Incomplete decoded Windows stream')
                    with mmap.mmap(raw.fileno(), 0, access=mmap.ACCESS_READ) as pixels:
                        for index in range(len(rows)):
                            validate_pixels(pixels[index*frame_bytes:(index+1)*frame_bytes],
                                            640, 360, 'sequence', index)
                decoded.append(dict(codec=codec, frames=len(rows), all_frames_pixel_checked=True,
                                    timestamps_verified=True, sequence_headers=sequence_info,
                                    sequence_ex_before_initialize_matches_active=True,
                                    negotiated_rate_segments=rate_segments,
                                    idr_frames=[row['frame'] for row in rows if row['idr']]))
            return dict(windows_pe_client=True, standard_nvenc_function_table=True,
                encoding_implemented=True, synthetic_only=True, uu_session_tested=False,
                source_format=source_format, raw_pixel_upload=False, raw_pixel_readback=False,
                input_bind=input_bind, sampleable_gpu_copy=input_bind != 'sampled',
                independent_decode_on_cpu=True, retained_output_buffers=2, registered_source_textures=2,
                exact_reference_settings_only=not negotiated_rates, decoded=decoded,
                negotiated_vbr_aq_rates=negotiated_rates, dynamic_framerate_and_bitrate=negotiated_rates,
                p1_ull_to_p4_hq_and_back=negotiated_rates,
                unrequested_reconfigure_idr_rejected=negotiated_rates,
                hardware_initialized_before_first_frame=True,
                resource_and_output_limits_verified=16, cross_session_handles_rejected=True,
                close_with_locked_output_and_mapped_input=True,
                abi_negative_checks_passed=True)
        finally:
            subprocess.run(['/opt/wine-stable/bin/wineserver', '-k'], env=env, capture_output=True, timeout=10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-format', choices=['bgra', 'nv12'], required=True)
    parser.add_argument('--missing-backend', action='store_true')
    parser.add_argument('--input-bind', choices=['sampled', 'rtv', 'uav'], default='sampled')
    parser.add_argument('--negotiated-rates', action='store_true')
    options = parser.parse_args()
    print(json.dumps(run(options.source_format, options.missing_backend, options.input_bind, options.negotiated_rates), indent=2))
