#!/usr/bin/python3
"""uuway: set up, refresh, check and remove the UUWay runtime for the current user.

The .deb installs a read-only tree under /usr/lib/uuway.  Everything that has to belong
to a user lives here instead: the Wine prefix with UU, the user-owned runtime bundle, the
user services, the screen-share grants.  Each step knows whether it is already done, so
the command can be repeated, resumed, or limited to one step.

The proven tools are run as they are (package-uu-native-runtime.py,
install-uu-native-service.py, configure-uu-wine-mappings.py,
probe-wayland-portal.py), exactly as install.sh does; this module only sequences them.
"""
import argparse
import contextlib
import ctypes
import fcntl
import filecmp
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uuway_update  # noqa: E402
import uuway_update_apply as update_checks  # noqa: E402

PYTHON = '/usr/bin/python3'
WINE_DIR = Path('/opt/wine-stable/bin')
WINE_SERIES = 'wine-11.0'
WINE_PIN = Path('/etc/apt/preferences.d/uuway-wine')
SERVICES = ('uu-native-display', 'uu-native-text', 'uu-native-bridge')
GDM_CONFIGS = (Path('/etc/gdm3/custom.conf'), Path('/etc/gdm/custom.conf'))  # Debian/Ubuntu, Fedora/Arch
AUTOLOGIN_KEYS = ('AutomaticLoginEnable', 'AutomaticLogin')
TIMED_KEYS = ('TimedLoginEnable', 'TimedLogin', 'TimedLoginDelay')
RECOVERY_DELAY = 10  # seconds the login screen waits before GDM logs the account in again
GDM_TRUE = ('true', '1', 'yes')
UDEV_RULE = '70-uurb-native-input.rules'
SHIPPED_RULE = Path('/usr/lib/udev/rules.d') / UDEV_RULE
LEGACY_RULE = Path('/etc/udev/rules.d') / UDEV_RULE
BRIDGE_UNIT = 'uu-native-bridge.service'
CGROUP_FILE = Path('/proc/self/cgroup')
DEB_ROOT = Path('/usr/lib/uuway')  # where the package puts the tree; a checkout is somewhere else
UPDATE_STATE = Path('/var/lib/uuway/update')  # root's record of updates; world-readable
UPDATE_UNIT = 'uuway-update'
UPDATE_RESUME_UNIT = 'uuway-update-resume'  # the one-shot that finishes an update a reboot interrupted
UPDATE_STALE_DAYS = 14
SERVICE_MARKER = '# Managed by UURB native installer;'
DESKTOP_MARKER = '# Managed by UUWay console installer'
AMD_DESKTOP_MARKER = '# Managed by UUWay AMD\n'
FCITX_MARKER = '# Managed by uuway setup'
HELPERS = ('uu-clipboard-bridge', 'uu-clipboard-files.exe', 'uu-conpty.dll',
           'uu-terminal-bridge', 'uu-terminal-proxy.exe')
GPU_BACKENDS = ('nvidia', 'amd')
GPU_BACKEND_ENV = 'UURB_BACKEND'
FCITX_CONF = '''{marker}
[Addon]
Name=UU Bridge native text input
Name[zh_CN]=UU Bridge 原生文字提交
Comment=Bounded same-user text commits to the focused input context; no clipboard
Type=SharedLibrary
Library={library}
Category=Module
Version=1.0.1
OnDemand=False
Enabled=True

[Addon/Dependencies]
0=core:5.1.7
'''

_color = sys.stdout.isatty()


def _paint(code, text):
    return f'\x1b[{code}m{text}\x1b[0m' if _color else text


def info(text):
    print('   ' + text)


def ok(text):
    print('   ' + _paint('32', '✓') + ' ' + text)


def warn(text):
    print('   ' + _paint('33', '!') + ' ' + text)


class SetupError(Exception):
    """A step cannot continue; the message tells the user what to do."""


def in_uu_terminal():
    """True when this shell is UU's own remote terminal: its cgroup is the bridge service."""
    try:
        return BRIDGE_UNIT in CGROUP_FILE.read_text()
    except OSError:
        return False


def remote_session():
    """True when the shell came in through UU's own terminal or SSH.

    Such a shell inherits DISPLAY and WAYLAND_DISPLAY from the desktop, but nobody is
    sitting there: a polkit dialog would open on an empty screen and the command would
    wait for ever.  UU's terminal is recognised by its cgroup, the bridge service.
    """
    return bool(os.environ.get('SSH_CONNECTION') or os.environ.get('SSH_TTY')) or in_uu_terminal()


