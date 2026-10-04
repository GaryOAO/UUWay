#!/usr/bin/python3
"""Launch only the configured native UU runtime, with private crash recovery."""
import argparse
import filecmp
import importlib.util
import os
from pathlib import Path
import secrets
import shlex
import signal
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_service_trial', ROOT / 'scripts/uu-native-trial.py')
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)

DISPLAY_REMAP_FAILURE = 'Native display remap requires restart'
DISPLAY_REMAP_LIMIT = 3
DISPLAY_REMAP_WINDOW_SECONDS = 300


class TerminalBridge:
    """Own the PowerShell compatibility entry point and a native Linux PTY.

    UU's controller starts the vendor ``powershell.exe`` entry point.  The
    reviewed replacement executable reads this private handoff and forwards
    the authenticated byte stream to ``uu-terminal-bridge``.  Keeping this
    owner beside the native service is important: the legacy RDP launcher is
    not running in the production path, so it cannot be the only owner of the
    PTY listener.
    """

    def __init__(self, prefix, state_parent):
        app = Path(prefix) / 'drive_c/Program Files/Netease/GameViewer'
        self.helper = Path(prefix) / 'compat/uu-terminal-bridge'
        self.proxy = app / 'bin/powershell.exe'
        self.compat_proxy = Path(prefix) / 'compat/uu-terminal-proxy.exe'
        self.config = app / 'bin/uu-terminal-bridge.runtime'
        # UU 4.39's psmux panes launch the system PowerShell path instead.
        # The proxy reads its handoff beside itself, so it needs a copy there.
        system_powershell = Path(prefix) / 'drive_c/windows/system32/WindowsPowerShell/v1.0'
        self.mux_proxy = system_powershell / 'powershell.exe'
        self.mux_placeholder = system_powershell / 'powershell.exe.uurb-wine'
        self.mux_config = system_powershell / 'uu-terminal-bridge.runtime'
        # conpty_bridge loads conpty.dll beside itself; ours joins UU's terminal
        # pipes directly to the Linux PTY instead of Wine's console host.
        self.conpty = app / 'bin/conpty.dll'
        self.conpty_vendor = app / 'bin/conpty.dll.uurb-vendor'
        self.compat_conpty = Path(prefix) / 'compat/uu-conpty.dll'
        self.ready = Path(state_parent) / 'terminal.port'
        self.log_path = Path(state_parent) / 'terminal-bridge.log'
        self.process = None
        self._log = None

    def start(self):
        # A missing compatibility payload must not prevent screen sharing;
        # terminal support is optional until the normal installer supplies it.
        try:
            compatible = (self.helper.is_file() and os.access(self.helper, os.X_OK) and
                          self.proxy.is_file() and self.compat_proxy.is_file() and
                          filecmp.cmp(self.proxy, self.compat_proxy, shallow=False))
        except OSError:
            compatible = False
        if not compatible:
            return False
        configs = [self.config]
        self._install_conpty_shim()
        if self._install_mux_proxy():
            configs.append(self.mux_config)
        self.config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.ready.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for path in (self.ready, self.config, self.mux_config):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        token = secrets.token_hex(32)
        self._log = self.log_path.open('ab')
        environment = dict(os.environ, UURB_TERMINAL_BRIDGE_TOKEN=token)
        self.process = subprocess.Popen(
            [str(self.helper), '--ready-file', str(self.ready)],
            env=environment, stdout=self._log, stderr=subprocess.STDOUT,
            start_new_session=True)
        deadline = time.monotonic() + 5
        port = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            try:
                value = self.ready.read_text().strip()
                if value.isdigit() and 1 <= int(value) <= 65535:
                    port = int(value)
                    break
            except (FileNotFoundError, OSError, UnicodeError):
                pass
            time.sleep(0.05)
        if port is None:
            self.stop()
            return False
        for config in configs:
            temporary = config.with_name(config.name + '.tmp')
            try:
                temporary.write_text(f'version=1\nport={port}\ntoken={token}\n')
                os.chmod(temporary, 0o600)
                os.replace(temporary, config)
            except OSError:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                self.stop()
                return False
        return True

    def _install_mux_proxy(self):
        """Put the proxy on psmux's PowerShell path, keeping Wine's placeholder.

        Only Wine's own placeholder or an identical proxy is replaced; any
        other file there is left alone and psmux terminals stay unsupported.
        """
        try:
            if filecmp.cmp(self.mux_proxy, self.compat_proxy, shallow=False):
                return True
        except FileNotFoundError:
            pass
        except OSError:
            return False
        try:
            with self.mux_proxy.open('rb') as current:
                if b'Wine builtin DLL' not in current.read(128):
                    return False
            if not self.mux_placeholder.exists():
                shutil.copy2(self.mux_proxy, self.mux_placeholder)
            temporary = self.mux_proxy.with_name(self.mux_proxy.name + '.tmp')
            shutil.copyfile(self.compat_proxy, temporary)
            os.chmod(temporary, 0o755)
            os.replace(temporary, self.mux_proxy)
        except OSError:
            return False
        return True

    def _install_conpty_shim(self):
        """Replace only Microsoft's conpty.dll, keeping it as the vendor copy."""
        try:
            if filecmp.cmp(self.conpty, self.compat_conpty, shallow=False):
                return True
            with self.conpty.open('rb') as current:
                # Our shim does not export the AsUser entry point.
                if b'ConptyCreatePseudoConsoleAsUser' not in current.read():
                    return False
            shutil.copy2(self.conpty, self.conpty_vendor)
            temporary = self.conpty.with_name(self.conpty.name + '.tmp')
            shutil.copyfile(self.compat_conpty, temporary)
            os.chmod(temporary, 0o755)
            os.replace(temporary, self.conpty)
        except OSError:
            return False
        return True

    def stop(self):
        for path in (self.config, self.mux_config, self.ready):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=2)
            self.process = None
        if self._log is not None:
            self._log.close()
            self._log = None


