#!/usr/bin/python3
"""Authorized Wayland GPU frames across processes, independently decoded.
This validates native transport with a native or isolated Winelib consumer,
not an actual UU session. Uses an inherited private socketpair and anonymous
temporary compressed output.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import socket
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('portal_probe', ROOT / 'scripts/probe-wayland-portal.py')
portal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(portal)


def stop(process):
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def wait_for_ready(process, marker, timeout=45):
    """Drain both startup pipes without TextIO read-ahead or unbounded logs."""
    deadline = time.monotonic() + timeout
    pending = b''
    diagnostics = b''
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
        if process.stderr is not None:
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == 'stderr':
                    diagnostics = (diagnostics + chunk)[-3500:]
                    continue
                pending += chunk
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    if line.rstrip(b'\r') == marker.encode():
                        return pending.decode(errors='replace'), diagnostics.decode(errors='replace')
                    diagnostics = (diagnostics + line + b'\n')[-3500:]
                if len(pending) > 65536:
                    raise RuntimeError('Receiver startup line exceeds limit')
    raise RuntimeError('Receiver did not reach ' + marker + ':\n' +
                       (diagnostics + pending)[-3500:].decode(errors='replace'))


def validate_pair(producer, consumer):
    if (producer.get('gpu_relayed') is not True or producer.get('encoded') is not False or
            producer.get('pixels_mapped') is not False or producer.get('dmabuf_only') is not True or
            consumer.get('gpu_channel_received') is not True or consumer.get('encoded') is not True):
        raise RuntimeError('Missing actual GPU relay/consumer evidence')
    for field in ('frames', 'width', 'height'):
        if (type(producer.get(field)) is not int or type(consumer.get(field)) is not int or
                producer[field] <= 0 or producer[field] != consumer[field]):
            raise RuntimeError('Producer/consumer frame count or geometry mismatch')


def validate_source_rate(producer, consumer):
    rate = producer.get('negotiated_framerate')
    maximum = producer.get('negotiated_max_framerate')
    for value in (rate, maximum):
        if (not isinstance(value, list) or len(value) != 2 or
                any(type(part) is not int or not 0 <= part <= 0xffffffff for part in value) or
                (value[0] and not value[1]) or (not value[0] and value[1] > 1)):
            raise RuntimeError('Missing valid negotiated source rate')
    nominal = rate if rate[0] else maximum if rate == [0, 1] and maximum[0] else [0, 1]
    if consumer.get('capture_nominal_rate') != nominal:
        raise RuntimeError('DXGI nominal cadence differs from source negotiation')


def log_tail(log):
    log.seek(max(0, os.fstat(log.fileno()).st_size - 6000))
    return log.read().decode(errors='replace')


def validate_pe_evidence(encoded):
    for field in ('windows_pe_client', 'dxgi_duplication_interface', 'metadata_and_lease_checks',
                  'qpc_timestamp_checks', 'terminal_access_lost_checked'):
        if encoded.get(field) is not True:
            raise RuntimeError('Missing PE DXGI acceptance evidence: ' + field)
    if encoded.get('encode_timestamp_source') != 'gpu-ready-qpc':
        raise RuntimeError('PE encoder must use GPU-ready QPC, not predicted source PTS')


def validate_dxvk_evidence(encoded, entrypoint):
    if entrypoint not in ('duplicate1', 'legacy'):
        raise ValueError('Unknown DXVK entry point')
    validate_pe_evidence(encoded)
    if (encoded.get('dxvk_creation_hook') is not True or encoded.get('capture_constructor') != entrypoint or
            encoded.get('output_geometry_matches_capture') is not True or encoded.get('hook_negative_checks') is not True):
        raise RuntimeError('Missing actual DXVK entry-point acceptance')


def run(codec, restore_state, windows_consumer=False, pe_consumer=False, dxvk_entrypoint=None, pe_child=False, socket_channel=False,
        cursor_composited=False, cursor_cycle=False):
    if cursor_cycle and not cursor_composited:
        raise ValueError('Cursor-cycle requires the composited cursor mode')
    if codec not in ('h264', 'hevc'):
        raise ValueError('Expected h264 or hevc')
    if dxvk_entrypoint not in (None, 'duplicate1', 'legacy'):
        raise ValueError('Unknown DXVK entry point')
    if pe_child and not dxvk_entrypoint:
        raise ValueError('PE child probe requires a DXVK entry point')
    if socket_channel and not dxvk_entrypoint:
        raise ValueError('Socket channel requires a DXVK entry point')
    pe_consumer = pe_consumer or dxvk_entrypoint is not None
    windows_consumer = windows_consumer or pe_consumer
    fixture = producer = consumer = None
    send, receive = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with send, receive, tempfile.TemporaryFile() as compressed, tempfile.TemporaryFile() as consumer_log, \
            tempfile.TemporaryDirectory(prefix='uurb-gpu-receiver-') as temporary:
        consumer_env = None
        listener = None
        cursor = None
        received_prefix = errors_prefix = ''
        try:
            if windows_consumer:
                stage = ROOT / 'build/native-presenter'
                consumer_env = dict(os.environ, WINEPREFIX=str(Path(temporary) / 'wine'),
                    WINELOADER='/opt/wine-stable/bin/wine', WINEDEBUG='-all',
                    WINEDLLOVERRIDES='mscoree,mshtml=;d3d11,dxgi=n', WINEDLLPATH=str(stage),
                    DXVK_CONFIG_FILE=str(ROOT / 'config/dxvk-native.conf'), DXVK_LOG_PATH=temporary, DXVK_LOG_LEVEL='warn')
                for name in ('DXVK_CONFIG', 'DXVK_FILTER_DEVICE_NAME', 'VK_INSTANCE_LAYERS'):
                    consumer_env.pop(name, None)
                for name in ('UURB_DXGI_CAPTURE_FD', 'UURB_DXGI_CAPTURE_OUTPUT', 'UURB_DXGI_CAPTURE_SOCKET'):
                    consumer_env.pop(name, None)
                application = temporary
                arguments = [str(stage / 'uu-d3d11-receiver-probe.exe'), str(receive.fileno()), str(compressed.fileno()), codec]
                inherited = (receive.fileno(), compressed.fileno())
                if pe_consumer:
                    application = Path(temporary) / 'app'
                    application.mkdir()
                    for name in ('uu-dxgi-pe-capture-probe.exe', 'uurb-dxgi-capture-loader.dll',
                                 'uurb-dxgi-capture.dll.so', 'uurb-nvenc-encode-loader.dll',
                                 'uurb-nvenc-encode.dll.so', 'd3d11.dll', 'dxgi.dll'):
                        shutil.copy2(stage / name, application / name)
                    executable = application / 'uu-dxgi-pe-capture-probe.exe'
                    with executable.open('rb') as binary:
                        if binary.read(2) != b'MZ':
                            raise RuntimeError('DXGI acceptance requires a true PE executable')
                    compressed_path = Path(temporary) / 'compressed.bin'
                    arguments = ['/opt/wine-stable/bin/wine', str(executable), str(receive.fileno()),
                                 'Z:' + str(compressed_path).replace('/', '\\'), codec]
                    inherited = (receive.fileno(),)
                    consumer_env.pop('WINEDLLPATH', None)
                    consumer_env['WINEDLLOVERRIDES'] += ';uurb-dxgi-capture-loader,uurb-nvenc-encode-loader=n'
                    if dxvk_entrypoint:
                        for name in ('dxgi.dll', 'd3d11.dll'):
                            shutil.copy2(ROOT / 'build/dxvk-capture/stage' / name, application / name)
                        arguments.append(dxvk_entrypoint)
                        consumer_env['UURB_DXGI_CAPTURE_FD'] = str(receive.fileno())
                        consumer_env['UURB_DXGI_CAPTURE_OUTPUT'] = r'\\.\DISPLAY1'
                        if socket_channel:
                            endpoint = Path(temporary) / 'capture.sock'
                            listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                            listener.bind(str(endpoint))
                            endpoint.chmod(0o600)
                            listener.listen(1)
                            listener.settimeout(10)
                            consumer_env.pop('UURB_DXGI_CAPTURE_FD')
                            consumer_env['UURB_DXGI_CAPTURE_SOCKET'] = str(endpoint)
                            inherited = ()
                        if pe_child:
                            shutil.copy2(stage / 'wine-capture-child.exe', application / 'wine-capture-child.exe')
                            arguments[1] = str(application / 'wine-capture-child.exe')
                display_geometry = '1920x1080x24' if dxvk_entrypoint else '640x360x24'
                consumer = subprocess.Popen(['xvfb-run', '-a', '--server-args=-screen 0 ' + display_geometry + ' -nolisten tcp',
                    *arguments],
                    env=consumer_env, cwd=application, pass_fds=inherited,
                    stdout=subprocess.PIPE, stderr=consumer_log, text=True, start_new_session=True)
                try:
                    received_prefix, errors_prefix = wait_for_ready(consumer, 'D3D11_RECEIVER_READY')
                except RuntimeError as error:
                    raise RuntimeError(str(error) + '\n' + log_tail(consumer_log)) from error
            fixture = subprocess.Popen(['/usr/bin/python3', str(ROOT / 'tests/probes/wayland_color_fixture.py'), '--frame-clock',
                *(['--cursor-cycle'] if cursor_cycle else [])],
                env=dict(os.environ, GDK_BACKEND='wayland'), stdout=subprocess.PIPE, text=True, start_new_session=True)
            with selectors.DefaultSelector() as selector:
                selector.register(fixture.stdout, selectors.EVENT_READ)
                ready = json.loads(fixture.stdout.readline()) if selector.select(timeout=5) else {}
                if not ready.get('fixture_ready'):
                    raise RuntimeError('Wayland color target was not ready')
            if not windows_consumer:
                consumer = subprocess.Popen([str(ROOT / 'build/native-presenter/uu-gpu-receiver-probe'),
                    str(receive.fileno()), str(compressed.fileno()), codec],
                    pass_fds=(receive.fileno(), compressed.fileno()), stdout=subprocess.PIPE,
                    stderr=consumer_log, text=True, start_new_session=True)
            receive.close()
            if socket_channel:
                send.close()
                send, _ = listener.accept()
                listener.close(); listener = None
            cursor_args = []
            inherited = (send.fileno(),)
            if cursor_composited:
                spec = importlib.util.spec_from_file_location('cursor_broker', ROOT / 'scripts/uu-native-capture-broker.py')
                broker = importlib.util.module_from_spec(spec); spec.loader.exec_module(broker)
                cursor = broker.CursorState(Path(temporary) / 'cursor.state', video_composited=True)
                cursor.reset(1)
                cursor_args = ['--cursor-mode', 'composited', '--cursor-state-fd', str(cursor.fd), '--cursor-backend', 'mutter']
                inherited += (cursor.fd,)
            producer = subprocess.Popen(['/usr/bin/python3', str(ROOT / 'scripts/probe-wayland-portal.py'),
                '--restore-state', str(restore_state), '--gpu-relay-fd', str(send.fileno()), *cursor_args],
                pass_fds=inherited, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True)
            send.close()
            output, errors = producer.communicate(timeout=35)
            if producer.returncode:
                stop(consumer)
                if consumer_env is not None:
                    subprocess.run(['/opt/wine-stable/bin/wineserver', '-k'], env=consumer_env,
                                   capture_output=True, timeout=10)
                receiver_output, _ = consumer.communicate(timeout=5)
                raise RuntimeError('GPU producer failed: ' + errors[-2500:] + '\nReceiver: ' +
                                   (received_prefix + receiver_output + errors_prefix + log_tail(consumer_log))[-6000:])
            if windows_consumer:
                # Wine services can inherit stdout/stderr after the actual
                # receiver exits. Check its exit first, then stop only this
                # disposable prefix so communicate can observe pipe EOF.
                consumer.wait(timeout=10)
                subprocess.run(['/opt/wine-stable/bin/wineserver', '-k'], env=consumer_env,
                               capture_output=True, timeout=10, check=True)
            received, errors = consumer.communicate(timeout=10)
            received = received_prefix + received
            errors = errors_prefix + log_tail(consumer_log)
            if consumer.returncode:
                raise RuntimeError('GPU consumer failed: ' + received[-3000:] + errors[-3000:])
            if pe_child and received.splitlines().count('UURB_PE_CHILD_COMPLETE 0') != 1:
                raise RuntimeError('Missing successful Windows child completion marker')
            if fixture.poll() is not None:
                raise RuntimeError('Color target stopped before relay completed')
            if windows_consumer:
                reports = [line.removeprefix('D3D11_RECEIVER_REPORT ') for line in received.splitlines()
                           if line.startswith('D3D11_RECEIVER_REPORT ')]
                if len(reports) != 1:
                    raise RuntimeError('Missing/duplicate D3D11 completion report:\n' + received[-3500:] + errors[-3500:])
                received = reports[0]
            capture, encoded = json.loads(output), json.loads(received)
            if cursor_composited and capture.get('cursor_composited') is not True:
                raise RuntimeError('Native worker did not verify the explicit GPU composition path')
            validate_pair(capture, encoded)
            if windows_consumer and (encoded.get('d3d11_texture_materialized') is not True or
                                     encoded.get('windows_nvenc_function_table') is not True):
                raise RuntimeError('Missing D3D11/Windows encoder interface evidence')
            if pe_consumer:
                validate_pe_evidence(encoded)
                validate_source_rate(capture, encoded)
                if dxvk_entrypoint:
                    validate_dxvk_evidence(encoded, dxvk_entrypoint)
                with compressed_path.open('rb') as pe_compressed:
                    if os.fstat(pe_compressed.fileno()).st_size != encoded.get('encoded_bytes'):
                        raise RuntimeError('PE compressed output size mismatch')
                    encoded.update(portal.validate_encoded_capture(pe_compressed, encoded, True, True))
            else:
                encoded.update(portal.validate_encoded_capture(compressed, encoded, True, True))
            if cursor_cycle:
                stop(fixture)
                rest, _ = fixture.communicate(timeout=5)
                observations = [json.loads(line)['cursor_fixture'] for line in rest.splitlines()
                                if line.startswith('{') and 'cursor_fixture' in json.loads(line)]
                if len(observations) != 1 or capture.get('cursor_only_frames', 0) < 2:
                    raise RuntimeError('Cursor-only delivery unverified: ' + json.dumps(dict(
                        codec=codec, decoded_frames=encoded['decoded_frames'],
                        desktop_frames=capture.get('desktop_frames'), cursor_only_frames=capture.get('cursor_only_frames'),
                        cursor_fixture=observations)))
            return dict(producer=capture, consumer=encoded, cross_process_gpu_frames=True,
                        raw_pixels_in_ipc=False, windows_consumer=windows_consumer,
                        pe_consumer=pe_consumer, dxvk_entrypoint=dxvk_entrypoint,
                        pe_child=pe_child, socket_channel=socket_channel, uu_session_tested=False)
        finally:
            if listener is not None:
                listener.close()
            send.close()
            stop(producer)
            if cursor is not None:
                cursor.close()
            stop(consumer)
            stop(fixture)
            if consumer_env is not None:
                subprocess.run(['/opt/wine-stable/bin/wineserver', '-k'], env=consumer_env,
                               capture_output=True, timeout=10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codec', choices=['h264', 'hevc'], required=True)
    parser.add_argument('--restore-state', type=Path, required=True)
    parser.add_argument('--windows-consumer', action='store_true', help='Use isolated Winelib D3D11 and Windows NVENC table')
    parser.add_argument('--pe-consumer', action='store_true', help='Use true PE consumer of the experimental DXGI COM object')
    parser.add_argument('--dxvk-entrypoint', choices=['duplicate1', 'legacy'], help='Use private DXVK creation hook in true PE')
    parser.add_argument('--pe-child', action='store_true', help='Launch the PE consumer through Windows CreateProcessW')
    parser.add_argument('--socket-channel', action='store_true', help='Connect a fresh channel from inside the DXVK consumer')
    parser.add_argument('--cursor-composited', action='store_true', help='Metadata source, GPU single cursor, native position snapshot')
    parser.add_argument('--cursor-cycle', action='store_true', help='Require actual cursor-only GPU frames on a static owned fullscreen')
    options = parser.parse_args()
    print(json.dumps(run(options.codec, options.restore_state, options.windows_consumer, options.pe_consumer,
                         options.dxvk_entrypoint, options.pe_child, options.socket_channel, options.cursor_composited,
                         options.cursor_cycle), indent=2))