class Context:
    def __init__(self, args):
        self.args = args
        self.home = Path.home()
        self.dry_run = bool(getattr(args, 'dry_run', False))
        self.assume_yes = bool(getattr(args, 'yes', False))
        self.prefix = Path(getattr(args, 'prefix', None) or os.environ.get('UUWAY_PREFIX')
                           or self.home / '.local/share/wineprefixes/uu-remote')
        self.state_dir = self.home / '.local/state/uurb'
        self.config_dir = self.home / '.config/uurb'
        self.releases = self.home / '.local/share/uuway/releases'
        self.restart_bridge = False
        self.restart_display = False

    @property
    def app_dir(self):
        return self.prefix / 'drive_c/Program Files/Netease/GameViewer'

    @property
    def server_exe(self):
        return self.app_dir / 'bin/GameViewerServer.exe'

    @property
    def restore_state(self):
        return self.state_dir / 'portal-probe.json'

    @property
    def text_token(self):
        return self.state_dir / 'text-portal.json'

    @property
    def runtime_config(self):
        return self.config_dir / 'native-runtime.json'

    @property
    def unit_dir(self):
        return self.home / '.config/systemd/user'

    def wine_env(self, **extra):
        return dict(os.environ, WINEPREFIX=str(self.prefix), WINEDEBUG='-all', **extra)

    def run(self, argv, *, env=None, capture=False, check=True):
        """Show a command and run it; a dry run only shows it."""
        print('   ' + _paint('2', '$ ' + ' '.join(str(part) for part in argv)))
        if self.dry_run:
            return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')
        result = subprocess.run([str(part) for part in argv], env=env, check=False,
                                capture_output=capture, text=True)
        if check and result.returncode != 0:
            raise SetupError(f'命令失败（退出码 {result.returncode}）：{" ".join(str(p) for p in argv)}')
        return result

    def probe(self, argv, *, env=None):
        """Run a read-only query even in a dry run; never raises."""
        try:
            return subprocess.run([str(part) for part in argv], env=env, check=False,
                                  capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return subprocess.CompletedProcess(argv, 127, stdout='', stderr='')

    def confirm(self, question):
        if self.assume_yes or self.dry_run:
            return True
        answer = input(f'   {question} [Y/n] ')
        return not answer or answer.lower().startswith('y')

    def wait_for_user(self, what):
        if self.dry_run:
            info(f'（演练）此处会等待：{what}')
            return
        input(f'   {what}，完成后按回车继续…')

    def elevate(self, argv, *, terminal_first=False):
        """pkexec when someone at the local desktop can answer its dialog, sudo otherwise.

        sudo asks in the terminal the command runs in, which is where a remote user is.
        `terminal_first` is for commands that must not be left waiting for a dialog:
        whenever a person can type here, ask here.
        """
        graphical = os.environ.get('WAYLAND_DISPLAY') or os.environ.get('DISPLAY')
        use_dialog = shutil.which('pkexec') and graphical and not remote_session()
        if terminal_first and sys.stdin.isatty():
            use_dialog = False
        return (['pkexec'] if use_dialog else ['sudo']) + list(argv)

    def elevation_hint(self):
        """What the user should expect after saying yes, for confirmation prompts."""
        return '会弹出授权窗口' if self.elevate([])[0] == 'pkexec' else '会在终端里要求输入 sudo 密码'


def systemctl(ctx, *args):
    return ctx.probe(['systemctl', '--user', *args])


def unit_active(ctx, unit):
    return systemctl(ctx, 'is-active', '--quiet', unit + '.service').returncode == 0


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def write_atomic(path, data, mode):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.uuway-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def same_file(left, right):
    try:
        return filecmp.cmp(left, right, shallow=False)
    except OSError:
        return False


# --------------------------------------------------------------------- environment

def wine_version(ctx):
    result = ctx.probe([WINE_DIR / 'wine', '--version'])
    return result.stdout.strip() if result.returncode == 0 else None


def refuse_root():
    if os.geteuid() == 0 or os.environ.get('SUDO_USER'):
        raise SetupError('请以普通用户运行，不要使用 sudo 或 root；需要权限的步骤会自己请求。')


def check_environment(ctx):
    refuse_root()
    release = {}
    with contextlib.suppress(OSError):
        for line in Path('/etc/os-release').read_text().splitlines():
            key, _, value = line.partition('=')
            release[key] = value.strip('"')
    if release.get('ID') == 'ubuntu' and release.get('VERSION_ID') == '24.04':
        ok('Ubuntu 24.04')
    else:
        warn(f'UUWay 只在 Ubuntu 24.04 上验证过（当前：{release.get("PRETTY_NAME", "未知")}），继续但不保证可用。')
    session = os.environ.get('XDG_SESSION_TYPE', '')
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', '')
    if session != 'wayland' or 'GNOME' not in desktop:
        raise SetupError(f'请在 GNOME Wayland 会话里运行（当前会话：{session or "未知"} / {desktop or "未知"}）。')
    ok('GNOME Wayland 会话')
    backend = select_backend(ctx)
    ctx.backend = backend
    if backend == 'amd':
        ok('AMD VA-API 硬件编码（兼容模式，画面经过内存中转）')
    else:
        gpus = ctx.probe(['nvidia-smi', '-L'])
        ok('NVIDIA 显卡：' + (gpus.stdout.splitlines()[0].split(' (UUID')[0]
                              if gpus.stdout.splitlines() else '驱动可用'))


def amd_backend_enabled():
    marker = ROOT / 'build/encoder-backend'
    return marker.is_file() and marker.read_text().strip() == 'amd-vaapi'


def backend_root(backend):
    """Locate an immutable backend tree in a package or source checkout."""
    for root in (ROOT / 'build/backends' / backend, DEB_ROOT / 'build/backends' / backend):
        if root.is_dir():
            return root
    # Old source installs had one tree selected by build/encoder-backend.
    if backend == 'amd' and amd_backend_enabled():
        return ROOT / 'build'
    if backend == 'nvidia' and not amd_backend_enabled() and (ROOT / 'build').is_dir():
        return ROOT / 'build'
    return None


def backend_available(backend):
    root = backend_root(backend)
    if not root:
        return False
    if root == ROOT / 'build':
        return root.is_dir()  # legacy marker layout; probe supplies the capability check
    return (root / 'native-presenter').is_dir() and (root / 'dxvk-capture').is_dir()


def amd_backend_probe(ctx, backend='amd'):
    root = backend_root(backend) or ROOT / 'build'
    binary = root / 'native-presenter/uu-vaapi-probe'
    with tempfile.TemporaryDirectory(prefix='uuway-amd-check-') as directory:
        return ctx.probe([binary, directory])


def probe_backend(ctx, backend):
    if not backend_available(backend):
        return False, 'runtime is not installed'
    if backend == 'nvidia':
        result = ctx.probe(['nvidia-smi', '-L'])
        stderr = getattr(result, 'stderr', '')
        stderr = stderr if isinstance(stderr, str) else ''
        return result.returncode == 0, stderr[-500:] or 'NVIDIA driver unavailable'
    result = amd_backend_probe(ctx)
    stderr = getattr(result, 'stderr', '')
    stderr = stderr if isinstance(stderr, str) else ''
    return result.returncode == 0, stderr[-500:] or 'VA-API H.264/HEVC probe failed'


def configured_backend(ctx):
    path = getattr(ctx, 'runtime_config', None)
    if not isinstance(path, (str, os.PathLike, Path)):
        return None
    config = read_json(path)
    value = config.get('backend') if isinstance(config, dict) else None
    return value if value in GPU_BACKENDS else None


def select_backend(ctx):
    """Select NVIDIA first, then a verified AMD VA-API runtime.

    UURB_BACKEND is an intentional diagnostic override.  A saved service choice
    wins over changing hardware so a refresh cannot silently swap transports.
    """
    forced = os.environ.get(GPU_BACKEND_ENV, '').strip().lower()
    if forced:
        if forced not in GPU_BACKENDS:
            raise SetupError(f'{GPU_BACKEND_ENV} 只能是 nvidia 或 amd。')
        ok_backend, detail = probe_backend(ctx, forced)
        if not ok_backend:
            raise SetupError(f'强制选择 {forced} 后端失败：{detail}')
        return forced
    saved = configured_backend(ctx)
    if saved:
        ok_backend, detail = probe_backend(ctx, saved)
        if not ok_backend:
            raise SetupError(f'已保存的 {saved} 后端当前不可用：{detail}。如需切换请先删除配置或设置 {GPU_BACKEND_ENV}。')
        return saved
    for backend in GPU_BACKENDS:
        ok_backend, _ = probe_backend(ctx, backend)
        if ok_backend:
            return backend
    raise SetupError('没有检测到可用的 NVIDIA NVENC 或 AMD VA-API 后端；请安装对应显卡驱动后重试。UUWay 不会自动安装或切换显卡驱动。')


def backend_for_context(ctx):
    backend = getattr(ctx, 'backend', None) or configured_backend(ctx)
    if backend in GPU_BACKENDS:
        return backend
    # Keep old launcher/uninstall behavior and source checkouts working before
    # setup has run; setup itself always calls select_backend first.
    return 'amd' if amd_backend_enabled() else 'nvidia'


# --------------------------------------------------------------------- step: wine

def wine_done(ctx):
    version = wine_version(ctx)
    return bool(version) and version.startswith(WINE_SERIES) and WINE_PIN.is_file()


def wine_pin_command(ctx):
    """Keep apt on the 11.0 series: the GPU capture path is built against its server protocol."""
    return ['sudo', 'install', '-D', '-m', '0644', str(ROOT / 'config/uuway-wine.preferences'), str(WINE_PIN)]


def wine_run(ctx):
    version = wine_version(ctx)
    if version and version.startswith(WINE_SERIES):
        info('WineHQ 已是 11.0。还差把它固定在 11.0 系列，免得 apt upgrade 悄悄升到不兼容的版本。')
        if ctx.confirm(f'现在用 sudo 写入 {WINE_PIN}？'):
            ctx.run(wine_pin_command(ctx))
        else:
            warn('没有固定 WineHQ 版本；升级 WineHQ 之后 UUWay 可能无法采集画面。')
        return
    info('需要 WineHQ stable 11.0（装在 /opt/wine-stable）；UUWay 的 GPU 采集依赖这个版本。')
    if not ctx.confirm('现在用 sudo 添加 WineHQ 软件源并安装 winehq-stable 11.0？'):
        raise SetupError('缺少 WineHQ stable 11.0，已停止。')
    sudo = ['sudo']
    ctx.run(sudo + ['dpkg', '--add-architecture', 'i386'])
    if not list(Path('/etc/apt/sources.list.d').glob('winehq*')):
        ctx.run(sudo + ['install', '-d', '-m', '0755', '/etc/apt/keyrings'])
        ctx.run(sudo + ['curl', '-fsSLo', '/etc/apt/keyrings/winehq-archive.asc',
                        'https://dl.winehq.org/wine-builds/winehq.key'])
        ctx.run(sudo + ['curl', '-fsSLo', '/etc/apt/sources.list.d/winehq-noble.sources',
                        'https://dl.winehq.org/wine-builds/ubuntu/dists/noble/winehq-noble.sources'])
        ctx.run(sudo + ['sed', '-i', 's@winehq-archive.key@winehq-archive.asc@',
                        '/etc/apt/sources.list.d/winehq-noble.sources'])
    else:
        info('已有 WineHQ 软件源配置，保持不变。')
    ctx.run(wine_pin_command(ctx))
    ctx.run(sudo + ['apt-get', 'update'])
    series = '=11.0.*'
    ctx.run(sudo + ['apt-get', 'install', '-y', '--install-recommends',
                    'winehq-stable' + series, 'wine-stable' + series, 'wine-stable-amd64' + series,
                    'wine-stable-i386:i386' + series])
    if not ctx.dry_run and not (wine_version(ctx) or '').startswith(WINE_SERIES):
        raise SetupError(f'装好的 WineHQ 不是 11.0 版（{wine_version(ctx) or "未找到"}）。')


# --------------------------------------------------------------------- step: udev

def udev_rule_source():
    for candidate in (SHIPPED_RULE, ROOT / 'config' / UDEV_RULE):
        if candidate.is_file():
            return candidate
    raise SetupError('找不到 uinput udev 规则文件。')


def uinput_usable():
    return os.access('/dev/uinput', os.W_OK)


def udev_done(ctx):
    return (SHIPPED_RULE.is_file() or LEGACY_RULE.is_file()) and uinput_usable()


def udev_run(ctx):
    info('键鼠注入需要当前桌面用户能访问 /dev/uinput（只授予本地登录会话，不改用户组）。')
    if not (SHIPPED_RULE.is_file() or LEGACY_RULE.is_file()):
        if not ctx.confirm(f'现在安装 udev 规则？（{ctx.elevation_hint()}）'):
            raise SetupError('没有输入权限，已停止。')
        ctx.run(ctx.elevate(['install', '-m', '0644', str(udev_rule_source()), str(LEGACY_RULE)]))
        ctx.run(ctx.elevate(['udevadm', 'control', '--reload']))
    ctx.run(ctx.elevate(['udevadm', 'trigger', '--subsystem-match=misc', '--sysname-match=uinput',
                         '--action=add']))
    if not ctx.dry_run:
        time.sleep(1)
        if not uinput_usable():
            warn('规则已安装，但当前会话还不能访问 /dev/uinput；注销并重新登录一次后再运行 uuway setup。')


# --------------------------------------------------------------------- step: prefix (UU)

def signed_in(ctx):
    pattern = 'drive_c/users/*/AppData/Local/GameViewer/setting_*.ini'
    return any(not path.name.startswith('setting_guest_') for path in ctx.prefix.glob(pattern))


def powershell_native(ctx):
    try:
        registry = (ctx.prefix / 'user.reg').read_text(errors='replace')
    except OSError:
        return False
    section = re.search(r'^\[Software\\\\Wine\\\\DllOverrides\][^\n]*\n(.*?)(?=^\[|\Z)', registry, re.M | re.S)
    return bool(section and re.search(r'^"powershell\.exe"="native"', section.group(1), re.M))


def find_installer(ctx):
    if getattr(ctx.args, 'installer', None):
        return Path(ctx.args.installer).expanduser()
    names = ('UURemote*.exe', '*UU*emote*.exe')
    for directory in (ctx.home / 'Downloads', ctx.home / '下载'):
        for pattern in names:
            for candidate in sorted(directory.glob(pattern)):
                return candidate
    return None


def prefix_done(ctx):
    return ctx.server_exe.is_file() and signed_in(ctx) and powershell_native(ctx) \
        and not getattr(ctx.args, 'login', False)


def open_uu(ctx):
    """Open the login UI with the service bootstrap recent UU versions need."""
    wine = WINE_DIR / 'wine'
    executable = r'C:\Program Files\Netease\GameViewer\GameViewer.exe'
    env = ctx.wine_env()
    argv = [str(wine), executable]
    if backend_for_context(ctx) == 'amd':
        if unit_active(ctx, 'uu-native-bridge'):
            raise SetupError('UU 后台运行在独立显示中，无法在桌面打开它的窗口。需要重新登录时请运行 uuway setup --login。')
        stage = ctx.state_dir / 'login-support'
        if not ctx.dry_run:
            stage.mkdir(parents=True, exist_ok=True, mode=0o700)
            for source, target in [('uu-native-bootstrap.exe', 'bootstrap.exe'),
                                   ('uu-native-winlogon.exe', 'winlogon.exe')]:
                shutil.copy2((backend_root('amd') or ROOT / 'build') / 'native-presenter' / source,
                             stage / target)
        argv = [str(wine), str(stage / 'bootstrap.exe'), '--login', executable,
                'UURB-NATIVE-LOGIN-AMD']
        env['UURB_NATIVE_BOOTSTRAP'] = '1'
    if ctx.dry_run:
        ctx.run(argv, env=env)
        return
    ctx.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ctx.state_dir / 'login.log').open('a') as log:
        return subprocess.Popen(argv, env=env, stdout=log, stderr=log,
                                start_new_session=True)


def command_open(args):
    ctx = Context(args)
    if not ctx.server_exe.is_file():
        raise SetupError('UU 尚未安装，请先运行 uuway-amd setup。')
    open_uu(ctx)