class ClipboardBridge:
    """Own the helper that joins UU's private X clipboard to the desktop's.

    UU runs on its own Xvfb, so its phone clipboard sync reaches only that
    display. The helper finds UU's and Xwayland's displays itself and
    reconnects when either restarts; it is optional, like the terminal.
    """

    def __init__(self, prefix, state_parent):
        self.helper = Path(prefix) / 'compat/uu-clipboard-bridge'
        self.log_path = Path(state_parent) / 'clipboard-bridge.log'
        self.process = None
        self._log = None

    def start(self):
        if not (self.helper.is_file() and os.access(self.helper, os.X_OK)):
            return False
        self.log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._log = self.log_path.open('ab')
        self.process = subprocess.Popen(
            [str(self.helper)], stdout=self._log, stderr=subprocess.STDOUT,
            start_new_session=True)
        return True

    def stop(self):
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=2)
            self.process = None
        if self._log is not None:
            self._log.close()
            self._log = None


def xdg_download_directory(home=None, config_home=None):
    """Return the user's XDG download directory without evaluating shell code."""
    return xdg_user_directory('DOWNLOAD', home, config_home)


def xdg_user_directory(kind, home=None, config_home=None):
    """Resolve one XDG user directory using only literal ``$HOME`` values."""
    home = Path.home() if home is None else Path(home)
    config_root = config_home or os.environ.get('XDG_CONFIG_HOME')
    config_root = Path(config_root) if config_root else home / '.config'
    user_dirs = config_root / 'user-dirs.dirs'
    key = f'XDG_{kind}_DIR='
    value = None
    try:
        for line in user_dirs.read_text().splitlines():
            if not line.startswith(key):
                continue
            try:
                values = shlex.split(line.split('=', 1)[1], comments=False, posix=True)
            except ValueError:
                values = []
            if len(values) == 1:
                value = values[0]
            break
    except (OSError, UnicodeError):
        pass
    if value == '$HOME':
        return home
    if value and value.startswith('$HOME/'):
        return home / value[6:]
    if value and value.startswith('/'):
        return Path(value)
    defaults = {
        'DESKTOP': 'Desktop',
        'DOCUMENTS': 'Documents',
        'DOWNLOAD': 'Downloads',
        'MUSIC': 'Music',
        'PICTURES': 'Pictures',
        'VIDEOS': 'Videos',
        'PUBLICSHARE': 'Public',
        'TEMPLATES': 'Templates',
    }
    return home / defaults.get(kind, kind.title())


def configured_download_directory(config_directory=None):
    """Use an explicit private directory when configured, otherwise XDG."""
    config_directory = (Path.home() / '.config/uurb' if config_directory is None
                        else Path(config_directory))
    try:
        value = trial.state_tools.read_private(config_directory / 'download-directory.json')
        if (isinstance(value, dict) and set(value) == {'version', 'path'} and
                value['version'] == 1 and isinstance(value['path'], str) and
                value['path'].startswith('/') and '\0' not in value['path']):
            return Path(value['path']).resolve()
    except (FileNotFoundError, OSError, ValueError, TypeError):
        pass
    return xdg_download_directory()


