#!/usr/bin/env python3
"""Isolated synthetic video/resize/watchdog test; no physical desktop capture.

Build with scripts/build-native-presenter.sh first. Creates an authenticated
Xvfb and disposable Wine prefix, and stops only its own processes. Results are
not an end-to-end UU test or a native capture/encoder performance measurement.
"""
import json
from contextlib import ExitStack
import mmap
import os
from pathlib import Path
import secrets
import select
import shutil
import struct
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
HEADER, DATA, SLOT, SIZE = 4096, 69632, 67108864, 134287360


def pattern(width, height):
    # BGRA quadrants: red, green, blue, white. Samples stay away from edges.
    upper = bytes((0, 0, 255, 255)) * (width // 2) + bytes((0, 255, 0, 255)) * (width - width // 2)
    lower = bytes((255, 0, 0, 255)) * (width // 2) + bytes((255, 255, 255, 255)) * (width - width // 2)
    return upper * (height // 2) + lower * (height - height // 2)


def stop(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def run():
    for tool in ['Xvfb', 'xauth', 'ffmpeg']:
        if not shutil.which(tool):
            raise RuntimeError(f'Missing test dependency: {tool}')
    wine = os.environ.get('UURB_TEST_WINE', '/opt/wine-stable/bin/wine')
    wineserver = str(Path(wine).with_name('wineserver'))
    app = ROOT / 'build/native-presenter/uu-native-presenter.exe'
    if not app.is_file():
        raise RuntimeError('Run bash scripts/build-native-presenter.sh first')
    report = {'synthetic_only': True, 'uu_session_tested': False, 'checks': []}
    with tempfile.TemporaryDirectory(prefix='uurb-native-probe-') as directory:
        directory = Path(directory)
        authority = directory / 'Xauthority'
        authority.touch(mode=0o600)
        # Xvfb validates the cookie bytes, independently of the display label.
        subprocess.run(['xauth', '-f', str(authority), 'add', ':0', '.',
                        secrets.token_hex(16)], check=True, capture_output=True)
        xserver = presenter = monitor = capture = None
        writer = None
        stopped = threading.Event()
        env = dict(os.environ, XAUTHORITY=str(authority), XDG_SESSION_TYPE='x11',
                   WINEPREFIX=str(directory / 'wine'), WINEDEBUG='-all',
                   WINEDLLOVERRIDES='mscoree,mshtml=;d3d11,dxgi=n',
                   DXVK_CONFIG_FILE=str(ROOT / 'config/dxvk-native.conf'),
                   DXVK_LOG_PATH=str(directory))
        # Exclude inherited GPU/config overrides from the diagnostic.
        for key in ['DXVK_CONFIG', 'DXVK_FILTER_DEVICE_NAME', 'WINEDLLPATH']:
            env.pop(key, None)
        read_fd, write_fd = os.pipe()
        try:
            with (directory / 'xvfb.log').open('w') as log:
                xserver = subprocess.Popen(['Xvfb', '-displayfd', str(write_fd),
                    '-screen', '0', '1920x1080x24', '-nolisten', 'tcp', '-noreset',
                    '-auth', str(authority)], pass_fds=(write_fd,), stdout=log, stderr=log)
            os.close(write_fd)
            write_fd = None
            if not select.select([read_fd], [], [], 10)[0]:
                raise RuntimeError('Xvfb startup timed out')
            number = os.read(read_fd, 32).decode().strip()
            if not number.isdigit():
                raise RuntimeError('Xvfb did not allocate a display')
            env['DISPLAY'] = ':' + number
            # Add the same server cookie under the allocated display name.
            cookie = subprocess.run(['xauth', '-f', str(authority), 'list'],
                                    capture_output=True, text=True, check=True).stdout.split()[-1]
            subprocess.run(['xauth', '-f', str(authority), 'add', env['DISPLAY'], '.', cookie],
                           check=True, capture_output=True)
            frame_path = directory / 'frames.bin'
            with frame_path.open('x+b') as file:
                os.chmod(frame_path, 0o600)
                file.truncate(SIZE)
                with mmap.mmap(file.fileno(), SIZE) as shared, ExitStack() as publisher_cleanup:
                    struct.pack_into('<4I', shared, 0, 0x46525555, 2, HEADER, SLOT)
                    state = [(640, 360, pattern(640, 360))]
                    def publish():
                        number = 0
                        sequences = [0, 0]
                        while not stopped.is_set():
                            started = time.monotonic()
                            w, h, pixels = state[0]
                            number += 1
                            slot = number % 2
                            meta = (5 + 7 * slot) * 4
                            sequences[slot] += 1
                            struct.pack_into('<I', shared, meta + 12, sequences[slot])
                            struct.pack_into('<3I', shared, meta, w, h, w * 4)
                            struct.pack_into('<3I', shared, meta + 16, number, 0, 0)
                            shared[DATA + slot * SLOT:DATA + slot * SLOT + len(pixels)] = pixels
                            sequences[slot] += 1
                            struct.pack_into('<I', shared, meta + 12, sequences[slot])
                            struct.pack_into('<I', shared, 16, slot)
                            struct.pack_into('<I', shared, 19 * 4, number)
                            stopped.wait(max(0, 1 / 60 - (time.monotonic() - started)))
                    writer = threading.Thread(target=publish, daemon=True)
                    writer.start()
                    publisher_cleanup.callback(writer.join, 3)
                    publisher_cleanup.callback(stopped.set)
                    with (directory / 'presenter.log').open('w') as log:
                        presenter = subprocess.Popen([wine, str(app),
                            'Z:' + str(frame_path).replace('/', '\\')], env=env,
                            cwd=directory, stdout=log, stderr=log)
                    deadline = time.monotonic() + 45
                    while 'Native frame texture:' not in (directory / 'presenter.log').read_text():
                        if presenter.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError('Presenter did not initialize')
                        time.sleep(0.1)
                    if shutil.which('nvidia-smi'):
                        with (directory / 'pmon.log').open('w') as log:
                            monitor = subprocess.Popen(['nvidia-smi', 'pmon', '-s', 'u', '-c', '12'],
                                                       stdout=log, stderr=log)
                    for w, h in [(640, 360), (3424, 1926)]:
                        state[0] = (w, h, pattern(w, h))
                        time.sleep(6)
                        screen = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'x11grab',
                            '-video_size', '1920x1080', '-i', env['DISPLAY'], '-frames:v', '1',
                            '-vf', 'scale=8:8:flags=neighbor', '-pix_fmt', 'rgb24',
                            '-f', 'rawvideo', '-'], env=env, capture_output=True, timeout=10, check=True).stdout
                        for x, y, rgb in [(1, 1, (255, 0, 0)), (6, 1, (0, 255, 0)),
                                          (1, 6, (0, 0, 255)), (6, 6, (255, 255, 255))]:
                            pixel = screen[(y * 8 + x) * 3:(y * 8 + x) * 3 + 3]
                            if len(pixel) != 3 or any(abs(a - b) > 8 for a, b in zip(pixel, rgb)):
                                raise RuntimeError(f'Incorrect output for {w}x{h}: {x},{y}={tuple(pixel)}')
                        report['checks'].append(f'{w}x{h} -> 1920x1080: all four quadrants correct')
                    backend = ROOT / 'build/rustdesk-backend/release/uurb-rustdesk-backend'
                    if backend.is_file():
                        probe = json.loads(subprocess.run([str(backend), 'probe'], env=env,
                            capture_output=True, text=True, check=True, timeout=10).stdout)
                        output = probe['outputs'][0]
                        raw = subprocess.run([str(backend), 'stream', output['id'], '1', '1'],
                            env=env, capture_output=True, check=True, timeout=10).stdout
                        if len(raw) != 1920 * 1080 * 4:
                            raise RuntimeError('Incorrect native captured frame size')
                        for x, y, bgr in [(240, 135, (0, 0, 255)), (1680, 135, (0, 255, 0)),
                                          (240, 945, (255, 0, 0)), (1680, 945, (255, 255, 255))]:
                            offset = (y * 1920 + x) * 4
                            if tuple(raw[offset:offset + 3]) != bgr:
                                raise RuntimeError('RustDesk capture returned incorrect pixels')
                        capture_path = directory / 'captured.bin'
                        with (directory / 'capture.log').open('w') as log:
                            capture = subprocess.Popen([str(backend), 'serve', output['id'],
                                str(capture_path), '20'], env=env, stdout=log, stderr=log)
                        deadline = time.monotonic() + 10
                        while not capture_path.exists() or capture_path.stat().st_size != SIZE:
                            if capture.poll() is not None or time.monotonic() > deadline:
                                raise RuntimeError('Native shared publisher failed to start')
                            time.sleep(0.05)
                        with capture_path.open('rb') as captured_file:
                            with mmap.mmap(captured_file.fileno(), SIZE, access=mmap.ACCESS_READ) as captured:
                                time.sleep(0.5)
                                if struct.unpack_from('<4I', captured) != (0x46525555, 2, HEADER, SLOT):
                                    raise RuntimeError('Rust/C frame ABI mismatch')
                                previous = struct.unpack_from('<I', captured, 19 * 4)[0]
                                time.sleep(0.5)
                                if struct.unpack_from('<I', captured, 19 * 4)[0] == previous:
                                    raise RuntimeError('Native publisher heartbeat not advancing')
                        if capture_path.stat().st_mode & 0o777 != 0o600:
                            raise RuntimeError('Native frame mapping permissions are not private')
                        stop(capture)
                        report['checks'].append('RustDesk synthetic X11 capture pixels, ABI v2, heartbeat and 0600 permissions')
                    else:
                        report['native_capture'] = 'not tested: build native backend first'
                    stopped.set()
                    writer.join(timeout=2)
                    if presenter.wait(timeout=10) != 3:
                        raise RuntimeError('Stale producer was not rejected with exit code 3')
                    report['checks'].append('stalled producer exits after 5 seconds')
                    log = (directory / 'presenter.log').read_text()
                    if 'DXVK Vulkan interop verified' not in log:
                        raise RuntimeError('Missing Vulkan interop confirmation')
                    report['renderer'] = [line for line in log.splitlines()
                        if any(token in line for token in ['Native D3D11 adapter:', 'changed frames/s',
                                                          'DXVK Vulkan interop verified'])]
                    if monitor:
                        monitor.wait(timeout=5)
                        report['gpu_samples'] = [line for line in (directory / 'pmon.log').read_text().splitlines()
                                                 if 'uu-native-prese' in line]
                    # A vendor-looking adapter must not silently select Wine GL.
                    builtin_env = dict(env, WINEDLLOVERRIDES='mscoree,mshtml=;d3d11,dxgi=b')
                    rejected = subprocess.run([wine, str(app),
                        'Z:' + str(frame_path).replace('/', '\\')], env=builtin_env,
                        cwd=directory, capture_output=True, text=True, timeout=20)
                    if rejected.returncode != 1 or 'DXVK Vulkan interop required' not in rejected.stderr:
                        raise RuntimeError('Builtin Wine renderer was not rejected')
                    report['checks'].append('Wine builtin renderer rejected, no silent GL fallback')
                    struct.pack_into('<I', shared, 4, 1)
                    rejected = subprocess.run([wine, str(app),
                        'Z:' + str(frame_path).replace('/', '\\')], env=env,
                        cwd=directory, capture_output=True, timeout=10)
                    if rejected.returncode != 2:
                        raise RuntimeError('Old ABI version was not rejected')
                    report['checks'].append('incompatible frame ABI rejected')
        except Exception:
            if (directory / 'presenter.log').exists():
                print((directory / 'presenter.log').read_text()[-5000:])
            raise
        finally:
            stopped.set()
            if writer:
                writer.join(timeout=3)
            stop(presenter)
            stop(monitor)
            stop(capture)
            # This unique test prefix is never the installed production prefix.
            subprocess.run([wineserver, '-k'], env=env, capture_output=True, timeout=10)
            stop(xserver)
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    run()
