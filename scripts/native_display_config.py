"""Single-output Mutter display transactions; no RDP or desktop restart.

Only compositor-advertised modes/scales are eligible. Temporary changes use
method 1, never persistent method 2. Exact serial/configuration guards prevent
rollback from overwriting a later local change. No monitor serial is exported.
"""
import math
import json
import time
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
MAX_REFRESH_HZ = 144


def _decode_modes(entries):
    if not 1 <= len(entries) <= 256:
        raise ValueError('Display mode count is outside the native bridge scope')
    modes = []
    current = None
    for identifier, width, height, refresh, preferred_scale, scales, flags in entries:
        supported = (isinstance(identifier, str) and 0 < len(identifier) <= 128 and
                     2 <= width <= 4096 and 2 <= height <= 4096 and not width % 2 and not height % 2 and
                     math.isfinite(refresh) and 1 <= refresh <= MAX_REFRESH_HZ and not flags.get('is-interlaced', False) and
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
    # A connected but disabled output has no current mode.  It is still part
    # of the monitor catalogue (and still has a mode list), but only a logical
    # monitor that references it requires a current mode.
    return modes, current


def _decode_topology(reply):
    serial, monitors, logical, properties = reply
    if not 1 <= len(monitors) <= 16 or not 1 <= len(logical) <= 16:
        raise ValueError('Display topology is outside the native bridge scope')
    physical = {}
    for specification, entries, monitor_properties in monitors:
        connector = specification[0]
        if (not isinstance(connector, str) or not connector or len(connector) > 128 or connector in physical):
            raise ValueError('Invalid or duplicate display output')
        modes, current = _decode_modes(entries)
        physical[connector] = dict(specification=tuple(specification), modes=modes, current=current,
                                   properties=monitor_properties)
    layouts, seen = [], set()
    for x, y, scale, transform, primary, outputs, _ in logical:
        if (type(x) is not int or type(y) is not int or type(transform) is not int or
                type(primary) is not bool or type(scale) not in (int, float) or
                not math.isfinite(scale) or not 0.5 <= scale <= 4 or transform):
            raise ValueError('Unsupported display layout')
        if len(outputs) != 1:
            raise ValueError('Mirrored display outputs are not supported')
        connector = outputs[0][0]
        entry = physical.get(connector)
        if (entry is None or entry['specification'] != tuple(outputs[0]) or connector in seen or
                entry['current'] is None):
            raise ValueError('Logical display output has no unique active mode')
        if not any(abs(scale - s) < 0.00001 for s in entry['current']['scales']):
            raise ValueError('Missing or incompatible current display mode')
        seen.add(connector)
        layouts.append(dict(connector=connector, x=int(x), y=int(y), scale=float(scale), transform=int(transform),
                            primary=bool(primary), mode=entry['current']['id'],
                            width=entry['current']['width'], height=entry['current']['height']))
    layout_mode = int(properties.get('layout-mode', 1))
    if layout_mode not in (1, 2):
        raise ValueError('Unknown compositor layout mode')
    return int(serial), physical, layouts, properties, layout_mode


def decode_state(reply):
    """Decode the origin output while keeping the whole active layout visible.

    The video/input bridge still serves one output.  That output is the
    logical monitor at the desktop origin (preferring the primary flag), so
    adding another monitor for a screencast/虚拟屏 no longer tears the bridge
    down.  The extra outputs remain in ``topology`` and make any mode
    transaction against the single-output guardian fail closed instead of
    overwriting the layout.
    """
    serial, physical, layouts, properties, layout_mode = _decode_topology(reply)
    candidates = [item for item in layouts if item['primary'] and not item['x'] and not item['y']]
    candidates += [item for item in layouts if not item['x'] and not item['y'] and item not in candidates]
    if not candidates:
        raise ValueError('Display layout has no primary origin output')
    selected = candidates[0]
    entry = physical[selected['connector']]
    topology = sorted((dict(connector=item['connector'], x=item['x'], y=item['y'], scale=item['scale'],
                            transform=item['transform'], primary=item['primary'], mode=item['mode'],
                            width=item['width'], height=item['height']) for item in layouts),
                      key=lambda item: (item['y'], item['x'], item['connector']))
    return dict(serial=serial, connector=selected['connector'], mode=selected['mode'],
                scale=selected['scale'], modes=entry['modes'], layout_mode=layout_mode,
                can_change_layout=bool(properties.get('supports-changing-layout-mode', False)),
                underscanning=bool(entry['properties'].get('is-underscanning', False)),
                supports_underscanning='is-underscanning' in entry['properties'],
                topology=topology, multi_output=len(layouts) != 1)


def decode_monitors(reply):
    """Describe every active output in the order Windows will enumerate it.

    The origin/primary output is first (``\\\\.\\DISPLAY1``), then the remaining
    outputs in a stable desktop order.  Only compositor-advertised modes are
    returned.  EDID serials and monitor device serial strings are never
    exported; the synthetic device path is stable for one boot/layout.
    """
    serial, physical, layouts, properties, layout_mode = _decode_topology(reply)
    origin = [item for item in layouts if item['primary'] and not item['x'] and not item['y']]
    origin += [item for item in layouts if not item['x'] and not item['y'] and item not in origin]
    if origin:
        ordered = [origin[0]] + sorted((item for item in layouts if item is not origin[0]),
                                       key=lambda item: (item['y'], item['x'], item['connector']))
    else:
        ordered = sorted(layouts, key=lambda item: (item['y'], item['x'], item['connector']))
    if len(ordered) > 8:
        raise ValueError('Active display count is outside the native bridge scope')
    result = []
    for index, item in enumerate(ordered):
        entry = physical[item['connector']]
        specification = entry['specification']
        friendly = entry['properties'].get('display-name')
        if not isinstance(friendly, str) or not friendly or len(friendly) > 128:
            friendly = specification[2] if isinstance(specification[2], str) and specification[2] else item['connector']
        modes = entry['modes']
        if len(modes) > 64:
            raise ValueError('Display mode list is outside the native bridge scope')
        result.append(dict(index=index, connector=item['connector'],
                           gdi_name='\\\\.\\DISPLAY%d' % (index + 1),
                           friendly=friendly[:128],
                           virtual=item['connector'].startswith('Meta-') or specification[1] == 'MetaVendor',
                           x=item['x'], y=item['y'], width=item['width'], height=item['height'],
                           refresh=entry['current']['refresh'], scale=item['scale'],
                           primary=item['primary'], mode=item['mode'],
                           device_path='\\\\?\\DISPLAY#UURB#%s#%d' % (item['connector'], index),
                           modes=[{key: mode[key] for key in ('width', 'height', 'refresh', 'scales')}
                                  for mode in modes]))
    return result


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

    def _reply(self):
        reply = self.bus.call_sync(SERVICE, PATH, SERVICE, 'GetCurrentState', None, None,
                                   Gio.DBusCallFlags.NONE, 2000, None)
        return reply.unpack()

    def current(self):
        return decode_state(self._reply())

    def monitors(self):
        return decode_monitors(self._reply())

    def configure(self, state, target, verify_only):
        # This backend can only describe the origin output.  Applying that
        # single-output configuration while another monitor is active would
        # remove the other monitor from the compositor layout.
        if state.get('multi_output'):
            raise ValueError('Display changes require the explicit-output topology backend')
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
        self.pending = dict(before=before, target=target, observed=None, confirmed=False,
                            requested_ns=time.monotonic_ns())
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