class DownloadPathMapping:
    """Expose UU's Windows receive directory in the Linux download folder."""

    relative_source = Path('drive_c/Program Files/Netease/GameViewer/Download')

    def __init__(self, prefix, destination=None, config_directory=None):
        self.prefix = Path(prefix)
        self.source = self.prefix / self.relative_source
        self.destination = (configured_download_directory(config_directory) if destination is None
                            else Path(destination)).resolve()

    def _backup_path(self):
        candidate = self.source.with_name(self.source.name + '.uurb-wine')
        suffix = 1
        while candidate.exists() or candidate.is_symlink():
            candidate = self.source.with_name(self.source.name + f'.uurb-wine-{suffix}')
            suffix += 1
        return candidate

    def apply(self):
        """Create the mapping, preserving an existing Wine directory if needed."""
        # Do not create a prefix or a host directory for an uninstalled setup.
        if not self.source.parent.is_dir():
            return False
        try:
            self.destination.mkdir(mode=0o700, parents=True, exist_ok=True)
            source_parent = self.source.parent.resolve()
            if self.destination == source_parent or source_parent in self.destination.parents:
                return False
            if self.source.is_symlink():
                if self.source.resolve(strict=False) == self.destination:
                    return True
                self.source.unlink()
            if self.source.exists():
                if not self.source.is_dir():
                    return False
                self.source.rename(self._backup_path())
            self.source.symlink_to(self.destination, target_is_directory=True)
            return True
        except OSError:
            return False


class WinePathMapping:
    """Safely map a Wine directory to a native Linux directory."""

    def __init__(self, source, destination):
        self.source = Path(source)
        self.destination = Path(destination).resolve()

    def _backup_path(self):
        candidate = self.source.with_name(self.source.name + '.uurb-wine')
        suffix = 1
        while candidate.exists() or candidate.is_symlink():
            candidate = self.source.with_name(self.source.name + f'.uurb-wine-{suffix}')
            suffix += 1
        return candidate

    def apply(self):
        if not self.source.parent.is_dir():
            return False
        try:
            self.destination.mkdir(mode=0o700, parents=True, exist_ok=True)
            source_parent = self.source.parent.resolve()
            if self.destination == source_parent or source_parent in self.destination.parents:
                return False
            if self.source.is_symlink():
                if self.source.resolve(strict=False) == self.destination:
                    return True
                self.source.unlink()
            if self.source.exists():
                if not self.source.is_dir():
                    return False
                self.source.rename(self._backup_path())
            self.source.symlink_to(self.destination, target_is_directory=True)
            return True
        except OSError:
            return False


class WineUserDirectoryMappings:
    """Expose the Linux XDG directories through Wine's user profile."""

    DIRECTORIES = (
        ('Desktop', 'DESKTOP'),
        ('Documents', 'DOCUMENTS'),
        ('Downloads', 'DOWNLOAD'),
        ('Music', 'MUSIC'),
        ('Pictures', 'PICTURES'),
        ('Videos', 'VIDEOS'),
        ('Public', 'PUBLICSHARE'),
        ('Templates', 'TEMPLATES'),
    )

    def __init__(self, prefix, home=None, config_home=None, user_name=None):
        self.prefix = Path(prefix)
        self.home = Path.home() if home is None else Path(home)
        self.config_home = config_home
        users = self.prefix / 'drive_c/users'
        preferred = user_name or self.home.name
        self.user_root = users / preferred
        if not self.user_root.is_dir():
            candidates = [item for item in users.iterdir() if item.is_dir() and
                          item.name.lower() not in {'public', 'default', 'default user', 'all users'}] \
                if users.is_dir() else []
            self.user_root = candidates[0] if len(candidates) == 1 else users / preferred
        self.mappings = []
        if self.user_root.is_dir():
            for windows_name, xdg_name in self.DIRECTORIES:
                destination = xdg_user_directory(xdg_name, self.home, self.config_home)
                self.mappings.append(WinePathMapping(self.user_root / windows_name, destination))

    def apply(self):
        return {mapping.source.name: mapping.apply() for mapping in self.mappings}


