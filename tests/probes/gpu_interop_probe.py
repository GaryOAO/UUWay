#!/usr/bin/env python3
"""Run only synthetic GPU interop in a disposable authenticated Wine session."""
import json
import mmap
import os
import signal
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def validate_pixels(pixels, width, height, content, frame=0):
    if len(pixels) != width * height * 3:
        raise RuntimeError('Incomplete decoded frame')
    samples = []
    for x, y, expected in [(width//8, height//8, (255, 0, 0)),
                           (width*7//8, height//8, (0, 255, 0)),
                           (width*5//8, height*5//8, (255, 255, 255)),
                           (width//8, height*7//8, (0, 0, 255)),
                           (width*7//8, height*7//8, (255, 255, 255))]:
        offset = (y * width + x) * 3
        sample = tuple(pixels[offset:offset + 3])
        if content == 'clear':
            expected = (255, 0, 0)
        elif content in ('sequence', 'nv12', 'nv12_planes'):
            expected = ((frame & 3) * 85, ((frame >> 2) & 3) * 85, ((frame >> 4) & 3) * 85)
            if content == 'nv12_planes':
                r, g, b = [value / 255 for value in expected]
                expected = (round(16 + 219 * (0.2126*r + 0.7152*g + 0.0722*b)),
                            round(128 + 224 * (-0.114572*r - 0.385428*g + 0.5*b)),
                            round(128 + 224 * (0.5*r - 0.454153*g - 0.045847*b)))
        if any(abs(a - b) > 8 for a, b in zip(sample, expected)):
            raise RuntimeError(f'GPU pattern decoded incorrectly at frame {frame}: {sample} != {expected}')
        samples.append(sample)
    return samples


def validate_metadata(entries, count):
    if len(entries) != count * 2 or any(entry['codec'] not in {'h264', 'hevc'} for entry in entries):
        raise RuntimeError('Incorrect codec or total packet count')
    for codec in ['h264', 'hevc']:
        frames = [entry for entry in entries if entry['codec'] == codec]
        if len(frames) != count:
            raise RuntimeError('Incorrect encoded frame count')
        for index, frame in enumerate(frames):
            if frame['frame'] != index or frame['timestamp'] != 1000 + index or frame['bytes'] <= 0:
                raise RuntimeError('Incorrect frame ordering, timestamp or packet size')
            if frame['reconfigured'] != (count > 1 and index == count // 2):
                raise RuntimeError('Incorrect reconfiguration boundary')
            if index in {0, count // 3, count // 2} and not frame['idr']:
                raise RuntimeError('Missing requested IDR')


def run(content, width, height, frames=1, bridge_session=False):
    wine = '/opt/wine-stable/bin/wine'
    with tempfile.TemporaryDirectory(prefix='uurb-interop-') as directory:
        directory = Path(directory)
        env = dict(os.environ, WINEPREFIX=str(directory / 'wine'), WINELOADER=wine,
            WINEDEBUG='-all', WINEDLLOVERRIDES='mscoree,mshtml=;d3d11,dxgi=n',
            DXVK_CONFIG_FILE=str(ROOT / 'config/dxvk-native.conf'), DXVK_LOG_PATH=str(directory))
        for key in ['DXVK_CONFIG', 'DXVK_FILTER_DEVICE_NAME', 'WINEDLLPATH', 'VK_INSTANCE_LAYERS',
                    'UURB_GPU_INSPECT_PIXEL', 'UURB_D3D11_ENCODE_SESSION']:
            env.pop(key, None)
        if bridge_session:
            env['UURB_D3D11_ENCODE_SESSION'] = '1'
        try:
            command = ['xvfb-run', '-a', '--server-args=-screen 0 1920x1080x24 -nolisten tcp',
                str(ROOT / 'build/native-presenter/uu-gpu-interop-probe.exe'), str(directory), content,
                str(width), str(height), str(frames)]
            with (directory / 'probe.log').open('w') as log:
                process = subprocess.Popen(command, env=env, cwd=directory,
                    stdout=log, stderr=log, start_new_session=True)
                try:
                    result = process.wait(timeout=90)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=5)
            report = {'synthetic_only': True, 'uu_session_tested': False, 'returncode': result, 'content': content,
                'source_texture_format': 'NV12' if content in ('nv12', 'nv12_planes', 'nv12_pattern') else 'BGRA',
                'gpu_yuv_to_rgb': content in ('nv12', 'nv12_pattern'), 'raw_pixel_upload': False,
                'd3d11_encode_session': bridge_session,
                'log': [line for line in (directory / 'probe.log').read_text().splitlines()
                        if line.startswith(('GPU source', 'Vulkan texture:', 'Shared D3D11', 'CUDA imported', 'Native NVENC', 'Native lifecycle', 'Adapter ', 'Bridge session:'))]}
            if result:
                raise RuntimeError('GPU interop probe failed')
            if bridge_session and not any(line.startswith('Bridge session:') for line in report['log']):
                raise RuntimeError('Missing multi-source encoding checks')
            if content != 'nv12_planes':
                if (f'Adapter cached submissions: {frames}; caller-state checks: passed' not in report['log'] or
                        'Adapter invalid-resource/context checks: passed' not in report['log']):
                    raise RuntimeError('Missing resource-adapter validation')
                report['adapter_caller_state_verified'] = True
                report['adapter_rejections_verified'] = True
            entries = [json.loads(line) for line in (directory / 'frames.jsonl').read_text().splitlines()]
            validate_metadata(entries, frames)
            report['decoded'] = []
            for codec in ['h264', 'hevc']:
                path = directory / (codec + '.bin')
                stream = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-f', codec,
                    '-show_entries', 'stream=codec_name,width,height,color_space,color_transfer,color_primaries,color_range',
                    '-of', 'json', str(path)],
                    capture_output=True, text=True, check=True, timeout=10).stdout)['streams'][0]
                if stream != {'codec_name': codec, 'width': width, 'height': height,
                              'color_space': 'bt709', 'color_transfer': 'bt709',
                              'color_primaries': 'bt709', 'color_range': 'tv'}:
                    raise RuntimeError(f'Unexpected stream properties: {stream}')
                decoded_frames = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-f', codec,
                    '-show_entries', 'frame=key_frame,pict_type', '-of', 'json', str(path)],
                    capture_output=True, text=True, check=True, timeout=30).stdout)['frames']
                packets = [entry for entry in entries if entry['codec'] == codec]
                if len(decoded_frames) != frames or sum(p['bytes'] for p in packets) != path.stat().st_size:
                    raise RuntimeError('Truncated or extra encoded frames/bytes')
                for index, decoded in enumerate(decoded_frames):
                    if bool(decoded['key_frame']) != packets[index]['idr'] or decoded['pict_type'] == 'B':
                        raise RuntimeError('Decoded keyframes or reordering disagree with encoder')
                # Bound host RAM while independently checking EVERY decoded frame.
                raw_path = directory / (codec + '.rgb')
                with raw_path.open('xb') as raw:
                    subprocess.run(['ffmpeg', '-v', 'error', '-f', codec, '-i', str(path),
                        '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                        stdout=raw, stderr=subprocess.PIPE, check=True, timeout=90)
                frame_bytes = width * height * 3
                if raw_path.stat().st_size != frame_bytes * frames:
                    raise RuntimeError('Incorrect decoded byte count')
                with raw_path.open('rb') as raw, mmap.mmap(raw.fileno(), 0, access=mmap.ACCESS_READ) as pixels:
                    for index in range(frames):
                        samples = validate_pixels(pixels[index * frame_bytes:(index + 1) * frame_bytes],
                                                  width, height, content, index)
                raw_path.unlink()  # Only this test's temporary synthetic decode.
                report['decoded'].append(dict(stream, bytes=path.stat().st_size, frames=frames,
                    idr_frames=[i for i, p in enumerate(packets) if p['idr']],
                    timestamps_verified=True, all_frames_pixel_checked=True, last_samples=samples))
            return report
        except Exception:
            if (directory / 'probe.log').exists():
                print((directory / 'probe.log').read_text()[-6000:])
            raise
        finally:
            subprocess.run(['/opt/wine-stable/bin/wineserver', '-k'], env=env,
                           capture_output=True, timeout=10)


if __name__ == '__main__':
    print(json.dumps([run('pattern', 640, 360), run('clear', 640, 360),
                      run('pattern', 3424, 1926), run('pattern', 3840, 2160),
                      run('sequence', 640, 360, 64), run('sequence', 3840, 2160, 16),
                      run('nv12_planes', 640, 360, 64), run('nv12', 640, 360, 64),
                      run('nv12', 3840, 2160, 16), run('nv12_pattern', 640, 360, 16),
                      run('nv12_pattern', 3840, 2160, 16)], indent=2))