def prefix_run(ctx):
    if unit_active(ctx, 'uu-native-bridge'):
        info('先停止正在运行的 UUWay 服务（之后会重新启动）。')
        ctx.run(['systemctl', '--user', 'stop', 'uu-native-bridge.service'])
    wine = WINE_DIR / 'wine'
    wineserver = WINE_DIR / 'wineserver'
    if ctx.server_exe.is_file():
        ok(f'UU 已安装在 {ctx.prefix}')
    else:
        installer = find_installer(ctx)
        if installer is None and not ctx.dry_run:
            info('请先从 UU 远程官网下载 Windows 版安装包（UUWay 不分发它）。')
            installer = Path(input('   安装包路径：').strip().replace('~', str(ctx.home), 1)).expanduser()
        installer = installer or ctx.home / 'Downloads/UURemote_Setup.exe'
        if not ctx.dry_run and not installer.is_file():
            raise SetupError(f'找不到安装包：{installer}')
        ok(f'安装包：{installer}')
        if not ctx.dry_run:
            ctx.prefix.parent.mkdir(parents=True, exist_ok=True)
        # Wine would ask to download Mono and Gecko while creating a fresh prefix; UU needs neither.
        ctx.run([WINE_DIR / 'wineboot', '-u'], env=ctx.wine_env(WINEDLLOVERRIDES='mscoree,mshtml='))
        if not ctx.dry_run:
            ctx.prefix.chmod(0o700)  # the service refuses a prefix other users can enter
        ctx.run([wine, installer, '/S'], env=ctx.wine_env())
        # Recent installers leave GameViewerService running after /S exits.
        # Stop only this owned prefix, then open the interactive login below.
        ctx.run([wineserver, '-k'], env=ctx.wine_env(), check=False)
        ctx.run([wineserver, '-w'], env=ctx.wine_env())
        if not ctx.dry_run and not ctx.server_exe.is_file():
            raise SetupError('安装包运行结束，但没有找到 GameViewerServer.exe。')
        ok('UU 已安装')
        ctx.args.login = True
    if getattr(ctx.args, 'login', False) or not signed_in(ctx):
        info('接下来会打开 UU 窗口：请登录你的 UU 账号，登录成功后从托盘图标完全退出 UU。')
        open_uu(ctx)
        ctx.wait_for_user('在 UU 里登录并完全退出')
        if not ctx.dry_run and not signed_in(ctx):
            warn('没有检测到登录信息；如果连接不上，请运行 uuway setup --login 重新登录。')
        ctx.run([wineserver, '-k'], env=ctx.wine_env(), check=False)
    # UU's terminal starts PowerShell; Wine must load UUWay's stand-in, not its placeholder.
    ctx.run([wine, 'reg', 'add', r'HKCU\Software\Wine\DllOverrides', '/v', 'powershell.exe',
             '/t', 'REG_SZ', '/d', 'native', '/f'], env=ctx.wine_env())
    ctx.run([wineserver, '-k'], env=ctx.wine_env(), check=False)
    ctx.run([wineserver, '-w'], env=ctx.wine_env())


# --------------------------------------------------------------------- step: helpers

def proxy_targets(ctx):
    """Files in the prefix that must follow a helper when the helper is updated."""
    app_bin = ctx.app_dir / 'bin'
    return {
        'uu-terminal-proxy.exe': [app_bin / 'powershell.exe',
                                  ctx.prefix / 'drive_c/windows/system32/WindowsPowerShell/v1.0/powershell.exe'],
        'uu-conpty.dll': [app_bin / 'conpty.dll'],
    }


def helpers_done(ctx):
    compat = ctx.prefix / 'compat'
    source = ROOT / 'build/helpers'
    if not all(same_file(source / name, compat / name) for name in HELPERS):
        return False
    # UU 4.42 ships no powershell.exe; the terminal stays off without ours beside it.
    return (ctx.app_dir / 'bin/powershell.exe').is_file()


def helpers_run(ctx):
    """Copy the helpers into <prefix>/compat and move along what depended on the old ones.

    The service enables the terminal only while bin/powershell.exe is byte-identical to
    compat/uu-terminal-proxy.exe, and it replaces other files only when they are Wine's
    placeholder or identical. So a file that still holds the previous proxy has to be
    upgraded here, and one that holds anything else is left alone.
    """
    source = ROOT / 'build/helpers'
    compat = ctx.prefix / 'compat'
    for name in HELPERS:
        if not (source / name).is_file():
            raise SetupError(f'安装包缺少 helper：{source / name}')
    ensure_backup(ctx)
    if ctx.dry_run:
        info(f'（演练）会把 {len(HELPERS)} 个 helper 复制到 {compat}，并同步终端代理。')
        return
    compat.mkdir(parents=True, exist_ok=True)
    kept = []
    for name in HELPERS:
        new = (source / name).read_bytes()
        old = (compat / name).read_bytes() if (compat / name).is_file() else None
        # Targets first: if this stops halfway, the next run still sees the old compat copy.
        for target in proxy_targets(ctx).get(name, []):
            if target.is_symlink() or not target.is_file():
                if target.name == 'powershell.exe' and target.parent.name == 'bin' and ctx.app_dir.is_dir():
                    write_atomic(target, new, 0o755)
                continue
            current = target.read_bytes()
            if current == new:
                continue
            if old is not None and current == old:
                write_atomic(target, new, 0o755)
            else:
                kept.append(str(target))
        write_atomic(compat / name, new, 0o755)
    ok(f'helper 已安装到 {compat}')
    for path in kept:
        warn(f'保留了不是 UUWay 旧版本的文件：{path}')


# --------------------------------------------------------------------- step: fcitx

def fcitx_conf_path(ctx):
    return ctx.home / '.local/share/fcitx5/addon/uurb-native-ime-v1.conf'


def fcitx_library():
    return ROOT / 'fcitx5/libuurb-native-ime.so'


def fcitx_done(ctx):
    return (not shutil.which('fcitx5') or not fcitx_library().is_file()
            or fcitx_conf_path(ctx).is_file())


def fcitx_run(ctx):
    conf = fcitx_conf_path(ctx)
    content = FCITX_CONF.format(marker=FCITX_MARKER, library=str(fcitx_library().with_suffix('')))
    if ctx.dry_run:
        info(f'（演练）会写入 {conf}')
        return
    write_atomic(conf, content.encode(), 0o644)
    ok(f'已启用 Fcitx5 输入法插件：{conf}')
    warn('Fcitx5 需要重启才会加载它：fcitx5 -r（手机输入法的中文提交依赖它）。')


# --------------------------------------------------------------------- step: portal

def portal_done(ctx):
    try:
        has_token = ctx.restore_state.stat().st_size > 0
    except OSError:
        has_token = False
    return has_token and not getattr(ctx.args, 'regrant_screen', False)


def portal_run(ctx):
    if not ctx.dry_run:
        ctx.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = None
    if getattr(ctx.args, 'regrant_screen', False) and ctx.restore_state.exists() and not ctx.dry_run:
        backup = ctx.restore_state.with_name(ctx.restore_state.name + f'.before-{int(time.time())}')
        ctx.restore_state.rename(backup)
    info('接下来会检查屏幕共享：请选择一块显示器，并勾选「记住」。')
    info('运行后，UU 可切换本次 GNOME 会话的所有活动显示器，包括之后创建的超级屏。')
    try:
        ctx.run([PYTHON, ROOT / 'scripts/probe-wayland-portal.py', '--capture-check',
                 '--restore-state', ctx.restore_state])
        if not ctx.dry_run and not (ctx.restore_state.is_file() and ctx.restore_state.stat().st_size):
            raise SetupError('没有保存屏幕共享授权，请重新运行并勾选「记住」。')
    except (SetupError, OSError):
        if backup is not None and not ctx.restore_state.exists():
            backup.rename(ctx.restore_state)  # keep the working grant when the new one failed
        raise
    ok('屏幕共享已授权')


# --------------------------------------------------------------------- step: runtime

def package_bundle(ctx):
    if not ctx.dry_run:
        ctx.releases.parent.mkdir(parents=True, exist_ok=True)
        ctx.releases.mkdir(mode=0o700, exist_ok=True)
    backend = backend_for_context(ctx)
    result = ctx.run([PYTHON, ROOT / 'scripts/package-uu-native-runtime.py', 'package', '--with-input',
                      '--backend', backend, ctx.releases], capture=True)
    if ctx.dry_run:
        return ctx.releases / '<sha256>'
    try:
        bundle = Path(json.loads(result.stdout)['bundle'])
    except (ValueError, KeyError):
        raise SetupError('打包没有产生运行时目录：' + result.stdout[-200:])
    if not bundle.is_dir():
        raise SetupError('打包没有产生运行时目录。')
    return bundle


def runtime_units_current(ctx):
    for name in SERVICES:
        try:
            text = (ctx.unit_dir / (name + '.service')).read_text()
        except OSError:
            return False
        if not text.startswith(SERVICE_MARKER):
            return False
        if name != 'uu-native-bridge' and str(ROOT) not in text:
            return False
    return True


def runtime_done(ctx):
    if ctx.dry_run:
        return False
    config = read_json(ctx.runtime_config)
    if not config or not runtime_units_current(ctx):
        return False
    return Path(config.get('bundle', '')) == package_bundle(ctx)


BACKUP_CONFIG = ('native-runtime.json', 'text-backend.json', 'input-settings.json')
RESTORE_NOTE = """To go back to the previous installation, from this directory:
  cp -a --remove-destination --no-preserve=mode home/. ~/
  systemctl --user daemon-reload && systemctl --user restart uu-native-bridge
Files are stored under home/ with the path they have relative to your home directory.
The old runtime directories are never deleted by uuway.
"""


def backup_candidates(ctx):
    """Every file a setup run can replace or remove in an existing installation."""
    app_bin = ctx.app_dir / 'bin'
    candidates = [ctx.unit_dir / (name + '.service') for name in SERVICES]
    candidates += [ctx.config_dir / name for name in BACKUP_CONFIG]
    if ctx.config_dir.is_dir():  # a rollback has to find the old installer's whole configuration as it was
        candidates += sorted(path for path in ctx.config_dir.rglob('*') if path not in candidates)
    candidates += [ctx.prefix / 'compat' / name for name in HELPERS]
    candidates += [app_bin / 'powershell.exe', app_bin / 'conpty.dll',
                   ctx.prefix / 'drive_c/windows/system32/WindowsPowerShell/v1.0/powershell.exe',
                   ctx.home / '.local/share/applications/uuway.desktop']
    found = []
    for path in candidates:
        try:
            path.relative_to(ctx.home)
        except ValueError:
            continue  # a prefix outside the home directory is not covered
        if path.is_file() and not path.is_symlink():
            found.append(path)
    return found


def ensure_backup(ctx):
    """Once per run, before the first change: keep what is needed to go back."""
    if getattr(ctx, 'backup_made', False) or not ctx.runtime_config.exists():
        return None
    ctx.backup_made = True
    present = backup_candidates(ctx)
    if ctx.dry_run:
        info(f'（演练）会先备份 {len(present)} 个现有的服务、配置和 helper 文件。')
        return None
    ctx.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkdtemp keeps two runs in the same second apart and creates the directory 0700.
    target = Path(tempfile.mkdtemp(prefix=time.strftime('backup-%Y%m%d-%H%M%S-'), dir=ctx.state_dir))
    for path in present:
        copy = target / 'home' / path.relative_to(ctx.home)
        copy.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(path, copy)
    (target / 'RESTORE.txt').write_text(RESTORE_NOTE)
    ok(f'已备份 {len(present)} 个现有文件：{target}（还原方法见其中的 RESTORE.txt）')
    return target


