#!/usr/bin/python3
"""Run official UU's synthetic codec detector offline in a disposable prefix.
This is not a desktop capture or an actual UU connection. No account files are
copied. The temporary nvEncodeAPI64 name exists ONLY in this disposable prefix.
"""
import hashlib
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
DETECTOR = (Path(os.environ.get('WINEPREFIX', Path.home() / '.local/share/wineprefixes/uu-remote')) / 'drive_c/Program Files/Netease/GameViewer/bin/StreamerCodecDetector.exe')
EXPECTED_SHA = 'ed79229f5b80320542e93f1875a824d9913888c58c3d61b545b0e26ed1e4f454'


def parse_output(output):
    lines = [line.strip() for line in output.splitlines()]
    if lines.count('BATCH_DONE') != 1:
        raise RuntimeError('Official detector did not complete:\n' + output[-6000:])
    traces = [json.loads(line.split('UURB_NVENC_TRACE ', 1)[1]) for line in output.splitlines()
              if 'UURB_NVENC_TRACE {' in line]
    if not traces:
        raise RuntimeError('No actual bridge NVENC calls observed:\n' + output[-6000:])
    rows = [line for line in lines if line.startswith('RESULT,')]
    expected = {(codec, chroma, depth) for codec in (1, 2) for chroma in (1, 3) for depth in (8, 10)}
    observed = set()
    supported = []
    for row in rows:
        if not re.fullmatch(r'RESULT,(?:[0-9]+,){5}[0-9]+', row):
            raise RuntimeError('Malformed official detector result row')
        codec, chroma, depth, width, height, success = map(int, row.split(',')[1:])
        key = (codec, chroma, depth)
        if key not in expected or key in observed or success not in (0, 1):
            raise RuntimeError('Duplicate or unexpected official detector result')
        observed.add(key)
        if success:
            if (width, height) not in ((1920, 1080), (2560, 1440), (3840, 2160)):
                raise RuntimeError('Unexpected successful detector geometry')
            supported.append(dict(codec='h264' if codec == 1 else 'hevc', chroma=chroma,
                                  bit_depth=depth, width=width, height=height))
        elif width or height:
            raise RuntimeError('Failed detector row has nonzero geometry')
    if observed != expected:
        raise RuntimeError('Incomplete official detector result matrix')
    if any(not isinstance(event, dict) or not isinstance(event.get('call'), str) for event in traces):
        raise RuntimeError('Malformed bridge call trace')
    if not any(event.get('call') == 'create' and event.get('status') == 0 for event in traces):
        raise RuntimeError('No successful bridge function-table creation')
    return dict(official_detector_executed=True, detector_sha256=EXPECTED_SHA,
                network_namespace_isolated=True, account_files_copied=False, desktop_captured=False,
                uu_session_tested=False, synthetic_detector_only=True, bridge_calls_observed=True,
                supported_modes=supported, trace=traces, results=rows)


def validate_stream(info, mode):
    streams = info.get('streams', [])
    if len(streams) != 1 or any(streams[0].get(key) != value for key, value in
        dict(codec_name=mode['codec'], width=mode['width'], height=mode['height'],
             pix_fmt='yuv420p', nb_read_frames='1').items()):
        raise RuntimeError('Official synthetic packet failed independent stream validation: ' + str(info))
    return streams[0]


def decode_packets(report, directory):
    packets = [event for event in report['trace'] if event['call'] == 'synthetic_packet']
    if len(packets) != 2 or {p.get('codec') for p in packets} != {'h264', 'hevc'}:
        raise RuntimeError('Expected one official synthetic encoded packet per codec')
    modes = {mode['codec']: mode for mode in report['supported_modes']}
    decoded = []
    for index, packet in enumerate(packets):
        codec = packet['codec']
        if packet.get('saved') is not True or packet.get('index') != index or codec not in modes:
            raise RuntimeError('Official synthetic packet was not safely saved or mode did not pass')
        path = directory / f'packet-{index}.{codec}'
        if path.stat().st_size != packet['bytes'] or not 0 < packet['bytes'] <= 4 * 1024 * 1024:
            raise RuntimeError('Official synthetic packet size mismatch')
        mode = modes[codec]
        info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-f', codec, '-count_frames',
            '-show_entries', 'stream=codec_name,width,height,pix_fmt,nb_read_frames,color_space,color_transfer,color_primaries,color_range',
            '-of', 'json', str(path)], text=True, timeout=20))
        stream = validate_stream(info, mode)
        subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-nostdin', '-f', codec, '-i', str(path),
                        '-f', 'null', '-'], check=True, capture_output=True, timeout=30)
        decoded.append(dict(codec=codec, bytes=packet['bytes'], stream=stream, decoded_frames=1,
                            synthetic_content_only=True, pixels_not_compared_to_desktop=True))
    return decoded


