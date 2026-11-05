"""Desktop selection and the bounded, read-only Hyprland experiment."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess


def backend_name(environment=None):
    environment = os.environ if environment is None else environment
    names = environment.get('XDG_CURRENT_DESKTOP', '').lower().split(':')
    return 'hyprland' if 'hyprland' in names else 'gnome'


def hyprland_state(monitors):
    """Only one unrotated output at the origin, at scale 1, is reviewed here."""
    if not isinstance(monitors, list) or len(monitors) != 1 or not isinstance(monitors[0], dict):
        raise ValueError('Hyprland 实验支持暂限一块活动显示器。')
    item = monitors[0]
    connector = item.get('name')
    width, height = item.get('width'), item.get('height')
    refresh, scale = item.get('refreshRate'), item.get('scale')
    if not isinstance(connector, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', connector):
        raise ValueError('Invalid Hyprland output name')
    if any(type(value) is not int or not 2 <= value <= 4096 or value % 2 for value in (width, height)):
        raise ValueError('Hyprland output dimensions are outside the bridge scope')
    if (type(refresh) not in (float, int) or not math.isfinite(refresh) or not 1 <= refresh <= 144
            or type(scale) not in (float, int) or not math.isfinite(scale) or scale != 1
            or item.get('x') != 0 or item.get('y') != 0 or item.get('transform') != 0
            or item.get('mirrorOf') not in (None, '', 'none')):
        raise ValueError('Hyprland 实验支持需要原点单屏、100% 缩放、无旋转，刷新率不超过 144 Hz。')
    mode = f'{width}x{height}@{refresh:.6f}'
    layout = dict(connector=connector, x=0, y=0, scale=1.0, transform=0,
                  primary=True, mode=mode, width=width, height=height)
    serial = int.from_bytes(hashlib.sha256(json.dumps(layout, sort_keys=True).encode()).digest()[:4], 'little')
    return dict(serial=serial, connector=connector, mode=mode, scale=1.0,
                modes=[dict(id=mode, width=width, height=height, refresh=float(refresh), scales=[1.0])],
                layout_mode=2, can_change_layout=False, underscanning=False,
                supports_underscanning=False, topology=[layout], multi_output=False,
                read_only=True)


class HyprlandDisplay:
    def __init__(self, connector=None):
        self.connector = connector

    def current(self):
        try:
            result = subprocess.run(['hyprctl', '-j', 'monitors'], capture_output=True, text=True,
                                    timeout=3, check=True)
            if len(result.stdout) > 65536:
                raise ValueError('Hyprland monitor response exceeds bounds')
            state = hyprland_state(json.loads(result.stdout))
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            raise RuntimeError('无法读取 Hyprland 显示器，请在 Hyprland 会话中运行。') from error
        if self.connector is not None and state['connector'] != self.connector:
            raise RuntimeError('Selected Hyprland output is no longer active')
        return state

    def identity(self):
        instance = os.environ.get('HYPRLAND_INSTANCE_SIGNATURE', '')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,256}', instance):
            raise RuntimeError('Hyprland instance identity is unavailable')
        return [Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'hyprland', instance]

    def monitors(self):
        state = self.current()
        mode = state['modes'][0]
        return [dict(index=0, connector=state['connector'], gdi_name=r'\\.\DISPLAY1',
                     friendly='Hyprland ' + state['connector'], virtual=False, x=0, y=0,
                     width=mode['width'], height=mode['height'], refresh=mode['refresh'], scale=1.0,
                     primary=True, mode=state['mode'], read_only=True,
                     device_path=r'\\?\DISPLAY#UURB#' + state['connector'] + '#0',
                     modes=[{key: mode[key] for key in ('width', 'height', 'refresh', 'scales')}])]

    def configure(self, state, target, verify_only):
        # Permit Windows to query/reapply its current mode; never issue a hyprctl write.
        current = self.current()
        if (current['serial'] != state['serial'] or target['mode'] != current['mode']
                or target['scale'] != current['scale']):
            raise ValueError('Hyprland 实验支持暂不允许远程修改显示模式。')


class HyprlandVirtualDisplays:
    """Expose the lack of Super Screen capability without invoking Mutter."""
    available = False
    screens = None
    recovery_path = None

    def __init__(self, backend):
        self.backend = backend
        self.screens = {}

    def status(self):
        outputs = [dict(connector=item['connector'], active=True, name=item['friendly'],
                        kind='physical', managed=False, slot=None,
                        width=item['width'], height=item['height'], refresh=item['refresh'])
                   for item in self.backend.monitors()]
        return dict(supported=False, exclusive=False, managed_connectors=[], outputs=outputs,
                    tracks_idle=False, cleanup_after_seconds=None, screens=[])

    def pump(self):
        pass

    def poll_lifetime(self, allow_idle=True):
        pass

    def close(self):
        pass

    def release_all(self):
        return dict(released=0)

    def session_state(self, pid, tracks):
        if type(tracks) is not int or not 0 <= tracks <= 8:
            raise ValueError('Invalid capture track count')
        return dict(tracks=tracks)

    @staticmethod
    def unavailable(*args, **kwargs):
        raise RuntimeError('Hyprland 实验支持暂不提供超级屏或显示布局修改。')

    ioctl = create = remove = set_output = unavailable


def hyprland_topology():
    state = HyprlandDisplay().current()
    mode = state['modes'][0]
    return dict(generation=state['serial'], connectors=[state['connector']],
                layout=[dict(x=0, y=0, scale=1.0, transform=0, outputs=1)],
                modes=[dict(width=mode['width'], height=mode['height'])])
