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
        self.buttons = []
        self.relative = self.wheel = self.invert = self.cursor = None
        self.backend = None
        self.image_label = None
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
        def worker():
            try:
                value = collect_status()
            except Exception as error:
                value = f"状态读取失败：{error}"
            self.GLib.idle_add(lambda: (self.status.set_text(value), False)[1])
        threading.Thread(target=worker, daemon=True).start()

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
        Gtk, GLib = self.Gtk, self.GLib
        self.window = Gtk.ApplicationWindow(application=application, title="UUWay 控制台")
        self.window.set_default_size(820, 820); self.window.set_border_width(20)
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12); self.window.add(shell)
        brand = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        if DEFAULT_IMAGE.is_file():
            brand.pack_start(Gtk.Image.new_from_file(str(DEFAULT_IMAGE)), False, False, 0)
        title = Gtk.Label(); title.set_markup('<span size="xx-large" weight="bold">UUWay</span>\n<span size="large">Linux 原生远程控制台</span>'); title.set_xalign(0)
        brand.pack_start(title, True, True, 0); shell.pack_start(brand, False, False, 0)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for text, operation in (("启动 UUWay", "start"), ("重连 UUWay", "restart"), ("停止 UUWay", "stop")):
            button = Gtk.Button(label=text); button.connect("clicked", self.service_action, operation); toolbar.pack_start(button, False, False, 0); self.buttons.append(button)
        shell.pack_start(toolbar, False, False, 0)
        self.notice = _label("UUWay 控制台只管理本机 bridge；不会重启 RustDesk、GNOME 或 Portal。"); shell.pack_start(self.notice, False, False, 0)
        notebook = Gtk.Notebook(); shell.pack_start(notebook, True, True, 0)
        scroll = Gtk.ScrolledWindow(); scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.status = _label("正在读取 UUWay 状态…"); self.status.set_selectable(True); self.status.set_yalign(0); self.status.set_margin_top(16); self.status.set_margin_bottom(16); self.status.set_margin_start(16); self.status.set_margin_end(16); scroll.add(self.status); notebook.append_page(scroll, _label("状态与能力"))
        settings = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10); settings.set_margin_top(16); settings.set_margin_bottom(16); settings.set_margin_start(16); settings.set_margin_end(16); notebook.append_page(settings, _label("输入与文字"))
        prefs = _optional_json(CONFIG_DIR / "input-settings.json") or {}; runtime = _runtime() or {}
        settings.pack_start(_label("输入设置"), False, False, 0)
        self.relative = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 25, 400, 5); self.relative.set_value(prefs.get("relative_percent", 100)); settings.pack_start(_label("相对鼠标速度（%）"), False, False, 0); settings.pack_start(self.relative, False, False, 0)
        self.wheel = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 25, 400, 5); self.wheel.set_value(prefs.get("wheel_percent", 100)); settings.pack_start(_label("滚轮速度（%）"), False, False, 0); settings.pack_start(self.wheel, False, False, 0)
        self.invert = Gtk.CheckButton(label="反转滚轮方向（横向和纵向）"); self.invert.set_active(bool(prefs.get("invert_wheel"))); settings.pack_start(self.invert, False, False, 0)
        save = Gtk.Button(label="保存输入设置"); save.connect("clicked", self.save_input); settings.pack_start(save, False, False, 0)
        settings.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)
        settings.pack_start(_label("光标显示方式"), False, False, 0); self.cursor = Gtk.ComboBoxText()
        for text in ("视频内真实光标", "独立光标元数据", "GPU 合成真实光标"):
            self.cursor.append_text(text)
        self.cursor.set_active({"embedded": 0, "metadata": 1, "composited": 2}.get(runtime.get("cursor_mode"), 1)); settings.pack_start(self.cursor, False, False, 0)
        cursor_save = Gtk.Button(label="保存光标设置"); cursor_save.connect("clicked", self.save_cursor); settings.pack_start(cursor_save, False, False, 0)
        settings.pack_start(_label("手机文字后端"), False, False, 0); self.backend = Gtk.ComboBoxText(); self.backend.append_text("Portal（兼容模式）"); self.backend.append_text("原生 Fcitx5");
        backend = _optional_json(CONFIG_DIR / "text-backend.json") or {}; self.backend.set_active(1 if backend.get("backend") == "fcitx" else 0); settings.pack_start(self.backend, False, False, 0); backend_save = Gtk.Button(label="保存文字后端"); backend_save.connect("clicked", self.save_backend); settings.pack_start(backend_save, False, False, 0)
        image_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10); image_page.set_margin_top(16); image_page.set_margin_start(16); image_page.set_margin_end(16); image_page.set_margin_bottom(16); notebook.append_page(image_page, _label("桌面照片与文件"))
        image, default = _desktop_image(); self.image_label = _label(("当前使用默认 Linux 企鹅图片" if default else f"当前自定义图片：{image}")); image_page.pack_start(self.image_label, False, False, 0)
        image_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); choose = Gtk.Button(label="选择自定义图片"); choose.connect("clicked", self.choose_image); image_buttons.pack_start(choose, False, False, 0); reset = Gtk.Button(label="恢复 Linux 企鹅"); reset.connect("clicked", self.reset_image); image_buttons.pack_start(reset, False, False, 0); image_page.pack_start(image_buttons, False, False, 0)
        source, destination, mapped = _mapping_state(); image_page.pack_start(_label(f"文件接收目录：{source or '运行配置不可用'}\nLinux 映射目标：{destination}\n当前状态：{'已建立映射' if mapped else '服务启动时建立映射'}"), False, False, 0)
        download_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); choose_download = Gtk.Button(label="选择接收目录"); choose_download.connect("clicked", self.choose_download_directory); download_buttons.pack_start(choose_download, False, False, 0); reset_download = Gtk.Button(label="恢复 XDG 下载目录"); reset_download.connect("clicked", self.reset_download_directory); download_buttons.pack_start(reset_download, False, False, 0); image_page.pack_start(download_buttons, False, False, 0)
        display = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10); display.set_margin_top(16); display.set_margin_start(16); display.set_margin_end(16); display.set_margin_bottom(16); notebook.append_page(display, _label("显示设置"))
        self.display_current = _label("正在读取显示服务…"); display.pack_start(self.display_current, False, False, 0); self.display_modes = Gtk.ComboBoxText(); self.display_modes.connect("changed", self.display_scale_options); display.pack_start(_label("分辨率与刷新率"), False, False, 0); display.pack_start(self.display_modes, False, False, 0); self.display_scales = Gtk.ComboBoxText(); display.pack_start(_label("Linux 桌面缩放"), False, False, 0); display.pack_start(self.display_scales, False, False, 0)
        display_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); refresh = Gtk.Button(label="刷新模式"); refresh.connect("clicked", self.display_inspect); apply = Gtk.Button(label="临时应用（30 秒保护）"); apply.connect("clicked", self.display_apply); keep = Gtk.Button(label="保留此设置"); keep.connect("clicked", self.display_finish, "confirm"); revert = Gtk.Button(label="立即恢复"); revert.connect("clicked", self.display_finish, "rollback");
        for button in (refresh, apply, keep, revert): display_buttons.pack_start(button, False, False, 0)
        display.pack_start(display_buttons, False, False, 0); self.window.connect("destroy", lambda *_: application.quit()); self.window.show_all(); self.refresh(); self.display_inspect(); GLib.timeout_add_seconds(5, lambda: (self.refresh(), True)[1])


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
