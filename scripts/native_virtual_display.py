"""Owned Mutter virtual monitors, shared by UU's IDD API and the console.

Only sessions created here may be resized or removed. Closing the D-Bus owner
also removes its monitors; no driver installation or saved host layout edit.
"""
import copy
import json
import os
import select
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time

from native_display_topology import SelectedMutterDisplay, planned_topology
from native_display_config import MAX_REFRESH_HZ

SC = 'org.gnome.Mutter.ScreenCast'
SC_PATH = '/org/gnome/Mutter/ScreenCast'
ADAPTER_NAME = 'GameViewer Virtual Display Adapter'
DEFAULT_MODES = [(1920, 1080, 60), (2560, 1440, 60), (1280, 720, 60), (3840, 2160, 60)]
MAX_MODES = 64  # Matches UURB_DISPLAY_MAX_MONITOR_MODES, including UU's 48-mode list.
TOPOLOGY_CONTROL = 0x55520001  # Internal bridge RPC, never a vendor IDD IOCTL.


def validate_modes(modes):
    if not isinstance(modes, list) or not 1 <= len(modes) <= MAX_MODES:
        raise ValueError('Expected 1 to 64 virtual display modes')
    result = []
    for index, mode in enumerate(modes):
        if (not isinstance(mode, (list, tuple)) or len(mode) != 3 or
                any(type(value) is not int for value in mode)):
            raise ValueError(f'Invalid virtual mode tuple at index {index}')
        if (any(not 320 <= value <= 4096 or value % 2 for value in mode[:2]) or
                not 1 <= mode[2] <= MAX_REFRESH_HZ):
            raise ValueError(f'Unsupported virtual mode at index {index}: '
                             f'{mode[0]}x{mode[1]}@{mode[2]} (maximum {MAX_REFRESH_HZ} Hz)')
        if tuple(mode) not in result:
            result.append(tuple(mode))
    return result


def mode_entry(mode):
    width, height, refresh = mode
    return dict(id=f'{width}x{height}@{refresh:.3f}', width=width, height=height,
                refresh=float(refresh), scales=[1.0])