class WineDriveMappings:
    """Keep Wine's C: sandbox and Z: Linux root drive aligned with the host."""

    def __init__(self, prefix):
        dosdevices = Path(prefix) / 'dosdevices'
        self.mappings = {
            'c:': WinePathMapping(dosdevices / 'c:', Path(prefix) / 'drive_c'),
            'z:': WinePathMapping(dosdevices / 'z:', Path('/')),
        }

    def apply(self):
        return {name: mapping.apply() for name, mapping in self.mappings.items()}


class LinuxWineMappings:
    """Apply all non-destructive Wine-to-Linux path mappings in one explicit step."""

    def __init__(self, prefix, config_directory=None, home=None, config_home=None):
        self.prefix = Path(prefix)
        self.config_directory = (Path.home() / '.config/uurb' if config_directory is None
                                 else Path(config_directory))
        self.home = Path.home() if home is None else Path(home)
        self.config_home = config_home
        self.download = DownloadPathMapping(
            self.prefix, config_directory=self.config_directory)
        self.user = WineUserDirectoryMappings(
            self.prefix, self.home, self.config_home)
        self.drives = WineDriveMappings(self.prefix)

    def apply(self):
        result = {'drive': self.drives.apply(), 'user': self.user.apply(),
                  'download': self.download.apply()}
        return result


class DesktopImageMapping:
    """Set the Windows desktop picture UU reports to controllers."""

    def __init__(self, prefix, config_directory=None, default_image=None, runner=None):
        self.prefix = Path(prefix)
        self.config_directory = (Path.home() / '.config/uurb' if config_directory is None
                                 else Path(config_directory))
        self.default_image = ROOT / 'assets/uuway-penguin.bmp' if default_image is None else Path(default_image)
        self.runner = subprocess.run if runner is None else runner

    def image(self):
        path = self.default_image
        try:
            value = trial.state_tools.read_private(self.config_directory / 'desktop-image.json')
            if (isinstance(value, dict) and set(value) == {'version', 'path'} and
                    value['version'] == 1 and isinstance(value['path'], str) and
                    value['path'].startswith('/')):
                path = Path(value['path'])
        except (FileNotFoundError, OSError, ValueError, TypeError):
            pass
        try:
            if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
                return None
        except OSError:
            return None
        return path

    def apply(self):
        if not (self.prefix / 'system.reg').is_file():
            return False
        image = self.image()
        if image is None:
            return False
        environment = dict(os.environ, WINEPREFIX=str(self.prefix), WINEDEBUG='-all')
        key = r'HKCU\Control Panel\Desktop'
        try:
            for name, value in (('Wallpaper', trial.winpath(image)),
                                ('WallpaperStyle', '10'), ('TileWallpaper', '0')):
                result = self.runner(
                    [trial.WINE, 'reg', 'add', key, '/v', name, '/t', 'REG_SZ', '/d', value, '/f'],
                    env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=10, check=False)
                if result.returncode != 0:
                    return False
        except (OSError, subprocess.SubprocessError):
            return False
        finally:
            # `wine reg` starts a wineserver even though it is a short-lived
            # helper.  Leave the prefix cold so the supervised trial can take
            # its lock immediately after this mapping phase.
            for action in ('-k', '-w'):
                try:
                    self.runner(
                        [trial.WINESERVER, action], env=environment,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=10, check=False)
                except (OSError, subprocess.SubprocessError):
                    pass
        return True


def apply_desktop_image(prefix, config_directory):
    """Refresh UU's desktop image on every bridge start.

    The console writes the selected image as private configuration while the
    Wine registry is owned by the bridge prefix. Applying it here makes a
    normal `systemctl --user restart uu-native-bridge` sufficient; the
    console never needs to race a live Wine process. A missing or unreadable
    image is intentionally non-fatal so a cover problem cannot prevent a
    remote session from starting.
    """
    try:
        return DesktopImageMapping(prefix, config_directory).apply()
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return False


def text_configuration(config, config_directory):
    """Explicit backend choice; never fall back after a native IME failure."""
    try:
        settings = trial.state_tools.read_private(config_directory / 'text-backend.json', max_bytes=1024)
    except FileNotFoundError:
        return 'portal', Path(config['text_socket'])
    if (not isinstance(settings, dict) or set(settings) != {'version', 'backend'} or
            type(settings['version']) is not int or settings['version'] != 1 or
            settings['backend'] not in ('portal', 'fcitx')):
        raise ValueError('Invalid explicit native text backend')
    return settings['backend'], (Path(config['state_parent']) / 'ime.sock' if settings['backend'] == 'fcitx'
                                 else Path(config['text_socket']))