def runtime_run(ctx):
    ensure_backup(ctx)
    previous = read_json(ctx.runtime_config) or {}
    if not getattr(ctx, 'backend', None):
        ctx.backend = backend_for_context(ctx)
    bundle = package_bundle(ctx)
    new_manifest = read_json(bundle / 'manifest.json') or {}
    if (new_manifest.get('contract', {}).get('virtual_display_emulation') is True and
            str(bundle) != previous.get('bundle')):
        # Reload the independent daemon on every runtime upgrade, including
        # fixes within the same virtual-display ABI. Text keeps running.
        ctx.restart_display = True
    if unit_active(ctx, 'uu-native-bridge'):
        info('先停止 UUWay 桥接服务，写好配置后再启动。')
        ctx.run(['systemctl', '--user', 'stop', 'uu-native-bridge.service'])
    ok(f'运行时：{bundle}')
    command = [PYTHON, ROOT / 'scripts/install-uu-native-service.py', '--bundle', bundle,
               '--prefix', ctx.prefix, '--restore-state', ctx.restore_state,
               '--state-parent', ctx.state_dir, '--text-socket', ctx.state_dir / 'text.sock',
               '--backend', ctx.backend]
    if not ctx.runtime_config.exists():
        # A re-run must keep what the console saved, so these only go in on a fresh install.
        command += ['--cursor-mode', 'metadata', '--preserve-display-session']
    ctx.run(command)
    # Wine's Linux path view is applied while the bridge is stopped, and the short-lived
    # wineserver the registry update starts is reaped before the bridge's Wine starts.
    ctx.run([PYTHON, ROOT / 'scripts/configure-uu-wine-mappings.py', '--prefix', ctx.prefix,
             '--config-directory', ctx.config_dir])
    wineserver = WINE_DIR / 'wineserver'
    ctx.run([wineserver, '-k'], env=ctx.wine_env(), check=False)
    ctx.run([wineserver, '-w'], env=ctx.wine_env(), check=False)
    backend_file = ctx.config_dir / 'text-backend.json'
    if not backend_file.exists():
        pgrep = ctx.probe(['pgrep', '-u', str(os.getuid()), '-x', 'fcitx5'])
        backend = 'fcitx' if pgrep.returncode == 0 and fcitx_conf_path(ctx).is_file() else 'portal'
        if ctx.dry_run:
            info(f'（演练）会写入 {backend_file}：{backend}')
        else:
            write_atomic(backend_file, json.dumps(dict(version=1, backend=backend)).encode(), 0o600)
            ok(f'手机输入法文字通道：{backend}')
    ctx.run(['systemctl', '--user', 'daemon-reload'])
    ctx.restart_bridge = True


# --------------------------------------------------------------------- step: start

def start_done(ctx):
    return (not ctx.restart_bridge and not ctx.restart_display and
            not getattr(ctx.args, 'restart_display', False) and
            not getattr(ctx.args, 'restart_services', False) and
            all(unit_active(ctx, name) for name in SERVICES))


def restart_services_confirmed(ctx):
    """Restarting the text service can take a long-running GNOME Shell down with it; say so before doing it."""
    timed = timed_state(ctx)
    safe = bool(timed and timed[0])
    warn('重启文字服务时，它会向 GNOME 无障碍总线重新注册监听；桌面运行很久之后，这可能让 GNOME Shell 崩溃（1.1.0 实测发生过一次），'
         '整个桌面会话随之结束，你正在用的应用会被关掉。')
    info('已开启崩溃/注销后自动重新登录，崩溃后十几秒内桌面和 UU 会回来。' if safe else
         '没有开启崩溃/注销后自动重新登录：崩溃后 UU 会一直离线，直到有人登录（先运行 uuway autologin on）。')
    return ctx.confirm('仍要重启文字与显示服务吗？')


def start_run(ctx):
    ctx.restart_bridge |= bool(getattr(ctx.args, 'restart_services', False))
    restart_display = ctx.restart_display or getattr(ctx.args, 'restart_display', False)
    if restart_display:
        if unit_active(ctx, 'uu-native-bridge'):
            ctx.run(['systemctl', '--user', 'stop', 'uu-native-bridge.service'])
        info('加载多屏与超级屏显示服务；现有超级屏会移除。')
        ctx.run(['systemctl', '--user', 'restart', 'uu-native-display.service'])
        ctx.restart_bridge = True
    # Enabling with --now would boot UU here, then immediately kill it in
    # the restart below. Its Windows services can still be initializing.
    ctx.run(['systemctl', '--user', 'enable', *(name + '.service' for name in SERVICES)])
    if ctx.restart_bridge:
        if getattr(ctx.args, 'restart_services', False) and restart_services_confirmed(ctx):
            ctx.run(['systemctl', '--user', 'restart', 'uu-native-display.service', 'uu-native-text.service'])
        elif not ctx.dry_run:
            info('文字服务保持运行，保留剪贴板所有权；'
                 '空闲时可用 uuway refresh --restart-services 一并重启。')
        ctx.run(['systemctl', '--user', 'restart', 'uu-native-bridge.service'])
    else:
        ctx.run(['systemctl', '--user', 'start', *(name + '.service' for name in SERVICES)])
    if not ctx.dry_run:
        time.sleep(5)
        if not unit_active(ctx, 'uu-native-bridge'):
            raise SetupError('uu-native-bridge 没有启动，请查看：journalctl --user -u uu-native-bridge -e')
    ctx.restart_bridge = False
    ctx.restart_display = False
    ok('服务已启动')


# --------------------------------------------------------------------- step: text consent

def consent_done(ctx):
    try:
        return ctx.text_token.stat().st_size > 0
    except OSError:
        return False


def consent_run(ctx):
    info('文字服务还会再弹出一个授权对话框（远程输入与剪贴板），请点「允许」；约 2 分钟内有效。')
    if ctx.dry_run:
        return
    deadline = time.monotonic() + 130
    while time.monotonic() < deadline:
        if consent_done(ctx):
            ok('文字服务已授权')
            return
        time.sleep(2)
    warn('没有等到文字服务的授权；稍后运行 systemctl --user restart uu-native-text 会再次弹出。')


# --------------------------------------------------------------------- step: launchers

def amd_launcher_text():
    return (AMD_DESKTOP_MARKER + '[Desktop Entry]\nType=Application\nName=UU远程\n'
            f'Exec=/bin/bash "{ROOT / "scripts/uuway-amd.sh"}" open\n'
            'Icon=024B_GameViewer.0\nStartupWMClass=gameviewer.exe\nTerminal=false\n')


def amd_launcher_targets(ctx):
    yield ctx.home / '.local/share/applications/uuway-amd.desktop'
    for path in (ctx.home / 'Desktop/UU远程.desktop',
                 ctx.home / '.local/share/applications/wine/Programs/UU远程.desktop'):
        if not path.is_file() or path.is_symlink():
            continue
        text = path.read_text()
        if text.startswith(AMD_DESKTOP_MARKER) or (
                'StartupWMClass=gameviewer.exe\n' in text and
                f'Path={ctx.app_dir}\n' in text):
            yield path


def remove_amd_launchers(ctx):
    # Also works when uninstall is invoked through the installed NVIDIA CLI.
    # Only restore/remove our marked regular files; never follow a backup link.
    paths = (ctx.home / '.local/share/applications/uuway-amd.desktop',
             ctx.home / 'Desktop/UU远程.desktop',
             ctx.home / '.local/share/applications/wine/Programs/UU远程.desktop')
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file() or not path.read_text().startswith(AMD_DESKTOP_MARKER):
                continue
            backup = path.with_suffix('.desktop.before-amd')
            if backup.is_symlink() or (backup.exists() and not backup.is_file()):
                warn(f'备份不是普通文件，保留入口：{path}')
                continue
            info(f'恢复快捷方式 {path}' if backup.is_file() else f'删除 {path}')
            if not ctx.dry_run:
                if backup.is_file():
                    os.replace(backup, path)
                else:
                    path.unlink()
        except OSError as error:
            warn(f'{path}：{error}')


def launchers_done(ctx):
    if backend_for_context(ctx) == 'amd':
        return all(path.is_file() and path.read_text() == amd_launcher_text()
                   for path in amd_launcher_targets(ctx))
    desktop = ctx.home / '.local/share/applications/uuway.desktop'
    try:
        return not desktop.read_text().startswith(DESKTOP_MARKER)
    except OSError:
        return True


def launchers_run(ctx):
    if backend_for_context(ctx) == 'amd':
        for path in amd_launcher_targets(ctx):
            if not ctx.dry_run:
                if path.is_file() and not path.read_text().startswith(AMD_DESKTOP_MARKER):
                    backup = path.with_suffix('.desktop.before-amd')
                    if not backup.exists():
                        shutil.copy2(path, backup)
                write_atomic(path, amd_launcher_text().encode(), 0o755)
        ok('UU 桌面与菜单入口已配置为 AMD 版启动器')
        return
    desktop = ctx.home / '.local/share/applications/uuway.desktop'
    with contextlib.suppress(OSError):
        if desktop.read_text().startswith(DESKTOP_MARKER):
            ensure_backup(ctx)
            # Same desktop id as the packaged entry, which would otherwise stay shadowed.
            info(f'移除旧的用户级菜单入口，改用系统自带的：{desktop}')
            if not ctx.dry_run:
                desktop.unlink()
    try:
        import native_launcher_migration
        result = native_launcher_migration.retire_legacy_launchers(ctx.home) if not ctx.dry_run else {}
    except ValueError as error:
        warn(f'旧入口没有处理：{error}')
        return
    if result.get('changed'):
        info('旧 UU 入口已停用并备份：' + str(result.get('backup')))
        ctx.run(['systemctl', '--user', 'daemon-reload'])


# --------------------------------------------------------------------- step: autologin

AUTOLOGIN_NOTE = """   UU 跟着你的桌面会话上线：重启、断电恢复、桌面崩溃或注销之后，没人在屏幕前登录，UU 就是离线的。
   开启后有两层：开机时 GDM 自动登录 {user}；桌面崩溃或手动注销后，登录界面停留约 {delay} 秒也会自动重新登录，UU 随之回来。
   代价：任何能接触这台机器的人，开机就能进入你的桌面；想在登录界面停留，要先 uuway autologin off。
   建议同时开启磁盘加密或固件密码，并设置自动锁屏。另外 GNOME 钥匙环不会自动解锁，个别应用第一次用到它时会询问密码。
   UU 看不到 GDM 登录界面：它属于另一个系统会话，UUWay 的采集和输入都只在你的桌面会话里。"""


