#!/usr/bin/python3
"""Request authorized Wayland capture; inspect DMA-BUF or validate GPU encoding.
No input injection, RDP, permission bypass or persistent screen recording.
Restore-token persistence is explicit opt-in and separate from RustDesk state.
"""
import json
import argparse
import os
from pathlib import Path
import secrets
import subprocess
import sys
import stat
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SERVICE = 'org.freedesktop.portal.Desktop'
PATH = '/org/freedesktop/portal/desktop'
SCREENCAST = 'org.freedesktop.portal.ScreenCast'


def load_restore_token(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError('Restore state must be a private file owned by this user')
        if info.st_size > 8192:
            raise RuntimeError('Restore state exceeds size limit')
        content = stream.read(8193)
        if len(content) > 8192:
            raise RuntimeError('Restore state exceeds size limit')
        data = json.loads(content)
    if not isinstance(data, dict):
        raise RuntimeError('Invalid restore state')
    token = data.get('restore_token')
    if data.get('version') != 1 or not isinstance(token, str) or not 0 < len(token) <= 4096:
        raise RuntimeError('Invalid restore state')
    return token


def save_restore_token(path, token):
    if not isinstance(token, str) or not 0 < len(token) <= 4096:
        raise RuntimeError('Portal returned an invalid restore token')
    payload = json.dumps(dict(version=1, restore_token=token), ensure_ascii=False).encode('utf-8')
    if len(payload) > 8192:
        raise RuntimeError('Restore state exceeds size limit')
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.uurb-restore-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def color_centroid(pixels, channel):
    width, height = 192, 108
    if not isinstance(pixels, (bytes, bytearray)) or len(pixels) != width * height * 3 or channel not in (0, 1, 2):
        raise ValueError('Expected a 192x108 RGB24 image and channel 0, 1 or 2')
    selected = set()
    for index in range(width * height):
        rgb = pixels[index * 3:index * 3 + 3]
        if rgb[channel] > 160 and all(rgb[c] < 90 for c in range(3) if c != channel):
            selected.add(index)
    largest = []
    while selected:
        component, pending = [], [selected.pop()]
        while pending:
            index = pending.pop()
            component.append(index)
            x, y = index % width, index // width
            neighbors = []
            if x: neighbors.append(index - 1)
            if x + 1 < width: neighbors.append(index + 1)
            if y: neighbors.append(index - width)
            if y + 1 < height: neighbors.append(index + width)
            for neighbor in neighbors:
                if neighbor in selected:
                    selected.remove(neighbor)
                    pending.append(neighbor)
        if len(component) > len(largest):
            largest = component
    if len(largest) < 30:
        raise RuntimeError('Visible RGB color target missing from decoded image')
    return (sum(i % width for i in largest) / len(largest),
            sum(i // width for i in largest) / len(largest))


def validate_encoded_capture(stream, report, color_fixture=False, fixture_warmup=False):
    codec = report.get('codec')
    if codec not in ('h264', 'hevc') or report.get('encoded') is not True:
        raise RuntimeError('Native worker did not report hardware encoding')
    for name in ('width', 'height', 'frames'):
        if type(report.get(name)) is not int or report[name] <= 0:
            raise RuntimeError('Missing or invalid native frame geometry/count')
    byte_count = os.fstat(stream.fileno()).st_size
    if not 0 < byte_count <= 64 * 1024 * 1024:
        raise RuntimeError('Encoded diagnostic size outside bound')
    fd = stream.fileno()
    path = f'/proc/self/fd/{fd}'
    stream.seek(0)
    info = subprocess.run(['ffprobe', '-v', 'error', '-f', codec, '-count_frames',
        '-show_entries', 'stream=codec_name,width,height,nb_read_frames', '-of', 'json', path],
        pass_fds=(fd,), capture_output=True, text=True, check=True, timeout=20)
    entries = json.loads(info.stdout).get('streams', [])
    if len(entries) != 1:
        raise RuntimeError('Expected one independently decodable stream')
    actual = entries[0]
    if (actual.get('codec_name') != codec or actual.get('width') != report['width'] or
        actual.get('height') != report['height'] or int(actual.get('nb_read_frames', 0)) != report['frames']):
        raise RuntimeError('Independent decoded frame count/geometry mismatch')
    stream.seek(0)
    decoded = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', codec, '-i', path,
        '-vf', 'signalstats,metadata=mode=print:file=-', '-f', 'null', '-'],
        pass_fds=(fd,), capture_output=True, text=True, check=True, timeout=20)
    minimum = [float(line.split('=', 1)[1]) for line in decoded.stdout.splitlines()
               if line.startswith('lavfi.signalstats.YMIN=')]
    maximum = [float(line.split('=', 1)[1]) for line in decoded.stdout.splitlines()
               if line.startswith('lavfi.signalstats.YMAX=')]
    if len(minimum) != report['frames'] or len(maximum) != report['frames']:
        raise RuntimeError('Independent pixel statistics missing for captured frames')
    result = dict(encoded_bytes=byte_count, decoded_frames=int(actual['nb_read_frames']),
                decoded_luma_min=min(minimum), decoded_luma_max=max(maximum),
                nonuniform_image=any(high - low > 8 for low, high in zip(minimum, maximum)),
                validation_decode_on_cpu=True, persistent_recording=False)
    if color_fixture:
        stream.seek(0)
        warmup = ['-ss', '1'] if fixture_warmup else []
        pixels = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', codec, '-i', path, *warmup,
            '-frames:v', '1', '-vf', 'scale=192:108:flags=area', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'],
            pass_fds=(fd,), capture_output=True, check=True, timeout=20).stdout
        if len(pixels) != 192 * 108 * 3:
            raise RuntimeError('Unexpected validation image size')
        red, green, blue = [color_centroid(pixels, c) for c in range(3)]
        if not (red[0] + 5 < green[0] and red[1] + 5 < blue[1] and
                abs(red[0] - blue[0]) < 5 and abs(red[1] - green[1]) < 5):
            raise RuntimeError('Decoded RGB target has wrong colors or orientation')
        stream.seek(0)
        hashes = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', codec, '-i', path,
            '-f', 'framemd5', '-'], pass_fds=(fd,), capture_output=True, text=True, check=True, timeout=20)
        frames = [line.rsplit(',', 1)[-1].strip() for line in hashes.stdout.splitlines()
                  if line and not line.startswith('#')]
        if len(frames) != report['frames'] or len(set(frames)) < 10:
            raise RuntimeError('Encoded animation is frozen or has too few distinct frames')
        result.update(color_fixture_verified=True, distinct_decoded_frames=len(set(frames)))
    return result


