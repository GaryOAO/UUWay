"""Actual GPU stop check, called only after private runtime/compositor guards."""
import contextlib
import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import time


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=3)


def result(log):
    log.seek(0)
    for line in log.read(65536).splitlines():
        if line.startswith(b'{'):
            value = json.loads(line)
            if isinstance(value, dict) and type(value.get('frames')) is int:
                return value
    raise RuntimeError('Missing native frame report')


def capture_stop(bus, runtime, producer_binary, receiver_binary):
    from gi.repository import Gio, GLib
    # Defense in depth; the caller additionally checks the private bus owner PID.
    if (runtime != Path(os.environ['XDG_RUNTIME_DIR']).resolve() or
            not runtime.parent.name.startswith('uurb-mutter-test-') or runtime.parent.parent != Path('/tmp')):
        raise RuntimeError('Expected owned synthetic runtime')
    for binary in (producer_binary, receiver_binary):
        info = binary.lstat()
        if (not binary.is_absolute() or not stat.S_ISREG(info.st_mode) or
                info.st_uid != os.getuid() or info.st_mode & 0o022 or not os.access(binary, os.X_OK)):
            raise ValueError('Expected explicit owned candidate executable')
    prefix = 'org.gnome.Mutter.ScreenCast'
    def call(path, interface, method, args=None):
        reply = bus.call_sync(prefix, path, interface, method, args, None,
                             Gio.DBusCallFlags.NO_AUTO_START, 3000, None)
        return reply.unpack() if reply else ()
    def pump():
        context = GLib.MainContext.default()
        for _ in range(32):
            if not context.pending(): break
            context.iteration(False)
    checks = []
    for codec, termination in (('h264', signal.SIGTERM), ('hevc', signal.SIGINT)):
        producer = receiver = session = subscription = None
        with contextlib.ExitStack() as resources:
            producer_log, receiver_log, compressed = [resources.enter_context(tempfile.TemporaryFile()) for _ in range(3)]
            remote = resources.enter_context(socket.socket(socket.AF_UNIX))
            send, receive = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            status, writer = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            for stream in (send, receive, status, writer): resources.enter_context(stream)
            try:
                session = call('/org/gnome/Mutter/ScreenCast', prefix, 'CreateSession', GLib.Variant('(a{sv})', ({},)))[0]
                stream = call(session, prefix + '.Session', 'RecordVirtual',
                              GLib.Variant('(a{sv})', ({'cursor-mode': GLib.Variant('u', 1)},)))[0]
                nodes = []
                subscription = bus.signal_subscribe(prefix, prefix + '.Stream', 'PipeWireStreamAdded', stream,
                    None, Gio.DBusSignalFlags.NONE, lambda *args: nodes.append(args[-1].unpack()[0]))
                call(session, prefix + '.Session', 'Start')
                deadline = time.monotonic() + 5
                while not nodes and time.monotonic() < deadline:
                    pump(); time.sleep(0.01)
                if len(nodes) != 1: raise RuntimeError('No private capture source')
                remote.connect(str(runtime / 'pipewire-0'))
                env = dict(os.environ, UURB_CAPTURE_SIZE='1280x720', UURB_CAPTURE_MAX_FPS='60',
                           UURB_CAPTURE_DURATION_SECONDS='0', UURB_CAPTURE_STATUS_FD=str(writer.fileno()))
                for key in ('UURB_CAPTURE_TARGET_SERIAL','UURB_CURSOR_COMPOSITE','UURB_CURSOR_STATE_FD','UURB_CURSOR_METADATA_BACKEND'):
                    env.pop(key, None)
                receiver = subprocess.Popen([str(receiver_binary), str(receive.fileno()), str(compressed.fileno()), codec],
                    pass_fds=(receive.fileno(),compressed.fileno()), stdout=receiver_log, stderr=receiver_log)
                receive.close()
                producer = subprocess.Popen([str(producer_binary), str(remote.fileno()), str(nodes[0]),
                    '--gpu-relay', str(send.fileno())], pass_fds=(remote.fileno(),send.fileno(),writer.fileno()),
                    env=env, stdout=producer_log, stderr=producer_log)
                remote.close(); send.close(); writer.close()
                deadline = time.monotonic() + 5
                linked = False
                while producer.poll() is None and time.monotonic() < deadline:
                    pump()
                    graph = json.loads(subprocess.check_output(['pw-dump'],text=True,timeout=2))
                    consumers = {str(o['id']) for o in graph if o['type'].endswith(':Node') and
                        o.get('info',{}).get('props',{}).get('node.name') == producer_binary.name}
                    outgoing, incoming = [], []
                    for obj in graph:
                        if not obj['type'].endswith(':Port'): continue
                        props = obj.get('info',{}).get('props',{})
                        if str(props.get('node.id')) == str(nodes[0]) and props.get('port.direction') == 'out': outgoing.append(obj['id'])
                        if str(props.get('node.id')) in consumers and props.get('port.direction') == 'in': incoming.append(obj['id'])
                    if len(outgoing) == len(incoming) == 1:
                        subprocess.run(['pw-link',str(outgoing[0]),str(incoming[0])], check=True,
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=2)
                        linked = True; break
                    time.sleep(0.05)
                if not linked: raise RuntimeError('Private GPU ports not linked')
                if not select.select([status],[],[],5)[0]: raise RuntimeError('No actual NVENC first ACK')
                packet, _, flags, _ = status.recvmsg(32)
                if flags or len(packet) != 32 or struct.unpack('<4I2Q',packet)[:5] != (0x53525555,1,1280,720,1):
                    raise RuntimeError('Invalid actual first-frame status')
                started = time.monotonic()
                producer.send_signal(termination)
                producer.wait(timeout=4); receiver.wait(timeout=4)
                elapsed = time.monotonic() - started
                if producer.returncode or receiver.returncode:
                    raise RuntimeError('Signal did not produce clean GPU END: producer=%s receiver=%s' % (producer.returncode,receiver.returncode))
                produced, encoded = result(producer_log), result(receiver_log)
                if (produced.get('pixels_mapped') is not False or produced.get('gpu_relayed') is not True or
                        produced.get('frames') != encoded.get('frames') or encoded.get('encoded') is not True):
                    raise RuntimeError('Unmatched GPU/encode evidence')
                compressed.seek(0)
                decoded = subprocess.check_output(['ffprobe','-v','error','-count_frames','-select_streams','v:0',
                    '-show_entries','stream=width,height,nb_read_frames','-of','json',f'/proc/self/fd/{compressed.fileno()}'],
                    pass_fds=(compressed.fileno(),), stderr=subprocess.DEVNULL,timeout=8)
                streams = json.loads(decoded)['streams']
                if len(streams) != 1 or (streams[0]['width'],streams[0]['height'],int(streams[0]['nb_read_frames'])) != (1280,720,encoded['frames']):
                    raise RuntimeError('Independent frame decode mismatch')
                checks.append(dict(codec=codec,signal=termination.name,frames=encoded['frames'],
                    clean_gpu_end=True,decoded=True,exit_seconds=round(elapsed,3)))
            finally:
                stop(producer); stop(receiver)
                if session:
                    try: call(session,prefix+'.Session','Stop')
                    except GLib.Error: pass
                if subscription: bus.signal_unsubscribe(subscription)
    return dict(synthetic_only=True,public_portal_tested=False,uu_controller_tested=False,
                desktop_input_sent=False,private_compositor_pid_verified=True,stop_checks=checks)
