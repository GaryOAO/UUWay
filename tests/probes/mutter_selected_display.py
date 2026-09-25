"""Actual explicit-output DPI transaction in the owned two-output compositor."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))


def check(bus, compositor_pid):
    from gi.repository import Gio, GLib
    from native_display_config import SERVICE, PATH, DisplayTransaction
    from native_display_topology import SelectedMutterDisplay
    runtime = Path(os.environ['XDG_RUNTIME_DIR']).resolve()
    if runtime.parent.parent != Path('/tmp') or not runtime.parent.name.startswith('uurb-mutter-test-'):
        raise RuntimeError('Not an owned private display-test runtime')
    owner_pid = bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
        'org.freedesktop.DBus', 'GetConnectionUnixProcessID', GLib.Variant('(s)', (SERVICE,)),
        None, Gio.DBusCallFlags.NO_AUTO_START, 1000, None).unpack()[0]
    if owner_pid != compositor_pid:
        raise RuntimeError('Display service is not the private child compositor')
    initial = bus.call_sync(SERVICE, PATH, SERVICE, 'GetCurrentState', None, None,
                           Gio.DBusCallFlags.NO_AUTO_START, 2000, None).unpack()
    if len(initial[1]) != 2 or len(initial[2]) != 2:
        raise RuntimeError('Expected exactly two fixture outputs')
    # Selection is based on this fixture's actual layout, never used by the
    # production backend as a proxy for the identity of an authorized stream.
    selected = max(initial[2], key=lambda logical: logical[0])
    if len(selected[5]) != 1:
        raise RuntimeError('Unexpected mirrored fixture output')
    backend = SelectedMutterDisplay(selected[5][0][0])
    before = backend.current()
    mode = next(m for m in before['modes'] if m['id'] == before['mode'])
    if 2. not in mode['scales'] or before['scale'] != 1.:
        raise RuntimeError('Fixture must advertise an actual 1x to 2x DPI change: '
                           + str(dict(current=before['scale'], supported=mode['scales'])))
    transaction = DisplayTransaction(backend)
    try:
        applied = transaction.apply(transaction.prepare(before['serial'], mode['width'], mode['height'],
                                                        mode['refresh'], 2.))
        after = backend.current()
        if not applied['changed'] or after['scale'] != 2.:
            raise RuntimeError('Actual selected-output DPI change did not occur')
        if not transaction.rollback():
            raise RuntimeError('Actual selected-output rollback did not occur')
        restored = backend.current()
        if restored['topology'] != before['topology']:
            raise RuntimeError('Fixture topology was not restored exactly')
        return dict(private_compositor_pid_verified=True, outputs=2,
                    dpi_changed=True, other_output_preserved=True, rollback_restored=True,
                    desktop_input_sent=False, public_portal_tested=False,
                    resolution_switch_tested=False, uu_super_screen_tested=False)
    finally:
        if transaction.pending is not None and transaction.pending['observed'] is not None:
            transaction.rollback()  # Guarded snapshot only; never force a stale layout.