def cursor_mode_value(name, available):
    # Hidden affects only the screencast, never the physical desktop cursor.
    modes = {'embedded': 2, 'hidden': 1, 'metadata': 4, 'composited': 4}
    if name not in modes:
        raise ValueError('Unsupported capture cursor policy')
    value = modes[name]
    if not int(available) & value:
        raise RuntimeError('Requested capture cursor mode is unavailable')
    return value


def composition_report(diagnostics, frames):
    if type(frames) is not int or frames < 1:
        raise RuntimeError('Invalid native GPU cursor composition total')
    prefix = 'UURB_CURSOR_COMPOSITE '
    lines = [line[len(prefix):] for line in diagnostics.splitlines() if line.startswith(prefix)]
    if len(lines) != 1:
        raise RuntimeError('Missing unambiguous native GPU cursor composition report')
    value = json.loads(lines[0])
    if (not isinstance(value, dict) or set(value) != {'cursor_only_frames', 'desktop_frames'} or
            any(type(n) is not int or n < 0 for n in value.values()) or
            value['desktop_frames'] < 1 or sum(value.values()) != frames):
        raise RuntimeError('Invalid native GPU cursor composition frame counts')
    return dict(cursor_composited=True, **value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', choices=['monitor', 'virtual'], default='monitor',
                        help='Explicitly authorize a physical or temporary virtual output')
    parser.add_argument('--authorization-timeout', type=int, default=120,
                        help='Seconds to allow for explicit portal UI consent, 5–120')
    parser.add_argument('--duration-seconds', type=int, default=8,
                        help='Native GPU relay lifetime, 1–3600 seconds; 0 for supervised relay')
    parser.add_argument('--capture-binary', type=Path,
                        help='Explicit version-pinned native GPU producer for managed runtime')
    parser.add_argument('--cursor-mode', choices=['embedded', 'hidden', 'metadata', 'composited'], default='embedded',
                        help='Hidden removes only the video cursor; requires a controller-rendered pointer')
    parser.add_argument('--cursor-state-fd', type=int, help='Private inherited cursor metadata snapshot FD')
    parser.add_argument('--capture-status-fd', type=int, help='Private inherited first GPU frame ACK status channel')
    parser.add_argument('--cursor-backend', choices=['spa', 'mutter'], default='spa',
                        help='Explicit backend semantics: Mutter uses invalid cursor id to hide the pointer')
    parser.add_argument('--snapshot', type=Path,
                        help='Explicit opt-in: save one PNG from the authorized encoded sample, mode 0600')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--capture-check', action='store_true',
                        help='Explicit raw-frame diagnostic, not a DMA-BUF/GPU validation')
    mode.add_argument('--native-dmabuf', action='store_true',
                      help='Use native PipeWire negotiation with Vulkan modifiers')
    mode.add_argument('--vulkan-import', action='store_true',
                      help='Validate native DMA-BUF import into Vulkan; no GPU commands or encoding')
    mode.add_argument('--encode', choices=['h264', 'hevc'],
                      help='GPU blit + NVENC, independently decode an anonymous temporary sample')
    mode.add_argument('--gpu-relay-fd', type=int,
                      help='Explicit private inherited seqpacket FD for GPU frames; no local encoding')
    parser.add_argument('--restore-state', type=Path,
                        help='Opt-in private file for this probe’s own portal restore token')
    parser.add_argument('--check-color-fixture', action='store_true',
                        help='Require the separately displayed RGB motion target in the decoded sample')
    args = parser.parse_args()
    if args.capture_status_fd is not None:
        import socket
        if (args.gpu_relay_fd is None or args.capture_status_fd < 3 or
                args.capture_status_fd in (args.gpu_relay_fd, args.cursor_state_fd)):
            parser.error('Capture status requires a separate inherited descriptor and GPU relay')
        try:
            with socket.socket(fileno=os.dup(args.capture_status_fd)) as status_channel:
                if status_channel.family != socket.AF_UNIX or status_channel.type != socket.SOCK_SEQPACKET:
                    parser.error('Capture status requires an AF_UNIX seqpacket socket')
                status_channel.getpeername()
        except OSError:
            parser.error('Capture status requires a connected inherited socket')
    if (args.cursor_mode in ('metadata', 'composited')) != (args.cursor_state_fd is not None) or (
            args.cursor_state_fd is not None and (args.gpu_relay_fd is None or args.cursor_state_fd < 3 or
            args.cursor_state_fd == args.gpu_relay_fd)):
        parser.error('Metadata cursor mode requires a separate inherited state FD and GPU relay')
    if not 0 <= args.duration_seconds <= 3600 or (args.duration_seconds != 8 and args.gpu_relay_fd is None):
        parser.error('Non-default duration requires GPU relay mode; 0 is supervised, otherwise 1 to 3600 seconds')
    if not 5 <= args.authorization_timeout <= 120:
        parser.error('authorization timeout must be from 5 to 120 seconds')
    if args.snapshot and not args.encode:
        parser.error('--snapshot requires --encode')
    if args.check_color_fixture and not args.encode:
        parser.error('--check-color-fixture requires --encode')
    if args.gpu_relay_fd is not None:
        import socket
        if args.gpu_relay_fd < 3:
            parser.error('--gpu-relay-fd must be an inherited descriptor >= 3')
        try:
            with socket.socket(fileno=os.dup(args.gpu_relay_fd)) as relay:
                if relay.family != socket.AF_UNIX or relay.type != socket.SOCK_SEQPACKET:
                    parser.error('--gpu-relay-fd requires an AF_UNIX seqpacket socket')
                relay.getpeername()
        except OSError:
            parser.error('--gpu-relay-fd requires a connected inherited socket')
    restore_token = load_restore_token(args.restore_state) if args.restore_state else None
    from gi.repository import Gio, GLib
    binary = ROOT / 'build/native-presenter' / (
        'uu-pipewire-native-probe' if args.native_dmabuf or args.vulkan_import or args.encode or args.gpu_relay_fd is not None else 'uu-pipewire-dmabuf-probe')
    if args.capture_binary is not None:
        binary = args.capture_binary
        info = binary.lstat()
        if (args.gpu_relay_fd is None or not binary.is_absolute() or
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                info.st_mode & 0o022 or not os.access(binary, os.X_OK)):
            raise ValueError('Expected an owned executable GPU relay producer')
    if not binary.is_file():
        raise RuntimeError('Run scripts/build-pipewire-probe.sh first')
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    sender = bus.get_unique_name()[1:].replace('.', '_')
    session = None
    remote_fd = None

    def request(method, signature, arguments, options):
        token = 'uurb_' + secrets.token_hex(12)
        path = f'/org/freedesktop/portal/desktop/request/{sender}/{token}'
        options = dict(options, handle_token=GLib.Variant('s', token))
        loop = GLib.MainLoop()
        response = []

        def receive(connection, name, object_path, interface, signal, parameters, user_data):
            response.append(parameters.unpack())
            loop.quit()

        subscription = bus.signal_subscribe(SERVICE, 'org.freedesktop.portal.Request', 'Response',
            path, None, Gio.DBusSignalFlags.NONE, receive, None)
        timeout = GLib.timeout_add_seconds(args.authorization_timeout, lambda: (loop.quit(), False)[1])
        try:
            returned = bus.call_sync(SERVICE, PATH, SCREENCAST, method,
                GLib.Variant(signature, (*arguments, options)), None, Gio.DBusCallFlags.NONE, 10000, None).unpack()[0]
            if returned != path:
                raise RuntimeError('Unexpected portal request handle')
            if not response:
                loop.run()
            if not response:
                raise RuntimeError(f'Screen sharing request {method} timed out')
            code, values = response[0]
            if code:
                raise RuntimeError(f'Screen sharing {method} failed or was cancelled (response {code})')
            print(f'Portal {method}: success', file=sys.stderr, flush=True)
            return values
        finally:
            bus.signal_unsubscribe(subscription)
            if GLib.MainContext.default().find_source_by_id(timeout):
                GLib.source_remove(timeout)
            if not response:
                try:
                    bus.call_sync(SERVICE, path, 'org.freedesktop.portal.Request', 'Close',
                                  None, None, Gio.DBusCallFlags.NONE, 3000, None)
                except GLib.Error:
                    pass

    try:
        properties = bus.call_sync(SERVICE, PATH, 'org.freedesktop.DBus.Properties', 'GetAll',
            GLib.Variant('(s)', (SCREENCAST,)), None, Gio.DBusCallFlags.NONE, 10000, None).unpack()[0]
        source_type = 4 if args.source == 'virtual' else 1
        if properties.get('version', 0) < 2 or not properties.get('AvailableSourceTypes', 0) & source_type:
            raise RuntimeError('Requested screen-cast source type is unavailable')
        # Keep the visible embedded default. A controller with its own pointer
        # can explicitly exclude the delayed video pointer to avoid doubling.
        cursor_mode = cursor_mode_value(args.cursor_mode, properties.get('AvailableCursorModes', 0))
        session_token = 'uurb_' + secrets.token_hex(12)
        values = request('CreateSession', '(a{sv})', (), {
            'session_handle_token': GLib.Variant('s', session_token)})
        session = values['session_handle']
        if session != f'/org/freedesktop/portal/desktop/session/{sender}/{session_token}':
            raise RuntimeError('Unexpected portal session handle')
        selection = {
            'types': GLib.Variant('u', source_type), 'multiple': GLib.Variant('b', False),
            'cursor_mode': GLib.Variant('u', cursor_mode)}
        if args.restore_state:
            if properties['version'] < 4:
                raise RuntimeError('Portal does not support persistent screen sharing')
            selection['persist_mode'] = GLib.Variant('u', 2)
            if restore_token:
                selection['restore_token'] = GLib.Variant('s', restore_token)
        request('SelectSources', '(oa{sv})', (session,), selection)
        notice = ('硬件编码样本仅用于匿名临时解码校验，不持久保存或上传画面。' if args.encode else
                  '仅检查帧到达情况/缓冲元数据，不保存画面。')
        if args.snapshot:
            notice = '本次显式保存一张私有 PNG 用于本机界面诊断，不保存录像或上传画面。'
        print('如出现授权窗口，请选择并允许共享屏幕；' + notice, file=sys.stderr, flush=True)
        start_time = time.monotonic()
        started = request('Start', '(osa{sv})', (session, ''), {})
        start_ms = round((time.monotonic() - start_time) * 1000)
        # Save a freshly issued token immediately: a capture failure must not
        # discard/roll back the portal's rotated capability. Never print it.
        if args.restore_state and started.get('restore_token'):
            save_restore_token(args.restore_state, started['restore_token'])
        streams = started.get('streams', [])
        if len(streams) != 1 or not 0 < streams[0][0] <= 0xffffffff:
            raise RuntimeError('Expected exactly one authorized screen stream')
        if streams[0][1].get('source_type') != source_type:
            raise RuntimeError('Portal selected an unexpected source type')
        value, fd_list = bus.call_with_unix_fd_list_sync(SERVICE, PATH, SCREENCAST, 'OpenPipeWireRemote',
            GLib.Variant('(oa{sv})', (session, {})), GLib.VariantType.new('(h)'),
            Gio.DBusCallFlags.NONE, 10000, None, None)
        remote_fd = fd_list.get(value.unpack()[0])
        command = [str(binary), str(remote_fd), str(streams[0][0])]
        if args.capture_check:
            command.append('--capture-check')
        if args.vulkan_import:
            command.append('--vulkan-import')
        with tempfile.TemporaryFile() as encoded:
            inherited = (remote_fd,)
            if args.encode:
                command.extend(['--encode-' + args.encode, str(encoded.fileno())])
                inherited += (encoded.fileno(),)
            if args.gpu_relay_fd is not None:
                command.extend(['--gpu-relay', str(args.gpu_relay_fd)])
                inherited += (args.gpu_relay_fd,)
            capture_env = dict(os.environ, UURB_CAPTURE_DURATION_SECONDS=str(args.duration_seconds))
            capture_env.pop('UURB_CAPTURE_STATUS_FD', None)
            if args.capture_status_fd is not None:
                inherited += (args.capture_status_fd,)
                capture_env['UURB_CAPTURE_STATUS_FD'] = str(args.capture_status_fd)
            capture_env.pop('UURB_CURSOR_STATE_FD', None)
            capture_env.pop('UURB_CURSOR_METADATA_BACKEND', None)
            capture_env.pop('UURB_CURSOR_COMPOSITE', None)
            if args.cursor_mode == 'composited':
                capture_env['UURB_CURSOR_COMPOSITE'] = '1'
            if args.cursor_state_fd is not None:
                inherited += (args.cursor_state_fd,)
                capture_env['UURB_CURSOR_STATE_FD'] = str(args.cursor_state_fd)
                capture_env['UURB_CURSOR_METADATA_BACKEND'] = args.cursor_backend
            result = subprocess.run(command, env=capture_env,
                pass_fds=inherited, capture_output=True, text=True,
                timeout=None if args.duration_seconds == 0 else max(20, args.duration_seconds + 12))
            if result.returncode:
                raise RuntimeError(f'Capture probe exited {result.returncode}: ' + result.stderr[-2500:])
            report = json.loads(result.stdout)
            if args.cursor_mode == 'composited':
                report.update(composition_report(result.stderr, report.get('frames')))
            if args.encode:
                # Gtk's first draw precedes the compositor's map/fullscreen
                # transition. Validate the moving target after one second on
                # physical AND virtual outputs, not a pre-map desktop frame.
                report.update(validate_encoded_capture(encoded, report, args.check_color_fixture, args.check_color_fixture))
            if args.snapshot:
                encoded.seek(0)
                snapshot_fd = os.open(args.snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(snapshot_fd, 'wb') as snapshot:
                    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', args.encode,
                        '-i', f'/proc/self/fd/{encoded.fileno()}', '-frames:v', '1', '-f', 'image2pipe',
                        '-vcodec', 'png', '-'], pass_fds=(encoded.fileno(),), stdout=snapshot,
                        stderr=subprocess.PIPE, check=True, timeout=20)
                report['explicit_snapshot_saved'] = True
        report.update(portal_authorized=True, portal_version=properties['version'],
                      uu_session_tested=False, physical_session_expected='wayland', cursor_mode=args.cursor_mode,
                      restore_requested=bool(restore_token), portal_start_ms=start_ms,
                      source_type=source_type, source_kind=args.source)
        print(json.dumps(report, indent=2))
    finally:
        if remote_fd is not None:
            os.close(remote_fd)
        if session:
            try:
                bus.call_sync(SERVICE, session, 'org.freedesktop.portal.Session', 'Close',
                              None, None, Gio.DBusCallFlags.NONE, 3000, None)
            except GLib.Error:
                pass


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.SubprocessError, OSError, ValueError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
