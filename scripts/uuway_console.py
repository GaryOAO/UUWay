#!/usr/bin/python3
"""UUWay's Linux control console.

The bridge is already Python/GTK on the host side, so the console deliberately
uses the same stack.  It only edits the owner-only JSON files under
``~/.config/uurb`` and talks to the existing display guardian socket; it never
reads UU credentials or vendor logs.
"""
import json
import html
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading

RELEASE_ROOT = Path(__file__).resolve().parent
ROOT = (RELEASE_ROOT.parent if (RELEASE_ROOT.parent / "assets").is_dir()
        else RELEASE_ROOT)
DEFAULT_IMAGE = (RELEASE_ROOT / "assets/uuway-penguin.bmp"
                 if (RELEASE_ROOT / "assets/uuway-penguin.bmp").is_file()
                 else ROOT / "assets/uuway-penguin.bmp")
CONFIG_DIR = Path.home() / ".config/uurb"
DOWNLOAD_RELATIVE = Path("drive_c/Program Files/Netease/GameViewer/Download")
MAX_JSON = 65536
MAX_IMAGE = 16 * 1024 * 1024


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


def _label(text, xalign=0):
    from gi.repository import Gtk
    widget = Gtk.Label(label=text, xalign=xalign)
    widget.set_line_wrap(True)
    return widget