def gdm_config():
    return next((path for path in GDM_CONFIGS if path.is_file()), None)


def gdm_values(text, keys):
    """The active [daemon] values for keys; a later line wins, as in GLib's key file parser."""
    values, section = {}, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
        elif section == 'daemon' and line[:1] not in ('#', ';'):
            key, _, value = line.partition('=')
            if key.strip() in keys:
                values[key.strip()] = value.strip()
    return values


def gdm_autologin(text):
    """(enabled, user) of the login GDM does when it starts."""
    values = gdm_values(text, AUTOLOGIN_KEYS)
    return values.get('AutomaticLoginEnable', '').lower() in GDM_TRUE, values.get('AutomaticLogin') or None


def gdm_timed_login(text):
    """(enabled, user) of the login GDM does after the login screen has been idle for a while."""
    values = gdm_values(text, TIMED_KEYS)
    return values.get('TimedLoginEnable', '').lower() in GDM_TRUE, values.get('TimedLogin') or None


def gdm_replace(text, keys, lines):
    """text without the active [daemon] lines for keys, then lines (if any) right under [daemon]; the rest is kept."""
    kept, section = [], None
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            section = stripped[1:-1]
        elif section == 'daemon' and stripped[:1] not in ('#', ';') and stripped.partition('=')[0].strip() in keys:
            continue
        kept.append(line)
    if not lines:
        return ''.join(kept)
    for index, line in enumerate(kept):
        if line.strip() == '[daemon]':
            kept[index] = line if line.endswith('\n') else line + '\n'
            kept.insert(index + 1, lines)
            return ''.join(kept)
    separator = '' if not kept or kept[-1].endswith('\n') else '\n'
    return ''.join(kept) + separator + '\n[daemon]\n' + lines


def gdm_set_autologin(text, user, enable):
    return gdm_replace(text, AUTOLOGIN_KEYS, f'AutomaticLoginEnable=true\nAutomaticLogin={user}\n' if enable else '')


def gdm_set_timed_login(text, user, enable):
    return gdm_replace(text, TIMED_KEYS,
                       f'TimedLoginEnable=true\nTimedLogin={user}\nTimedLoginDelay={RECOVERY_DELAY}\n' if enable else '')


def autologin_state(ctx):
    """(config path, enabled, user), or None when this machine has no GDM config to edit."""
    path = gdm_config()
    if path is None:
        return None
    try:
        return (path, *gdm_autologin(path.read_text()))
    except OSError:
        return None


def timed_state(ctx):
    """(enabled, user) of the idle-login-screen login, or None without a GDM config."""
    path = gdm_config()
    try:
        return gdm_timed_login(path.read_text()) if path else None
    except OSError:
        return None


def autologin_declined(ctx):
    return (ctx.state_dir / 'autologin-declined').exists()


def autologin_done(ctx):
    state = autologin_state(ctx)
    if state is None or autologin_declined(ctx):
        return True
    _, enabled, current = state
    if enabled and current != getpass.getuser():
        return True  # another account's setup is never ours to complete
    return enabled and timed_state(ctx)[0]


def autologin_apply(ctx, path, text, note):
    """Write text over the GDM config as root: a backup next to it first, then install."""
    stage = ctx.state_dir / 'gdm-custom.conf.new'
    backup = path.with_name(path.name + time.strftime('.before-uuway-%Y%m%d-%H%M%S'))
    if not ctx.dry_run:
        write_atomic(stage, text.encode(), 0o600)
    try:
        ctx.run(ctx.elevate(['cp', '-p', str(path), str(backup)]))
        ctx.run(ctx.elevate(['install', '-m', '0644', '-o', 'root', '-g', 'root', str(stage), str(path)]))
    finally:
        if not ctx.dry_run:
            stage.unlink(missing_ok=True)
    ok(note + f'（原文件备份为 {backup}）')
    return backup


def autologin_run(ctx):
    state = autologin_state(ctx)
    if state is None:
        info('没有找到 GDM 配置（/etc/gdm3/custom.conf），不是 GNOME 登录管理器 GDM，跳过。')
        return
    path, enabled, current = state
    user = getpass.getuser()
    if enabled and current != user:
        warn(f'GDM 已把自动登录开给了 {current}，UUWay 不会改动它；要给 {user} 开启，请自行编辑 {path}。')
        return
    timed_enabled, timed_user = gdm_timed_login(path.read_text())
    need_boot = not enabled
    need_recovery = not timed_enabled
    if timed_enabled and timed_user != user:
        warn(f'GDM 已把登录界面的自动重新登录开给了 {timed_user}，UUWay 不会改动它。')
        need_recovery = False
    if not need_boot and not need_recovery:
        ok(f'已经为 {user} 开启了开机自动登录和崩溃/注销后的自动重新登录')
        return
    what = '开机自动登录和崩溃/注销后自动重新登录' if need_boot and need_recovery else (
        '开机自动登录' if need_boot else '崩溃/注销后自动重新登录')
    explicit = 'autologin' in parse_steps(getattr(ctx.args, 'only', None))
    print(AUTOLOGIN_NOTE.format(user=user, delay=RECOVERY_DELAY))
    if not explicit:
        # A security setting: --yes and a non-interactive run never turn it on behind the user's back.
        if ctx.assume_yes or ctx.dry_run or not sys.stdin.isatty():
            info('这一步需要你亲自决定，已跳过；需要时运行：uuway autologin on')
            return
        if not input(f'   开启{what}？[y/N] ').lower().startswith('y'):
            ctx.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            (ctx.state_dir / 'autologin-declined').touch(mode=0o600)
            info('不开启。以后需要：uuway autologin on')
            return
    elif not ctx.confirm(f'为 {user} 开启{what}？（{ctx.elevation_hint()}）'):
        raise SetupError('没有开启自动登录。')
    text = path.read_text()
    if need_boot:
        text = gdm_set_autologin(text, user, True)
    if need_recovery:
        text = gdm_set_timed_login(text, user, True)
    backup = autologin_apply(ctx, path, text, f'已为 {user} 开启{what}')
    if not ctx.dry_run:
        (ctx.state_dir / 'autologin-declined').unlink(missing_ok=True)
        before = read_json(ctx.state_dir / 'autologin.json') or {}
        # Remember which parts uuway itself set, so that `off` never reverts what the owner had set up.
        write_atomic(ctx.state_dir / 'autologin.json', json.dumps(dict(
            version=2, user=user, config=str(path), backup=str(backup),
            boot=bool(before.get('boot', before.get('user') is not None)) or need_boot,
            recovery=bool(before.get('recovery')) or need_recovery)).encode(), 0o600)


def autologin_off(ctx):
    state = autologin_state(ctx)
    if state is None:
        info('没有找到 GDM 配置，没有可关闭的自动登录。')
        return
    path, enabled, current = state
    timed_enabled, timed_user = gdm_timed_login(path.read_text())
    recorded = read_json(ctx.state_dir / 'autologin.json')
    if not enabled and not timed_enabled:
        info('开机自动登录和崩溃/注销后的自动重新登录本来就没有开启。')
    else:
        owner = recorded.get('user') if recorded else None
        # A 1.1.0 record has no "boot"/"recovery": it only ever set the boot login.
        undo_boot = bool(recorded) and recorded.get('boot', True) and enabled and current == owner
        undo_recovery = bool(recorded) and recorded.get('recovery', False) and timed_enabled and timed_user == owner
        if not undo_boot and not undo_recovery:
            warn(f'这些自动登录设置不是 UUWay 开启的，没有改动。要关闭，请编辑 {path}，'
                 '把 AutomaticLoginEnable / TimedLoginEnable 改为 false。')
            return
        text = path.read_text()
        if undo_boot:
            text = gdm_set_autologin(text, current, False)
        if undo_recovery:
            text = gdm_set_timed_login(text, timed_user, False)
        autologin_apply(ctx, path, text, '已关闭 UUWay 开启的' + '和'.join(
            name for name, flag in (('开机自动登录', undo_boot), ('崩溃/注销后自动重新登录', undo_recovery)) if flag))
    if not ctx.dry_run:
        (ctx.state_dir / 'autologin.json').unlink(missing_ok=True)


def command_autologin(args):
    ctx = Context(args)
    if getattr(args, 'action') == 'status':
        state = autologin_state(ctx)
        if state is None:
            print('没有找到 GDM 配置，不适用。')
        else:
            path, enabled, user = state
            timed_enabled, timed_user = timed_state(ctx)
            print(f'开机自动登录：已为 {user} 开启（{path}）' if enabled else f'开机自动登录：未开启（{path}）')
            print(f'崩溃/注销后自动重新登录：已为 {timed_user} 开启（登录界面约 {RECOVERY_DELAY} 秒后）'
                  if timed_enabled else '崩溃/注销后自动重新登录：未开启（桌面崩溃或注销后会停在登录界面，UU 离线）')
        return 0
    if args.action == 'on':
        args.only = 'autologin'
        autologin_run(ctx)
    else:
        autologin_off(ctx)
    return 0


STEPS = (
    ('wine', 'WineHQ stable 11.0', wine_done, wine_run),
    ('udev', '键鼠注入权限（uinput）', udev_done, udev_run),
    ('prefix', '安装并登录 UU 远程', prefix_done, prefix_run),
    ('helpers', '安装辅助程序', helpers_done, helpers_run),
    ('fcitx', 'Fcitx5 输入法插件', fcitx_done, fcitx_run),
    ('portal', '授权屏幕共享', portal_done, portal_run),
    ('runtime', '打包运行时并写入服务配置', runtime_done, runtime_run),
    ('start', '启动服务', start_done, start_run),
    ('consent', '文字服务授权', consent_done, consent_run),
    ('launchers', '清理旧入口', launchers_done, launchers_run),
    ('autologin', '开机自动登录（远程使用推荐，可选）', autologin_done, autologin_run),
)
STEP_NAMES = tuple(name for name, *_ in STEPS)
REFRESH_STEPS = ('helpers', 'runtime', 'start')


def parse_steps(text):
    names = [part.strip() for part in text.split(',') if part.strip()] if text else []
    unknown = [name for name in names if name not in STEP_NAMES]
    if unknown:
        raise SetupError(f'未知步骤：{", ".join(unknown)}（可用：{", ".join(STEP_NAMES)}）')
    return set(names)


