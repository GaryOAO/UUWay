#!/usr/bin/python3
"""Inspect/verify the native display boundary without changing the desktop.

Transaction execution is not exposed until a supervised rollback guardian and
the actual UU request mapping are connected. Verification is Mutter method 0.
"""
import argparse
import json
from native_display_config import MutterDisplay, DisplayTransaction


def run(verify_current=False):
    backend = MutterDisplay()
    state = backend.current()
    current = next(m for m in state['modes'] if m['id'] == state['mode'])
    verified = False
    if verify_current:
        DisplayTransaction(backend).prepare(state['serial'], current['width'], current['height'], current['refresh'], state['scale'])
        verified = True
    # No connector, EDID/serial, mode ID, account value, or display content.
    print(json.dumps(dict(current=dict(width=current['width'], height=current['height'], refresh=current['refresh'], scale=state['scale']),
        modes=[{k: m[k] for k in ('width', 'height', 'refresh', 'scales')} for m in state['modes']],
        compositor_verified=verified, desktop_changed=False, uu_setting_connected=False)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-current', action='store_true')
    args = parser.parse_args()
    try:
        run(args.verify_current)
    except Exception as error:
        print(json.dumps(dict(event='native_display_check_failed', error_type=type(error).__name__)))
        raise SystemExit(1)
