"""Single-output Mutter display transactions; no RDP or desktop restart.

Only compositor-advertised modes/scales are eligible. Temporary changes use
method 1, never persistent method 2. Exact serial/configuration guards prevent
rollback from overwriting a later local change. No monitor serial is exported.
"""
import math
import json
from pathlib import Path

# PyGObject is only required when a live Mutter D-Bus transaction is opened.
# Keep the decoder/transaction planning helpers importable on headless hosts
# and in recovery tooling where the GUI stack is intentionally absent.
try:
    from gi.repository import Gio, GLib
except ImportError:  # pragma: no cover - exercised by headless deployments
    Gio = None
    GLib = None

SERVICE = 'org.gnome.Mutter.DisplayConfig'
PATH = '/org/gnome/Mutter/DisplayConfig'


def decode_state(reply):
    serial, monitors, logical, properties = reply
    if len(monitors) != 1 or len(logical) != 1:
        raise ValueError('Display bridge currently requires exactly one physical output')
    specification, entries, monitor_properties = monitors[0]
    x, y, scale, transform, primary, outputs, _ = logical[0]
    if len(outputs) != 1 or tuple(outputs[0]) != tuple(specification) or x or y or transform or not primary:
        raise ValueError('Unsupported display layout')
    connector = specification[0]
    if not isinstance(connector, str) or not connector or len(connector) > 128 or not 0.5 <= scale <= 4:
        raise ValueError('Invalid display topology')
    if len(entries) > 256:
        raise ValueError('Display mode count exceeds bound')
    modes = []
    current = None
    for identifier, width, height, refresh, preferred_scale, scales, flags in entries:
        supported = (isinstance(identifier, str) and 0 < len(identifier) <= 128 and
                     2 <= width <= 4096 and 2 <= height <= 4096 and not width % 2 and not height % 2 and
                     math.isfinite(refresh) and 1 <= refresh <= 120 and not flags.get('is-interlaced', False) and
                     flags.get('refresh-rate-mode', 'fixed') == 'fixed')
        if flags.get('is-current') and not supported:
            raise ValueError('Current display mode is outside the native bridge scope')
        if not supported:
            continue
        valid_scales = [float(s) for s in scales if math.isfinite(s) and 0.5 <= s <= 4]
        if not valid_scales or len(valid_scales) > 64:
            raise ValueError('Invalid compositor scale list')
        item = dict(id=identifier, width=int(width), height=int(height), refresh=float(refresh), scales=valid_scales)
        if flags.get('is-current'):
            if current is not None:
                raise ValueError('Ambiguous current display mode')
            current = item
        modes.append(item)
    if current is None or not any(abs(scale - s) < 0.00001 for s in current['scales']):
        raise ValueError('Missing or incompatible current display mode')
    layout_mode = int(properties.get('layout-mode', 1))
    if layout_mode not in (1, 2):
        raise ValueError('Unknown compositor layout mode')
    return dict(serial=int(serial), connector=connector, mode=current['id'], scale=float(scale), modes=modes,
                layout_mode=layout_mode, can_change_layout=bool(properties.get('supports-changing-layout-mode', False)),
                underscanning=bool(monitor_properties.get('is-underscanning', False)),
                supports_underscanning='is-underscanning' in monitor_properties)


def signature(state):
    base = (state['serial'], state['connector'], state['mode'], state['scale'], state['layout_mode'], state['underscanning'])
    # Explicit-output candidate journals retain the whole layout, including
    # non-selected outputs. Preserve legacy single-output journal signatures.
    return base + (json.dumps(state['topology'], sort_keys=True, separators=(',', ':'), allow_nan=False),) if 'topology' in state else base


def choose(state, width, height, refresh=0, scale=None):
    if type(width) is not int or type(height) is not int or type(refresh) not in (int, float) or not math.isfinite(refresh):
        raise ValueError('Invalid numeric display request')
    if scale is None:
        scale = state['scale']
    if type(scale) not in (int, float) or not math.isfinite(scale) or not 0.5 <= scale <= 4:
        raise ValueError('Invalid requested DPI scale')
    candidates = [m for m in state['modes'] if m['width'] == width and m['height'] == height and
                  (refresh in (0, 1) or abs(m['refresh'] - refresh) < 0.75) and
                  any(abs(s - scale) < 0.00001 for s in m['scales'])]
    if not candidates:
        raise ValueError('Requested resolution, refresh or scale is not advertised')
    # Windows rates 0/1 mean default. Prefer the fastest advertised fixed mode,
    # never fabricate 60 Hz for a 4K dummy-plug mode actually reporting 17 Hz.
    target = max(candidates, key=lambda m: m['refresh'])
    return dict(mode=target['id'], scale=float(scale), width=target['width'], height=target['height'], refresh=target['refresh'])


