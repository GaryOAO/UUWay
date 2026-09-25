"""Synthetic-only private GNOME/PipeWire test; no production capture entry point.
Called only by mutter_bundle_probe.py inside its disposable bus/runtime.
"""
import json
import os
import pwd
from pathlib import Path
import selectors
import socket
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]


def capture(bus, shell_pid, lifecycle=False, joint_input=False, stop_binaries=None):
    from gi.repository import Gio, GLib
    runtime = Path(os.environ['XDG_RUNTIME_DIR']).resolve()
    if not runtime.parent.name.startswith('uurb-mutter-test-') or runtime.parent.parent != Path('/tmp'):
        raise RuntimeError('Not an owned private synthetic runtime')
    owner = bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
        'GetConnectionUnixProcessID', GLib.Variant('(s)', ('org.gnome.Mutter.ScreenCast',)), None,
        Gio.DBusCallFlags.NO_AUTO_START, 1000, None).unpack()[0]
    if owner != shell_pid:
        raise RuntimeError('Capture service is not the private child compositor')
    if stop_binaries:
        from mutter_private_capture_stop import capture_stop
        return capture_stop(bus, runtime, *stop_binaries)
    if joint_input:
        input_owner = bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
            'GetConnectionUnixProcessID', GLib.Variant('(s)', ('org.gnome.Mutter.RemoteDesktop',)), None,
            Gio.DBusCallFlags.NO_AUTO_START, 1000, None).unpack()[0]
        if input_owner != shell_pid:
            raise RuntimeError('EIS service is not the private child compositor')
        return capture_lifecycle(bus, runtime, joint_input=True)
    if lifecycle:
        return capture_lifecycle(bus, runtime)

    def call(path, interface, method, args=None):
        value = bus.call_sync('org.gnome.Mutter.ScreenCast', path, interface, method, args,
            None, Gio.DBusCallFlags.NO_AUTO_START, 3000, None)
        return value.unpack() if value else ()

    prefix = 'org.gnome.Mutter.ScreenCast'
    env = dict(os.environ, WAYLAND_DISPLAY='uurb-private-test', GDK_BACKEND='wayland',
               UURB_CAPTURE_SIZE='3840x2160', UURB_CAPTURE_MAX_FPS='60',
               GTK_IM_MODULE='gtk-im-context-simple', NO_AT_BRIDGE='1', GTK_USE_PORTAL='0')
    fixture = None
    worker = None
    session = None
    subscription = None
    with tempfile.TemporaryFile(mode='w+') as errors, tempfile.TemporaryFile(mode='w+') as output:
        try:
            fixture = subprocess.Popen(['/usr/bin/python3', str(ROOT / 'tests/probes/wayland_color_fixture.py'),
                '--frame-clock', '--virtual-output', '--gpu'], env=env, stdout=subprocess.PIPE, stderr=errors, text=True)
            # A headless virtual view may not paint until a capture consumer
            # drives it. Let GTK discover the original view before connecting.
            time.sleep(1)
            session = call('/org/gnome/Mutter/ScreenCast', prefix, 'CreateSession', GLib.Variant('(a{sv})', ({},)))[0]
            stream = call(session, prefix + '.Session', 'RecordVirtual',
                GLib.Variant('(a{sv})', ({'cursor-mode': GLib.Variant('u', 1)},)))[0]
            nodes = []
            subscription = bus.signal_subscribe('org.gnome.Mutter.ScreenCast', prefix + '.Stream',
                'PipeWireStreamAdded', stream, None, Gio.DBusSignalFlags.NONE,
                lambda connection, sender, path, interface, signal, params: nodes.append(params.unpack()[0]))
            call(session, prefix + '.Session', 'Start')
            deadline = time.monotonic() + 5
            while not nodes and time.monotonic() < deadline:
                while GLib.MainContext.default().pending():
                    GLib.MainContext.default().iteration(False)
                time.sleep(0.01)
            if len(nodes) != 1:
                raise RuntimeError('No unambiguous private PipeWire source')
            with socket.socket(socket.AF_UNIX) as remote:
                remote.connect(str(runtime / 'pipewire-0'))
                worker = subprocess.Popen([str(ROOT / 'build/native-presenter/uu-pipewire-native-probe'),
                    str(remote.fileno()), str(nodes[0])], pass_fds=(remote.fileno(),), env=env, stdout=output, stderr=errors)
            # No hardware/audio session manager. Link only our two private video
            # ports explicitly; no production PipeWire nodes can be reached.
            deadline = time.monotonic() + 7
            linked = False
            while worker.poll() is None and time.monotonic() < deadline:
                objects = json.loads(subprocess.check_output(['/usr/bin/pw-dump'], env=env, text=True, timeout=2))
                consumers = {str(obj['id']) for obj in objects if obj['type'].endswith(':Node')
                    and obj.get('info', {}).get('props', {}).get('node.name') == 'uu-pipewire-native-probe'}
                outgoing, incoming = [], []
                for obj in objects:
                    if not obj['type'].endswith(':Port'):
                        continue
                    props = obj.get('info', {}).get('props', {})
                    if str(props.get('node.id')) == str(nodes[0]) and props.get('port.direction') == 'out':
                        outgoing.append(obj['id'])
                    if str(props.get('node.id')) in consumers and props.get('port.direction') == 'in':
                        incoming.append(obj['id'])
                if len(outgoing) == len(incoming) == 1:
                    subprocess.run(['/usr/bin/pw-link', str(outgoing[0]), str(incoming[0])],
                        env=env, check=True, stdout=errors, stderr=errors, timeout=3)
                    linked = True
                    break
                time.sleep(0.1)
            code = worker.wait(timeout=12)
            output.seek(0)
            if code or not linked:
                errors.seek(0)
                nodes_and_ports = [dict(id=obj['id'], type=obj['type'], props=obj.get('info', {}).get('props'))
                    for obj in objects if obj['type'].endswith((':Node', ':Port', ':Link'))]
                raise RuntimeError('Private video capture failed: ' + errors.read()[-2500:]
                    + '\nPrivate graph: ' + json.dumps(nodes_and_ports)[-9000:])
            report = json.loads(output.read())
            with selectors.DefaultSelector() as selector:
                selector.register(fixture.stdout, selectors.EVENT_READ)
                if not selector.select(timeout=2):
                    raise RuntimeError('Private fixture never produced a frame')
                ready = json.loads(fixture.stdout.readline())
                if not ready.get('fixture_ready'):
                    raise RuntimeError('Private fixture is not ready')
            report.update(synthetic_only=True, private_compositor_pid_verified=True,
                          fixture_gpu_renderer=ready.get('fixture_gpu_renderer'))
            if fixture.poll() is not None:
                raise RuntimeError('Private fixture exited before capture finished')
            fixture.terminate()
            fixture.wait(timeout=3)
            from wayland_refresh_probe import validate_fixture_samples
            report.update(validate_fixture_samples(report,
                [json.loads(line) for line in fixture.stdout.read().splitlines() if line], 'virtual'))
            return report
        finally:
            if worker and worker.poll() is None:
                worker.terminate()
                try:
                    worker.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait(timeout=3)
            if session:
                try:
                    call(session, prefix + '.Session', 'Stop')
                except GLib.Error:
                    # A failed compositor may have already destroyed the
                    # session. Still reap the fixture and the private worker.
                    pass
            if subscription:
                bus.signal_unsubscribe(subscription)
            if fixture:
                fixture.terminate()
                try:
                    fixture.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    fixture.kill()
                    fixture.communicate(timeout=3)