@contextlib.contextmanager
def setup_lock(ctx):
    if ctx.dry_run:
        yield
        return
    ctx.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (ctx.state_dir / 'setup.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SetupError('另一个 uuway setup 正在运行。')
        yield


def run_steps(ctx, only, force):
    total = len(STEPS)
    for number, (name, title, done, run) in enumerate(STEPS, 1):
        if only and name not in only:
            continue
        print(f'\n{_paint("1;36", f"━━ [{number}/{total}] {title}")}')
        if name not in force and done(ctx):
            ok('已完成，跳过')
            continue
        try:
            run(ctx)
        except SetupError as error:
            raise SetupError(f'{error}\n   修复后可只重做这一步：uuway setup --only {name}')


def command_setup(args):
    ctx = Context(args)
    only = parse_steps(args.only)
    if args.system:
        only |= {'wine', 'udev'}
    print(_paint('1', 'UUWay 安装向导') + '（可以反复运行：已完成的步骤会跳过）')
    check_environment(ctx)
    with setup_lock(ctx):
        run_steps(ctx, only, parse_steps(args.force))
    print('\n' + _paint('1;32', '🎉 完成！') + ' 打开手机上的 UU 远程，设备列表里会出现这台电脑。'
          '\n   设置请打开应用菜单里的「UUWay 控制台」；排错用：uuway doctor')
    return 0


def command_refresh(args):
    ctx = Context(args)
    args.only = None
    args.force = None
    args.system = False
    check_environment(ctx)
    if unit_active(ctx, 'uu-native-bridge') and not ctx.confirm('刷新会重启桥接服务，正在进行的远程会话会断开。继续吗？'):
        return 1
    with setup_lock(ctx):
        steps = set(REFRESH_STEPS)
        if ROOT.resolve() == DEB_ROOT:
            # Remove a source install's immutable per-user menu entry, which
            # otherwise shadows the console delivered by each package update.
            steps.add('launchers')
        run_steps(ctx, steps, set())
    return 0


# --------------------------------------------------------------------- doctor

def command_doctor(args):
    ctx = Context(args)
    results = []

    def check(level, name, detail=''):
        results.append(dict(level=level, name=name, detail=detail))

    session = os.environ.get('XDG_SESSION_TYPE', '')
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', '')
    check('ok' if session == 'wayland' and 'GNOME' in desktop else 'fail', 'GNOME Wayland 会话',
          f'{session or "?"} / {desktop or "?"}')
    try:
        backend = select_backend(ctx)
    except SetupError as error:
        backend = configured_backend(ctx) or ('amd' if amd_backend_enabled() else 'nvidia')
        check('fail', 'GPU 后端自动选择', str(error))
    else:
        check('ok', 'GPU 后端自动选择', f'已选择 {backend}（NVIDIA 优先，AMD VA-API 作为后备）')
    if backend == 'amd':
        result = amd_backend_probe(ctx)
        check('ok' if result.returncode == 0 else 'fail', 'AMD VA-API 硬件编码',
              'H.264/HEVC 编码验证通过；内存中转模式' if result.returncode == 0 else result.stderr[-500:])
    else:
        gpus = ctx.probe(['nvidia-smi', '-L'])
        check('ok' if gpus.returncode == 0 else 'fail', 'NVIDIA 驱动',
              gpus.stdout.splitlines()[0] if gpus.returncode == 0 else '没有找到可用的 NVIDIA 驱动')
        for library in ('libcuda.so.1', 'libnvidia-encode.so.1'):
            try:
                ctypes.CDLL(library)
                check('ok', library)
            except OSError:
                check('fail', library, '驱动没有提供它（NVENC 需要）')
        with contextlib.suppress(ImportError):
            import native_runtime_state
            mismatch = native_runtime_state.nvidia_driver_mismatch()
            if mismatch:
                check('fail', 'NVIDIA 内核模块与用户态库版本不一致',
                      f'{mismatch["kernel_module"]} ≠ {mismatch["userspace"]}，请重启')
    version = wine_version(ctx)
    check('ok' if version and version.startswith(WINE_SERIES) else 'fail', 'WineHQ stable 11.0',
          version or '没有找到 /opt/wine-stable/bin/wine（运行 uuway setup）')
    if version and version.startswith(WINE_SERIES):
        check('ok' if WINE_PIN.is_file() else 'warn', 'WineHQ 已固定在 11.0 系列',
              str(WINE_PIN) if WINE_PIN.is_file() else '升级 WineHQ 可能让 UUWay 无法采集画面；运行 uuway setup --only wine')
    check('ok' if uinput_usable() else 'fail', '/dev/uinput 可写', '' if uinput_usable()
          else '运行 uuway setup --only udev；装好规则后可能需要重新登录')
    for unit in ('gnome-remote-desktop', 'uu-remote-bridge'):
        if unit_active(ctx, unit):
            check('fail', f'{unit} 正在运行', '它与 UUWay 冲突，桥接服务会拒绝启动')
    prefix_ok = ctx.prefix.is_dir() and (ctx.prefix.stat().st_mode & 0o077) == 0
    check('ok' if prefix_ok else 'fail', 'Wine prefix 存在且仅本人可访问', str(ctx.prefix))
    check('ok' if ctx.server_exe.is_file() else 'fail', 'UU 已安装')
    check('ok' if signed_in(ctx) else 'warn', 'UU 已登录')
    check('ok' if powershell_native(ctx) else 'warn', '终端：powershell.exe 覆盖为 native')
    check('ok' if helpers_done(ctx) else 'warn', '辅助程序与终端代理已同步')
    token = ctx.restore_state
    check('ok' if token.is_file() and token.stat().st_size else 'warn', '屏幕共享授权', str(token))
    check('ok' if consent_done(ctx) else 'warn', '文字服务授权', str(ctx.text_token))
    config = read_json(ctx.runtime_config)
    check('ok' if config else 'fail', '服务配置', str(ctx.runtime_config))
    if config:
        bundle = Path(config.get('bundle', ''))
        check('ok' if bundle.is_dir() else 'fail', '运行时目录存在', str(bundle))
        check('ok' if runtime_units_current(ctx) else 'warn', '服务单元由 UUWay 管理且指向当前安装')
    for name in SERVICES:
        check('ok' if unit_active(ctx, name) else 'fail', f'{name} 运行中')
        for dropin in sorted((ctx.unit_dir / (name + '.service.d')).glob('*.conf')):
            check('warn', f'{name} 有 drop-in 覆盖', str(dropin))
    login = autologin_state(ctx)
    if login:
        path, enabled, user = login
        me = getpass.getuser()
        timed_enabled, timed_user = timed_state(ctx)
        recovers = timed_enabled and timed_user == me
        if enabled and user == me and recovers:
            check('ok', '开机自动登录', f'已为 {me} 开启；桌面崩溃或注销后登录界面约 {RECOVERY_DELAY} 秒会自动重新登录')
        elif autologin_declined(ctx):
            check('ok', '开机自动登录', '未完全开启（你选择了不开启）：重启或桌面崩溃后需要有人登录，UU 才会上线')
        elif enabled and user == me:
            check('warn', '崩溃/注销后自动重新登录', '开机自动登录已开，但桌面崩溃或手动注销后会停在登录界面，UU 不会自动回来；开启：uuway autologin on')
        elif enabled:
            check('warn', '开机自动登录开给了另一个账号', f'{user}：重启后 {me} 的桌面不会自动出现，UU 不会上线')
        else:
            check('warn', '开机自动登录', '未开启：重启或断电恢复后需要有人在屏幕前登录，UU 才会上线；开启：uuway autologin on')
    if shutil.which('fcitx5') and fcitx_library().is_file():
        check('ok' if fcitx_conf_path(ctx).is_file() else 'warn', 'Fcitx5 输入法插件',
              str(fcitx_conf_path(ctx)))
    for level, name, detail in update_notices(ctx):
        check(level, name, detail)
    failed = [item for item in results if item['level'] == 'fail']
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        marks = dict(ok=_paint('32', '✓'), warn=_paint('33', '!'), fail=_paint('31', '✗'))
        for item in results:
            print(f'{marks[item["level"]]} {item["name"]}' + (f'  {item["detail"]}' if item['detail'] else ''))
        print(f'\n{len(failed)} 项失败，{sum(i["level"] == "warn" for i in results)} 项警告')
    return 1 if failed else 0


# --------------------------------------------------------------------- update

def new_fetcher():
    return uuway_update.Fetcher()


def deb_version(ctx):
    """The installed package's version, or None when this is not a package install."""
    if ROOT.resolve() != DEB_ROOT:
        return None
    result = ctx.probe(['dpkg-query', '-W', '-f=${Version}', 'uuway'])
    return result.stdout.strip() or None if result.returncode == 0 else None


def is_newer(ctx, new, old):
    try:
        return update_checks.version_is_newer(new, old, run=lambda argv, **kwargs: ctx.probe(argv))
    except update_checks.VerifyError as error:
        raise SetupError(str(error))


def update_cache(ctx):
    """What the last `uuway update` learned, if it is well formed."""
    cache = read_json(ctx.state_dir / 'update.json')
    if (isinstance(cache, dict) and isinstance(cache.get('tag'), str) and update_checks.TAG.fullmatch(cache['tag'])
            and cache.get('version') == cache['tag'][1:]):
        return cache
    return {}


def latest_known(ctx):
    """The newest published release; the answer is cached so that `doctor` can mention it offline."""
    cache = update_cache(ctx)
    try:
        latest = uuway_update.latest_release(new_fetcher(), etag=cache.get('etag'))
        if latest is None:  # nothing new since the cached answer
            latest = uuway_update.Latest(cache['tag'], cache['version'], cache.get('published', ''),
                                         cache.get('etag'), uuway_update.RELEASE_PAGE.format(tag=cache['tag']))
    except uuway_update.FetchError as error:
        raise SetupError(str(error))
    if not ctx.dry_run:
        record = dict(schema=1, checked_at=int(time.time()), tag=latest.tag, version=latest.version,
                      published=latest.published, etag=latest.etag)
        write_atomic(ctx.state_dir / 'update.json', json.dumps(record).encode(), 0o600)
    return latest


def update_busy(ctx):
    """True while an update is running, or being finished after a reboot."""
    for unit in (UPDATE_UNIT, UPDATE_RESUME_UNIT):
        if ctx.probe(['systemctl', 'is-active', unit]).stdout.strip() in ('active', 'activating', 'reloading'):
            return True
    return False


def update_check(ctx, args):
    installed = deb_version(ctx)
    latest = latest_known(ctx)
    if getattr(args, 'json', False):
        print(json.dumps(dict(installed=installed, latest=latest.version, tag=latest.tag, url=latest.url,
                              published=latest.published, checked_at=int(time.time()),
                              available=bool(installed and is_newer(ctx, latest.version, installed)),
                              installable=bool(installed and not installed.startswith('0.0.0~')))))
        return 0
    if installed is None:
        info(f'最新正式版：{latest.version}（{latest.url}）')
        warn('这不是用安装包装的 UUWay（源码目录或开发构建），没法比较版本，也不能用 uuway update 安装。')
        return 0
    if is_newer(ctx, latest.version, installed):
        print(f'   {_paint("1;32", "有新版本")} {latest.version}（当前 {installed}，发布于 {latest.published[:10]}）')
        info(f'更新说明：{latest.url}')
        info('安装：uuway update install（安装时远程连接会断开几分钟，会自动检查并在不正常时回滚）')
    else:
        ok(f'已是最新（{installed}）。')
    return 0


def read_progress(limit=200):
    try:
        return (UPDATE_STATE / 'progress.log').read_text().splitlines()[-limit:]
    except OSError:
        return []


def describe_result(result):
    """Print how an update ended; returns the outcome."""
    outcome = result.get('outcome')
    when = time.strftime('%Y-%m-%d %H:%M', time.localtime(result.get('finished', 0)))
    steps = f'{result.get("from_version")} → {result.get("to_version")}'
    message = result.get('message', '')
    if outcome == 'success':
        ok(f'更新成功（{steps}，{when}）：{message}')
    elif outcome == 'rolled_back' and result.get('manual'):
        ok(f'已回滚到 {result.get("rolled_back_to")}（{when}）：{message}')
    elif outcome == 'rolled_back':
        warn(f'更新没有成功，已自动回滚（{steps}，{when}）：{message}')
        info('机器已恢复；失败的原因见 journalctl -u uuway-update。重试前先看看发布说明。')
    elif outcome == 'not_started':
        info(f'更新没有开始，系统没有任何改变（{when}）：{message}')
    elif outcome == 'rollback_failed':
        warn(f'更新失败，自动回滚也没有成功（{steps}，{when}）：{message}')
        info(f'恢复步骤：{result.get("recovery") or UPDATE_STATE / "RECOVERY.txt"}（按里面的命令一步步做）')
    else:
        info(f'上次更新的结果无法识别：{outcome}')
    return outcome


def result_succeeded(result):
    """A finished update that worked, or a rollback that was asked for and worked."""
    return result.get('outcome') == 'success' or (result.get('outcome') == 'rolled_back' and bool(result.get('manual')))


def update_status(ctx, args):
    if getattr(args, 'json', False):
        installed = deb_version(ctx)
        result = read_json(UPDATE_STATE / 'last-result.json')
        result = result if isinstance(result, dict) else None
        target = result.get('rollback_to') if result else None
        previous = target.get('deb') if isinstance(target, dict) else None
        print(json.dumps(dict(installed=installed, installable=bool(installed and not installed.startswith('0.0.0~')),
                              busy=update_busy(ctx), result=result, progress=read_progress(20),
                              rollback_available=bool(installed and isinstance(previous, str) and
                                                      Path(previous).is_absolute() and Path(previous).is_file())),
                         ensure_ascii=False))
        return 0
    if update_busy(ctx):
        warn('更新正在进行中，最近的进度：')
        for line in read_progress(12):
            info(line)
        return 0
    result = read_json(UPDATE_STATE / 'last-result.json')
    if not isinstance(result, dict):
        info('还没有更新记录。')
        return 0
    return 1 if describe_result(result) == 'rollback_failed' else 0


def update_recovery_layer(ctx, args):
    """An update that crashes the desktop must not leave the machine at a login screen for good."""
    timed = timed_state(ctx)
    if timed and timed[0] and timed[1] == getpass.getuser():
        return
    if getattr(args, 'allow_no_recovery', False):
        return  # Explicit GUI/CLI consent; never enables autologin implicitly.
    warn('这台机器在桌面崩溃或注销后不会自动重新登录：万一更新让桌面崩溃，没有人在本机登录，UU 就一直离线。')
    info('先开启：uuway autologin on')
    if ctx.assume_yes or not sys.stdin.isatty():
        raise SetupError('没有开启“崩溃后自动重新登录”，--yes 或非交互运行不会替你冒这个险。'
                         '先运行 uuway autologin on，或在终端里交互运行并明确同意。')
    if not input('   仍然继续？[y/N] ').lower().startswith('y'):
        raise SetupError('已取消。')


def health_baseline(ctx):
    """How the machine looks before the update, as the user; the update compares against it."""
    job = update_checks.Job(update_checks.Layout(), user=getpass.getuser(), uid=os.getuid(), home=ctx.home,
                            prefix=ctx.prefix)
    job.as_user = lambda argv, **kwargs: ctx.probe(argv)
    return update_checks.snapshot_health(job)


def check_disk_space(deb, *also):
    need = 4 * deb.stat().st_size + (100 << 20)  # the new package, the saved old one, their unpacking, and margin
    for directory in (Path('/var/lib'), Path('/usr'), *also):
        while not directory.exists() and directory != directory.parent:
            directory = directory.parent  # not made yet: what matters is the disk it will be made on
        free = shutil.disk_usage(directory).free
        if free < need:
            raise SetupError(f'{directory} 所在的磁盘空间不够：需要约 {need >> 20} MiB，只剩 {free >> 20} MiB。')


def apply_command(mode, *, tag=None, stage=None, user=None, uid=None, home=None, prefix=None, snapshot='',
                  baseline=None, reboot=False):
    """The one command that runs as root: a systemd unit, so that it outlives this terminal.

    The terminal this runs in may be UU's own, and the update restarts the service that
    terminal belongs to.  A transient system unit is not in that service's cgroup, so it is
    not killed with it; the inhibitor keeps the machine from sleeping or shutting down
    underneath it.
    """
    script = [PYTHON, '-I', '-B', str(DEB_ROOT / 'scripts/uuway_update_apply.py'), mode]
    if mode == 'apply':
        script += ['--tag', tag, '--stage', str(stage), '--user', user, '--uid', str(uid), '--home', str(home),
                   '--prefix', str(prefix), '--snapshot', str(snapshot or ''), '--baseline', str(baseline)]
    if reboot:
        script.append('--reboot-if-stuck')
    return ['systemd-run', f'--unit={UPDATE_UNIT}', '--collect', '--no-ask-password', '-q',
            '-p', 'OOMScoreAdjust=-500', '-E', 'DEBIAN_FRONTEND=noninteractive', '-E', 'NEEDRESTART_SUSPEND=1',
            'systemd-inhibit', '--what=shutdown:sleep', '--who=uuway', '--why=UUWay update', '--mode=block', *script]


def follow_progress(ctx, started, already, limit=1500):
    """Show the update's progress until it ends.  The update does not depend on this process."""
    info('更新在后台进行，这个终端断开也不会影响它；重连后运行 uuway update status 查看结果。')
    shown = already
    deadline = time.monotonic() + limit
    idle = 0
    while time.monotonic() < deadline:
        lines = read_progress(10 ** 6)
        for line in lines[shown:]:
            info(line)
        shown = len(lines)
        result = read_json(UPDATE_STATE / 'last-result.json')
        if isinstance(result, dict) and result.get('finished', 0) >= started:
            describe_result(result)
            return 0 if result_succeeded(result) else 1
        # Not running and no result: it died before it could say how it ended.  A few looks, not one,
        # because the unit takes a moment to show up after systemd-run returns.
        idle = 0 if update_busy(ctx) else idle + 1
        if idle >= 3:
            warn('更新进程已经退出，但没有留下结果。看看发生了什么：journalctl -u uuway-update')
            info('机器可能处在中间状态；运行 uuway update status 和 uuway doctor 看看，必要时按 '
                 f'{UPDATE_STATE / "RECOVERY.txt"} 里的步骤恢复。')
            return 1
        time.sleep(2)
    warn('等了很久还没有结束；运行 uuway update status 查看。')
    return 1


def launch_update(ctx, command):
    started = time.time()
    already = len(read_progress(10 ** 6))
    ctx.run(ctx.elevate(command, terminal_first=True))
    return follow_progress(ctx, started, already)


def announce_outage(ctx, args, what):
    if in_uu_terminal():
        warn(f'你正在 UU 的远程终端里：{what}时这个连接会断开，通常 2～5 分钟内恢复。'
             '更新在后台继续，不受断线影响；重连后运行 uuway update status 看结果。')
    else:
        info(f'{what}时远程连接会断开几分钟。')
    if args.reboot_if_stuck:
        warn('已加 --reboot-if-stuck：回滚后桌面仍然回不来时，会自动重启这台机器一次（正在跑的任务会中断）。')


def confirm_default_no(ctx, question):
    if ctx.assume_yes:
        return True
    if not sys.stdin.isatty():
        raise SetupError('需要你确认，但这里不是交互终端；在终端里运行，或加 --yes。')
    return input(f'   {question} [y/N] ').lower().startswith('y')


def update_install(ctx, args):
    installed = deb_version(ctx)
    if installed is None:
        raise SetupError('uuway update install 只支持用安装包（.deb）装的 UUWay；源码目录请自己更新。')
    if installed.startswith('0.0.0~'):
        raise SetupError(f'这是开发构建（{installed}），不能用 OTA 更新。')
    if update_busy(ctx):
        raise SetupError('已经有一个更新在进行；运行 uuway update status 查看。')
    audit = ctx.probe(['dpkg', '--audit'])
    if audit.returncode != 0 or audit.stdout.strip():
        raise SetupError('dpkg 处于未完成状态，先修好它：sudo dpkg --configure -a')
    source = getattr(args, 'source', None)
    if source:
        # A rehearsal: a release laid out by hand in a directory (the deb and its SHA256SUMS, see
        # docs/design.md, "Updates"), with no network. It is checked exactly like a download.
        match = update_checks.TAG.fullmatch(getattr(args, 'tag', None) or '')
        if not match:
            raise SetupError('--source 要和 --tag vX.Y.Z 一起用。')
        tag, version = args.tag, match.group(1)
        if not is_newer(ctx, version, installed):
            raise SetupError(f'{version} 不比已安装的 {installed} 新。')
        stage = Path(source).resolve()
    else:
        latest = latest_known(ctx)
        if not is_newer(ctx, latest.version, installed):
            ok(f'已是最新（{installed}）。')
            return 0
        tag, version = latest.tag, latest.version
        stage = ctx.state_dir / 'update' / version
    baseline_file = stage / 'baseline.json'
    command = apply_command('apply', tag=tag, stage=stage, user=getpass.getuser(), uid=os.getuid(), home=ctx.home,
                            prefix=ctx.prefix, snapshot='<更新前快照>', baseline=baseline_file, reboot=args.reboot_if_stuck)
    if ctx.dry_run:
        info(f'（演练）会把 {installed} 更新到 {version}：下载并校验哈希，备份，然后以 root 运行：')
        print('   ' + _paint('2', '$ ' + ' '.join(ctx.elevate(command, terminal_first=True))))
        return 0
    update_recovery_layer(ctx, args)
    baseline = health_baseline(ctx)
    problems = update_checks.baseline_problems(baseline)
    if problems:
        raise SetupError('机器现在就不太正常，不适合更新：' + '、'.join(problems) + '。先运行 uuway doctor。')
    try:
        if not source:
            shutil.rmtree(ctx.state_dir / 'update', ignore_errors=True)
            uuway_update.download_release(
                new_fetcher(), tag, stage,
                before_deb=lambda staged: update_checks.check_checksums(staged, tag))
        release = update_checks.check_release(stage, tag, installed, run=subprocess.run)
    except (uuway_update.FetchError, update_checks.VerifyError) as error:
        raise SetupError(str(error))
    ok(f'已{"读取" if source else "下载"}并校验 {version}（哈希对得上）。')
    check_disk_space(release.deb, ctx.state_dir)  # where the download and the snapshot live
    write_atomic(baseline_file, json.dumps(baseline).encode(), 0o600)
    snapshot = ensure_backup(ctx)
    command = apply_command('apply', tag=tag, stage=stage, user=getpass.getuser(), uid=os.getuid(), home=ctx.home,
                            prefix=ctx.prefix, snapshot=snapshot or '', baseline=baseline_file,
                            reboot=args.reboot_if_stuck)
    print(f'\n将把 UUWay 从 {installed} 更新到 {version}。')
    info('过程：备份当前版本 → 安装 → 重启桥接服务 → 观察约 90 秒 → 不正常就自动回滚。')
    announce_outage(ctx, args, '安装')
    if not confirm_default_no(ctx, '现在更新？'):
        info('已取消，什么都没有改。')
        return 1
    return launch_update(ctx, command)


def update_rollback(ctx, args):
    result = read_json(UPDATE_STATE / 'last-result.json')
    target = result.get('rollback_to') if isinstance(result, dict) else None
    previous = Path(str(target.get('deb') or '/nonexistent')) if isinstance(target, dict) else None
    if previous is None or not previous.is_file():
        raise SetupError('没有可以回到的旧版本（还没有用 uuway update 更新过，或者备份已被清掉）。')
    if update_busy(ctx):
        raise SetupError('已经有一个更新在进行；运行 uuway update status 查看。')
    print(f'\n将回到更新前的版本 {target.get("version")}（现在是 {deb_version(ctx) or "未知"}）。')
    if ctx.dry_run:
        info('（演练）会以 root 运行：')
        print('   ' + _paint('2', '$ ' + ' '.join(ctx.elevate(apply_command('rollback', reboot=args.reboot_if_stuck),
                                                             terminal_first=True))))
        return 0
    update_recovery_layer(ctx, args)
    announce_outage(ctx, args, '回滚')
    if not confirm_default_no(ctx, '现在回滚？'):
        info('已取消，什么都没有改。')
        return 1
    return launch_update(ctx, apply_command('rollback', reboot=args.reboot_if_stuck))


def command_update(args):
    ctx = Context(args)
    refuse_root()
    handler = dict(check=update_check, install=update_install, status=update_status, rollback=update_rollback)
    return handler[args.action](ctx, args)


def update_notices(ctx):
    """What `doctor` says about updates, from what is already on disk: it never goes online."""
    notices = []
    installed = deb_version(ctx)
    cache = update_cache(ctx)
    if installed and cache:
        with contextlib.suppress(SetupError):
            if is_newer(ctx, cache['version'], installed):
                checked = time.strftime('%Y-%m-%d', time.localtime(cache.get('checked_at', 0)))
                notices.append(('warn', f'有新版本 {cache["version"]}', f'运行 uuway update 查看（上次检查：{checked}）'))
    result = read_json(UPDATE_STATE / 'last-result.json')
    if isinstance(result, dict) and result.get('outcome') in ('rolled_back', 'rollback_failed') \
            and not result_succeeded(result):
        if time.time() - result.get('finished', 0) < UPDATE_STALE_DAYS * 86400:
            if result['outcome'] == 'rolled_back':
                notices.append(('warn', '上次更新没有成功，已自动回滚', f'{result.get("message", "")}；详情：uuway update status'))
            else:
                notices.append(('warn', '上次更新失败，自动回滚也没有成功',
                                f'按 {result.get("recovery") or UPDATE_STATE / "RECOVERY.txt"} 里的步骤恢复'))
    return notices


# --------------------------------------------------------------------- uninstall

def command_uninstall(args):
    ctx = Context(args)
    turned_on_autologin = (ctx.state_dir / 'autologin.json').exists()  # --purge deletes the record below
    print(_paint('1', '卸载 UUWay 的用户级文件') + '（只处理带 UUWay 标记的文件）')
    if not ctx.confirm('停止并停用 UUWay 服务，删除它的单元文件和菜单入口？'):
        return 1
    ctx.run(['systemctl', '--user', 'disable', '--now', *(name + '.service' for name in SERVICES)], check=False)
    removable = [ctx.unit_dir / (name + '.service') for name in SERVICES]
    removable += [(ctx.home / '.local/share/applications/uuway.desktop', DESKTOP_MARKER),
                  (fcitx_conf_path(ctx), FCITX_MARKER)]
    for entry in removable:
        path, marker = entry if isinstance(entry, tuple) else (entry, SERVICE_MARKER)
        try:
            if path.is_file() and not path.is_symlink() and path.read_text().startswith(marker):
                info(f'删除 {path}')
                if not ctx.dry_run:
                    path.unlink()
            elif path.exists():
                warn(f'不是 UUWay 管理的文件，已保留：{path}')
        except OSError as error:
            warn(f'{path}：{error}')
    remove_amd_launchers(ctx)
    ctx.run(['systemctl', '--user', 'daemon-reload'], check=False)
    if args.purge:
        for directory in (ctx.config_dir, ctx.state_dir, ctx.releases.parent):
            info(f'删除 {directory}')
            if not ctx.dry_run:
                shutil.rmtree(directory, ignore_errors=True)
    if turned_on_autologin:
        print('\n开机自动登录是 UUWay 开启的，卸载没有改动它；它的记录已随 --purge 删除，要关闭请编辑 GDM 配置。'
              if args.purge else '\n开机自动登录是 UUWay 开启的，卸载没有改动它；要关闭：uuway autologin off')
    print('\nWine prefix（含 UU 登录信息）没有动：' + str(ctx.prefix)
          + '\n不再需要时可手动删除；要卸载程序本身：sudo apt remove uuway')
    return 0


def installed_version():
    result = subprocess.run(['dpkg-query', '-W', '-f=${Version}', 'uuway'], capture_output=True, text=True,
                            check=False) if shutil.which('dpkg-query') else None
    return result.stdout if result and result.returncode == 0 and result.stdout else '源码目录'


def build_parser():
    parser = argparse.ArgumentParser(prog='uuway', description=__doc__.split('\n\n')[0])
    parser.add_argument('--version', action='version', version='uuway ' + installed_version())
    commands = parser.add_subparsers(dest='command', required=True)

    def common(sub):
        sub.add_argument('--yes', '-y', action='store_true', help='不再逐项确认')
        sub.add_argument('--dry-run', action='store_true', help='只显示将要执行的命令，不做任何改动')

    setup = commands.add_parser('setup', help='安装并配置（可反复运行）')
    common(setup)
    setup.add_argument('--installer', type=Path, help='UU 远程官方 Windows 安装包')
    setup.add_argument('--prefix', type=Path, help='Wine prefix（默认 ~/.local/share/wineprefixes/uu-remote）')
    setup.add_argument('--only', help='只运行这些步骤（逗号分隔）：' + ', '.join(STEP_NAMES))
    setup.add_argument('--force', help='即使已完成也重做这些步骤')
    setup.add_argument('--system', action='store_true', help='只做需要管理员权限的部分（WineHQ、udev）')
    setup.add_argument('--login', action='store_true', help='即使已登录也重新打开 UU 登录一次')
    setup.add_argument('--regrant-screen', action='store_true', help='重新授权屏幕共享')
    setup.add_argument('--restart-services', action='store_true', help='同时重启文字与显示服务')
    setup.add_argument('--restart-display', action='store_true', help='重启显示服务以加载多屏与超级屏更新（移除现有超级屏）')
    setup.set_defaults(handler=command_setup)

    launch = commands.add_parser('open', help='打开 UU 窗口（含新版 UU 登录所需的服务引导）')
    launch.add_argument('--prefix', type=Path)
    launch.set_defaults(handler=command_open)

    refresh = commands.add_parser('refresh', help='升级软件包后，重打运行时并重启桥接服务')
    common(refresh)
    refresh.add_argument('--prefix', type=Path)
    refresh.add_argument('--restart-services', action='store_true', help='同时重启文字与显示服务')
    refresh.add_argument('--restart-display', action='store_true', help='重启显示服务并移除现有超级屏，保留文字服务')
    refresh.set_defaults(handler=command_refresh)

    doctor = commands.add_parser('doctor', help='检查环境与安装状态')
    doctor.add_argument('--prefix', type=Path)
    doctor.add_argument('--json', action='store_true')
    doctor.set_defaults(handler=command_doctor)

    autologin = commands.add_parser('autologin', help='开机自动登录（远程使用推荐）：重启后自动进入桌面，UU 随之上线')
    common(autologin)
    autologin.add_argument('action', choices=('on', 'off', 'status'))
    autologin.set_defaults(handler=command_autologin)

    update = commands.add_parser('update', help='检查并安装 UUWay 的新版本（不正常会自动回滚）')
    common(update)
    update.add_argument('action', nargs='?', choices=('check', 'install', 'status', 'rollback'), default='check',
                        help='check 查看有没有新版本（默认）；install 安装；status 看上次更新的结果；rollback 回到更新前的版本')
    update.add_argument('--prefix', type=Path)
    update.add_argument('--source', type=Path, help=argparse.SUPPRESS)  # rehearsal: a release staged by hand
    update.add_argument('--tag', help=argparse.SUPPRESS)
    update.add_argument('--json', action='store_true', help='以 JSON 返回检查结果或更新状态')
    update.add_argument('--allow-no-recovery', action='store_true',
                        help='明确允许未配置桌面自动恢复时安装更新；不更改自动登录设置')
    update.add_argument('--reboot-if-stuck', action='store_true',
                        help='回滚后桌面仍然回不来时，自动重启这台机器一次（会中断正在跑的任务）')
    update.set_defaults(handler=command_update)

    uninstall = commands.add_parser('uninstall', help='删除用户级服务与菜单入口')
    common(uninstall)
    uninstall.add_argument('--prefix', type=Path)
    uninstall.add_argument('--purge', action='store_true', help='同时删除配置、状态和运行时（不含 Wine prefix）')
    uninstall.set_defaults(handler=command_uninstall)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except SetupError as error:
        print(f'\n{_paint("31", "✗")} {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if getattr(args, 'command', None) == 'update':
            # Only the watching stops: an update that has started carries on in its own unit.
            print('\n已中断。如果更新已经开始，它会在后台继续；运行 uuway update status 查看结果。', file=sys.stderr)
        else:
            print('\n已中断；重新运行 uuway setup 会从未完成的步骤继续。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
