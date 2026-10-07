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

PYTHON = '/usr/bin/python3'
WINE_DIR = Path('/opt/wine-stable/bin')
WINE_SERIES = 'wine-11.0'
WINE_PIN = Path('/etc/apt/preferences.d/uuway-wine')
SERVICES = ('uu-native-display', 'uu-native-text', 'uu-native-bridge')
GDM_CONFIGS = (Path('/etc/gdm3/custom.conf'), Path('/etc/gdm/custom.conf'))  # Debian/Ubuntu, Fedora/Arch
AUTOLOGIN_KEYS = ('AutomaticLoginEnable', 'AutomaticLogin')
UDEV_RULE = '70-uurb-native-input.rules'
SHIPPED_RULE = Path('/usr/lib/udev/rules.d') / UDEV_RULE
LEGACY_RULE = Path('/etc/udev/rules.d') / UDEV_RULE
SERVICE_MARKER = '# Managed by UURB native installer;'
DESKTOP_MARKER = '# Managed by UUWay console installer'
FCITX_MARKER = '# Managed by uuway setup'
HELPERS = ('uu-clipboard-bridge', 'uu-clipboard-files.exe', 'uu-conpty.dll',
           'uu-terminal-bridge', 'uu-terminal-proxy.exe')
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

    def elevate(self, argv):
        """pkexec when a desktop session can show its prompt, sudo otherwise."""
        graphical = os.environ.get('WAYLAND_DISPLAY') or os.environ.get('DISPLAY')
        return (['pkexec'] if shutil.which('pkexec') and graphical else ['sudo']) + list(argv)


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