class MutterDisplay:
    def __init__(self):
        if Gio is None or GLib is None:
            raise RuntimeError('PyGObject (gi.repository Gio/GLib) is required for live Mutter display transactions')
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def identity(self):
        bus_id = self.bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
            'GetId', None, None, Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
        owner = self.bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
            'GetNameOwner', GLib.Variant('(s)', (SERVICE,)), None, Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
        return [Path('/proc/sys/kernel/random/boot_id').read_text().strip(), bus_id, owner]

    def current(self):
        reply = self.bus.call_sync(SERVICE, PATH, SERVICE, 'GetCurrentState', None, None,
                                   Gio.DBusCallFlags.NONE, 2000, None)
        return decode_state(reply.unpack())

    def configure(self, state, target, verify_only):
        monitor_properties = {}
        if state['supports_underscanning']:
            monitor_properties['enable_underscanning'] = GLib.Variant('b', state['underscanning'])
        properties = {}
        if state['can_change_layout']:
            properties['layout-mode'] = GLib.Variant('u', state['layout_mode'])
        configuration = [(0, 0, target['scale'], 0, True,
                          [(state['connector'], target['mode'], monitor_properties)])]
        self.bus.call_sync(SERVICE, PATH, SERVICE, 'ApplyMonitorsConfig',
            GLib.Variant('(uua(iiduba(ssa{sv}))a{sv})',
                         (state['serial'], 0 if verify_only else 1, configuration, properties)),
            None, Gio.DBusCallFlags.NONE, 2000, None)


class DisplayTransaction:
    def __init__(self, backend, checkpoint=None):
        self.backend = backend
        self.pending = None
        self.checkpoint = checkpoint or (lambda pending: None)

    def prepare(self, serial, width, height, refresh=0, scale=None):
        if self.pending is not None:
            raise RuntimeError('Another display change still requires confirmation')
        state = self.backend.current()
        if type(serial) is not int or serial != state['serial']:
            raise RuntimeError('Display configuration changed since enumeration')
        target = choose(state, width, height, refresh, scale)
        self.backend.configure(state, target, True)
        if signature(self.backend.current()) != signature(state):
            raise RuntimeError('Display changed during mode verification')
        return dict(before=state, target=target)

    def apply(self, plan):
        if self.pending is not None or signature(self.backend.current()) != signature(plan['before']):
            raise RuntimeError('Display plan is stale or another transaction is pending')
        before, target = plan['before'], plan['target']
        if not plan.get('force_reset', False) and before['mode'] == target['mode'] and abs(before['scale'] - target['scale']) < 0.00001:
            return dict(changed=False, serial=before['serial'])
        # Do not retry an ambiguous D-Bus reply. Keep intent available to the
        # caller even if the compositor changes state then the reply is lost.
        self.pending = dict(before=before, target=target, observed=None, confirmed=False)
        try:
            self.checkpoint(self.pending)
        except Exception:
            self.pending = None  # No compositor write was issued.
            raise
        self.backend.configure(before, target, False)
        observed = self.backend.current()
        if hasattr(self.backend, 'validate_observed'):
            self.backend.validate_observed(before, target, observed)
        self.pending['observed'] = observed
        self.checkpoint(self.pending)
        if (observed['connector'] != before['connector'] or observed['mode'] != target['mode'] or
                abs(observed['scale'] - target['scale']) > 0.00001 or observed['layout_mode'] != before['layout_mode']):
            raise RuntimeError('Compositor state does not match the requested display mode')
        return dict(changed=True, serial=observed['serial'])

    def confirm(self, serial):
        if self.pending is None or self.pending['observed'] is None:
            raise RuntimeError('No observed display transaction to confirm')
        current = self.backend.current()
        if serial != current['serial'] or signature(current) != signature(self.pending['observed']):
            raise RuntimeError('Display changed before confirmation')
        self.checkpoint(None)
        self.pending = None

    def rollback(self):
        if self.pending is None:
            return False
        pending = self.pending
        current = self.backend.current()
        if pending['observed'] is None:
            raise RuntimeError('Ambiguous display change requires observation before rollback')
        if signature(current) != signature(pending['observed']):
            self.checkpoint(None)
            self.pending = None
            return False  # Preserve a later local/user display change.
        before = pending['before']
        self.backend.configure(current, dict(mode=before['mode'], scale=before['scale']), True)
        if signature(self.backend.current()) != signature(current):
            self.checkpoint(None)
            self.pending = None
            return False
        self.backend.configure(current, dict(mode=before['mode'], scale=before['scale']), False)
        restored = self.backend.current()
        if hasattr(self.backend, 'validate_observed'):
            self.backend.validate_observed(current, dict(mode=before['mode'], scale=before['scale']), restored)
        if (restored['connector'], restored['mode'], restored['scale'], restored['layout_mode'], restored['underscanning']) != (
                before['connector'], before['mode'], before['scale'], before['layout_mode'], before['underscanning']):
            raise RuntimeError('Display rollback did not restore the previous mode')
        self.checkpoint(None)
        self.pending = None
        return True
