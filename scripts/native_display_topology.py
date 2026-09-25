"""Explicit-output Mutter transactions; no automatic screen selection or creation.

This candidate backend retains every other logical output when changing one
non-mirrored output. The installed single-output guardian is unchanged. Connector
selection must come from a separately established screen identity, never list
order or an assumed virtual-monitor name. EDID identity fields are not retained.
"""
import copy
import math
try:
    from gi.repository import Gio, GLib
except ImportError:  # Keep topology decoding usable on headless hosts.
    Gio = None
    GLib = None
from native_display_config import MutterDisplay, SERVICE, PATH, decode_state


class _PlanningVariant:
    """Unpackable stand-in for offline topology argument inspection."""
    def __init__(self, value):
        self.value = value

    def unpack(self):
        return self.value


def _variant(signature, value):
    if GLib is not None:
        return GLib.Variant(signature, value)
    # GLib unpack() recursively unwraps scalar dictionary values. Mirror that
    # shape so offline callers see the same argument tree as D-Bus callers.
    if signature in ('b', 'u'):
        return value
    return _PlanningVariant(value)


def _no_auto_start():
    return Gio.DBusCallFlags.NO_AUTO_START if Gio is not None else 0


def identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 128 and not any(ord(c) < 32 for c in value)


def decode_selected(reply, connector):
    if not identifier(connector):
        raise ValueError('An explicit bounded output connector is required')
    serial, monitors, logical, properties = reply
    if (type(serial) is not int or not 0 <= serial < 2**32 or
            not 1 <= len(monitors) <= 16 or not 1 <= len(logical) <= 16):
        raise ValueError('Invalid bounded display topology')
    physical = {}
    for specification, entries, monitor_properties in monitors:
        name = specification[0]
        if not identifier(name) or name in physical or not 1 <= len(entries) <= 256:
            raise ValueError('Invalid or duplicate output identity')
        current = [m for m in entries if m[-1].get('is-current')]
        if len(current) > 1 or (current and not identifier(current[0][0])):
            raise ValueError('Ambiguous current output mode')
        physical[name] = dict(specification=tuple(specification), entries=entries,
                              properties=monitor_properties,
                              mode=current[0][0] if current else None)
    layouts, seen = [], set()
    selected = None
    for x, y, scale, transform, primary, outputs, _ in logical:
        if (any(type(v) is not int or not -(2**31) <= v < 2**31 for v in (x, y)) or
                type(scale) not in (int, float) or not math.isfinite(scale) or not 0.5 <= scale <= 4 or
                type(transform) is not int or not 0 <= transform <= 7 or type(primary) is not bool or
                not 1 <= len(outputs) <= 16):
            raise ValueError('Invalid logical display geometry')
        members = []
        for specification in outputs:
            name = specification[0]
            entry = physical.get(name)
            if (entry is None or entry['specification'] != tuple(specification) or
                    name in seen or entry['mode'] is None):
                raise ValueError('Logical output has no unique active physical mode')
            seen.add(name)
            props = entry['properties']
            members.append(dict(connector=name, mode=entry['mode'],
                                underscanning=bool(props.get('is-underscanning', False)),
                                supports_underscanning='is-underscanning' in props))
            if name == connector:
                if len(outputs) != 1:
                    raise ValueError('Changing one member of a mirrored output is unsupported')
                # Reuse existing selected-mode/rate/DPI capability validation;
                # actual position, rotation and primary flag are retained below.
                selected = decode_state((serial,
                    [(specification, entry['entries'], props)],
                    [(0, 0, scale, 0, True, [specification], {})], properties))
        layouts.append(dict(x=x, y=y, scale=float(scale), transform=transform,
                            primary=primary, monitors=sorted(members, key=lambda m: m['connector'])))
    if selected is None or sum(item['primary'] for item in layouts) != 1:
        raise ValueError('Selected output is inactive/missing or primary is ambiguous')
    if any((entry['mode'] is not None) != (name in seen) for name, entry in physical.items()):
        raise ValueError('Active physical mode is inconsistent with logical layout')
    selected['topology'] = dict(connected=sorted(physical),
        logical=sorted(layouts, key=lambda item: tuple(m['connector'] for m in item['monitors'])),
        global_scale_required=bool(properties.get('global-scale-required', False)))
    return selected


def planned_topology(state, target):
    if (not identifier(target['mode']) or type(target['scale']) not in (int, float) or
            not math.isfinite(target['scale']) or not 0.5 <= target['scale'] <= 4):
        raise ValueError('Invalid selected output target')
    topology = copy.deepcopy(state['topology'])
    matches = 0
    for logical in topology['logical']:
        for monitor in logical['monitors']:
            if monitor['connector'] != state['connector']:
                continue
            if len(logical['monitors']) != 1:
                raise ValueError('Cannot mutate one mirrored output member')
            monitor['mode'] = target['mode']
            logical['scale'] = float(target['scale'])
            matches += 1
    if matches != 1:
        raise ValueError('Selected output identity is no longer unique')
    if topology['global_scale_required'] and any(
            abs(item['scale'] - target['scale']) > 0.00001 for item in topology['logical']):
        raise ValueError('Requested DPI requires changing other outputs')
    return topology


class SelectedMutterDisplay(MutterDisplay):
    def __init__(self, connector):
        if not identifier(connector):
            raise ValueError('Explicit output identity required')
        if Gio is None or GLib is None:
            raise RuntimeError('PyGObject (gi.repository Gio/GLib) is required for live Mutter display transactions')
        super().__init__()
        self.connector = connector
        self.owner = self.bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
            'org.freedesktop.DBus', 'GetNameOwner', GLib.Variant('(s)', (SERVICE,)), None,
            Gio.DBusCallFlags.NO_AUTO_START, 2000, None).unpack()[0]

    def identity(self):
        return [*super().identity(), 'selected-output-v1', self.connector]

    def current(self):
        reply = self.bus.call_sync(self.owner, PATH, SERVICE, 'GetCurrentState', None, None,
                                  Gio.DBusCallFlags.NO_AUTO_START, 2000, None)
        return decode_selected(reply.unpack(), self.connector)

    def configure(self, state, target, verify_only):
        if state['connector'] != self.connector:
            raise ValueError('Display transaction belongs to another output')
        topology = planned_topology(state, target)
        configuration = []
        for logical in topology['logical']:
            outputs = []
            for monitor in logical['monitors']:
                props = ({'enable_underscanning': _variant('b', monitor['underscanning'])}
                         if monitor['supports_underscanning'] else {})
                outputs.append((monitor['connector'], monitor['mode'], props))
            configuration.append((logical['x'], logical['y'], logical['scale'],
                                  logical['transform'], logical['primary'], outputs))
        properties = ({'layout-mode': _variant('u', state['layout_mode'])}
                      if state['can_change_layout'] else {})
        self.bus.call_sync(self.owner, PATH, SERVICE, 'ApplyMonitorsConfig',
            _variant('(uua(iiduba(ssa{sv}))a{sv})',
                         (state['serial'], 0 if verify_only else 1, configuration, properties)),
            None, _no_auto_start(), 2000, None)

    def validate_observed(self, before, target, observed):
        # Validate the very snapshot the transaction will journal, not an
        # earlier read followed by a second, potentially changed observation.
        if (observed['connector'] != self.connector or
                observed['topology'] != planned_topology(before, target) or
                observed['layout_mode'] != before['layout_mode']):
            raise RuntimeError('Compositor did not preserve the requested complete topology')