class VirtualDisplays:
    def __init__(self, backend, capture_binary, enabled=True):
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.backend = backend
        self.bus = backend.bus
        self.capture_binary = Path(capture_binary)
        self.available = enabled is True and self.capture_binary.is_absolute() and self.capture_binary.is_file()
        self.screens = {}
        self.modes = {index: validate_modes(DEFAULT_MODES) for index in range(8)}
        self.debug_enabled = False
        self.display_ids = {}
        self.exclusive_baseline = None
        self.recovery_path = None
        self.recovery_identity = backend.identity()
        self.owner_fd = None
        self.owner_pid = None
        self.parent_fd = None
        self.session_seen = False
        self.idle_since = None
        self.session_updated = 0
        self.managed_connectors = set()
        self.decorate(backend.monitors())

    @staticmethod
    def _connectors(layout):
        return {monitor['connector'] for monitor in layout.get('monitors', [])}

    def _event(self, name, **fields):
        """Emit bounded lifecycle breadcrumbs to the display-service journal."""
        value = dict(event='virtual_' + name)
        for key, item in fields.items():
            if isinstance(item, (str, int, bool)):
                value[key] = item
        print(json.dumps(value, sort_keys=True), flush=True)

    def call(self, path, interface, method, args=None):
        reply = self.bus.call_sync(SC, path, interface, method, args, None,
                                  self.Gio.DBusCallFlags.NO_AUTO_START, 3000, None)
        return reply.unpack() if reply else ()

    def pump(self):
        context = self.GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

    def find(self, connector):
        return next((screen for screen in self.screens.values()
                     if screen['connector'] == connector), None)

    def entries(self, screen):
        modes = list(self.modes[screen['slot']])
        modes += [mode for mode in screen.get('history', []) if mode not in modes]
        return [mode_entry(mode) for mode in modes[:64]]

    def _topology(self):
        return SelectedMutterDisplay(self.backend.monitors()[0]['connector']).current()

    def restore_physical(self):
        baseline = self.exclusive_baseline
        if baseline is None:
            return
        self._event('exclusive_restore_begin')
        # Restore before stopping the last virtual output: a desktop must
        # always have an active output, including on remote/client crashes.
        self._restore_layout(baseline, self._topology())
        self.exclusive_baseline = None
        if self.recovery_path is not None:
            self.recovery_path.unlink(missing_ok=True)
        self._event('exclusive_restore_complete')

    def session_state(self, pid, tracks):
        if type(tracks) is not int or not 0 <= tracks <= 8:
            raise ValueError('Invalid remote video-track count')
        if pid != self.owner_pid:
            # Acquire both descriptors before replacing the existing owner.
            # A caller can exit between its RPC and this /proc lookup.
            owner_fd = os.pidfd_open(pid)
            try:
                process_stat = (Path('/proc') / str(pid) / 'stat').read_text()
                parent_pid = int(process_stat[process_stat.rindex(')') + 2:].split()[1])
                parent_fd = os.pidfd_open(parent_pid)
            except BaseException:
                os.close(owner_fd)
                raise
            if self.owner_fd is not None:
                self.release_all()
                os.close(self.owner_fd)
            self.owner_fd = owner_fd
            self.owner_pid = pid
            if self.parent_fd is not None:
                os.close(self.parent_fd)
            self.parent_fd = parent_fd
            self._event('session_owner', pid=pid)
        now = time.monotonic()
        if tracks:
            self.session_seen = True
            self.idle_since = None
        elif self.idle_since is None:
            self.idle_since = now
        self.session_updated = now
        return dict(completed=True)

    def bind_screen_owner(self, slot, pid):
        screen = self.screens.get(slot)
        if screen and screen.get('owner_pid') != pid:
            if screen.get('owner_fd') is not None:
                os.close(screen['owner_fd'])
            screen.update(owner_pid=pid, owner_fd=os.pidfd_open(pid))

    def poll_lifetime(self, allow_idle=True):
        now = time.monotonic()
        if self.owner_fd is not None and (select.select([self.owner_fd, self.parent_fd], [], [], 0)[0] or
                                          now - self.session_updated > 30):
            self._event('owner_lost', pid=self.owner_pid)
            self.release_all()
            os.close(self.owner_fd)
            self.owner_fd, self.owner_pid = None, None
            if self.parent_fd is not None:
                os.close(self.parent_fd)
                self.parent_fd = None
        for slot, screen in list(self.screens.items()):
            if screen.get('owner_fd') is not None and select.select([screen['owner_fd']], [], [], 0)[0]:
                self._event('uu_process_exited', slot=slot)
                try:
                    self.remove(slot)
                except Exception as error:
                    self._event('owner_cleanup_failed', slot=slot, error_type=type(error).__name__)
        # DXGI tracks close briefly during mode changes and screen switching.
        # A 15-second quiet window exceeds UU's observed video recreation and
        # preserves the independent 30-second mode confirmation guardian.
        if allow_idle and self.screens and self.idle_since is not None and now - self.idle_since >= 15:
            self._event('remote_tracks_closed', quiet_seconds=15)
            self.release_all()

    def apply_topology(self, data):
        if len(data) < 36:
            raise ValueError('Incomplete native topology')
        serial, flags, count = struct.unpack_from('<3I', data)
        if not 1 <= count <= 8 or len(data) != 12 + count * 24 or flags not in (0, 1):
            raise ValueError('Invalid bounded native topology')
        current = self._topology()
        self._event('topology_received', requested_serial=serial, current_serial=current['serial'],
                    paths=count, verify=bool(flags))
        if serial != current['serial']:
            raise ValueError('Stale native topology')
        requested = list(struct.iter_unpack('<4I2i', data[12:]))
        if len({entry[0] for entry in requested}) != count:
            raise ValueError('Duplicate native output')
        reverse = {value: key for key, value in self.display_ids.items()}
        names = [reverse.get(entry[0]) for entry in requested]
        if None in names:
            raise ValueError('Unknown native output identity')
        owned = {screen['connector'] for screen in self.screens.values()}
        if set(names) <= owned:
            # Validate the whole request before removing or resizing any
            # output. A bad later entry must not partially apply the plan.
            monitors = {item['connector']: item for item in self.backend.monitors()}
            for name, (_, width, height, refresh, x, y) in zip(names, requested):
                screen = self.find(name)
                if (name not in monitors or x < 0 or y < 0 or not any(
                        entry['width'] == width and entry['height'] == height and
                        abs(refresh - entry['refresh'] * 1000) <= 1 for entry in self.entries(screen))):
                    raise ValueError('Topology requests must select an advertised virtual mode')
            if sum(x == 0 and y == 0 for _, _, _, _, x, y in requested) != 1:
                raise ValueError('Virtual topology needs one origin output')
            # Negotiating a new virtual mode can make GNOME reactivate a
            # saved kernel/physical output. Restore the layout from before
            # that hotplug, not the compositor's incidental fallback layout.
            entry_baseline = copy.deepcopy(current)
            for slot, screen in list(self.screens.items()):
                if screen['connector'] not in names and not flags:
                    self.remove(slot)
            layouts = []
            for name, (_, width, height, refresh, x, y) in zip(names, requested):
                monitor = monitors[name]
                screen = self.find(name)
                if not flags and ((width, height) != (monitor['width'], monitor['height']) or
                                  abs(refresh - monitor['refresh'] * 1000) > 1):
                    self._negotiate(screen, (width, height, round(refresh / 1000)))
                    self._event('topology_mode_selected', slot=screen['slot'], width=width, height=height,
                                refresh=round(refresh / 1000))
                    current = self._topology()
                layout = next(copy.deepcopy(item) for item in current['topology']['logical']
                              if name in self._connectors(item))
                layout.update(x=x, y=y, primary=(x == 0 and y == 0))
                layouts.append(layout)
            if sum(item['primary'] for item in layouts) != 1:
                raise ValueError('Virtual topology needs one origin output')
            if not flags:
                baseline = self.exclusive_baseline or entry_baseline
                if self.exclusive_baseline is None:
                    self.exclusive_baseline = copy.deepcopy(baseline)
                    self.exclusive_baseline['topology']['logical'] = [
                        item for item in self.exclusive_baseline['topology']['logical']
                        if not self._connectors(item) & owned]
                    if self.recovery_path is not None:
                        from native_runtime_state import write_private
                        physical = copy.deepcopy(self.exclusive_baseline)
                        write_private(self.recovery_path, dict(version=1, identity=self.recovery_identity,
                                                              baseline=physical))
                self._event('exclusive_begin', outputs=len(layouts))
                self._preserve_layout(dict(current, topology=dict(current['topology'], logical=layouts)))
                self._event('exclusive_complete', outputs=len(self.backend.monitors()))
        else:
            baseline = self.exclusive_baseline
            if baseline is None:
                # UU can repeat its physical restore after its unplug IOCTL
                # has already restored the baseline. A precise no-op is safe.
                live = {item['connector']: item for item in self.backend.monitors()}
                if set(names) != set(live) or any(
                        (width, height, x, y) != (live[name]['width'], live[name]['height'],
                                                live[name]['x'], live[name]['y']) or
                        abs(refresh - live[name]['refresh'] * 1000) > 1
                        for name, (_, width, height, refresh, x, y) in zip(names, requested)):
                    raise ValueError('No owned topology restoration is pending')
                return dict(completed=True)
            expected = {name for item in baseline['topology']['logical']
                        for name in self._connectors(item) if name not in owned}
            if set(names) != expected:
                raise ValueError('Only the saved physical topology may be restored')
            if not flags:
                self.restore_physical()
        return dict(completed=True)

    def _preserve_layout(self, before, added=None, removed=None):
        current = self._topology()
        layouts = [copy.deepcopy(item) for item in before['topology']['logical']
                   if not any(m['connector'] == removed for m in item['monitors'])]
        if added is not None:
            extra = next(copy.deepcopy(item) for item in current['topology']['logical']
                         if any(m['connector'] == added for m in item['monitors']))
            extra.update(x=max(item['x'] + item['size'][0] for item in layouts), y=0, primary=False)
            layouts.append(extra)
        configuration = []
        for item in layouts:
            outputs = [(m['connector'], m['mode'],
                        {'enable_underscanning': self.GLib.Variant('b', m['underscanning'])}
                        if m['supports_underscanning'] else {}) for m in item['monitors']]
            configuration.append((item['x'], item['y'], item['scale'], item['transform'], item['primary'], outputs))
        properties = ({'layout-mode': self.GLib.Variant('u', before['layout_mode'])}
                      if before['can_change_layout'] else {})
        self.bus.call_sync('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig',
            'org.gnome.Mutter.DisplayConfig', 'ApplyMonitorsConfig',
            self.GLib.Variant('(uua(iiduba(ssa{sv}))a{sv})', (current['serial'], 1, configuration, properties)),
            None, self.Gio.DBusCallFlags.NO_AUTO_START, 3000, None)

    def _restore_layout(self, baseline, current, removed=None):
        """Restore the physical baseline while retaining other live virtual screens.

        UU can temporarily make the native output inactive while Super Screen is
        active.  The current topology then contains only the virtual output, so
        rebuilding from ``current`` would lose the physical output forever.  The
        creation snapshot is authoritative for physical outputs; still-live
        virtual outputs are merged back from the post-stop topology.
        """
        active_virtual = {screen['connector'] for screen in self.screens.values()}
        known_virtual = active_virtual | ({removed} if removed is not None else set())
        layouts = []
        for layout in baseline.get('topology', {}).get('logical', []):
            connectors = self._connectors(layout)
            if removed is not None and removed in connectors:
                continue
            if connectors & known_virtual:
                continue
            layouts.append(copy.deepcopy(layout))
        for layout in current.get('topology', {}).get('logical', []):
            connectors = self._connectors(layout)
            if connectors & active_virtual:
                layout = copy.deepcopy(layout)
                layout.update(x=max((item['x'] + item['size'][0] for item in layouts), default=0),
                              y=0, primary=False)
                layouts.append(layout)
        if not layouts:
            raise RuntimeError('No output remains while restoring the virtual-display baseline')
        target = dict(baseline, topology=dict(baseline['topology'], logical=layouts))
        self._preserve_layout(target)
        observed = self._topology()
        expected = {tuple(sorted(self._connectors(item))) for item in layouts}
        actual = {tuple(sorted(self._connectors(item))) for item in observed['topology']['logical']}
        self._event('restore_check', expected=len(expected), actual=len(actual))
        if expected != actual:
            raise RuntimeError('Mutter did not restore the physical/virtual output topology')

    def _negotiate(self, screen, mode):
        width, height, refresh = mode
        self._event('negotiate_begin', slot=screen['slot'], width=width, height=height, refresh=refresh)
        environment = dict(os.environ, UURB_CAPTURE_SIZE=f'{width}x{height}',
                           UURB_CAPTURE_MAX_FPS=str(refresh), UURB_CAPTURE_DURATION_SECONDS='1')
        for key in ('UURB_CAPTURE_STATUS_FD', 'UURB_CURSOR_STATE_FD', 'UURB_CURSOR_COMPOSITE',
                    'UURB_CAPTURE_TARGET_SERIAL', 'UURB_CURSOR_METADATA_BACKEND'):
            environment.pop(key, None)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as remote, tempfile.TemporaryFile() as log:
            remote.connect(str(Path(os.environ['XDG_RUNTIME_DIR']) / 'pipewire-0'))
            child = subprocess.Popen([str(self.capture_binary), str(remote.fileno()), str(screen['node'])],
                env=environment, pass_fds=(remote.fileno(),), stdout=log, stderr=log)
            deadline = time.monotonic() + 5
            try:
                while child.poll() is None and time.monotonic() < deadline:
                    self.pump()
                    time.sleep(0.01)
                if child.poll() is None:
                    raise RuntimeError('Virtual display negotiation timed out')
                log.seek(0)
                reports = []
                for line in log.read(65536).splitlines():
                    if line.startswith(b'{'):
                        try:
                            reports.append(json.loads(line))
                        except ValueError:
                            pass
                self._event('negotiate_result', slot=screen['slot'], exit_code=child.returncode,
                            reports=len(reports), frames=sum(item.get('frames', 0) for item in reports))
                if child.returncode or not any(
                        item.get('width') == width and item.get('height') == height and
                        item.get('frames', 0) > 0 and item.get('pixels_mapped') is False for item in reports):
                    raise RuntimeError('Virtual display did not negotiate the requested mode')
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=1)
        self.pump()
        history = screen.setdefault('history', [])
        if tuple(mode) not in history:
            history.append(tuple(mode))
            del history[:-32]

    def create(self, slot, modes=None):
        if not self.available:
            raise RuntimeError('This runtime has no pinned virtual display negotiator')
        if type(slot) is not int or not 0 <= slot < 8:
            raise ValueError('Invalid virtual display slot')
        if slot in self.screens:
            return self.screens[slot]
        if modes is not None:
            self.modes[slot] = validate_modes(modes)
        before = {item['connector']: item for item in self.backend.monitors()}
        before_layout = self._topology()
        self._event('create_begin', slot=slot, active_before=len(before),
                    logical_before=len(before_layout['topology']['logical']))
        if len(before) >= 8:
            raise ValueError('At most eight active displays are supported')
        session = self.call(SC_PATH, SC, 'CreateSession', self.GLib.Variant('(a{sv})', ({},)))[0]
        subscription = None
        stage = 'record_virtual'
        try:
            # This stream owns the monitor and negotiates its mode. Video is
            # captured separately by RecordMonitor with the user's cursor
            # policy; embedding a sprite here can bake it into the shared
            # virtual framebuffer before that metadata capture reads it.
            stream = self.call(session, SC + '.Session', 'RecordVirtual',
                self.GLib.Variant('(a{sv})', ({'cursor-mode': self.GLib.Variant('u', 0)},)))[0]
            self._event('owner_cursor_policy', slot=slot, cursor_mode='hidden')
            nodes = []
            subscription = self.bus.signal_subscribe(SC, SC + '.Stream', 'PipeWireStreamAdded', stream,
                None, self.Gio.DBusSignalFlags.NONE, lambda *args: nodes.append(args[-1].unpack()[0]))
            stage = 'start_session'
            self.call(session, SC + '.Session', 'Start')
            stage = 'wait_pipewire_node'
            deadline = time.monotonic() + 3
            while not nodes and time.monotonic() < deadline:
                self.pump()
                time.sleep(0.01)
            if len(nodes) != 1:
                raise RuntimeError('No unique virtual PipeWire source')
            screen = dict(slot=slot, session=session, stream=stream, node=int(nodes[0]), connector=None,
                          baseline_topology=copy.deepcopy(before_layout))
            stage = 'negotiate_mode'
            self._negotiate(screen, self.modes[slot][0])
            stage = 'identify_monitor'
            after = {item['connector']: item for item in self.backend.monitors()}
            # GNOME may simultaneously re-enable a connected VKMS/physical
            # output from its saved layout. Only the new Mutter virtual
            # connector belongs to this RecordVirtual session.
            added = {name for name in set(after) - set(before) if after[name].get('virtual') is True}
            self._event('identity_candidates', slot=slot, count=len(added))
            if len(added) != 1:
                raise RuntimeError('Virtual monitor identity is ambiguous')
            screen['connector'] = added.pop()
            # GNOME can load its saved physical modes when a monitor appears.
            # Restore the live layout the user had immediately before creation.
            stage = 'preserve_layout'
            self._preserve_layout(before_layout, added=screen['connector'])
            stage = 'verify_layout'
            after = {item['connector']: item for item in self.backend.monitors()}
            for connector, item in before.items():
                if connector not in after or any(after[connector][key] != item[key] for key in
                        ('width', 'height', 'refresh', 'scale', 'x', 'y', 'primary')):
                    raise RuntimeError('Virtual creation changed another display')
            self.screens[slot] = screen
            self.managed_connectors.add(screen['connector'])
            if self.idle_since is not None:
                self.idle_since = time.monotonic()
            self._event('create_complete', slot=slot, active_after=len(after), connector=screen['connector'])
            return screen
        except BaseException as error:
            self._event('create_failed', slot=slot, stage=stage, error_type=type(error).__name__)
            try:
                self.call(session, SC + '.Session', 'Stop')
                self._preserve_layout(before_layout)
            except Exception:
                pass
            raise
        finally:
            if subscription is not None:
                self.bus.signal_unsubscribe(subscription)

    def remove(self, slot):
        if type(slot) is not int or not 0 <= slot < 8:
            raise ValueError('Invalid virtual display slot')
        screen = self.screens.get(slot)
        if screen is not None:
            if self.exclusive_baseline is not None:
                self.restore_physical()
            baseline = copy.deepcopy(screen.get('baseline_topology'))
            before = self._topology()
            self._event('remove_begin', slot=slot, connector=screen['connector'],
                        logical_before=len(before['topology']['logical']))
            self.call(screen['session'], SC + '.Session', 'Stop')
            del self.screens[slot]
            if screen.get('owner_fd') is not None:
                os.close(screen['owner_fd'])
            self.pump()
            current = self._topology()
            try:
                self._restore_layout(baseline or before, current, removed=screen['connector'])
            except BaseException as error:
                self._event('remove_failed', slot=slot, error_type=type(error).__name__, reason=str(error)[:200])
                raise
            self._event('remove_complete', slot=slot, active_after=len(self.backend.monitors()))
        return dict(removed=screen is not None)

    def release_all(self):
        """Remove every virtual output owned by this display-service session.

        The display service is shared by the console and the UU bridge, but a
        virtual output must never outlive the UU remote session that created
        it.  The bridge calls this during its finally/cleanup path; keep the
        operation idempotent so recovery after a crashed Wine process is safe.
        """
        removed = []
        # Later screens may have a baseline that includes earlier ones. Remove
        # in reverse creation order so each nested baseline can be restored.
        failed = []
        for slot in reversed(tuple(self.screens)):
            try:
                result = self.remove(slot)
                if result.get('removed'):
                    removed.append(slot)
            except Exception as error:
                # Continue releasing the remaining outputs.  The service
                # shutdown path will remove any session that is still alive.
                failed.append(slot)
                self._event('release_failed', slot=slot, error_type=type(error).__name__)
        return dict(released=removed, count=len(removed), failed=failed)

    def decorate(self, monitors):
        for item in monitors:
            if item['connector'] not in self.display_ids:
                used = set(self.display_ids.values())
                value = next((index for index in range(8) if index not in used), None)
                if value is None:
                    raise ValueError('Native display identity capacity exceeded')
                self.display_ids[item['connector']] = value
            value = self.display_ids[item['connector']]
            item.update(gdi_name='\\\\.\\DISPLAY%d' % (value + 1),
                        device_path='\\\\?\\DISPLAY#UURB#%s#%d' % (item['connector'], value))
            screen = self.find(item['connector'])
            if screen:
                item.update(virtual=True, friendly=ADAPTER_NAME, virtual_slot=screen['slot'])
                entries = self.entries(screen)
                item['modes'] += [{key: entry[key] for key in ('width', 'height', 'refresh', 'scales')}
                                  for entry in entries if not any(
                                      old['width'] == entry['width'] and old['height'] == entry['height'] and
                                      abs(old['refresh'] - entry['refresh']) < .01 for old in item['modes'])]
        return monitors

    def status(self):
        self.pump()
        active = {item['connector']: item for item in self.backend.monitors()}
        outputs = []
        for specification, modes, properties in self.backend._reply()[1]:
            connector = specification[0]
            screen = self.find(connector)
            vkms = any('/faux/vkms/' in str(path.resolve()) for path in Path('/sys/class/drm').glob('card*-' + connector))
            outputs.append(dict(connector=connector, enabled=connector in active,
                                name=str(properties.get('display-name') or specification[2] or connector)[:128],
                                kind='super_screen' if screen else 'mutter' if connector.startswith('Meta-') else
                                     'vkms' if vkms else 'physical',
                                managed=screen is not None,
                                slot=screen['slot'] if screen else None,
                                width=active.get(connector, {}).get('width'),
                                height=active.get(connector, {}).get('height'),
                                refresh=active.get(connector, {}).get('refresh')))
        return dict(supported=self.available, exclusive=self.exclusive_baseline is not None,
                    managed_connectors=sorted(self.managed_connectors),
                    outputs=outputs, tracks_idle=self.idle_since is not None,
                    cleanup_after_seconds=max(0, round(15 - (time.monotonic() - self.idle_since)))
                        if self.screens and self.idle_since is not None else None,
                    screens=[dict(slot=item['slot'], connector=item['connector'])
                    for item in self.screens.values()])

    def set_output(self, connector, enabled):
        if not isinstance(connector, str) or len(connector) > 128 or type(enabled) is not bool:
            raise ValueError('Invalid output selection')
        if self.find(connector):
            raise ValueError('Use virtual-remove for owned Super Screens')
        state = self._topology()
        layouts = copy.deepcopy(state['topology']['logical'])
        exists = any(connector in self._connectors(item) for item in layouts)
        if exists == enabled:
            return dict(completed=True)
        if not enabled:
            layouts = [item for item in layouts if connector not in self._connectors(item)]
            if not layouts:
                raise ValueError('The last active desktop output cannot be disabled')
            if not any(item['primary'] for item in layouts):
                layouts[0]['primary'] = True
            if not any(item['x'] == 0 and item['y'] == 0 for item in layouts):
                dx, dy = layouts[0]['x'], layouts[0]['y']
                for item in layouts:
                    item['x'] -= dx; item['y'] -= dy
        else:
            raw = next((item for item in self.backend._reply()[1] if item[0][0] == connector), None)
            if raw is None:
                raise ValueError('Output is no longer connected')
            from native_display_config import _decode_modes
            modes, _ = _decode_modes(raw[1])
            if not modes:
                raise ValueError('No supported output mode')
            mode = modes[0]
            scale = 1.0 if 1.0 in mode['scales'] else mode['scales'][0]
            width, height = mode['width'], mode['height']
            if state['layout_mode'] == 2:
                width, height = round(width / scale), round(height / scale)
            layouts.append(dict(x=max(item['x'] + item['size'][0] for item in layouts), y=0,
                                primary=False, scale=scale, transform=0, size=[width,height],
                                monitors=[dict(connector=connector, mode=mode['id'], underscanning=False,
                                               supports_underscanning=False)]))
        # Method 2 needs GNOME Shell's separate confirmation and otherwise
        # silently reverts after its timeout. Keep console toggles temporary,
        # like the rest of the remote display lifecycle.
        self._preserve_layout(dict(state, topology=dict(state['topology'], logical=layouts)))
        self._event('output_changed', connector=connector, enabled=enabled, persistent=False)
        return dict(completed=True)

    def ioctl(self, code, payload):
        if type(code) is not int or not isinstance(payload, list) or len(payload) > 1024 or any(
                type(value) is not int or not 0 <= value <= 255 for value in payload):
            raise ValueError('Invalid bounded virtual display request')
        data = bytes(payload)
        if code == TOPOLOGY_CONTROL:
            try:
                return self.apply_topology(data)
            except (ValueError, RuntimeError) as error:
                self._event('topology_rejected', reason=str(error)[:200])
                raise
        if code == 0 and not data:
            if not self.available:
                raise RuntimeError('Virtual display support is unavailable in this runtime')
            return self.status()
        if code == 0x30c014 and len(data) == 1:
            self.debug_enabled = bool(data[0])
        elif code == 0x30c004 and len(data) == 24:
            slot, edid_type = struct.unpack_from('<2I', data)
            if edid_type > 2:
                raise ValueError('Unknown virtual monitor description')
            self.create(slot)
        elif code == 0x30c008 and len(data) == 4:
            self.remove(struct.unpack('<I', data)[0])
        elif code in (0x30c00c, 0x30c010) and len(data) >= 20:
            slot, count = struct.unpack_from('<2I', data)
            if slot >= 8 or not 1 <= count <= MAX_MODES or len(data) != 8 + count * 12:
                raise ValueError('Invalid virtual monitor mode array')
            # Reviewed IDD wire: slot, count, then (width, height, refresh).
            received = [list(mode) for mode in struct.iter_unpack('<3I', data[8:])]
            self._event('modes_received', slot=slot, count=count,
                        max_width=max(mode[0] for mode in received),
                        max_height=max(mode[1] for mode in received),
                        max_refresh=max(mode[2] for mode in received),
                        catalogue=json.dumps(received, separators=(',', ':')))
            try:
                modes = validate_modes(received)
            except ValueError as error:
                self._event('modes_rejected', slot=slot, reason=str(error))
                raise
            self.modes[slot] = modes
            self._event('modes_accepted', slot=slot, count=len(modes))
            if code == 0x30c00c and slot in self.screens:
                self._negotiate(self.screens[slot], modes[0])
        else:
            raise ValueError('Unsupported virtual display operation')
        return dict(completed=True)

    def close(self):
        self.release_all()
        if self.owner_fd is not None:
            os.close(self.owner_fd)
            self.owner_fd = None
        if self.parent_fd is not None:
            os.close(self.parent_fd)
            self.parent_fd = None