def capture_lifecycle(bus, runtime, joint_input=False):
    """Two metadata-only consumers, one virtual stream; no login-bus access.

    Called after capture() validates both the private runtime and compositor
    PID. This answers whether a persistent *session*, rather than a permanently
    encoding GPU worker, can own a Super Screen across video reconnects.
    """
    from gi.repository import Gio, GLib
    prefix = 'org.gnome.Mutter.ScreenCast'
    remote_prefix = 'org.gnome.Mutter.RemoteDesktop'

    def call(path, interface, method, args=None):
        service = remote_prefix if path.startswith('/org/gnome/Mutter/RemoteDesktop') else prefix
        result = bus.call_sync(service, path, interface, method, args, None,
                              Gio.DBusCallFlags.NO_AUTO_START, 3000, None)
        return result.unpack() if result else ()

    def monitors():
        reply = bus.call_sync('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig',
            'org.gnome.Mutter.DisplayConfig', 'GetCurrentState', None, None,
            Gio.DBusCallFlags.NO_AUTO_START, 2000, None).unpack()
        return {str(m[0][0]): [(int(t[1]), int(t[2])) for t in m[1] if t[-1].get('is-current')]
                for m in reply[1]}

    def pump():
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)

    original = monitors()
    session = None
    subscription = None
    worker = None
    input_worker = None
    input_lease = None
    input_control = None
    input_output = None
    transition_owner = transition_worker = None
    controller = None
    input_client = None
    remote_session = None
    mapped_input_ready = False
    input_rebound_after_resize = False
    tcp_connection_retained = False
    reports = []
    connector = None

    def open_private_eis():
        reply, descriptors = bus.call_with_unix_fd_list_sync(remote_prefix, remote_session,
            remote_prefix + '.Session', 'ConnectToEIS',
            GLib.Variant('(a{sv})', ({'device-types': GLib.Variant('u', 3)},)),
            GLib.VariantType.new('(h)'), Gio.DBusCallFlags.NO_AUTO_START, 2000, None, None)
        if reply.unpack() != (0,) or descriptors is None or descriptors.get_length() != 1:
            raise RuntimeError('Invalid private EIS descriptor')
        return descriptors.get(0)

    def rejected_input(sequence, error):
        # Type99 is invalid and cannot emit an event. This verifies continuity
        # of the authenticated TCP stream without touching even the test desktop.
        input_client.sendall(struct.pack('<4I',0x58315255,sequence,1,0) + struct.pack('<IIiiIHH',99,0,0,0,0,0,0))
        reply = input_client.recv(16, socket.MSG_WAITALL)
        if reply != struct.pack('<4I',0x58315255,sequence,0,error):
            raise RuntimeError('Input TCP connection did not preserve transition semantics')
    try:
        properties = {}
        if joint_input:
            remote_session = call('/org/gnome/Mutter/RemoteDesktop', remote_prefix, 'CreateSession')[0]
            remote_id = call(remote_session, 'org.freedesktop.DBus.Properties', 'Get',
                             GLib.Variant('(ss)', (remote_prefix + '.Session', 'SessionId')))[0]
            properties['remote-desktop-session-id'] = GLib.Variant('s', remote_id)
        session = call('/org/gnome/Mutter/ScreenCast', prefix, 'CreateSession', GLib.Variant('(a{sv})', (properties,)))[0]
        stream = call(session, prefix + '.Session', 'RecordVirtual',
                      GLib.Variant('(a{sv})', ({'cursor-mode': GLib.Variant('u', 1)},)))[0]
        nodes = []
        subscription = bus.signal_subscribe(prefix, prefix + '.Stream', 'PipeWireStreamAdded', stream,
            None, Gio.DBusSignalFlags.NONE, lambda *args: nodes.append(args[-1].unpack()[0]))
        if joint_input:
            call(remote_session, remote_prefix + '.Session', 'Start')
        else:
            call(session, prefix + '.Session', 'Start')
        deadline = time.monotonic() + 5
        while not nodes and time.monotonic() < deadline:
            pump(); time.sleep(0.01)
        if len(nodes) != 1:
            raise RuntimeError('Missing private virtual source')
        for width, height in ((1280, 720), (1600, 900)):
            if joint_input and input_worker is not None:
                controller.suspend()
                rejected_input(2, 0x2003)
            env = dict(os.environ, UURB_CAPTURE_SIZE=f'{width}x{height}', UURB_CAPTURE_MAX_FPS='60',
                       UURB_CAPTURE_DURATION_SECONDS='3')
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors, socket.socket(socket.AF_UNIX) as remote:
                remote.connect(str(runtime / 'pipewire-0'))
                worker = subprocess.Popen([str(ROOT / 'build/native-presenter/uu-pipewire-native-probe'),
                    str(remote.fileno()), str(nodes[0])], pass_fds=(remote.fileno(),), env=env, stdout=output, stderr=errors)
                deadline = time.monotonic() + 2
                linked = False
                while worker.poll() is None and time.monotonic() < deadline:
                    pump()
                    objects = json.loads(subprocess.check_output(['/usr/bin/pw-dump'], env=env, text=True, timeout=2))
                    consumers = {str(obj['id']) for obj in objects if obj['type'].endswith(':Node') and
                        obj.get('info', {}).get('props', {}).get('node.name') == 'uu-pipewire-native-probe'}
                    outgoing, incoming = [], []
                    for obj in objects:
                        if not obj['type'].endswith(':Port'):
                            continue
                        props = obj.get('info', {}).get('props', {})
                        if str(props.get('node.id')) == str(nodes[0]) and props.get('port.direction') == 'out':
                            outgoing.append(obj['id'])
                        if str(props.get('node.id')) in consumers and props.get('port.direction') == 'in':
                            incoming.append(obj['id'])
                    if len(outgoing) == len(incoming) == 1:
                        subprocess.run(['/usr/bin/pw-link', str(outgoing[0]), str(incoming[0])],
                            env=env, check=True, stdout=errors, stderr=errors, timeout=2)
                        linked = True; break
                    time.sleep(0.05)
                code = worker.wait(timeout=5)
                output.seek(0)
                if code or not linked:
                    raise RuntimeError('Private lifecycle consumer did not deliver metadata')
                report = json.loads(output.read(65537))
                if (report.get('width'), report.get('height')) != (width, height) or report.get('frames', 0) < 1:
                    raise RuntimeError('Private lifecycle consumer geometry mismatch')
                if report.get('pixels_mapped') is not False or report.get('encoded') is not False:
                    raise RuntimeError('Lifecycle probe must be metadata-only')
                reports.append(dict(width=width,height=height,frames=report['frames'],pixels_mapped=False))
                worker = None
            # Both PipeWire client and its remote FD are now gone; retaining
            # an fd in this parent must not mask a teardown dependency.
            time.sleep(1); pump()
            current = monitors()
            added = set(current) - set(original)
            if len(added) != 1 or any(current.get(k) != v for k, v in original.items()):
                raise RuntimeError('Virtual output did not survive consumer disconnect')
            selected = next(iter(added))
            if connector is not None and selected != connector:
                raise RuntimeError('Reconnect replaced the virtual output identity')
            connector = selected
            if current[connector] != [(width, height)]:
                raise RuntimeError('Virtual output did not preserve negotiated geometry')
            if joint_input and input_worker is None:
                # Mutter publishes the virtual EIS viewport only once PipeWire
                # negotiated the stream. Do not wait for mapped input before
                # starting the first capture consumer: that would deadlock.
                parameters = call(stream, 'org.freedesktop.DBus.Properties', 'Get',
                                  GLib.Variant('(ss)', (prefix + '.Stream', 'Parameters')))[0]
                mapping = parameters.get('mapping-id')
                if not isinstance(mapping, str) or not mapping or len(mapping.encode()) > 4096:
                    raise RuntimeError('Private virtual stream has no bounded EIS mapping')
                eis_fd = open_private_eis()
                input_lease, input_control = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                transition_owner, transition_worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                sys.path.insert(0,str(ROOT/'scripts'))
                from native_eis_control import EisController
                controller = EisController(transition_owner,mapping)
                token = runtime / 'eis-fixture-token'
                with token.open('xb') as secret:
                    os.chmod(token, 0o600); secret.write(b'a' * 64) # test-only authentication value
                input_output = tempfile.TemporaryFile()
                try:
                    input_worker = subprocess.Popen([str(ROOT / 'build/native-eis/uu-native-eis-input'),
                        '--user', pwd.getpwuid(os.getuid()).pw_name, '--token-file', str(token),
                        '--seconds', '20', '--mapped-output'],
                        env=dict(os.environ, UURB_EIS_FD=str(eis_fd), UURB_EIS_LEASE_FD=str(input_lease.fileno()),
                                 UURB_EIS_PARENT_PID=str(os.getpid()), UURB_EIS_MAPPING_ID=mapping,
                                 UURB_EIS_CONTROL_FD=str(transition_worker.fileno())),
                        pass_fds=(eis_fd, input_lease.fileno(), transition_worker.fileno()), stdout=input_output, stderr=subprocess.DEVNULL)
                finally:
                    os.close(eis_fd)
                input_lease.close(); input_lease = None
                transition_worker.close(); transition_worker = None
                deadline = time.monotonic() + 6
                while input_worker.poll() is None and time.monotonic() < deadline:
                    input_output.seek(0)
                    lines = input_output.read(4096).splitlines()
                    for line in lines:
                        if line.startswith(b'UURB_NATIVE_INPUT ') and line.endswith(b'}'):
                            report = json.loads(line[len(b'UURB_NATIVE_INPUT '):])
                            if report.get('event') == 'ready' and report.get('backend') == 'eis':
                                mapped_input_ready = report.get('mapped_absolute_input') is True
                                input_port = report.get('port')
                    if mapped_input_ready: break
                    pump(); time.sleep(0.02)
                if not mapped_input_ready:
                    input_output.seek(0)
                    diagnostics = []
                    for line in input_output.read(4096).splitlines():
                        if line.startswith(b'UURB_NATIVE_INPUT ') and line.endswith(b'}'):
                            value = json.loads(line[len(b'UURB_NATIVE_INPUT '):])
                            if value.get('event') == 'eis_exit':
                                diagnostics.append({k:value[k] for k in ('stage','reason','lease_or_dispatch_failed')})
                    raise RuntimeError('Native input did not negotiate the actual private virtual viewport: ' + json.dumps(diagnostics))
                input_client = socket.create_connection(('127.0.0.1',input_port),timeout=2)
                input_client.sendall(struct.pack('<II64s',0x58315255,3,b'a'*64))
                if input_client.recv(16,socket.MSG_WAITALL) != struct.pack('<4I',0x58315255,0,1,0):
                    raise RuntimeError('Private native input authentication failed')
                rejected_input(1,0x2002)
            elif joint_input:
                eis_fd = open_private_eis()
                try:
                    dimensions = controller.resume(eis_fd)
                finally:
                    os.close(eis_fd)
                if dimensions != dict(logical_width=width,logical_height=height):
                    raise RuntimeError('Rebound input does not match private logical geometry')
                rejected_input(3,0x2002)
                input_rebound_after_resize = True
                tcp_connection_retained = input_worker.poll() is None
        if joint_input:
            call(remote_session, remote_prefix + '.Session', 'Stop'); remote_session = None; session = None
            if input_worker.wait(timeout=3) == 0:
                raise RuntimeError('Closed joint session did not revoke its EIS worker')
        else:
            call(session, prefix + '.Session', 'Stop'); session = None
        deadline = time.monotonic() + 3
        while monitors() != original and time.monotonic() < deadline:
            pump(); time.sleep(0.05)
        if monitors() != original:
            raise RuntimeError('Closing owned session did not remove only its virtual output')
        report = dict(synthetic_only=True, private_compositor_pid_verified=True, uu_controller_tested=False,
            same_virtual_output_across_consumers=True, output_survived_consumer_exit=True,
            output_removed_with_session=True, original_outputs_preserved=True, consumers=reports)
        if joint_input:
            report.update(actual_mutter_eis_mapping_ready=mapped_input_ready,
                          input_rebound_after_resize=input_rebound_after_resize,
                          input_tcp_connection_retained=tcp_connection_retained, desktop_input_sent=False,
                          public_portal_tested=False)
        return report
    finally:
        if input_worker and input_worker.poll() is None:
            input_worker.terminate()
            try: input_worker.wait(timeout=3)
            except subprocess.TimeoutExpired: input_worker.kill(); input_worker.wait(timeout=3)
        if input_lease: input_lease.close()
        if input_control: input_control.close()
        if input_output: input_output.close()
        if transition_owner: transition_owner.close()
        if transition_worker: transition_worker.close()
        if input_client: input_client.close()
        if worker and worker.poll() is None:
            worker.terminate()
            try: worker.wait(timeout=3)
            except subprocess.TimeoutExpired: worker.kill(); worker.wait(timeout=3)
        if remote_session:
            try: call(remote_session, remote_prefix + '.Session', 'Stop')
            except GLib.Error: pass
        elif session:
            try: call(session, prefix + '.Session', 'Stop')
            except GLib.Error: pass
        if subscription:
            bus.signal_unsubscribe(subscription)