class Console:
    def __init__(self, Gtk, GLib, Gio):
        self.Gtk, self.GLib, self.Gio = Gtk, GLib, Gio
        self.window = None
        self.status = None
        self.notice = None
        self.header_status = None
        self.buttons = []
        self.relative = self.wheel = self.invert = self.cursor = None
        self.backend = None
        self.image_label = None
        self.image_preview = None
        self.mapping_label = None
        self.display_modes = self.display_scales = self.display_current = None
        self.display_state = None
        self.display_transaction = None

    def message(self, text):
        if self.notice:
            self.notice.set_text(text)

    def refresh(self):
        if not self.status:
            return
        self.status.set_text("正在读取 UUWay 状态…")
        if self.header_status:
            self.header_status.set_text("正在同步")
        def worker():
            try:
                value = collect_status()
            except Exception as error:
                value = f"状态读取失败：{error}"
            services = _service_properties()
            active = sum(fields.get("ActiveState") == "active" for fields in services.values())
            label = f"{active}/3 服务运行中" if services else "服务状态未知"
            def update():
                if self.status:
                    self.status.set_text(value)
                if self.header_status:
                    self.header_status.set_text(label)
                self._update_mapping_label()
                return False
            self.GLib.idle_add(update)
        threading.Thread(target=worker, daemon=True).start()

    def _update_mapping_label(self):
        if not self.mapping_label:
            return
        source, destination, mapped = _mapping_state()
        self.mapping_label.set_markup(
            f"<b>Windows 接收目录</b>\n"
            f"<span size=\"small\">{html.escape(str(source or '运行配置不可用'))}</span>\n\n"
            f"<b>Linux 保存位置</b>\n"
            f"<span size=\"small\">{html.escape(str(destination))}</span>\n\n"
            f"<b>映射状态</b>　"
            f"<span foreground=\"{'#0f8a72' if mapped else '#b26a00'}\">"
            f"{'已连接' if mapped else '等待服务建立'}</span>")

    def _set_image_preview(self, path):
        if not self.image_preview:
            return
        try:
            from gi.repository import GdkPixbuf
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 240, 150, True)
            self.image_preview.set_from_pixbuf(pixbuf)
        except Exception:
            self.image_preview.set_from_file(str(path))

    def service_action(self, button, operation):
        if operation == "stop":
            dialog = self.Gtk.MessageDialog(self.window, self.Gtk.DialogFlags.MODAL,
                self.Gtk.MessageType.WARNING, self.Gtk.ButtonsType.OK_CANCEL,
                "停止 UUWay 会断开当前 UU 连接；之后需要本机或其他远程通道重新启动。继续吗？")
            proceed = dialog.run() == self.Gtk.ResponseType.OK
            dialog.destroy()
            if not proceed:
                return
        for item in self.buttons:
            item.set_sensitive(False)
        self.message("正在执行 UUWay 服务操作…")
        process = self.Gio.Subprocess.new(
            ["/usr/bin/systemctl", "--user", operation, "uu-native-bridge.service"],
            self.Gio.SubprocessFlags.STDOUT_SILENCE | self.Gio.SubprocessFlags.STDERR_SILENCE)
        def done(source, result, _data):
            try:
                ok = source.wait_check_finish(result)
            except Exception:
                ok = False
            for item in self.buttons:
                item.set_sensitive(True)
            self.message("UUWay 服务操作已完成。" if ok else "UUWay 服务操作失败，已保存配置未清除。")
            self.refresh()
        process.wait_check_async(None, done, None)

    def save_input(self, button):
        try:
            _write_json(CONFIG_DIR / "input-settings.json", {
                "version": 1, "relative_percent": int(self.relative.get_value()),
                "wheel_percent": int(self.wheel.get_value()), "invert_wheel": self.invert.get_active()})
            self.message("输入设置已保存。")
        except (OSError, ValueError) as error:
            self.message(f"输入设置保存失败：{error}")

    def save_cursor(self, button):
        runtime = _runtime()
        if not runtime:
            self.message("运行配置不可用，未修改光标设置。")
            return
        modes = ("embedded", "metadata", "composited")
        runtime["cursor_mode"] = modes[max(0, self.cursor.get_active())]
        try:
            _write_json(CONFIG_DIR / "native-runtime.json", runtime)
            self.message("光标设置已保存，请重启 UUWay 服务后生效。")
        except (OSError, ValueError) as error:
            self.message(f"光标设置保存失败：{error}")

    def choose_image(self, button):
        dialog = self.Gtk.FileChooserDialog("选择 UUWay 桌面图片", self.window,
            self.Gtk.FileChooserAction.OPEN, ("取消", self.Gtk.ResponseType.CANCEL, "选择", self.Gtk.ResponseType.OK))
        image_filter = self.Gtk.FileFilter(); image_filter.set_name("图片文件"); image_filter.add_mime_type("image/*")
        dialog.add_filter(image_filter)
        if dialog.run() == self.Gtk.ResponseType.OK:
            path = Path(dialog.get_filename())
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size == 0 or info.st_size > MAX_IMAGE:
                    raise ValueError("图片必须是非空普通文件且不超过 16 MiB")
                _write_json(CONFIG_DIR / "desktop-image.json", {"version": 1, "path": str(path.resolve())})
                self.image_label.set_text(f"当前自定义图片：{path}")
                self._set_image_preview(path)
                self.message("桌面图片已保存，请重启 UUWay 服务后生效。")
            except (OSError, ValueError) as error:
                self.message(f"桌面图片保存失败：{error}")
        dialog.destroy()

    def reset_image(self, button):
        try:
            path = CONFIG_DIR / "desktop-image.json"
            if path.exists() or path.is_symlink():
                path.unlink()
            self.image_label.set_text("当前使用默认 Linux 企鹅图片")
            self._set_image_preview(DEFAULT_IMAGE)
            self.message("已恢复默认企鹅图片，请重启 UUWay 服务后生效。")
        except OSError as error:
            self.message(f"恢复默认图片失败：{error}")

    def choose_download_directory(self, button):
        dialog = self.Gtk.FileChooserDialog("选择 UUWay 文件接收目录", self.window,
            self.Gtk.FileChooserAction.SELECT_FOLDER,
            ("取消", self.Gtk.ResponseType.CANCEL, "选择", self.Gtk.ResponseType.OK))
        if dialog.run() == self.Gtk.ResponseType.OK:
            path = Path(dialog.get_filename())
            try:
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError("接收目录必须是目录")
                _write_json(CONFIG_DIR / "download-directory.json", {"version": 1, "path": str(path.resolve())})
                self.message("文件接收目录已保存，请重启 UUWay 服务后生效。")
                self._update_mapping_label()
                self.refresh()
            except (OSError, ValueError) as error:
                self.message(f"文件接收目录保存失败：{error}")
        dialog.destroy()

    def reset_download_directory(self, button):
        try:
            path = CONFIG_DIR / "download-directory.json"
            if path.exists() or path.is_symlink():
                path.unlink()
            self.message("已恢复 XDG 下载目录，请重启 UUWay 服务后生效。")
            self._update_mapping_label()
            self.refresh()
        except OSError as error:
            self.message(f"恢复 XDG 下载目录失败：{error}")

    def save_backend(self, button):
        try:
            _write_json(CONFIG_DIR / "text-backend.json", {"version": 1, "backend": "fcitx" if self.backend.get_active() == 1 else "portal"})
            self.message("文字后端已保存，请重启 UUWay 服务后生效。")
        except (OSError, ValueError) as error:
            self.message(f"文字后端保存失败：{error}")

    def display_inspect(self, button=None):
        runtime = _runtime()
        try:
            value = _display_request(runtime, {"version": 1, "op": "inspect"})
            self.display_state = value
            self.display_current.set_text(f"当前：{value['width']} × {value['height']} · {value['refresh']:.2f} Hz · 缩放 {value['scale'] * 100:.0f}%")
            self.display_modes.remove_all()
            selected = 0
            for index, mode in enumerate(value.get("modes", [])):
                self.display_modes.append_text(f"{mode['width']} × {mode['height']} · {mode['refresh']:.2f} Hz")
                if mode["width"] == value["width"] and mode["height"] == value["height"] and abs(mode["refresh"] - value["refresh"]) < .001:
                    selected = index
            self.display_modes.set_active(selected)
            self.display_scale_options()
            self.message("显示状态已刷新。")
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            self.display_current.set_text("无法读取显示服务；可能尚未启动或当前桌面布局不支持。")
            self.message(f"显示状态读取失败：{error}")

    def display_scale_options(self, combo=None):
        if not self.display_state:
            return
        index = self.display_modes.get_active()
        modes = self.display_state.get("modes", [])
        if index < 0 or index >= len(modes):
            return
        mode = modes[index]
        self.display_scales.remove_all()
        selected = 0
        for index, scale in enumerate(mode.get("scales", [])):
            self.display_scales.append_text(f"{scale * 100:.0f}%")
            if abs(scale - self.display_state["scale"]) < .001:
                selected = index
        self.display_scales.set_active(selected)

    def display_apply(self, button):
        if not self.display_state:
            return
        dialog = self.Gtk.MessageDialog(self.window, self.Gtk.DialogFlags.MODAL, self.Gtk.MessageType.WARNING,
            self.Gtk.ButtonsType.OK_CANCEL, "切换分辨率、刷新率或缩放可能短暂断开 UU。30 秒内不确认将自动回滚，继续吗？")
        proceed = dialog.run() == self.Gtk.ResponseType.OK; dialog.destroy()
        if not proceed:
            return
        mode_index, scale_index = self.display_modes.get_active(), self.display_scales.get_active()
        try:
            mode = self.display_state["modes"][mode_index]
            scale = mode["scales"][scale_index]
            result = _display_request(_runtime(), {"version": 1, "op": "apply", "serial": self.display_state["serial"],
                "width": mode["width"], "height": mode["height"], "refresh": mode["refresh"], "scale": scale})
            self.display_transaction = result.get("transaction")
            self.message("已申请临时切换，请确认画面与点击位置正常后保留；否则会自动回滚。")
            self.display_inspect()
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            self.message(f"显示设置未应用：{error}")

    def display_finish(self, button, operation):
        if not self.display_transaction:
            self.message("当前没有待确认的显示切换。")
            return
        try:
            request = {"version": 1, "op": operation, "transaction": self.display_transaction}
            if operation == "confirm" and self.display_state:
                request["serial"] = self.display_state["serial"]
            _display_request(_runtime(), request)
            self.display_transaction = None
            self.message("显示设置已保留。" if operation == "confirm" else "显示设置已恢复。")
            self.display_inspect()
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            self.message(f"显示操作失败：{error}")

    def build(self, application):
        """Build the console with a stable navigation shell and compact cards."""
        Gtk, GLib = self.Gtk, self.GLib
        try:
            from gi.repository import GdkPixbuf
        except ImportError:
            GdkPixbuf = None

        self.window = Gtk.ApplicationWindow(application=application, title="UUWay 控制台")
        self.window.set_default_size(1120, 760)
        self.window.set_size_request(860, 600)

        css = Gtk.CssProvider()
        css.load_from_data(b"""
            window { background: #f5f7fb; }
            headerbar { background: #102a43; color: #ffffff; padding: 7px 14px; }
            headerbar label { color: #ffffff; }
            .brand-title { font-size: 17px; font-weight: 700; }
            .brand-subtitle { color: #b8d8e8; font-size: 11px; }
            .header-state { color: #b8e3d5; font-size: 12px; padding: 6px 10px; }
            .sidebar { background: #edf2f7; border-right: 1px solid #d9e2ec; }
            .sidebar-caption { color: #627d98; font-size: 11px; font-weight: 700; letter-spacing: 0.8px; }
            .nav-list { background: transparent; }
            .nav-list row { border-radius: 8px; margin: 3px 10px; padding: 2px; }
            .nav-list row:selected { background: #d8eef0; color: #0b7285; }
            .nav-list row:hover { background: #e2edf3; }
            .nav-label { font-size: 13px; font-weight: 600; }
            .nav-hint { color: #627d98; font-size: 11px; }
            .page-title { color: #102a43; font-size: 24px; font-weight: 700; }
            .page-subtitle { color: #627d98; font-size: 13px; }
            .card { background: #ffffff; border: 1px solid #d9e2ec; border-radius: 12px; padding: 18px; }
            .card-title { color: #243b53; font-size: 15px; font-weight: 700; }
            .card-subtitle { color: #627d98; font-size: 12px; }
            .muted { color: #627d98; font-size: 12px; }
            .value { color: #102a43; font-size: 14px; }
            .primary-action { background: #0b7285; color: #ffffff; border: 0; }
            .primary-action:hover { background: #095c6b; }
            .secondary-action { color: #0b7285; }
            .danger-action { color: #b42318; }
            .notice { background: #e6f4f1; border-top: 1px solid #c5e5dc; padding: 9px 16px; color: #245b52; font-size: 12px; }
            scale trough { min-height: 6px; }
            scale highlight { background: #0b7285; }
            combobox box { min-height: 34px; }
            button { min-height: 34px; padding: 0 13px; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            self.window.get_screen(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        def add_class(widget, name):
            widget.get_style_context().add_class(name)
            return widget

        def label(text, css_name=None, xalign=0):
            widget = Gtk.Label(label=text, xalign=xalign)
            widget.set_line_wrap(True)
            if css_name:
                add_class(widget, css_name)
            return widget

        def page_header(title, subtitle):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.pack_start(label(title, "page-title"), False, False, 0)
            box.pack_start(label(subtitle, "page-subtitle"), False, False, 0)
            return box

        def card(title, subtitle=None):
            box = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12), "card")
            box.pack_start(label(title, "card-title"), False, False, 0)
            if subtitle:
                box.pack_start(label(subtitle, "card-subtitle"), False, False, 0)
            return box

        def page(title, subtitle):
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
            content.set_margin_top(26); content.set_margin_bottom(28)
            content.set_margin_start(30); content.set_margin_end(30)
            content.pack_start(page_header(title, subtitle), False, False, 0)
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.add(content)
            return content, scroll

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("")
        self.window.set_titlebar(header)
        brand = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        if DEFAULT_IMAGE.is_file():
            logo = Gtk.Image()
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(DEFAULT_IMAGE), 40, 40, True)
                logo.set_from_pixbuf(pixbuf)
            except Exception:
                logo.set_from_file(str(DEFAULT_IMAGE))
            brand.pack_start(logo, False, False, 0)
        brand_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        brand_text.pack_start(label("UUWay", "brand-title"), False, False, 0)
        brand_text.pack_start(label("Linux 原生远程控制台", "brand-subtitle"), False, False, 0)
        brand.pack_start(brand_text, False, False, 0)
        header.set_custom_title(brand)
        self.header_status = label("正在同步", "header-state")
        header.pack_end(self.header_status)
        header_refresh = Gtk.Button()
        header_refresh.set_tooltip_text("刷新 UUWay 状态")
        header_refresh.add(Gtk.Image.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON))
        header_refresh.connect("clicked", lambda *_: self.refresh())
        header.pack_end(header_refresh)

        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        shell.pack_start(body, True, True, 0)
        self.window.add(shell)

        sidebar = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12), "sidebar")
        sidebar.set_size_request(216, -1)
        sidebar.set_margin_top(24); sidebar.set_margin_bottom(18)
        sidebar.pack_start(label("工作台", "sidebar-caption"), False, False, 18)
        nav = add_class(Gtk.ListBox(), "nav-list")
        nav.set_selection_mode(Gtk.SelectionMode.SINGLE)
        sidebar.pack_start(nav, False, False, 0)
        sidebar.pack_end(label("设置仅作用于本机\nUUWay 不读取账号或令牌", "nav-hint"), False, False, 18)
        body.pack_start(sidebar, False, False, 0)

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        stack.set_hexpand(True); stack.set_vexpand(True)
        body.pack_start(stack, True, True, 0)

        def add_nav(title, hint, icon, name):
            row = Gtk.ListBoxRow()
            row.set_name(name)
            item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            item.set_margin_top(8); item.set_margin_bottom(8)
            item.set_margin_start(8); item.set_margin_end(8)
            item.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU), False, False, 0)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            text.pack_start(label(title, "nav-label"), False, False, 0)
            text.pack_start(label(hint, "nav-hint"), False, False, 0)
            item.pack_start(text, True, True, 0)
            row.add(item); nav.add(row)
            return row

        overview, overview_scroll = page("概览", "快速查看 UUWay 服务、连接能力和文件映射")
        input_page, input_scroll = page("输入与文字", "把远程操作调成适合你的速度、光标和文字输入方式")
        display_page, display_scroll = page("显示", "安全调整分辨率、刷新率和桌面缩放")
        desktop_page, desktop_scroll = page("桌面与文件", "管理客户端显示图片和从其他设备接收文件的位置")
        stack.add_named(overview_scroll, "overview")
        stack.add_named(input_scroll, "input")
        stack.add_named(display_scroll, "display")
        stack.add_named(desktop_scroll, "desktop")
        rows = [
            add_nav("概览", "服务与能力", "view-dashboard-symbolic", "overview"),
            add_nav("输入与文字", "鼠标、滚轮、文字", "input-mouse-symbolic", "input"),
            add_nav("显示", "分辨率与缩放", "video-display-symbolic", "display"),
            add_nav("桌面与文件", "图片与接收目录", "folder-download-symbolic", "desktop"),
        ]
        nav.connect("row-selected", lambda _nav, row: stack.set_visible_child_name(row.get_name()) if row else None)
        nav.select_row(rows[0])

        # Overview
        status_card = card("服务状态", "三个用户级服务共同组成 UUWay 的本机运行面")
        self.status = label("正在读取 UUWay 状态…", "value")
        self.status.set_selectable(True); self.status.set_yalign(0)
        status_scroll = Gtk.ScrolledWindow()
        status_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        status_scroll.set_size_request(-1, 220)
        status_scroll.add(self.status)
        status_card.pack_start(status_scroll, True, True, 0)
        service_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for text, operation, css_name in (
                ("启动 UUWay", "start", "primary-action"),
                ("重连 UUWay", "restart", "secondary-action"),
                ("停止 UUWay", "stop", "danger-action")):
            button = Gtk.Button(label=text)
            add_class(button, css_name)
            button.connect("clicked", self.service_action, operation)
            service_actions.pack_start(button, False, False, 0)
            self.buttons.append(button)
        status_card.pack_start(service_actions, False, False, 0)
        overview.pack_start(status_card, False, False, 0)

        overview_columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        overview.pack_start(overview_columns, False, False, 0)
        mapping_card = card("文件接收映射", "从其他设备发送的文件会落到 Linux 下载目录")
        self.mapping_label = label("正在读取映射…", "value")
        mapping_card.pack_start(self.mapping_label, False, False, 0)
        overview_columns.pack_start(mapping_card, True, True, 0)
        capability_card = card("已接入能力", "所有配置均保存在本机用户目录")
        capability_card.pack_start(label(
            "鼠标与滚轮控制\n文字输入（Portal / Fcitx5）\n桌面照片与 Linux 企鹅默认图\n显示临时切换与自动回滚\n剪贴板、终端和文件桥接", "value"), False, False, 0)
        overview_columns.pack_start(capability_card, True, True, 0)

        # Input and text
        prefs = _optional_json(CONFIG_DIR / "input-settings.json") or {}
        runtime = _runtime() or {}
        input_columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        input_page.pack_start(input_columns, False, False, 0)
        input_card = card("输入速度", "范围 25%–400%，保存后由本机 bridge 使用")
        input_grid = Gtk.Grid(column_spacing=16, row_spacing=12)
        input_card.pack_start(input_grid, False, False, 0)
        self.relative = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 25, 400, 5)
        self.relative.set_value(prefs.get("relative_percent", 100)); self.relative.set_digits(0)
        self.relative.set_draw_value(True); self.relative.set_hexpand(True)
        input_grid.attach(label("相对鼠标速度", "value"), 0, 0, 1, 1); input_grid.attach(self.relative, 1, 0, 1, 1)
        self.wheel = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 25, 400, 5)
        self.wheel.set_value(prefs.get("wheel_percent", 100)); self.wheel.set_digits(0)
        self.wheel.set_draw_value(True); self.wheel.set_hexpand(True)
        input_grid.attach(label("滚轮速度", "value"), 0, 1, 1, 1); input_grid.attach(self.wheel, 1, 1, 1, 1)
        self.invert = Gtk.CheckButton(label="反转横向和纵向滚轮")
        self.invert.set_active(bool(prefs.get("invert_wheel")))
        input_grid.attach(self.invert, 0, 2, 2, 1)
        save = Gtk.Button(label="保存输入设置"); add_class(save, "primary-action")
        save.connect("clicked", self.save_input); input_grid.attach(save, 0, 3, 2, 1)
        input_columns.pack_start(input_card, True, True, 0)

        compat_card = card("光标与文字", "选择远程画面和手机文字输入的兼容方式")
        compat_grid = Gtk.Grid(column_spacing=14, row_spacing=12)
        compat_card.pack_start(compat_grid, False, False, 0)
        self.cursor = Gtk.ComboBoxText()
        for text in ("视频内真实光标", "独立光标元数据", "GPU 合成真实光标"):
            self.cursor.append_text(text)
        self.cursor.set_active({"embedded": 0, "metadata": 1, "composited": 2}.get(runtime.get("cursor_mode"), 1))
        compat_grid.attach(label("光标显示", "value"), 0, 0, 1, 1); compat_grid.attach(self.cursor, 1, 0, 1, 1)
        cursor_save = Gtk.Button(label="保存光标设置"); add_class(cursor_save, "secondary-action")
        cursor_save.connect("clicked", self.save_cursor); compat_grid.attach(cursor_save, 1, 1, 1, 1)
        self.backend = Gtk.ComboBoxText(); self.backend.append_text("Portal（兼容模式）"); self.backend.append_text("原生 Fcitx5")
        backend = _optional_json(CONFIG_DIR / "text-backend.json") or {}
        self.backend.set_active(1 if backend.get("backend") == "fcitx" else 0)
        compat_grid.attach(label("手机文字", "value"), 0, 2, 1, 1); compat_grid.attach(self.backend, 1, 2, 1, 1)
        backend_save = Gtk.Button(label="保存文字后端"); add_class(backend_save, "secondary-action")
        backend_save.connect("clicked", self.save_backend); compat_grid.attach(backend_save, 1, 3, 1, 1)
        input_columns.pack_start(compat_card, True, True, 0)

        # Display
        display_status_card = card("当前显示状态", "应用新模式时会保留 30 秒保护窗口，未确认将自动回滚")
        self.display_current = label("正在读取显示服务…", "value")
        display_status_card.pack_start(self.display_current, False, False, 0)
        display_page.pack_start(display_status_card, False, False, 0)
        display_controls = card("显示模式", "只会列出当前桌面支持的分辨率、刷新率和缩放组合")
        display_grid = Gtk.Grid(column_spacing=16, row_spacing=12)
        display_controls.pack_start(display_grid, False, False, 0)
        self.display_modes = Gtk.ComboBoxText(); self.display_modes.connect("changed", self.display_scale_options)
        self.display_scales = Gtk.ComboBoxText()
        display_grid.attach(label("分辨率与刷新率", "value"), 0, 0, 1, 1); display_grid.attach(self.display_modes, 1, 0, 1, 1)
        display_grid.attach(label("Linux 桌面缩放", "value"), 0, 1, 1, 1); display_grid.attach(self.display_scales, 1, 1, 1, 1)
        display_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        refresh_modes = Gtk.Button(label="刷新模式"); add_class(refresh_modes, "secondary-action"); refresh_modes.connect("clicked", self.display_inspect)
        apply = Gtk.Button(label="临时应用（30 秒保护）"); add_class(apply, "primary-action"); apply.connect("clicked", self.display_apply)
        keep = Gtk.Button(label="保留此设置"); add_class(keep, "secondary-action"); keep.connect("clicked", self.display_finish, "confirm")
        revert = Gtk.Button(label="立即恢复"); add_class(revert, "danger-action"); revert.connect("clicked", self.display_finish, "rollback")
        for button in (refresh_modes, apply, keep, revert): display_buttons.pack_start(button, False, False, 0)
        display_controls.pack_start(display_buttons, False, False, 4)
        display_page.pack_start(display_controls, False, False, 0)

        # Desktop and files
        image_columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        desktop_page.pack_start(image_columns, False, False, 0)
        image, default = _desktop_image()
        image_card = card("桌面照片", "客户端显示的桌面缩略图，默认使用 UUWay Linux 企鹅")
        self.image_preview = Gtk.Image()
        self._set_image_preview(image)
        self.image_preview.set_halign(Gtk.Align.START)
        image_card.pack_start(self.image_preview, False, False, 0)
        self.image_label = label(("当前使用默认 Linux 企鹅图片" if default else f"当前自定义图片：{image}"), "muted")
        image_card.pack_start(self.image_label, False, False, 0)
        image_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        choose = Gtk.Button(label="选择自定义图片"); add_class(choose, "primary-action"); choose.connect("clicked", self.choose_image)
        reset = Gtk.Button(label="恢复 Linux 企鹅"); add_class(reset, "secondary-action"); reset.connect("clicked", self.reset_image)
        image_buttons.pack_start(choose, False, False, 0); image_buttons.pack_start(reset, False, False, 0)
        image_card.pack_start(image_buttons, False, False, 0)
        image_columns.pack_start(image_card, True, True, 0)

        file_card = card("文件接收目录", "Windows 端接收目录已经映射到 Linux，默认使用 XDG 下载目录")
        self.mapping_label = label("正在读取映射…", "value")
        file_card.pack_start(self.mapping_label, False, False, 0)
        download_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        choose_download = Gtk.Button(label="选择接收目录"); add_class(choose_download, "primary-action"); choose_download.connect("clicked", self.choose_download_directory)
        reset_download = Gtk.Button(label="恢复 XDG 下载目录"); add_class(reset_download, "secondary-action"); reset_download.connect("clicked", self.reset_download_directory)
        download_buttons.pack_start(choose_download, False, False, 0); download_buttons.pack_start(reset_download, False, False, 0)
        file_card.pack_start(download_buttons, False, False, 0)
        image_columns.pack_start(file_card, True, True, 0)

        self.notice = add_class(label("UUWay 控制台只管理本机 bridge；不会重启 RustDesk、GNOME 或 Portal。"), "notice")
        shell.pack_end(self.notice, False, False, 0)
        self.window.connect("destroy", lambda *_: application.quit())
        self.window.show_all()
        self._update_mapping_label()
        self.refresh()
        self.display_inspect()
        GLib.timeout_add_seconds(5, lambda: (self.refresh(), True)[1])


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
        console.build(app)
        if smoke:
            GLib.timeout_add(500, app.quit)
    application.connect("activate", activate)
    return application.run(argv or ["uuway-console"])


if __name__ == "__main__":
    main()