def check_environment(ctx):
    if os.geteuid() == 0 or os.environ.get('SUDO_USER'):
        raise SetupError('请以普通用户运行，不要使用 sudo 或 root；需要权限的步骤会自己请求。')
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
    gpus = ctx.probe(['nvidia-smi', '-L'])
    if gpus.returncode != 0:
        raise SetupError('没有找到可用的 NVIDIA 显卡驱动。UUWay 需要支持 NVENC 的 NVIDIA 显卡和官方驱动。')
    ok('NVIDIA 显卡：' + gpus.stdout.splitlines()[0].split(' (UUID')[0])


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
        ctx.run(sudo + ['curl', '-fsSLo', '/etc/apt/keyrings/winehq-archive.key',
                        'https://dl.winehq.org/wine-builds/winehq.key'])
        ctx.run(sudo + ['curl', '-fsSLo', '/etc/apt/sources.list.d/winehq-noble.sources',
                        'https://dl.winehq.org/wine-builds/ubuntu/dists/noble/winehq-noble.sources'])
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
        if not ctx.confirm('现在安装 udev 规则？（会弹出授权窗口）'):
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
        ctx.run([wineserver, '-w'], env=ctx.wine_env())
        if not ctx.dry_run and not ctx.server_exe.is_file():
            raise SetupError('安装包运行结束，但没有找到 GameViewerServer.exe。')
        ok('UU 已安装')
        ctx.args.login = True
    if getattr(ctx.args, 'login', False) or not signed_in(ctx):
        info('接下来会打开 UU 窗口：请登录你的 UU 账号，登录成功后从托盘图标完全退出 UU。')
        if not ctx.dry_run:
            subprocess.Popen([str(wine), r'C:\Program Files\Netease\GameViewer\GameViewer.exe'],
                             env=ctx.wine_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        ctx.wait_for_user('在 UU 里登录并完全退出')
        if not ctx.dry_run and not signed_in(ctx):
            warn('没有检测到登录信息；如果连接不上，请运行 uuway setup --login 重新登录。')
        ctx.run([wineserver, '-k'], env=ctx.wine_env(), check=False)
    # UU's terminal starts PowerShell; Wine must load UUWay's stand-in, not its placeholder.
    ctx.run([wine, 'reg', 'add', r'HKCU\Software\Wine\DllOverrides', '/v', 'powershell.exe',
             '/t', 'REG_SZ', '/d', 'native', '/f'], env=ctx.wine_env())
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
    info('接下来会弹出屏幕共享对话框：请选择要串流给手机的显示器，并勾选「记住」。')
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
    result = ctx.run([PYTHON, ROOT / 'scripts/package-uu-native-runtime.py', 'package', '--with-input',
                      ctx.releases], capture=True)
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
  cp -a home/. ~/
  systemctl --user daemon-reload && systemctl --user restart uu-native-bridge
Files are stored under home/ with the path they have relative to your home directory.
The old runtime directories are never deleted by uuway.
"""


def backup_candidates(ctx):
    """Every file a setup run can replace or remove in an existing installation."""
    app_bin = ctx.app_dir / 'bin'
    candidates = [ctx.unit_dir / (name + '.service') for name in SERVICES]
    candidates += [ctx.config_dir / name for name in BACKUP_CONFIG]
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
    if unit_active(ctx, 'uu-native-bridge'):
        info('先停止 UUWay 桥接服务，写好配置后再启动。')
        ctx.run(['systemctl', '--user', 'stop', 'uu-native-bridge.service'])
    bundle = package_bundle(ctx)
    ok(f'运行时：{bundle}')
    command = [PYTHON, ROOT / 'scripts/install-uu-native-service.py', '--bundle', bundle,
               '--prefix', ctx.prefix, '--restore-state', ctx.restore_state,
               '--state-parent', ctx.state_dir, '--text-socket', ctx.state_dir / 'text.sock']
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
    return not ctx.restart_bridge and all(unit_active(ctx, name) for name in SERVICES)


def start_run(ctx):
    ctx.run(['systemctl', '--user', 'enable', '--now', *(name + '.service' for name in SERVICES)])
    if ctx.restart_bridge:
        if getattr(ctx.args, 'restart_services', False):
            ctx.run(['systemctl', '--user', 'restart', 'uu-native-display.service', 'uu-native-text.service'])
        elif not ctx.dry_run:
            info('文字与显示服务保持运行，不会被隐式重启（会丢失剪贴板所有权）；'
                 '空闲时可用 uuway refresh --restart-services 一并重启。')
        ctx.run(['systemctl', '--user', 'restart', 'uu-native-bridge.service'])
    if not ctx.dry_run:
        time.sleep(5)
        if not unit_active(ctx, 'uu-native-bridge'):
            raise SetupError('uu-native-bridge 没有启动，请查看：journalctl --user -u uu-native-bridge -e')
    ctx.restart_bridge = False
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

def launchers_done(ctx):
    desktop = ctx.home / '.local/share/applications/uuway.desktop'
    try:
        return not desktop.read_text().startswith(DESKTOP_MARKER)
    except OSError:
        return True


def launchers_run(ctx):
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

AUTOLOGIN_NOTE = """   UU 跟着你的桌面会话上线：重启、断电恢复或注销之后，没人在屏幕前登录，UU 就是离线的。
   开启开机自动登录后，GDM 会在开机时自动登录 {user}，UU 随之上线；之后锁屏、息屏都不受影响。
   代价：任何能接触这台机器的人，开机就能进入你的桌面。建议同时开启磁盘加密或固件密码，并设置自动锁屏。
   另外 GNOME 钥匙环不会自动解锁，个别应用第一次用到它时会询问密码。
   说明：自动登录只在 GDM 启动时触发；手动注销后会停在登录界面，需要重启（或在那里登录）UU 才会回来。
   UU 看不到 GDM 登录界面：它属于另一个系统会话，UUWay 的采集和输入都只在你的桌面会话里。"""


def gdm_config():
    return next((path for path in GDM_CONFIGS if path.is_file()), None)


def gdm_autologin(text):
    """(enabled, user) as the [daemon] section sets them; a later line wins, as in GLib's key file parser."""
    values, section = {}, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
        elif section == 'daemon' and line[:1] not in ('#', ';'):
            key, _, value = line.partition('=')
            if key.strip() in AUTOLOGIN_KEYS:
                values[key.strip()] = value.strip()
    return values.get('AutomaticLoginEnable', '').lower() in ('true', '1', 'yes'), values.get('AutomaticLogin') or None


def gdm_set_autologin(text, user, enable):
    """The config with AutomaticLogin* set for user, or removed; every other line stays as it was."""
    kept, section = [], None
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            section = stripped[1:-1]
        elif section == 'daemon' and stripped[:1] not in ('#', ';') and stripped.partition('=')[0].strip() in AUTOLOGIN_KEYS:
            continue
        kept.append(line)
    if not enable:
        return ''.join(kept)
    lines = f'AutomaticLoginEnable=true\nAutomaticLogin={user}\n'
    for index, line in enumerate(kept):
        if line.strip() == '[daemon]':
            kept.insert(index + 1, lines)
            return ''.join(kept)
    separator = '' if not kept or kept[-1].endswith('\n') else '\n'
    return ''.join(kept) + separator + '\n[daemon]\n' + lines


def autologin_state(ctx):
    """(config path, enabled, user), or None when this machine has no GDM config to edit."""
    path = gdm_config()
    if path is None:
        return None
    try:
        return (path, *gdm_autologin(path.read_text()))
    except OSError:
        return None


def autologin_declined(ctx):
    return (ctx.state_dir / 'autologin-declined').exists()


def autologin_done(ctx):
    state = autologin_state(ctx)
    return state is None or state[1] or autologin_declined(ctx)


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
    if enabled:
        ok(f'已经为 {user} 开启了开机自动登录')
        return
    explicit = 'autologin' in parse_steps(getattr(ctx.args, 'only', None))
    print(AUTOLOGIN_NOTE.format(user=user))
    if not explicit:
        # A security setting: --yes and a non-interactive run never turn it on behind the user's back.
        if ctx.assume_yes or ctx.dry_run or not sys.stdin.isatty():
            info('这一步需要你亲自决定，已跳过；需要时运行：uuway autologin on')
            return
        if not input('   开启开机自动登录？[y/N] ').lower().startswith('y'):
            ctx.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            (ctx.state_dir / 'autologin-declined').touch(mode=0o600)
            info('不开启。以后需要：uuway autologin on')
            return
    elif not ctx.confirm(f'为 {user} 开启开机自动登录？（会弹出授权窗口）'):
        raise SetupError('没有开启自动登录。')
    backup = autologin_apply(ctx, path, gdm_set_autologin(path.read_text(), user, True), f'已为 {user} 开启开机自动登录')
    if not ctx.dry_run:
        (ctx.state_dir / 'autologin-declined').unlink(missing_ok=True)
        write_atomic(ctx.state_dir / 'autologin.json',
                     json.dumps(dict(version=1, user=user, config=str(path), backup=str(backup))).encode(), 0o600)


def autologin_off(ctx):
    state = autologin_state(ctx)
    if state is None:
        info('没有找到 GDM 配置，没有可关闭的自动登录。')
        return
    path, enabled, current = state
    recorded = read_json(ctx.state_dir / 'autologin.json')
    if not enabled:
        info('开机自动登录本来就没有开启。')
    elif not recorded or recorded.get('user') != current:
        warn(f'这个自动登录不是 UUWay 开启的，没有改动。要关闭，请编辑 {path}，把 AutomaticLoginEnable 改为 false。')
        return
    else:
        autologin_apply(ctx, path, gdm_set_autologin(path.read_text(), current, False), '已关闭开机自动登录')
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
            print(f'开机自动登录：已为 {user} 开启（{path}）' if enabled else f'开机自动登录：未开启（{path}）')
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
        run_steps(ctx, set(REFRESH_STEPS), set())
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
        if enabled and user == me:
            check('ok', '开机自动登录', f'已为 {me} 开启')
        elif enabled:
            check('warn', '开机自动登录开给了另一个账号', f'{user}：重启后 {me} 的桌面不会自动出现，UU 不会上线')
        elif autologin_declined(ctx):
            check('ok', '开机自动登录', '未开启（你选择了不开启）：重启后需要有人登录，UU 才会上线')
        else:
            check('warn', '开机自动登录', '未开启：重启或断电恢复后需要有人在屏幕前登录，UU 才会上线；开启：uuway autologin on')
    if shutil.which('fcitx5') and fcitx_library().is_file():
        check('ok' if fcitx_conf_path(ctx).is_file() else 'warn', 'Fcitx5 输入法插件',
              str(fcitx_conf_path(ctx)))
    failed = [item for item in results if item['level'] == 'fail']
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        marks = dict(ok=_paint('32', '✓'), warn=_paint('33', '!'), fail=_paint('31', '✗'))
        for item in results:
            print(f'{marks[item["level"]]} {item["name"]}' + (f'  {item["detail"]}' if item['detail'] else ''))
        print(f'\n{len(failed)} 项失败，{sum(i["level"] == "warn" for i in results)} 项警告')
    return 1 if failed else 0


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
    setup.set_defaults(handler=command_setup)

    refresh = commands.add_parser('refresh', help='升级软件包后，重打运行时并重启桥接服务')
    common(refresh)
    refresh.add_argument('--prefix', type=Path)
    refresh.add_argument('--restart-services', action='store_true', help='同时重启文字与显示服务')
    refresh.set_defaults(handler=command_refresh)

    doctor = commands.add_parser('doctor', help='检查环境与安装状态')
    doctor.add_argument('--prefix', type=Path)
    doctor.add_argument('--json', action='store_true')
    doctor.set_defaults(handler=command_doctor)

    autologin = commands.add_parser('autologin', help='开机自动登录（远程使用推荐）：重启后自动进入桌面，UU 随之上线')
    common(autologin)
    autologin.add_argument('action', choices=('on', 'off', 'status'))
    autologin.set_defaults(handler=command_autologin)

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
        print('\n已中断；重新运行 uuway setup 会从未完成的步骤继续。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