def run(loader_debug=False, decode=False):
    if hashlib.sha256(DETECTOR.read_bytes()).hexdigest() != EXPECTED_SHA:
        raise RuntimeError('Unreviewed official detector revision')
    # Fail rather than run with network access when isolation is unavailable.
    subprocess.run(['unshare', '--user', '--map-current-user', '--net', 'true'], check=True, capture_output=True, timeout=5)
    stage = ROOT / 'build/native-presenter'
    with tempfile.TemporaryDirectory(prefix='uurb-offline-codec-') as temporary:
        directory = Path(temporary)
        app = directory / 'app'
        app.mkdir()
        encoded = directory / 'encoded'
        encoded.mkdir(mode=0o700)
        shutil.copy2(DETECTOR, app / DETECTOR.name)
        for name in ('uu-codec-detector-launcher.exe', 'd3d11.dll', 'dxgi.dll'):
            shutil.copy2(stage / name, app / name)
        # The pinned official detector uses LoadLibraryExW(..., 0x800):
        # LOAD_LIBRARY_SEARCH_SYSTEM32, not the application directory.
        system32 = directory / 'wine/drive_c/windows/system32'
        system32.mkdir(parents=True)
        shutil.copy2(stage / 'uurb-nvenc-audit-loader.dll', system32 / 'nvEncodeAPI64.dll')
        shutil.copy2(stage / 'uurb-nvenc-audit.dll.so', system32 / 'uurb-nvenc-audit.dll.so')
        env = dict(os.environ, WINEPREFIX=str(directory / 'wine'), WINELOADER='/opt/wine-stable/bin/wine',
                   WINEDEBUG='-all,+loaddll' if loader_debug else '-all', WINEDLLOVERRIDES='mscoree,mshtml=;d3d11,dxgi,nvEncodeAPI64=n',
                   DXVK_CONFIG_FILE=str(ROOT / 'config/dxvk-native.conf'), DXVK_LOG_PATH=temporary, DXVK_LOG_LEVEL='warn')
        for name in ('WINEDLLPATH', 'DXVK_CONFIG', 'DXVK_FILTER_DEVICE_NAME', 'VK_INSTANCE_LAYERS',
                     'UURB_DXGI_CAPTURE_FD', 'UURB_DXGI_CAPTURE_OUTPUT', 'UURB_NVENC_SYNTHETIC_OUTPUT'):
            env.pop(name, None)
        if decode:
            env['UURB_NVENC_SYNTHETIC_OUTPUT'] = str(encoded)
        process = None
        try:
            with (directory / 'probe.log').open('w') as log:
                process = subprocess.Popen(['unshare', '--user', '--map-current-user', '--net',
                    'xvfb-run', '-a', '--server-args=-screen 0 640x360x24 -nolisten tcp',
                    '/opt/wine-stable/bin/wine', str(app / 'uu-codec-detector-launcher.exe')],
                    env=env, cwd=app, stdout=log, stderr=log, start_new_session=True)
                status = process.wait(timeout=60)
            output = (directory / 'probe.log').read_text(errors='replace')
            if loader_debug:
                # Only library-loading diagnostics from the account-free child.
                for line in output.splitlines():
                    if any(word in line.lower() for word in ('nvenc', 'nvencode', 'uurb-', 'err:module')):
                        print(line, file=sys.stderr)
            if status:
                raise RuntimeError('Official detector process failed: ' + str(status) + '\n' + output[-6000:])
            report = parse_output(output)
            report['independent_decode'] = decode_packets(report, encoded) if decode else []
            used = [app / name for name in ('uu-codec-detector-launcher.exe', 'd3d11.dll', 'dxgi.dll')]
            used += [system32 / name for name in ('nvEncodeAPI64.dll', 'uurb-nvenc-audit.dll.so')]
            report['artifacts_sha256'] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in used}
            return report
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            subprocess.run(['/opt/wine-stable/bin/wineserver', '-k'], env=env, capture_output=True, timeout=10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--loader-debug', action='store_true')
    parser.add_argument('--decode', action='store_true', help='Independently decode synthetic packets; requires --detector-profile audit build')
    args = parser.parse_args()
    print(json.dumps(run(loader_debug=args.loader_debug, decode=args.decode), indent=2))
