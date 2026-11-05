#!/usr/bin/python3
"""Capture the sole Hyprland output through an explicitly approved portal."""
import argparse
import os
from pathlib import Path
import sys

from native_desktop import HyprlandDisplay


def command(args):
    state = HyprlandDisplay(args.connector).current()
    mode = state['modes'][0]
    result = ['/usr/bin/python3', str(Path(__file__).with_name('probe-wayland-portal.py')),
              '--gpu-relay-fd', str(args.gpu_relay_fd), '--duration-seconds', str(args.duration_seconds),
              '--capture-binary', str(args.capture_binary), '--cursor-mode', args.cursor_mode,
              '--cursor-backend', 'spa', '--expected-size', str(mode['width']), str(mode['height'])]
    if args.cursor_state_fd >= 3:
        result += ['--cursor-state-fd', str(args.cursor_state_fd)]
    if args.capture_status_fd >= 3:
        result += ['--capture-status-fd', str(args.capture_status_fd)]
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connector', required=True)
    parser.add_argument('--gpu-relay-fd', type=int, required=True)
    parser.add_argument('--duration-seconds', type=int, default=0)
    parser.add_argument('--capture-binary', type=Path, required=True)
    parser.add_argument('--cursor-mode', choices=['embedded', 'hidden', 'metadata', 'composited'], default='embedded')
    parser.add_argument('--cursor-state-fd', type=int, default=-1)
    parser.add_argument('--capture-status-fd', type=int, default=-1)
    try:
        argv = command(parser.parse_args())
        os.execv(argv[0], argv)
    except Exception as error:
        print(f'Hyprland capture: {error}', file=sys.stderr)
        raise SystemExit(1)
