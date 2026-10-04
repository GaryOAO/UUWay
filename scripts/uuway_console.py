#!/usr/bin/python3
"""UUWay's Linux control console.

The bridge is already Python/GTK on the host side, so the console deliberately
uses the same stack.  It only edits the owner-only JSON files under
``~/.config/uurb`` and talks to the existing display guardian socket; it never
reads UU credentials or vendor logs.
"""
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote

RELEASE_ROOT = Path(__file__).resolve().parent
ROOT = (RELEASE_ROOT.parent if (RELEASE_ROOT.parent / "assets").is_dir()
        else RELEASE_ROOT)
DEFAULT_IMAGE = (RELEASE_ROOT / "assets/uuway-penguin.bmp"
                 if (RELEASE_ROOT / "assets/uuway-penguin.bmp").is_file()
                 else ROOT / "assets/uuway-penguin.bmp")
BRAND_ICON = (RELEASE_ROOT / "assets/uuway-icon.svg"
              if (RELEASE_ROOT / "assets/uuway-icon.svg").is_file()
              else ROOT / "assets/uuway-icon.svg")
CONFIG_DIR = Path.home() / ".config/uurb"
DOWNLOAD_RELATIVE = Path("drive_c/Program Files/Netease/GameViewer/Download")
MAX_JSON = 65536
MAX_IMAGE = 16 * 1024 * 1024
MAX_DESKTOP_SEND_FILES = 256
MAX_DESKTOP_SEND_PATH = 4096
SERVICE_NAMES = {
    "uu-native-bridge.service": "远程连接",
    "uu-native-text.service": "文字兼容服务",
    "uu-native-display.service": "显示保护服务",
}


def _private_dir(path):
    """Validate a private directory, without following a symlink."""
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("配置目录权限不安全")


def _read_json(path):
    try:
        _private_dir(path.parent)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077 or info.st_size == 0 or info.st_size > MAX_JSON):
            raise ValueError("配置文件权限或大小不安全")
        return json.loads(stream.read(MAX_JSON + 1))