def text_endpoint(config, config_directory):
    return text_configuration(config, config_directory)[1]


def run(config_path, preserve_display_session=False, allow_display_reconfigure=False):
    trial.private_directory(config_path.parent)
    config = trial.state_tools.read_private(config_path)
    names = {'schema', 'prefix', 'bundle', 'restore_state', 'state_parent', 'text_socket', 'cursor_mode'}
    if set(config) != names or config['schema'] != 1 or config['cursor_mode'] not in ('metadata', 'embedded', 'composited'):
        raise ValueError('Unreviewed native service configuration')
    for name in names - {'schema', 'cursor_mode'}:
        if not isinstance(config[name], str) or not Path(config[name]).is_absolute():
            raise ValueError('Native service requires absolute configured paths')
    bundle = Path(config['bundle'])
    verified = trial.bundle_tools.verify(bundle)
    # Reapply a custom/default cover after every bridge restart. This used to
    # happen only in the one-time Wine mapping phase, so changes made from the
    # console survived in JSON but never reached HKCU after a restart.
    apply_desktop_image(Path(config['prefix']), config_path.parent)
    # Expose the native display catalogue to UU so its mode list stays
    # complete.  The native bridge is read-only by default; only an explicit
    # opt-in enables remote writes to Mutter.
    display_socket = (Path(config['state_parent']) / 'display.sock'
                      if verified.get('native_display_included') else None)
    preserve_display_session = bool(preserve_display_session and allow_display_reconfigure)
    backend, endpoint = text_configuration(config, config_path.parent)
    stopping = False

    def request_stop(signum, frame):
        nonlocal stopping
        stopping = True

    # One signal owner for the full service lifetime, including trial cleanup
    # and the reconnect delay.  A signal must never be lost between trials.
    previous = {s: signal.signal(s, request_stop) for s in (signal.SIGTERM, signal.SIGINT)}
    terminal_bridge = TerminalBridge(Path(config['prefix']), Path(config['state_parent']))
    clipboard_bridge = ClipboardBridge(Path(config['prefix']), Path(config['state_parent']))
    try:
        if terminal_bridge.start():
            trial.event('native_terminal_bridge_ready')
        if clipboard_bridge.start():
            trial.event('native_clipboard_bridge_started')
        remap_window = time.monotonic()
        remap_count = 0
        while not stopping:
            try:
                trial.run(Path(config['prefix']), bundle, Path(config['restore_state']),
                          Path(config['state_parent']), None, True, config['cursor_mode'], endpoint, 'user', display_socket,
                          preserve_display_session,
                          pending_native_ime=backend == 'fcitx' and verified.get('native_ime_deferred_start_included', False),
                          allow_display_reconfigure=allow_display_reconfigure,
                          stop_requested=lambda: stopping)
                return
            except RuntimeError as error:
                # Cleanup failures remain fail-closed, even during shutdown.
                if str(error) != DISPLAY_REMAP_FAILURE:
                    raise
                if stopping:
                    return
                now = time.monotonic()
                if now - remap_window >= DISPLAY_REMAP_WINDOW_SECONDS:
                    remap_window, remap_count = now, 0
                remap_count += 1
                if remap_count > DISPLAY_REMAP_LIMIT:
                    raise
                trial.event('native_service_display_reconnect', attempt=remap_count)
                deadline = time.monotonic() + 1
                while not stopping and time.monotonic() < deadline:
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    finally:
        try:
            clipboard_bridge.stop()
            terminal_bridge.stop()
            # STOPPING puts systemd into a timed stopping state.  A worker
            # remap is not a service shutdown; only this owner may send it.
            trial.state_tools.notify('STOPPING=1\nSTATUS=UU native service stopped')
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--preserve-display-session', action='store_true',
                        help='Opt-in same-output video recreation candidate; requires a geometry-aware bundle')
    parser.add_argument('--allow-display-reconfigure', action='store_true',
                        help='Allow remote UU display calls to change the Linux/Mutter mode; installed services enable this')
    args = parser.parse_args()
    try:
        run(args.config, args.preserve_display_session, args.allow_display_reconfigure)
    except Exception as error:
        # No tracebacks: subprocess exceptions could contain private argv.
        trial.event('native_service_failed', error_type=type(error).__name__)
        raise SystemExit(1)