def recover_layout(config):
    """ExecStopPost/startup recovery after the D-Bus owner was killed."""
    from native_runtime_state import read_private
    from native_display_config import MutterDisplay
    path = Path(config['state_parent']) / 'virtual-layout-recovery.json'
    if not path.exists():
        return
    record = read_private(path)
    backend = MutterDisplay()
    if record.get('version') != 1 or record.get('identity') != backend.identity():
        path.unlink()  # A previous compositor must not rewrite this desktop.
        return
    manager = VirtualDisplays(backend, '/nonexistent', enabled=False)
    baseline = record['baseline']
    current = manager._topology()
    connected = set(current['topology']['connected'])
    baseline['topology']['logical'] = [item for item in baseline['topology']['logical']
                                     if manager._connectors(item) <= connected]
    if not baseline['topology']['logical']:
        raise RuntimeError('No saved physical output remains connected')
    manager._preserve_layout(baseline)
    path.unlink()
    manager._event('crash_layout_restored')


class VirtualSelectedDisplay(SelectedMutterDisplay):
    def __init__(self, connector, manager):
        super().__init__(connector)
        self.manager = manager

    @property
    def ephemeral(self):
        return self.manager.find(self.connector) is not None

    def identity(self):
        base = super().identity()
        screen = self.manager.find(self.connector)
        return base + ['virtual-session', screen['session']] if screen else base

    def monitors(self):
        return self.manager.decorate(super().monitors())

    def current(self):
        state = super().current()
        screen = self.manager.find(self.connector)
        if screen:
            known = {entry['id'] for entry in state['modes']}
            state['modes'] += [entry for entry in self.manager.entries(screen)
                               if entry['id'] not in known]
        return state

    def configure(self, state, target, verify_only):
        screen = self.manager.find(self.connector)
        if screen is None or target['mode'] == state['mode']:
            return super().configure(state, target, verify_only)
        planned_topology(state, target)
        mode = next((entry for entry in state['modes'] if entry['id'] == target['mode']), None)
        if mode is None or target['scale'] != 1:
            raise ValueError('Unsupported virtual monitor mode or scale')
        if verify_only:
            return
        self.manager._negotiate(screen, (mode['width'], mode['height'], round(mode['refresh'])))
        # Negotiation creates a new native mode and bumps the topology serial.
        # Apply the original plan with that fresh serial to retain placement.
        updated = copy.deepcopy(state)
        updated['serial'] = super().current()['serial']
        return super().configure(updated, target, False)