def _optional_json(path):
    try:
        return _read_json(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _write_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private_dir(path.parent)
    data = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    if len(data) > MAX_JSON:
        raise ValueError("配置过大")
    fd, temporary = tempfile.mkstemp(prefix=".uuway-console-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _runtime():
    try:
        value = _read_json(CONFIG_DIR / "native-runtime.json")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("schema") != 1:
        return None
    names = {"schema", "prefix", "bundle", "restore_state", "state_parent", "text_socket", "cursor_mode"}
    if set(value) != names or value.get("cursor_mode") not in {"embedded", "metadata", "composited"}:
        return None
    if any(not isinstance(value[name], str) or not Path(value[name]).is_absolute()
           for name in names - {"schema", "cursor_mode"}):
        return None
    return value


def _download_directory(home=None, config_home=None):
    home = Path.home() if home is None else Path(home)
    root = Path(config_home) if config_home else Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    try:
        for line in (root / "user-dirs.dirs").read_text().splitlines():
            if not line.startswith("XDG_DOWNLOAD_DIR="):
                continue
            value = line.split("=", 1)[1].strip().strip('"')
            if value == "$HOME":
                return home
            if value.startswith("$HOME/"):
                return home / value[6:]
            if value.startswith("/"):
                return Path(value)
            break
    except (OSError, UnicodeError):
        pass
    return home / "Downloads"


def _configured_download_directory():
    value = _optional_json(CONFIG_DIR / "download-directory.json")
    if (isinstance(value, dict) and set(value) == {"version", "path"}
            and value.get("version") == 1 and isinstance(value.get("path"), str)
            and value["path"].startswith("/") and "\0" not in value["path"]):
        return Path(value["path"]).resolve()
    return _download_directory().resolve()


def _desktop_image():
    try:
        value = _read_json(CONFIG_DIR / "desktop-image.json")
        path = Path(value["path"])
        if (isinstance(value, dict) and set(value) == {"version", "path"} and value["version"] == 1
                and isinstance(value["path"], str) and path.is_absolute() and path.is_file()
                and path.stat().st_size <= MAX_IMAGE):
            return path, False
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        pass
    return DEFAULT_IMAGE, True


def _desktop_file_uris(paths):
    """Validate selected Linux files and return clipboard-ready file URIs."""
    if not isinstance(paths, (list, tuple)) or not paths or len(paths) > MAX_DESKTOP_SEND_FILES:
        raise ValueError("请选择 1–256 个文件或目录")
    result = []
    for raw in paths:
        if not isinstance(raw, (str, os.PathLike)):
            raise ValueError("桌面发送路径无效")
        path = Path(raw)
        if not path.is_absolute() or len(str(path)) > MAX_DESKTOP_SEND_PATH:
            raise ValueError("桌面发送路径必须是短的绝对路径")
        try:
            resolved = path.resolve(strict=True)
            info = resolved.stat()
        except OSError as error:
            raise ValueError("桌面发送路径不可读取") from error
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError("只能发送普通文件或目录")
        # Path.as_uri handles the file:// form but leaves no room for a
        # malformed control character in a selected name.
        uri = "file://" + quote(str(resolved), safe="/:@")
        if len(uri) > MAX_DESKTOP_SEND_PATH * 3:
            raise ValueError("桌面发送路径过长")
        result.append(uri)
    return result


def _mapping_state():
    runtime = _runtime()
    destination = _configured_download_directory()
    if not runtime:
        return None, destination, False
    source = Path(runtime["prefix"]) / DOWNLOAD_RELATIVE
    try:
        return source, destination, source.is_symlink() and source.resolve() == destination
    except OSError:
        return source, destination, False


def _xdg_user_directory(kind, home=None, config_home=None):
    home = Path.home() if home is None else Path(home)
    root = Path(config_home) if config_home else Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    defaults = {
        "DESKTOP": "Desktop", "DOCUMENTS": "Documents", "DOWNLOAD": "Downloads",
        "MUSIC": "Music", "PICTURES": "Pictures", "VIDEOS": "Videos",
        "PUBLICSHARE": "Public", "TEMPLATES": "Templates",
    }
    key = f"XDG_{kind}_DIR="
    try:
        for line in (root / "user-dirs.dirs").read_text().splitlines():
            if not line.startswith(key):
                continue
            value = line.split("=", 1)[1].strip().strip('"')
            if value == "$HOME":
                return home
            if value.startswith("$HOME/"):
                return home / value[6:]
            if value.startswith("/"):
                return Path(value)
            break
    except (OSError, UnicodeError):
        pass
    return home / defaults.get(kind, kind.title())


def _mapping_rows(runtime):
    """Return drive, profile and receive mappings for the console."""
    if not runtime:
        return []
    prefix = Path(runtime["prefix"])
    rows = []
    dosdevices = prefix / "dosdevices"
    try:
        entries = sorted(dosdevices.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        entries = []
    for entry in entries:
        is_drive = len(entry.name) == 2 and entry.name[1] == ":"
        is_device = len(entry.name) == 3 and entry.name[1:] == "::"
        if (is_drive or is_device) and entry.is_symlink():
            try:
                target = entry.resolve(strict=False)
            except OSError:
                target = Path(os.readlink(entry))
            name = entry.name[:2].upper() if is_device else entry.name.upper()
            rows.append((f"{name} 设备" if is_device else name, str(target), True))

    users = prefix / "drive_c/users"
    preferred = Path.home().name
    user_root = users / preferred
    if not user_root.is_dir():
        try:
            candidates = [item for item in users.iterdir() if item.is_dir() and
                          item.name.lower() not in {"public", "default", "default user", "all users"}]
        except OSError:
            candidates = []
        if len(candidates) == 1:
            user_root = candidates[0]
    xdg_names = (
        ("Desktop", "DESKTOP"), ("Documents", "DOCUMENTS"), ("Downloads", "DOWNLOAD"),
        ("Music", "MUSIC"), ("Pictures", "PICTURES"), ("Videos", "VIDEOS"),
        ("Public", "PUBLICSHARE"), ("Templates", "TEMPLATES"),
    )
    for windows_name, xdg_name in xdg_names:
        source = user_root / windows_name
        target = _configured_download_directory() if xdg_name == "DOWNLOAD" else _xdg_user_directory(xdg_name)
        try:
            mapped = source.is_symlink() and source.resolve() == target.resolve()
            exists = source.exists() or source.is_symlink()
        except OSError:
            mapped, exists = False, False
        rows.append((f"用户/{windows_name}", str(target), mapped if exists else None))
    source, destination, mapped = _mapping_state()
    if source:
        rows.append(("UU 接收/Download", str(destination), mapped))
    return rows


def _service_properties():
    units = ("uu-native-bridge.service", "uu-native-text.service", "uu-native-display.service")
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "--user", "show", *units,
             "--property=Id,ActiveState,SubState,MainPID,NRestarts"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    sections = {}
    for section in result.stdout.split("\n\n"):
        fields = dict(line.split("=", 1) for line in section.splitlines() if "=" in line)
        if fields.get("Id"):
            sections[fields["Id"]] = fields
    return sections


def _service_label(fields):
    state = {"active": "运行中", "activating": "启动中", "failed": "失败", "inactive": "已停止"}
    return f"{state.get(fields.get('ActiveState'), '未知')} · PID {fields.get('MainPID', '0')} · 自动重启 {fields.get('NRestarts', '0')} 次"


def _connection_state(runtime, services):
    """Describe the local service without claiming a controller is connected."""
    if not runtime:
        return ("先完成本机安装", "尚未找到运行配置。完成 UUWay 安装后，再回到这里启动服务。", "warning", "start")
    state = services.get("uu-native-bridge.service", {}).get("ActiveState")
    return {
        "active": ("远程服务已就绪", "打开手机上的 UU 远程，选择这台 Linux 设备开始连接。", "success", "restart"),
        "inactive": ("远程服务未启动", "启动后，即可从 UU 远程访问这台 Linux 设备。", "neutral", "start"),
        "failed": ("远程服务需要处理", "可以尝试重新启动；如果仍然失败，请到诊断页查看服务状态。", "error", "restart"),
        "activating": ("正在启动远程服务", "服务正在准备，请稍候。状态会自动更新。", "warning", None),
        "deactivating": ("正在停止远程服务", "当前连接正在结束，请稍候。", "warning", None),
    }.get(state, ("暂时无法读取状态", "请刷新状态，或到诊断页查看本机服务。", "warning", None))


def _collect_snapshot():
    runtime = _runtime()
    result = {"runtime": runtime, "services": _service_properties(),
              "mappings": _mapping_rows(runtime), "download": _configured_download_directory(),
              "display": None, "display_error": None}
    try:
        result["display"] = _display_request(runtime, {"version": 1, "op": "inspect"})
    except (OSError, ValueError, KeyError, TypeError) as error:
        result["display_error"] = str(error)
    return result


def _publish_file_uris(uris):
    # xclip is already installed for UUWay's desktop/Xwayland clipboard bridge.
    # Gtk.Clipboard has no introspected set_uris method in GTK 3.
    try:
        result = subprocess.run(
            ["/usr/bin/xclip", "-selection", "clipboard", "-t", "text/uri-list", "-i"],
            input=("\r\n".join(uris) + "\r\n").encode(), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=3, check=False)
    except FileNotFoundError as error:
        raise ValueError("缺少 xclip，请重新运行 UUWay 安装程序补齐依赖") from error
    if result.returncode:
        raise ValueError("无法写入桌面剪贴板，请确认本机桌面已登录")


def _display_request(runtime, request):
    if not runtime:
        raise ValueError("运行配置不可用")
    path = Path(runtime["state_parent"]) / "display.sock"
    _private_dir(path.parent)
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("显示服务接口不可核实")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | getattr(socket, "SOCK_CLOEXEC", 0))
    try:
        sock.settimeout(3)
        sock.connect(str(path))
        sock.send(json.dumps(request, separators=(",", ":")).encode())
        response = json.loads(sock.recv(65536))
        if response.get("ok") is not True:
            raise ValueError("显示服务拒绝请求")
        return response
    finally:
        sock.close()


def collect_status():
    lines = ["UUWay 状态与能力", ""]
    names = {
        "uu-native-bridge.service": "主服务",
        "uu-native-text.service": "文本兼容服务",
        "uu-native-display.service": "显示回滚服务",
    }
    services = _service_properties()
    lines.append("服务")
    for unit, title in names.items():
        lines.append(f"  {title}：{_service_label(services[unit]) if unit in services else '未找到'}")
    runtime = _runtime()
    lines.append("")
    lines.append("文件接收")
    source, destination, mapped = _mapping_state()
    lines.append(f"  Linux 下载目录：{destination}")
    if source:
        lines.append(f"  UU Windows 接收目录：{source}")
        lines.append(f"  目录映射：{'已启用' if mapped else '待服务启动后建立'}")
    else:
        lines.append("  目录映射：运行配置不可用")
    lines.append("")
    lines.append("Linux 原生路径映射")
    mapping_rows = _mapping_rows(runtime)
    if mapping_rows:
        for name, target, mapped in mapping_rows:
            state = "已连接" if mapped else ("待建立" if mapped is None else "需修复")
            lines.append(f"  {name} → {target} · {state}")
    else:
        lines.append("  运行配置不可用")
    lines.append("")
    lines.append("桌面照片")
    image, is_default = _desktop_image()
    lines.append(f"  {'默认 Linux 企鹅' if is_default else '自定义图片'}：{image}")
    lines.append("")
    lines.append("可配置能力")
    if runtime:
        input_settings = _optional_json(CONFIG_DIR / "input-settings.json")
        if isinstance(input_settings, dict) and input_settings.get("version") == 1:
            lines.append(f"  输入：相对速度 {input_settings.get('relative_percent', 100)}% · 滚轮 {input_settings.get('wheel_percent', 100)}% · {'反向' if input_settings.get('invert_wheel') else '正常'}")
        else:
            lines.append("  输入：使用默认速度")
        cursor_names = {"embedded": "视频内真实光标", "metadata": "独立光标元数据", "composited": "GPU 合成真实光标"}
        lines.append(f"  光标：{cursor_names.get(runtime['cursor_mode'], '未知')}")
        try:
            backend = _optional_json(CONFIG_DIR / "text-backend.json")
        except (ValueError, json.JSONDecodeError):
            backend = None
        backend_name = "原生 Fcitx5" if isinstance(backend, dict) and backend.get("backend") == "fcitx" else "Portal"
        lines.append(f"  手机文字：{backend_name}")
        try:
            display = _display_request(runtime, {"version": 1, "op": "inspect"})
            lines.append(f"  显示：{display['width']} × {display['height']} · {display['refresh']:.2f} Hz · 缩放 {display['scale'] * 100:.0f}%")
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            lines.append("  显示：显示回滚服务尚未提供可核实状态")
    else:
        lines.append("  运行配置不可用；服务启动后可配置输入、文字、光标和显示")
    lines.extend(("", "能力边界", "  主服务、文本服务、显示回滚、剪贴板桥、终端桥、文件接收目录映射和桌面图片映射均由 UUWay 管理。", "  本页不显示账号、令牌、剪贴板内容或原始厂商日志。"))
    return "\n".join(lines)


class Console:
    """Native GTK shell; blocking host operations stay off the UI thread."""

    def __init__(self, Gtk, GLib, Gio):
        self.Gtk, self.GLib, self.Gio = Gtk, GLib, Gio
        self.window = None
        self.closed = False
        self.refreshing = self.service_busy = self.display_busy = False
        self.refresh_pending = self.force_display_pending = False
        self.snapshot = None
        self.display_state = self.display_transaction = None
        self.display_deadline = 0
        self.display_generation = 0
        self.display_dirty = self.updating_display = False
        self.input_original = None
        self.restart_reasons = set()
        self.service_labels = {}
        self.nav_rows = {}
        self.nav_labels = []
        self.timers = []
        self.report = ""
        self.compact = None

    @staticmethod
    def style(widget, name):
        widget.get_style_context().add_class(name)
        return widget

    def label(self, text, name="body", wrap=True):
        from gi.repository import Pango
        item = self.Gtk.Label(label=text, xalign=0)
        item.set_line_wrap(wrap)
        item.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        item.set_max_width_chars(48)
        return self.style(item, name)

    def box(self, vertical=True, spacing=0, name=None):
        item = self.Gtk.Box(orientation=(self.Gtk.Orientation.VERTICAL if vertical
                                       else self.Gtk.Orientation.HORIZONTAL), spacing=spacing)
        return self.style(item, name) if name else item

    def button(self, text, callback, name="secondary", icon=None):
        item = self.style(self.Gtk.Button(), name)
        if icon:
            content = self.box(False, 8)
            content.set_halign(self.Gtk.Align.CENTER)
            content.pack_start(self.Gtk.Image.new_from_icon_name(icon, self.Gtk.IconSize.BUTTON), False, False, 0)
            content.pack_start(self.label(text, wrap=False), False, False, 0)
            item.add(content)
        else:
            item.set_label(text)
        item.connect("clicked", callback)
        return item

    def actions(self, *buttons):
        row = self.box(False, 10)
        for button in buttons:
            row.pack_start(button, False, False, 0)
        return row

    def card(self, title=None, subtitle=None):
        item = self.box(spacing=16, name="card")
        if title:
            heading = self.box(spacing=5)
            heading.pack_start(self.label(title, "section-title"), False, False, 0)
            if subtitle:
                heading.pack_start(self.label(subtitle, "muted"), False, False, 0)
            item.pack_start(heading, False, False, 0)
        return item

    def setting_row(self, parent, title, hint, control):
        row = self.box(False, 20, "setting-row")
        text = self.box(spacing=4)
        text.set_hexpand(True)
        text.pack_start(self.label(title, "row-title"), False, False, 0)
        if hint:
            text.pack_start(self.label(hint, "muted"), False, False, 0)
        control.set_valign(self.Gtk.Align.CENTER)
        row.pack_start(text, True, True, 0)
        row.pack_end(control, False, False, 0)
        parent.pack_start(row, False, False, 0)
        return row

    def separator(self, parent):
        parent.pack_start(self.Gtk.Separator(orientation=self.Gtk.Orientation.HORIZONTAL), False, False, 0)

    def page(self, name, title, description):
        content = self.box(spacing=22)
        content.set_margin_start(32)
        content.set_margin_end(32)
        content.set_margin_top(30)
        content.set_margin_bottom(32)
        heading = self.box(spacing=7)
        heading.pack_start(self.label(title, "page-title"), False, False, 0)
        heading.pack_start(self.label(description, "page-description"), False, False, 0)
        content.pack_start(heading, False, False, 0)
        scroll = self.Gtk.ScrolledWindow()
        scroll.set_policy(self.Gtk.PolicyType.NEVER, self.Gtk.PolicyType.AUTOMATIC)
        scroll.add(content)
        self.stack.add_named(scroll, name)
        return content

    def navigate(self, name):
        self.nav.select_row(self.nav_rows[name])

    def _async(self, work, done):
        def worker():
            try:
                value, error = work(), None
            except Exception as problem:
                value, error = None, problem
            def deliver():
                if not self.closed:
                    done(value, error)
                return False
            self.GLib.idle_add(deliver)
        threading.Thread(target=worker, daemon=True).start()

    def message(self, text, kind="success"):
        self.notice.set_text(text)
        context = self.notice_box.get_style_context()
        for name in ("success", "warning", "error"):
            context.remove_class(name)
        context.add_class(kind)
        self.notice_revealer.set_reveal_child(True)

    def _needs_restart(self, reason):
        self.restart_reasons.add(reason)
        self.restart_label.set_text("已保存：" + "、".join(sorted(self.restart_reasons)) + "。重启服务后生效。")
        self.restart_revealer.set_reveal_child(True)

    def refresh(self, force_display=False):
        if self.closed:
            return
        if self.refreshing:
            self.refresh_pending = True
            self.force_display_pending |= force_display
            return
        self.refreshing = True
        self.refresh_button.set_sensitive(False)
        generation = self.display_generation
        def done(value, error):
            self.refreshing = False
            self.refresh_button.set_sensitive(True)
            if self.refresh_pending:
                force = self.force_display_pending
                self.refresh_pending = self.force_display_pending = False
                self.GLib.idle_add(lambda: self.refresh(force))
            if error:
                self.header_status.set_text("状态读取失败")
                self.message(f"无法刷新状态：{error}", "error")
                return
            self.snapshot = value
            title, hint, tone, operation = _connection_state(value["runtime"], value["services"])
            self.connection_title.set_text(title)
            self.connection_hint.set_text(hint)
            self.header_status.set_text({"success": "主服务运行中", "neutral": "主服务已停止",
                                         "error": "服务异常", "warning": "等待就绪"}[tone])
            self.hero_badge.set_text({"success": "●  服务就绪", "neutral": "○  服务停止",
                                      "error": "!  需要处理", "warning": "◌  等待就绪"}[tone])
            self.primary_operation = operation
            self.connect_button.set_label("重新连接" if operation == "restart" else "启动服务")
            self.connect_button.set_sensitive(bool(value["runtime"] and operation and not self.service_busy))
            for unit, (state_label, detail_label) in self.service_labels.items():
                fields = value["services"].get(unit, {})
                state = fields.get("ActiveState")
                state_label.set_text({"active": "运行中", "inactive": "已停止", "failed": "运行失败",
                                      "activating": "启动中", "deactivating": "停止中"}.get(state, "不可用"))
                context = state_label.get_style_context()
                for name in ("good-text", "bad-text", "muted"):
                    context.remove_class(name)
                context.add_class("good-text" if state == "active" else "bad-text" if state == "failed" else "muted")
                detail_label.set_text(f"PID {fields.get('MainPID', '—')}  ·  自动重启 {fields.get('NRestarts', '0')} 次")
            backend = _optional_json(CONFIG_DIR / "text-backend.json") or {}
            self.text_summary.set_text("Fcitx5" if isinstance(backend, dict) and backend.get("backend") == "fcitx" else "Portal")
            self.download_label.set_text(str(value["download"]))
            self._update_mapping_label(value["mappings"])
            if generation == self.display_generation and not self.display_busy:
                self._show_display(value["display"], force_display)
            self.stop_button.set_sensitive(not self.service_busy and operation == "restart")
            self.restart_button.set_sensitive(not self.service_busy and bool(value["runtime"]))
        self._async(_collect_snapshot, done)

    def _update_mapping_label(self, rows=None):
        if rows is None:
            self.refresh()
            return
        for child in self.mapping_box.get_children():
            self.mapping_box.remove(child)
        if not rows:
            self.mapping_box.pack_start(self.label("完成安装并启动服务后，这里会显示 Linux 目录映射。", "muted"), False, False, 0)
        for name, target, mapped in rows:
            row = self.box(spacing=5)
            top = self.box(False, 12)
            top.pack_start(self.label(name, "row-title"), True, True, 0)
            top.pack_end(self.label("已连接" if mapped else "待建立" if mapped is None else "需修复",
                                    "good-text" if mapped else "muted"), False, False, 0)
            row.pack_start(top, False, False, 0)
            path = self.label(target, "path")
            path.set_selectable(True)
            row.pack_start(path, False, False, 0)
            self.mapping_box.pack_start(row, False, False, 0)
        self.mapping_box.show_all()

    def _confirm(self, title, detail, accept):
        Gtk = self.Gtk
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True,
                                   message_type=Gtk.MessageType.WARNING, text=title)
        dialog.format_secondary_text(detail)
        dialog.add_button("取消", Gtk.ResponseType.CANCEL)
        dialog.add_button(accept, Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        self.style(dialog, "uuway")
        result = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        return result

    def service_action(self, button, operation):
        if self.service_busy or operation not in ("start", "restart", "stop"):
            return
        if operation in ("stop", "restart"):
            detail = ("当前远程连接会断开。之后需要在本机或通过其他远程通道重新启动 UUWay。"
                      if operation == "stop" else "当前远程连接会短暂断开。已保存的设置会在服务重新启动后生效。")
            if not self._confirm("停止远程服务？" if operation == "stop" else "重新启动远程服务？",
                                 detail, "停止服务" if operation == "stop" else "重启服务"):
                return
        self.service_busy = True
        for item in (self.connect_button, self.stop_button, self.restart_button):
            item.set_sensitive(False)
        self.message("正在处理服务，请稍候…", "warning")
        def work():
            result = subprocess.run(["/usr/bin/systemctl", "--user", operation, "uu-native-bridge.service"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=45, check=False)
            if result.returncode:
                raise ValueError("请到诊断页查看服务状态")
        def done(value, error):
            self.service_busy = False
            if error:
                self.message(f"服务操作未完成：{error}", "error")
            else:
                if operation in ("start", "restart"):
                    self.restart_reasons.clear()
                    self.restart_revealer.set_reveal_child(False)
                self.message("服务已停止。" if operation == "stop" else "服务已启动，正在更新状态。")
            self.refresh()
        self._async(work, done)

    def _input_values(self):
        return (int(self.relative.get_value()), int(self.wheel.get_value()), self.invert.get_active())

    def _input_changed(self, *_):
        dirty = self._input_values() != self.input_original
        self.input_save.set_sensitive(dirty)
        self.input_hint.set_text("有未保存的更改" if dirty else "保存后立即生效")

    def reset_input(self, *_):
        self.relative.set_value(100)
        self.wheel.set_value(100)
        self.invert.set_active(False)

    def save_input(self, *_):
        relative, wheel, invert = self._input_values()
        try:
            _write_json(CONFIG_DIR / "input-settings.json", {"version": 1, "relative_percent": relative,
                                                            "wheel_percent": wheel, "invert_wheel": invert})
            self.input_original = self._input_values()
            self._input_changed()
            self.message("鼠标与滚轮设置已保存，无需重启。")
        except (OSError, ValueError) as error:
            self.message(f"输入设置保存失败：{error}", "error")

    def save_cursor(self, *_):
        if self.loading_preferences:
            return
        runtime = _runtime()
        if not runtime:
            self.message("请先完成安装，再设置光标显示方式。", "warning")
            self._restore_preference(self.cursor, self.cursor_saved)
            return
        try:
            runtime["cursor_mode"] = ("embedded", "metadata", "composited")[self.cursor.get_active()]
            _write_json(CONFIG_DIR / "native-runtime.json", runtime)
            self.cursor_saved = self.cursor.get_active()
            self._needs_restart("光标显示")
        except (OSError, ValueError) as error:
            self._restore_preference(self.cursor, self.cursor_saved)
            self.message(f"光标设置保存失败：{error}", "error")

    def save_backend(self, *_):
        if self.loading_preferences:
            return
        try:
            _write_json(CONFIG_DIR / "text-backend.json", {"version": 1, "backend": "fcitx" if self.backend.get_active() == 1 else "portal"})
            self.backend_saved = self.backend.get_active()
            self._needs_restart("文字输入")
        except (OSError, ValueError) as error:
            self._restore_preference(self.backend, self.backend_saved)
            self.message(f"文字输入设置保存失败：{error}", "error")

    def _restore_preference(self, widget, value):
        self.loading_preferences = True
        widget.set_active(value)
        self.loading_preferences = False

    def _set_image_preview(self, path):
        from gi.repository import GdkPixbuf
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 384, 216, True)
            self.image_preview.set_from_pixbuf(pixbuf)
        except Exception:
            self.image_preview.set_from_icon_name("image-missing", self.Gtk.IconSize.DIALOG)

    def choose_image(self, *_):
        Gtk = self.Gtk
        dialog = Gtk.FileChooserDialog(title="选择设备封面", transient_for=self.window,
            action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "使用图片", Gtk.ResponseType.OK)
        image_filter = Gtk.FileFilter()
        image_filter.set_name("图片文件")
        image_filter.add_mime_type("image/*")
        dialog.add_filter(image_filter)
        try:
            if dialog.run() == Gtk.ResponseType.OK:
                path = Path(dialog.get_filename()).resolve()
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_IMAGE:
                    raise ValueError("图片须小于 16 MiB")
                from gi.repository import GdkPixbuf
                if not GdkPixbuf.Pixbuf.get_file_info(str(path))[0]:
                    raise ValueError("无法读取这张图片")
                _write_json(CONFIG_DIR / "desktop-image.json", {"version": 1, "path": str(path)})
                self.image_label.set_text(path.name)
                self.image_label.set_tooltip_text(str(path))
                self._set_image_preview(path)
                self._needs_restart("设备封面")
        except (OSError, ValueError) as error:
            self.message(f"封面未更换：{error}", "error")
        finally:
            dialog.destroy()

    def reset_image(self, *_):
        try:
            (CONFIG_DIR / "desktop-image.json").unlink(missing_ok=True)
            self.image_label.set_text("UUWay · 默认 Linux 企鹅")
            self.image_label.set_tooltip_text(None)
            self._set_image_preview(DEFAULT_IMAGE)
            self._needs_restart("设备封面")
        except OSError as error:
            self.message(f"恢复封面失败：{error}", "error")

    def send_desktop_files(self, button, folders=False):
        Gtk = self.Gtk
        dialog = Gtk.FileChooserDialog(title="选择文件夹" if folders else "选择要发送的文件",
            transient_for=self.window, action=Gtk.FileChooserAction.SELECT_FOLDER if folders else Gtk.FileChooserAction.OPEN)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "准备发送", Gtk.ResponseType.OK)
        dialog.set_select_multiple(True)
        try:
            if dialog.run() != Gtk.ResponseType.OK:
                return
            uris = _desktop_file_uris(dialog.get_filenames())
        except (OSError, ValueError) as error:
            self.message(f"无法选择项目：{error}", "error")
            return
        finally:
            dialog.destroy()
        self._async(lambda: _publish_file_uris(uris), lambda value, error: self.message(
            f"发送准备失败：{error}" if error else f"已准备 {len(uris)} 个项目，请到手机端粘贴或接收。",
            "error" if error else "success"))

    def choose_download_directory(self, *_):
        Gtk = self.Gtk
        dialog = Gtk.FileChooserDialog(title="选择接收目录", transient_for=self.window,
                                      action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "使用此目录", Gtk.ResponseType.OK)
        dialog.set_current_folder(str(_configured_download_directory()))
        try:
            if dialog.run() == Gtk.ResponseType.OK:
                path = Path(dialog.get_filename()).resolve()
                if not path.is_dir():
                    raise ValueError("请选择一个已有目录")
                _write_json(CONFIG_DIR / "download-directory.json", {"version": 1, "path": str(path)})
                self.download_label.set_text(str(path))
                self._needs_restart("接收目录")
                self.refresh()
        except (OSError, ValueError) as error:
            self.message(f"目录未更改：{error}", "error")
        finally:
            dialog.destroy()

    def reset_download_directory(self, *_):
        try:
            (CONFIG_DIR / "download-directory.json").unlink(missing_ok=True)
            self.download_label.set_text(str(_configured_download_directory()))
            self._needs_restart("接收目录")
            self.refresh()
        except OSError as error:
            self.message(f"恢复接收目录失败：{error}", "error")

    def open_download_directory(self, *_):
        try:
            self.Gio.AppInfo.launch_default_for_uri(_configured_download_directory().as_uri(), None)
        except Exception as error:
            self.message(f"无法打开接收目录：{error}", "error")

    def generate_report(self, button):
        button.set_sensitive(False)
        def done(value, error):
            button.set_sensitive(True)
            if error:
                self.message(f"无法生成报告：{error}", "error")
                return
            self.report = value
            self.status.set_text(value)
            self.report_copy.set_sensitive(True)
            self.report_expander.set_expanded(True)
        self._async(collect_status, done)

    def copy_report(self, *_):
        from gi.repository import Gdk
        clipboard = self.Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(self.report, -1)
        clipboard.store()
        self.message("诊断报告已复制。")

    def display_inspect(self, *_):
        self.refresh(force_display=True)

    def _show_display(self, value, force=False):
        if value is None:
            self.display_state = None
            self.display_current.set_text("暂时无法读取显示器")
            self.display_summary.set_text("未就绪")
            self.display_detail.set_text("启动远程服务后重试；多显示器或未登录桌面也可能导致模式不可用。")
            self.display_modes.set_sensitive(False)
            self.display_scales.set_sensitive(False)
            self.display_apply_button.set_sensitive(False)
            return
        self.display_current.set_text(f"{value['width']} × {value['height']}")
        self.display_detail.set_text(f"{value['refresh']:g} Hz  ·  {value['scale'] * 100:g}% 缩放")
        self.display_summary.set_text(f"{value['width']} × {value['height']}")
        if self.display_transaction and not value.get("pending"):
            self.display_transaction = None
            self.confirm_revealer.set_reveal_child(False)
        if not force and value == self.display_state:
            self._display_changed()
            return
        old_choice = None
        if self.display_dirty and self.display_state and not force:
            old_choice = self._display_choice()
        self.display_state = value
        self.updating_display = True
        self.display_modes.remove_all()
        selected = -1
        for index, mode in enumerate(value.get("modes", [])):
            self.display_modes.append_text(f"{mode['width']} × {mode['height']}  ·  {mode['refresh']:g} Hz")
            choice = old_choice if old_choice else value
            if all(abs(mode[key] - choice[key]) < .001 for key in ("width", "height", "refresh")):
                selected = index
        self.display_modes.set_active(selected if selected >= 0 else (0 if value.get("modes") else -1))
        self.display_scale_options(preferred=old_choice.get("scale") if old_choice else None)
        self.updating_display = False
        enabled = not self.display_busy and not value.get("pending")
        self.display_modes.set_sensitive(enabled)
        self.display_scales.set_sensitive(enabled)
        self._tick_confirmation()
        self._display_changed()

    def display_scale_options(self, combo=None, preferred=None):
        if not self.display_state:
            return
        index = self.display_modes.get_active()
        modes = self.display_state.get("modes", [])
        self.display_scales.remove_all()
        if not 0 <= index < len(modes):
            return
        selected = 0
        target = self.display_state["scale"] if preferred is None else preferred
        for index, scale in enumerate(modes[index].get("scales", [])):
            self.display_scales.append_text(f"{scale * 100:g}%")
            if abs(scale - target) < .001:
                selected = index
        self.display_scales.set_active(selected)
        self._display_changed()

    def _display_choice(self):
        if not self.display_state:
            return None
        mode_index, scale_index = self.display_modes.get_active(), self.display_scales.get_active()
        modes = self.display_state.get("modes", [])
        if not 0 <= mode_index < len(modes) or not 0 <= scale_index < len(modes[mode_index].get("scales", [])):
            return None
        mode = modes[mode_index]
        return {key: mode[key] for key in ("width", "height", "refresh")} | {"scale": mode["scales"][scale_index]}

    def _display_changed(self, *_):
        if self.updating_display:
            return
        choice = self._display_choice()
        self.display_dirty = bool(choice and any(abs(choice[key] - self.display_state[key]) > .001 for key in choice))
        self.display_apply_button.set_sensitive(bool(self.display_dirty and not self.display_busy and
                                                    not self.display_state.get("pending")))

    def display_apply(self, *_):
        choice = self._display_choice()
        if not choice or self.display_busy or not self.display_dirty:
            return
        if not self._confirm("试用新的显示设置？", "画面可能短暂中断。切换后有 30 秒确认时间；未确认会自动恢复。", "开始试用"):
            return
        # The console requests manual confirmation. Automated GPU-frame
        # confirmation is reserved for changes made through the remote client.
        request = {"version": 1, "op": "apply", "serial": self.display_state["serial"], **choice}
        def done(value):
            self.display_transaction = value.get("transaction")
            self.display_deadline = time.monotonic() + value.get("confirmation_seconds", 30)
            self.confirm_revealer.set_reveal_child(bool(self.display_transaction))
            self.display_dirty = False
            self.message("请检查画面与点击位置，再选择保留设置。" if self.display_transaction else "当前已是这个显示模式。")
            self._tick_confirmation()
        self._display_operation(request, done)

    def _display_operation(self, request, success):
        self.display_busy = True
        self.display_generation += 1
        self.display_state = None
        for item in (self.display_apply_button, self.display_modes, self.display_scales, self.keep_button, self.revert_button):
            item.set_sensitive(False)
        runtime = _runtime()
        def done(value, error):
            self.display_busy = False
            if error:
                self.message(f"显示操作未完成：{error}", "error")
            else:
                success(value)
            self.keep_button.set_sensitive(False)
            self.revert_button.set_sensitive(bool(self.display_transaction))
            self.refresh(force_display=True)
        self._async(lambda: _display_request(runtime, request), done)

    def display_finish(self, button, operation):
        if not self.display_transaction or self.display_busy:
            return
        request = {"version": 1, "op": operation, "transaction": self.display_transaction}
        if operation == "confirm":
            if not self.display_state:
                return
            request["serial"] = self.display_state["serial"]
        def done(value):
            self.display_transaction = None
            self.confirm_revealer.set_reveal_child(False)
            self.message("新的显示设置已保留。" if operation == "confirm" else "已恢复之前的显示设置。")
        self._display_operation(request, done)

    def _tick_confirmation(self):
        if self.display_transaction:
            remaining = max(0, int(self.display_deadline - time.monotonic() + .999))
            self.confirm_label.set_text(f"画面正常吗？{remaining} 秒后自动恢复" if remaining else "正在等待显示服务恢复…")
            self.keep_button.set_sensitive(remaining > 0 and not self.display_busy and self.display_state is not None)
        return not self.closed

    def _close_requested(self, *_):
        if self.input_original is not None and self._input_values() != self.input_original:
            return not self._confirm("放弃未保存的输入设置？", "鼠标与滚轮的改动尚未保存。其他已保存设置不受影响。", "放弃并关闭")
        return False

    def _destroy(self, *_):
        self.closed = True
        for source in self.timers:
            self.GLib.source_remove(source)

    def build(self, application):
        Gtk = self.Gtk
        self.window = Gtk.ApplicationWindow(application=application, title="UUWay 控制台")
        self.style(self.window, "uuway")
        self.window.set_default_size(1120, 800)
        self.window.set_size_request(780, 600)
        self.window.set_icon_from_file(str(BRAND_ICON))
        self._install_styles()
        header = Gtk.HeaderBar(title="UUWay 控制台")
        header.set_show_close_button(True)
        self.window.set_titlebar(header)
        self.header_status = self.label("正在读取状态", "muted", False)
        header.pack_start(self.header_status)
        self.refresh_button = self.button("刷新", lambda *_: self.refresh(), "quiet", "view-refresh-symbolic")
        self.refresh_button.set_tooltip_text("立即刷新；状态每 5 秒自动更新")
        header.pack_end(self.refresh_button)

        shell = self.box(False)
        self.window.add(shell)
        self.sidebar = self.box(spacing=26, name="sidebar")
        self.sidebar.set_size_request(186, -1)
        brand = self.box(False, 10)
        brand.set_margin_top(28)
        brand.set_margin_start(24)
        brand.set_margin_bottom(12)
        from gi.repository import GdkPixbuf
        brand.pack_start(Gtk.Image.new_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_scale(str(BRAND_ICON), 34, 34, True)), False, False, 0)
        self.brand_word = self.label("UUWay", "brand", False)
        brand.pack_start(self.brand_word, False, False, 0)
        self.sidebar.pack_start(brand, False, False, 0)
        self.nav = self.style(Gtk.ListBox(), "navigation")
        self.nav.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.sidebar.pack_start(self.nav, False, False, 0)
        self.sidebar_footer = self.label("LINUX. ANYWHERE.\n本机控制台", "sidebar-footer")
        self.sidebar_footer.set_margin_start(25)
        self.sidebar_footer.set_margin_bottom(28)
        self.sidebar.pack_end(self.sidebar_footer, False, False, 0)
        shell.pack_start(self.sidebar, False, False, 0)

        main = self.box()
        main.set_hexpand(True)
        shell.pack_start(main, True, True, 0)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(140)
        self.stack.set_hhomogeneous(False)
        self.stack.set_vhomogeneous(False)
        main.pack_start(self.stack, True, True, 0)

        pages = (("overview", "概览", "view-grid-symbolic"),
                 ("display", "显示与外观", "video-display-symbolic"),
                 ("input", "鼠标与输入", "input-mouse-symbolic"),
                 ("files", "文件传输", "folder-symbolic"),
                 ("diagnostics", "诊断", "utilities-system-monitor-symbolic"))
        for name, title, icon in pages:
            row = Gtk.ListBoxRow()
            row.set_name(name)
            row.set_tooltip_text(title)
            inner = self.box(False, 13)
            inner.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON), False, False, 0)
            text = self.label(title, "nav-label", False)
            self.nav_labels.append(text)
            inner.pack_start(text, False, False, 0)
            row.add(inner)
            self.nav.add(row)
            self.nav_rows[name] = row
        self.nav.connect("row-selected", lambda _, row: self.stack.set_visible_child_name(row.get_name()) if row else None)

        self._build_overview()
        self._build_display()
        self._build_input()
        self._build_files()
        self._build_diagnostics()

        self.restart_revealer = Gtk.Revealer()
        restart = self.box(False, 16, "restart-bar")
        self.restart_label = self.label("", "body")
        restart.pack_start(self.restart_label, True, True, 0)
        self.restart_button = self.button("重启以应用", lambda b: self.service_action(b, "restart"), "primary")
        restart.pack_end(self.restart_button, False, False, 0)
        self.restart_revealer.add(restart)
        main.pack_start(self.restart_revealer, False, False, 0)

        self.notice_revealer = Gtk.Revealer()
        self.notice_box = self.box(False, 12, "notice")
        self.notice = self.label("")
        self.notice_box.pack_start(self.notice, True, True, 0)
        dismiss = self.button("关闭", lambda *_: self.notice_revealer.set_reveal_child(False), "quiet")
        self.notice_box.pack_end(dismiss, False, False, 0)
        self.notice_revealer.add(self.notice_box)
        main.pack_start(self.notice_revealer, False, False, 0)
        self.window.connect("delete-event", self._close_requested)
        self.window.connect("destroy", self._destroy)
        self.window.connect("size-allocate", self._resize)
        self.window.show_all()
        self.navigate("overview")
        self.refresh()
        self.timers.append(self.GLib.timeout_add_seconds(5, self._poll))
        self.timers.append(self.GLib.timeout_add_seconds(1, self._tick_confirmation))

    def _poll(self):
        self.refresh()
        return not self.closed

    def _resize(self, window, allocation):
        compact = allocation.width < 960
        if compact == self.compact:
            return
        self.compact = compact
        # Mutating requisitions inside size-allocate drops GTK's resize pass.
        def update():
            if self.closed:
                return False
            self.sidebar.set_size_request(76 if self.compact else 186, -1)
            for label in self.nav_labels + [self.brand_word, self.sidebar_footer]:
                label.set_visible(not self.compact)
            return False
        self.GLib.idle_add(update)

    def _build_overview(self):
        page = self.page("overview", "连接概览", "你的 Linux，随时连接。")
        hero = self.box(False, 24, "connection-card")
        text = self.box(spacing=14)
        self.hero_badge = self.label("◌  正在检查", "hero-badge")
        self.connection_title = self.label("正在读取服务状态", "hero-title")
        self.connection_hint = self.label("稍候，即将显示本机的连接状态。", "hero-description")
        for widget in (self.hero_badge, self.connection_title, self.connection_hint):
            text.pack_start(widget, False, False, 0)
        self.primary_operation = None
        self.connect_button = self.button("启动服务", lambda b: self.service_action(b, self.primary_operation), "hero-action")
        self.connect_button.set_sensitive(False)
        self.connect_button.set_halign(self.Gtk.Align.START)
        self.connect_button.set_margin_top(8)
        text.pack_start(self.connect_button, False, False, 0)
        hero.pack_start(text, True, True, 0)
        from gi.repository import GdkPixbuf
        picture = self.Gtk.Image.new_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_scale(str(BRAND_ICON), 146, 146, True))
        hero.pack_end(picture, False, False, 0)
        page.pack_start(hero, False, False, 0)

        page.pack_start(self.label("常用操作", "section-title"), False, False, 0)
        shortcuts = self.box(False, 14)
        shortcuts.set_homogeneous(True)
        for title, hint, icon, destination in (
                ("调整显示", "分辨率与设备封面", "video-display-symbolic", "display"),
                ("调节手感", "鼠标与文字输入", "input-mouse-symbolic", "input"),
                ("传送文件", "发送与接收目录", "folder-download-symbolic", "files")):
            button = self.style(self.Gtk.Button(), "shortcut")
            box = self.box(spacing=11)
            top = self.box(False)
            icon_widget = self.Gtk.Image.new_from_icon_name(icon, self.Gtk.IconSize.LARGE_TOOLBAR)
            top.pack_start(self.style(icon_widget, "good-text"), False, False, 0)
            top.pack_end(self.label("↗", "muted"), False, False, 0)
            box.pack_start(top, False, False, 0)
            box.pack_start(self.label(title, "row-title"), False, False, 0)
            box.pack_start(self.label(hint, "muted"), False, False, 0)
            button.add(box)
            button.connect("clicked", lambda _, dest=destination: self.navigate(dest))
            shortcuts.pack_start(button, True, True, 0)
        page.pack_start(shortcuts, False, False, 0)

        details = self.card()
        self.display_summary = self.label("正在读取", "summary-value")
        self.text_summary = self.label("—", "summary-value")
        summary = self.box(False, 20)
        summary.set_homogeneous(True)
        for title, value in (("当前显示", self.display_summary), ("手机文字输入", self.text_summary)):
            item = self.box(spacing=8)
            item.pack_start(self.label(title, "muted"), False, False, 0)
            item.pack_start(value, False, False, 0)
            summary.pack_start(item, True, True, 0)
        details.pack_start(summary, False, False, 0)
        page.pack_start(details, False, False, 0)
        page.pack_start(self.label("这里显示本机服务状态；远程设备的实际连接情况请在 UU 远程中查看。", "footnote"), False, False, 0)

    def _build_display(self):
        Gtk = self.Gtk
        page = self.page("display", "显示与外观", "让远程画面和这台设备一样，恰到好处。")
        modes = self.card("显示器", "选择本机支持的模式；试用后确认，超时自动恢复。")
        current = self.box(False, 16, "display-readout")
        icon = Gtk.Image.new_from_icon_name("video-display-symbolic", Gtk.IconSize.DIALOG)
        current.pack_start(icon, False, False, 0)
        numbers = self.box(spacing=5)
        self.display_current = self.label("正在读取显示器", "summary-value")
        self.display_detail = self.label("请稍候", "muted")
        numbers.pack_start(self.display_current, False, False, 0)
        numbers.pack_start(self.display_detail, False, False, 0)
        current.pack_start(numbers, True, True, 0)
        modes.pack_start(current, False, False, 0)
        self.display_modes = Gtk.ComboBoxText()
        self.display_modes.set_size_request(246, -1)
        self.display_modes.connect("changed", self.display_scale_options)
        self.display_scales = Gtk.ComboBoxText()
        self.display_scales.set_size_request(246, -1)
        self.display_scales.connect("changed", self._display_changed)
        self.setting_row(modes, "分辨率与刷新率", "仅列出这台显示器支持的组合", self.display_modes)
        self.separator(modes)
        self.setting_row(modes, "界面缩放", "调整文字与控件的显示大小", self.display_scales)
        self.display_apply_button = self.button("试用此设置", self.display_apply, "primary")
        self.display_apply_button.set_sensitive(False)
        modes.pack_start(self.actions(self.display_apply_button, self.button("重新检测", self.display_inspect, "quiet")), False, False, 0)
        self.confirm_revealer = Gtk.Revealer()
        confirm = self.box(spacing=14, name="confirmation")
        self.confirm_label = self.label("", "row-title")
        confirm.pack_start(self.confirm_label, False, False, 0)
        self.keep_button = self.button("保留设置", lambda b: self.display_finish(b, "confirm"), "primary")
        self.revert_button = self.button("恢复之前的设置", lambda b: self.display_finish(b, "rollback"))
        confirm.pack_start(self.actions(self.keep_button, self.revert_button), False, False, 0)
        self.confirm_revealer.add(confirm)
        modes.pack_start(self.confirm_revealer, False, False, 0)
        page.pack_start(modes, False, False, 0)

        cover = self.card("设备封面", "在 UU 客户端中显示的桌面缩略图。")
        self.image_preview = Gtk.Image()
        self.image_preview.set_halign(Gtk.Align.START)
        image, default = _desktop_image()
        self._set_image_preview(image)
        cover.pack_start(self.image_preview, False, False, 0)
        self.image_label = self.label("UUWay · 默认 Linux 企鹅" if default else image.name, "muted")
        self.image_label.set_tooltip_text(str(image))
        cover.pack_start(self.image_label, False, False, 0)
        cover.pack_start(self.actions(self.button("更换图片", self.choose_image),
                                      self.button("恢复默认", self.reset_image, "quiet")), False, False, 0)
        page.pack_start(cover, False, False, 0)

    def _build_input(self):
        Gtk = self.Gtk
        page = self.page("input", "鼠标与输入", "调到顺手，再开始工作。")
        prefs = _optional_json(CONFIG_DIR / "input-settings.json")
        prefs = prefs if isinstance(prefs, dict) else {}
        speed = self.card("鼠标与滚轮", "100% 为默认速度，可在 25%–400% 之间调整。")
        for attribute, key, title, hint in (
                ("relative", "relative_percent", "鼠标速度", "相对鼠标移动的灵敏度"),
                ("wheel", "wheel_percent", "滚轮速度", "横向与纵向的滚动幅度")):
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 25, 400, 5)
            raw = prefs.get(key, 100)
            scale.set_value(raw if type(raw) in (int, float) else 100)
            scale.set_digits(0)
            scale.set_size_request(236, -1)
            scale.connect("format-value", lambda _, value: f"{value:.0f}%")
            setattr(self, attribute, scale)
            self.setting_row(speed, title, hint, scale)
            self.separator(speed)
        self.invert = Gtk.Switch()
        self.invert.set_active(bool(prefs.get("invert_wheel", False)))
        self.setting_row(speed, "反向滚动", "同时反转横向和纵向滚轮", self.invert)
        footer = self.box(False, 12)
        self.input_hint = self.label("保存后立即生效", "muted")
        footer.pack_start(self.input_hint, True, True, 0)
        footer.pack_end(self.button("恢复默认", self.reset_input, "quiet"), False, False, 0)
        self.input_save = self.button("保存更改", self.save_input, "primary")
        self.input_save.set_sensitive(False)
        footer.pack_end(self.input_save, False, False, 0)
        speed.pack_start(footer, False, False, 0)
        self.input_original = self._input_values()
        self.relative.connect("value-changed", self._input_changed)
        self.wheel.connect("value-changed", self._input_changed)
        self.invert.connect("notify::active", self._input_changed)
        page.pack_start(speed, False, False, 0)

        compat = self.card("光标与文字", "选择后自动保存；重启远程服务后生效。")
        self.loading_preferences = True
        runtime = _runtime() or {}
        self.cursor = Gtk.ComboBoxText()
        for text in ("画面内光标", "独立光标", "GPU 合成光标"):
            self.cursor.append_text(text)
        self.cursor.set_active({"embedded": 0, "metadata": 1, "composited": 2}.get(runtime.get("cursor_mode"), 1))
        self.cursor_saved = self.cursor.get_active()
        self.cursor.set_sensitive(bool(runtime))
        self.cursor.set_size_request(236, -1)
        self.cursor.connect("changed", self.save_cursor)
        self.setting_row(compat, "光标显示", "独立光标由手机端显示", self.cursor)
        self.separator(compat)
        self.backend = Gtk.ComboBoxText()
        for text in ("Portal · 兼容输入", "Fcitx5 · 原生输入"):
            self.backend.append_text(text)
        backend = _optional_json(CONFIG_DIR / "text-backend.json") or {}
        self.backend.set_active(1 if isinstance(backend, dict) and backend.get("backend") == "fcitx" else 0)
        self.backend_saved = self.backend.get_active()
        self.backend.set_size_request(236, -1)
        self.backend.connect("changed", self.save_backend)
        self.setting_row(compat, "手机文字输入", "Fcitx5 适合中文等输入法，需本机已配置", self.backend)
        self.loading_preferences = False
        page.pack_start(compat, False, False, 0)

    def _build_files(self):
        page = self.page("files", "文件传输", "文件往来，各有去处。")
        send = self.card("发送到手机", "选择项目后，在手机端粘贴或接收。")
        banner = self.box(False, 16, "transfer-banner")
        banner.pack_start(self.Gtk.Image.new_from_icon_name("document-send-symbolic", self.Gtk.IconSize.DIALOG), False, False, 0)
        steps = self.box(spacing=5)
        steps.pack_start(self.label("这台 Linux  →  你的手机", "row-title"), False, False, 0)
        steps.pack_start(self.label("通过 UU 远程传输，最多选择 256 个项目。", "muted"), False, False, 0)
        banner.pack_start(steps, True, True, 0)
        send.pack_start(banner, False, False, 0)
        send.pack_start(self.actions(self.button("选择文件", self.send_desktop_files, "primary"),
                                     self.button("选择文件夹", lambda b: self.send_desktop_files(b, True))), False, False, 0)
        send.pack_start(self.label("项目会放入桌面剪贴板；在手机端完成接收前，请勿复制其他内容。", "footnote"), False, False, 0)
        page.pack_start(send, False, False, 0)

        receive = self.card("接收文件", "其他设备发来的文件会保存到这个目录。")
        self.download_label = self.label(str(_configured_download_directory()), "path")
        self.download_label.set_selectable(True)
        path_box = self.box(spacing=8, name="path-well")
        path_box.pack_start(self.label("接收目录", "muted"), False, False, 0)
        path_box.pack_start(self.download_label, False, False, 0)
        receive.pack_start(path_box, False, False, 0)
        receive.pack_start(self.actions(self.button("打开目录", self.open_download_directory),
                                        self.button("更改目录", self.choose_download_directory),
                                        self.button("恢复默认", self.reset_download_directory, "quiet")), False, False, 0)
        page.pack_start(receive, False, False, 0)

    def _build_diagnostics(self):
        Gtk = self.Gtk
        page = self.page("diagnostics", "诊断", "需要排查时，再深入一步。")
        services = self.card("本机服务", "文字兼容服务按输入方式使用；它停止时不一定影响远程连接。")
        for index, (unit, title) in enumerate(SERVICE_NAMES.items()):
            if index:
                self.separator(services)
            right = self.box(spacing=5)
            state = self.label("正在读取", "row-title")
            detail = self.label("", "muted")
            right.pack_start(state, False, False, 0)
            right.pack_start(detail, False, False, 0)
            self.setting_row(services, title, unit, right)
            self.service_labels[unit] = (state, detail)
        self.stop_button = self.button("停止远程服务", lambda b: self.service_action(b, "stop"), "danger")
        services.pack_start(self.actions(self.stop_button), False, False, 0)
        page.pack_start(services, False, False, 0)

        mapping = self.card()
        expander = Gtk.Expander(label="Linux 目录映射")
        self.mapping_box = self.box(spacing=18)
        self.mapping_box.set_margin_top(20)
        expander.add(self.mapping_box)
        mapping.pack_start(expander, False, False, 0)
        page.pack_start(mapping, False, False, 0)

        report = self.card("诊断报告", "包含服务状态、设置和本机路径，不包含账号、令牌或文件内容。")
        self.report_copy = self.button("复制报告", self.copy_report)
        self.report_copy.set_sensitive(False)
        report.pack_start(self.actions(self.button("生成报告", self.generate_report), self.report_copy), False, False, 0)
        self.report_expander = Gtk.Expander(label="查看报告")
        self.status = self.label("点击“生成报告”读取当前状态。", "report")
        self.status.set_selectable(True)
        self.status.set_margin_top(16)
        self.report_expander.add(self.status)
        report.pack_start(self.report_expander, False, False, 0)
        page.pack_start(report, False, False, 0)

    def _install_styles(self):
        css = self.Gtk.CssProvider()
        css.load_from_data(b"""
            window.uuway { background: #f4f6fa; color: #25344a; }
            .uuway { font-family: 'Inter', 'Noto Sans CJK SC', sans-serif; font-size: 13px; }
            .uuway label { color: #293a50; }
            .uuway headerbar { background: #f4f6fa; background-image: none; border-bottom: 1px solid #e0e6ee; box-shadow: none; padding: 5px 12px; }
            .uuway headerbar .title { font-size: 13px; font-weight: 600; color: #45566e; }
            .uuway .sidebar { background: #111d31; }
            .uuway .brand { color: #ecf6fc; font-size: 22px; font-weight: 800; letter-spacing: -.7px; }
            .uuway .sidebar-footer { color: #8399b5; font-size: 10px; letter-spacing: 1px; }
            .uuway headerbar button.titlebutton { min-height: 22px; min-width: 22px; padding: 2px; background: transparent; border-color: transparent; }
            .uuway headerbar button.titlebutton:hover { background: #e5edf5; }
            .uuway .navigation { background: transparent; }
            .uuway .navigation row { background: transparent; border-radius: 9px; margin: 3px 12px; padding: 13px 12px; color: #a2b3ca; }
            .uuway .navigation row label, .uuway .navigation row image { color: #a2b3ca; }
            .uuway .navigation row:hover { background: #1a2b43; }
            .uuway .navigation row:selected { background: #223853; }
            .uuway .navigation row:selected label, .uuway .navigation row:selected image { color: #c2ebf5; }
            .uuway .nav-label { font-weight: 600; }
            .uuway .page-title { font-size: 28px; font-weight: 700; letter-spacing: -.6px; color: #182e49; }
            .uuway .page-description { color: #77869b; font-size: 13px; }
            .uuway .section-title { font-size: 16px; font-weight: 700; color: #2b425e; }
            .uuway .row-title { font-size: 14px; font-weight: 600; }
            .uuway .muted, .uuway .footnote { color: #77869b; font-size: 12px; }
            .uuway .footnote { font-size: 11px; }
            .uuway .summary-value { color: #2b4768; font-size: 21px; font-weight: 600; }
            .uuway .path { color: #47627f; font-family: monospace; font-size: 12px; }
            .uuway .report { font-family: monospace; font-size: 12px; color: #5e728c; }
            .uuway .card { background: #ffffff; border: 1px solid #e0e6ef; border-radius: 14px; padding: 24px; }
            .uuway .setting-row { padding: 5px 0; }
            .uuway separator { background: #e8edf4; min-height: 1px; }
            .uuway .connection-card { background: linear-gradient(115deg, #132940, #2c3557); border-radius: 16px; padding: 28px; }
            .uuway .hero-title { color: #eff7ff; font-size: 27px; font-weight: 700; }
            .uuway .hero-description { color: #b5c8df; font-size: 13px; }
            .uuway .hero-badge { color: #92d9ee; font-size: 11px; font-weight: 600; }
            .uuway button { background: #fff; background-image: none; color: #35536e; border: 1px solid #d5e0eb; border-radius: 8px; box-shadow: none; text-shadow: none; min-height: 32px; padding: 3px 15px; }
            .uuway button label { color: #35536e; font-weight: 500; }
            .uuway button:hover { background: #eef5fa; border-color: #a9c2d4; }
            .uuway button:active { background: #deedf6; }
            .uuway button:focus { outline: 2px solid #87b5ce; outline-offset: 2px; }
            .uuway button.primary { background: #236e8a; border-color: #236e8a; }
            .uuway button.primary:hover { background: #175b75; }
            .uuway button.primary label { color: #ffffff; }
            .uuway button.hero-action { background: #caebf6; border-color: #caebf6; padding: 4px 20px; }
            .uuway button.hero-action:hover { background: #e0f4fd; }
            .uuway button.hero-action label { color: #173b58; font-weight: 600; }
            .uuway button.quiet { background: transparent; border-color: transparent; }
            .uuway button.quiet:hover { background: #e5edf5; }
            .uuway button.danger { border-color: #ead8d6; }
            .uuway button.danger label { color: #a45151; }
            .uuway button:disabled { opacity: .45; }
            .uuway button.shortcut { background: #fff; border: 1px solid #e0e6ef; border-radius: 12px; padding: 19px; }
            .uuway button.shortcut:hover { background: #f8fbff; border-color: #a0bbd3; }
            .uuway .good-text { color: #258074; }
            .uuway .bad-text { color: #b25151; }
            .uuway .display-readout, .uuway .path-well, .uuway .transfer-banner { background: #f0f4fa; border-radius: 10px; padding: 18px; }
            .uuway combobox button { background: #f8fafd; }
            .uuway combobox cellview { color: #35536e; }
            .uuway menu { background: #fff; color: #293a50; }
            .uuway menuitem:hover { background: #e5edf5; }
            .uuway scale value { color: #36708e; font-size: 12px; }
            .uuway scale trough { min-height: 5px; background: #e1e9f2; border: none; }
            .uuway scale highlight { background: #3d88a6; border: none; }
            .uuway scale slider { background: #ffffff; border: 2px solid #3d88a6; box-shadow: none; min-width: 14px; min-height: 14px; }
            .uuway switch { background: #d4dfeb; border: none; }
            .uuway switch:checked { background: #3d88a6; }
            .uuway switch slider { background: #ffffff; border: none; }
            .uuway .confirmation { background: #ecf1ff; border-radius: 9px; padding: 18px; }
            .uuway .restart-bar { background: #edf0fc; padding: 14px 24px; border-top: 1px solid #d7def2; }
            .uuway .notice { background: #e7f2f8; padding: 10px 24px; border-top: 1px solid #d1e3ee; }
            .uuway .notice.error { background: #faeaea; }
            .uuway .notice.warning { background: #f6f0df; }
            .uuway expander title { padding: 4px 0; }
            .uuway scrollbar { background: transparent; }
            .uuway scrollbar slider { background: #c2cfdf; min-width: 5px; border-radius: 8px; }
        """)
        self.Gtk.StyleContext.add_provider_for_screen(self.window.get_screen(), css,
                                                      self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv[1:])
    if args == ["--help"]:
        print("uuway-console [--status|--smoke-test] — UUWay Linux 控制台")
        return 0
    if args == ["--status"]:
        print(collect_status())
        return 0
    if args not in ([], ["--smoke-test"]):
        print("不支持的参数；使用 --help 查看帮助", file=sys.stderr)
        return 2
    smoke = args == ["--smoke-test"]
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gio, GLib, Gtk
    except (ImportError, ValueError) as error:
        raise SystemExit(f"UUWay 控制台需要 python3-gi/GTK 3：{error}")
    flags = Gio.ApplicationFlags.NON_UNIQUE if smoke else Gio.ApplicationFlags.FLAGS_NONE
    application = Gtk.Application(application_id="io.uuway.Console", flags=flags)
    console = Console(Gtk, GLib, Gio)
    def activate(app):
        if console.window and not console.closed:
            console.window.present()
            return
        console.build(app)
        if smoke:
            GLib.timeout_add(500, app.quit)
    application.connect("activate", activate)
    return application.run(argv or ["uuway-console"])


if __name__ == "__main__":
    raise SystemExit(main())
