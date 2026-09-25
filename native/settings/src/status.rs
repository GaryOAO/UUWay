use crate::{directory, read_private, runtime};
use serde_json::Value;
use std::{
    fs::OpenOptions,
    io::{Read, Seek, SeekFrom},
    os::unix::fs::{FileTypeExt, MetadataExt, OpenOptionsExt},
    path::Path,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

// Shared, read-only guidance for the GTK page and CLI status. This describes
// a reviewed client branch, not the connected phone's current configuration.
const CURSOR_FOLLOW_HELP: &str = concat!(
    "手机画面不跟随（客户端设置，非本机光标速度）\n",
    "官方 Android 4.39.1 的代码显示：智能鼠标开启且独立光标隐藏时，",
    "相对移动会跳过本地指针的画面平移路径。其他版本和 iPhone 未验证。\n",
    "可在手机远程会话的“更多 → 高级选项”中只关闭“智能鼠标”，",
    "保持本机真实光标方式不变，再放大画面并将指针移到边缘。",
    "无需重连，不要为此开启第二个光标。\n",
    "这只是待实测的候选办法；须确认画面跟随、单光标、定位和拖动。",
    "选项可能受版本或设备能力限制。控制台未读取手机设置，也不能代改。\n",
    "\0"
);

#[no_mangle]
pub extern "C" fn uurb_cursor_follow_help() -> *const u8 {
    CURSOR_FOLLOW_HELP.as_ptr()
}

fn text_backend_state(parent: &Path, config: &Value) -> String {
    let backend = match read_private(&parent.join("text-backend.json")) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => "portal",
        Ok(ref value) if value.as_object().map(|v| v.len()) == Some(2)
            && value["version"].as_u64() == Some(1)
            && matches!(value["backend"].as_str(), Some("fcitx" | "portal")) => {
                if value["backend"] == "fcitx" { "fcitx" } else { "portal" }
            }
        _ => return "文本后端：配置不可核实，不推断为剪贴板模式。\n".into(),
    };
    let (mut text, endpoint) = if backend == "fcitx" {
        (String::from("已保存文本后端：原生 Fcitx 提交（不使用剪贴板；候选词替换尚未支持）\n"),
         config["state_parent"].as_str().map(|p| Path::new(p).join("ime.sock")))
    } else {
        (String::from("已保存文本后端：Portal 剪贴板兼容提交\n"),
         config["text_socket"].as_str().map(std::path::PathBuf::from))
    };
    let Some(endpoint) = endpoint.filter(|p| p.is_absolute()) else {
        text.push_str("文本接口：路径配置无效\n");
        return text;
    };
    if endpoint.parent().is_none_or(|p| crate::private_parent(p).is_err()) {
        text.push_str("文本接口：私有目录不可核实\n");
        return text;
    }
    text.push_str(match std::fs::symlink_metadata(&endpoint) {
        Ok(info) if info.file_type().is_socket() && info.uid() == unsafe { libc::getuid() }
            && info.mode() & 0o077 == 0 => match endpoint_listener_state(&endpoint) {
                Some(true) =>
                    "文本接口文件：存在；内核有匹配路径的监听记录，未探测焦点或实际插入，不能据此判定输入成功。\n",
                Some(false) =>
                    "文本接口文件：存在；匹配路径的记录不是可用的文本监听，未探测焦点或实际插入，不能据此判定输入成功。\n",
                None =>
                    "文本接口文件：存在；监听状态不可核实，未探测焦点或实际插入，不能据此判定输入成功。\n",
            },
        Err(error) if error.kind() == std::io::ErrorKind::NotFound =>
                "文本接口：尚未出现；当前提交会失败，不会自动改用其他后端。\n",
        _ => "文本接口：类型或权限不可核实；未连接、未发送测试文字。\n",
    });
    text
}

fn display_capability_state(config: &Value) -> String {
    let Some(bundle) = config["bundle"].as_str().filter(|p| Path::new(p).is_absolute()) else {
        return "显示能力：运行 bundle 路径不可核实；未推断 DPI 或超级屏能力。\n".into();
    };
    let path = Path::new(bundle);
    if crate::private_parent(path).is_err() {
        return "显示能力：运行 bundle 目录权限不可核实；未推断 DPI 或超级屏能力。\n".into();
    }
    let manifest = path.join("manifest.json");
    let Ok(value) = read_private(&manifest) else {
        return "显示能力：bundle manifest 不可读取；未推断 DPI 或超级屏能力。\n".into();
    };
    let contract = &value["contract"];
    let schema = contract["schema_version"].as_u64();
    let dpi = contract["dpi_boundary"].as_str().is_some();
    let virtual_display = contract["virtual_display_emulation"].as_bool();
    let mut out = String::new();
    if dpi && schema == Some(37) && contract["display_native_abi_version"].as_u64() == Some(2) {
        out.push_str("DPI/缩放：bundle 声明精确离散缩放 ABI v2（schema37）；只接受系统真实支持的比例。\nWindows 连续 DPI 查询无法表达稀疏比例时明确不支持；手机 DPI 菜单尚未完成验收。\n");
    } else if dpi && schema == Some(36) {
        out.push_str("DPI/缩放：旧主机 ABI（schema36）存在离散比例虚报风险；手机 DPI 菜单尚未完成验收。\n");
    } else {
        out.push_str("DPI/缩放：当前 bundle 未声明已知 DPI ABI；未推断手机 DPI 支持。\n");
    }
    if virtual_display == Some(false) {
        out.push_str("UU 超级屏：当前明确未启用虚拟显示 emulation；无虚拟输出能力声明。\n");
        out.push_str("阻断原因：UU 的 GameViewerIddDriver 是 Windows UMDF/WDF IDD（依赖 WUDFRd.sys），Wine 不能注册为 Linux DRM/Wayland 输出；不能把 vkms 或 Portal 流冒充超级屏。\n");
    } else {
        out.push_str("UU 超级屏：bundle 能力不可核实；未宣称虚拟输出。\n");
    }
    out
}

/*
 * Read-only observation, not a connect/readiness test. Do not connect to probe:
 * a SOCK_SEQPACKET connect would enqueue a client in the text daemon and can
 * race a real IME transaction.  /proc/net/unix exposes the path and state
 * without delivering anything to the service. State 01 alone includes bound
 * sockets, so require SOCK_SEQPACKET and the SO_ACCEPTCON flag as well.
 * See net/unix/af_unix.c:unix_seq_show in the Linux kernel. Duplicate paths
 * are ambiguous (e.g. unlinked old listener plus replacement); report unknown.
 * The inode in /proc/net/unix is a
 * kernel socket inode and must not be compared with stat(2)'s filesystem
 * inode. Even a unique path record does not prove focus/insertion/readiness.
 */
fn parse_unix_socket_listener_state(contents: &str, endpoint: &Path) -> Option<bool> {
    let endpoint = endpoint.to_str()?;
    if !endpoint.starts_with('/') || endpoint.contains(['\n', '\r', '\0'])
        || !contents.ends_with('\n') {
        return None;
    }
    let mut matches = contents.lines().skip(1).filter_map(|line| {
        // Consume exactly seven fields. Do not suffix-match the pathname:
        // '/tmp/other /tmp/ime.sock' must not match '/tmp/ime.sock'.
        let mut tail = line;
        let mut fields = [""; 7];
        for field in &mut fields {
            tail = tail.trim_start_matches([' ', '\t']);
            let end = tail.find([' ', '\t']).unwrap_or(tail.len());
            if end == 0 { return None; }
            *field = &tail[..end];
            tail = &tail[end..];
        }
        if tail.trim_start_matches([' ', '\t']) != endpoint { return None; }
        Some(fields)
    });
    let fields = matches.next()?;
    if matches.next().is_some() { return None; }
    let flags = u32::from_str_radix(fields[3], 16).ok()?;
    let socket_type = u32::from_str_radix(fields[4], 16).ok()?;
    let state = u32::from_str_radix(fields[5], 16).ok()?;
    let inode = fields[6].parse::<u64>().ok()?;
    if inode == 0 || fields[2] != "00000000" || flags & !0x10000 != 0
        || !(1..=4).contains(&state) {
        return None;
    }
    Some(flags == 0x10000 && socket_type == 5 && state == 1)
}

fn endpoint_listener_state(endpoint: &Path) -> Option<bool> {
    let identity = || {
        let info = std::fs::symlink_metadata(endpoint).ok()?;
        if !info.file_type().is_socket() || info.uid() != unsafe { libc::getuid() }
            || info.mode() & 0o077 != 0 { return None; }
        Some((info.dev(), info.ino(), info.mode(), info.uid(), info.ctime(), info.ctime_nsec()))
    };
    let before = identity()?;
    const LIMIT: usize = 2 * 1024 * 1024;
    let mut contents = String::new();
    std::fs::File::open("/proc/net/unix").ok()?.take((LIMIT + 1) as u64)
        .read_to_string(&mut contents).ok()?;
    if contents.len() > LIMIT || identity()? != before { return None; }
    parse_unix_socket_listener_state(&contents, endpoint)
}

fn services() -> String {
    let Ok(mut child) = Command::new("/usr/bin/systemctl")
        .args([
            "--user",
            "show",
            "uu-native-bridge.service",
            "uu-native-text.service",
            "uu-native-display.service",
            "pipewire.service",
            "pipewire-pulse.service",
            "wireplumber.service",
            "--property=Id,ActiveState,SubState,MainPID,NRestarts",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    else {
        return "服务状态：无法读取".into();
    };
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => break,
            Ok(Some(_)) | Err(_) => return "服务状态：查询失败".into(),
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(20)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return "服务状态：查询超时".into();
            }
        }
    }
    let mut text = String::new();
    if let Some(output) = child.stdout.take() {
        let _ = output.take(16384).read_to_string(&mut text);
    }
    let mut out = String::new();
    for section in text.split("\n\n") {
        let fields: std::collections::HashMap<_, _> =
            section.lines().filter_map(|s| s.split_once('=')).collect();
        let name = match fields.get("Id").copied() {
            Some("uu-native-bridge.service") => "UU 主服务",
            Some("uu-native-text.service") => "文本兼容与剪贴板服务（非原生 Fcitx 输入法）",
            Some("uu-native-display.service") => "显示回滚服务",
            _ => continue,
        };
        let active = match fields.get("ActiveState").copied() {
            Some("active") => "运行中",
            Some("activating") => "启动中",
            Some("failed") => "失败",
            Some("inactive") => "已停止",
            _ => "未知",
        };
        let pid = fields
            .get("MainPID")
            .and_then(|v| v.parse::<u32>().ok())
            .unwrap_or(0);
        let restarts = fields
            .get("NRestarts")
            .and_then(|v| v.parse::<u32>().ok())
            .unwrap_or(0);
        out.push_str(&format!(
            "{name}：{active} · PID {pid} · 自动重启 {restarts} 次\n"
        ));
    }
    if out.is_empty() { out.push_str("服务状态：未找到原生服务\n"); }
    out.push_str(&audio_status(&text));
    out
}

// Reuse the bounded systemctl query above; never add synchronous unbounded
// is-active processes to a console refresh.
fn audio_status(properties: &str) -> String {
    let units = ["pipewire.service", "pipewire-pulse.service", "wireplumber.service"];
    let active = units.iter().all(|unit| {
        let mut sections = properties.split("\n\n").filter(|section| {
            section.lines().any(|line| line.strip_prefix("Id=") == Some(*unit))
        });
        let Some(section) = sections.next() else { return false; };
        sections.next().is_none() && section.lines().filter_map(|line|
            line.strip_prefix("ActiveState=")).collect::<Vec<_>>() == ["active"]
    });
    if active {
        "音频基础：PipeWire/PipeWire-Pulse/WirePlumber 均运行；UU 音频端到端、编码与远端播放尚无可核实接口。\n".into()
    } else {
        "音频基础：PipeWire 音频服务状态不可完整核实；未推断 UU 音频链路。\n".into()
    }
}

fn capture_state(path: &Path) -> Option<String> {
    crate::private_parent(path.parent()?).ok()?;
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)
        .ok()?;
    let info = file.metadata().ok()?;
    if !info.is_file() || info.uid() != unsafe { libc::getuid() } || info.mode() & 0o077 != 0 {
        return None;
    }
    let start = info.len().saturating_sub(65536);
    file.seek(SeekFrom::Start(start)).ok()?;
    let mut data = Vec::new();
    file.take(65536).read_to_end(&mut data).ok()?;
    Some(capture_summary(&data, start > 0, peer_matches))
}

fn encoder_state(path: &Path) -> String {
    let Ok(parent) = path.parent().ok_or(()) else {
        return "硬件编码：状态路径无效，未作推断。\n".into();
    };
    if crate::private_parent(parent).is_err() {
        return "硬件编码：私有目录不可核实，未作推断。\n".into();
    }
    let Ok(mut file) = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)
    else {
        return "硬件编码：本次会话尚无可核实的编码记录。\n".into();
    };
    let Ok(info) = file.metadata() else {
        return "硬件编码：本次会话状态不可核实。\n".into();
    };
    if !info.is_file()
        || info.uid() != unsafe { libc::getuid() }
        || info.mode() & 0o077 != 0
        || info.len() > 4 * 1024 * 1024
    {
        return "硬件编码：日志类型或权限不可核实。\n".into();
    }
    let start = info.len().saturating_sub(65536);
    if file.seek(SeekFrom::Start(start)).is_err() {
        return "硬件编码：本次会话状态不可核实。\n".into();
    }
    let mut data = Vec::new();
    if file.take(65536).read_to_end(&mut data).is_err() {
        return "硬件编码：本次会话状态不可核实。\n".into();
    }
    encoder_summary(&data, start > 0)
}

fn encoder_summary(data: &[u8], starts_mid_line: bool) -> String {
    const MARKER: &[u8] = b"UURB_NATIVE_ENCODER ";
    let mut latest: Option<(u64, u64, u64, u64, &'static str)> = None;
    let mut first = false;
    // A complete JSON value without its terminating newline can still be an
    // in-progress write. As with capture status, use complete records only.
    let end = data
        .iter()
        .rposition(|b| *b == b'\n')
        .map(|i| i + 1)
        .unwrap_or(0);
    for (index, line) in data[..end].split(|c| *c == b'\n').enumerate() {
        if starts_mid_line && index == 0 || line.len() > 2048 {
            continue;
        }
        let Some(offset) = line.windows(MARKER.len()).position(|v| v == MARKER) else {
            continue;
        };
        let Ok(value) = serde_json::from_slice::<Value>(&line[offset + MARKER.len()..]) else {
            continue;
        };
        let first_frame = match value["event"].as_str() {
            Some("first_frame") => true,
            Some("initialize") => {
                // A new initialization attempt invalidates the previous
                // encoder's evidence even when the new attempt fails.
                latest = None;
                false
            }
            _ => continue,
        };
        // Failed or malformed first-frame records must never upgrade an
        // initialization to success. New initializations also reset this flag.
        first = false;
        if value["status"].as_u64() != Some(0) {
            continue;
        }
        let (width, height, rate_num, rate_den) = (
            value["width"].as_u64(),
            value["height"].as_u64(),
            value["rate_num"].as_u64(),
            value["rate_den"].as_u64(),
        );
        let (Some(width), Some(height), Some(rate_num), Some(rate_den)) =
            (width, height, rate_num, rate_den)
        else {
            continue;
        };
        let codec = match value["codec"].as_u64() {
            Some(1) => "H.264",
            Some(2) => "HEVC",
            _ => continue,
        };
        if !(2..=4096).contains(&width)
            || !(2..=4096).contains(&height)
            || width % 2 != 0
            || height % 2 != 0
            || !(1..=240).contains(&rate_num)
            || !(1..=240).contains(&rate_den)
        {
            continue;
        }
        latest = Some((width, height, rate_num, rate_den, codec));
        first = first_frame;
    }
    let Some((width, height, rate_num, rate_den, codec)) = latest else {
        return "硬件编码：本次会话尚无可核实的成功初始化记录。\n".into();
    };
    // Each first-frame record is self-contained. Do not claim its initialization
    // was observed too: it may be outside this bounded tail or from another peer.
    let phase = if first { "首帧确认" } else { "初始化确认" };
    format!(
        "硬件编码记录：{codec} GPU/NVENC {phase} · {width} × {height} · {rate_num}/{rate_den} FPS\n这是本次运行日志的本地证据，不证明编码器当前仍在运行、手机端持续显示或远端 FPS。\n"
    )
}

fn peer_matches(pid: u32, ticks: u64) -> bool {
    if pid == 0 || ticks == 0 {
        return false;
    }
    let path = format!("/proc/{pid}/stat");
    let Ok(file) = OpenOptions::new().read(true).open(path) else {
        return false;
    };
    if file
        .metadata()
        .map(|m| m.uid() != unsafe { libc::getuid() })
        .unwrap_or(true)
    {
        return false;
    }
    let mut stat = String::new();
    if file.take(4096).read_to_string(&mut stat).is_err() {
        return false;
    }
    let Some((_, fields)) = stat.rsplit_once(") ") else {
        return false;
    };
    let values: Vec<_> = fields.split_whitespace().collect();
    !matches!(values.first(), Some(&"Z") | Some(&"X"))
        && values.get(19).and_then(|v| v.parse::<u64>().ok()) == Some(ticks)
}

fn capture_summary(
    data: &[u8],
    starts_mid_line: bool,
    peer_alive: impl Fn(u32, u64) -> bool,
) -> String {
    let mut generation = None;
    let mut frame = None;
    let mut ended = None;
    let mut status_unavailable = false;
    // Only complete records; an interrupted write is not a state transition.
    let end = data
        .iter()
        .rposition(|b| *b == b'\n')
        .map(|i| i + 1)
        .unwrap_or(0);
    for (index, line) in data[..end].split(|c| *c == b'\n').enumerate() {
        if starts_mid_line && index == 0 {
            continue;
        }
        if line.len() > 4096 {
            continue;
        }
        let Ok(value) = serde_json::from_slice::<Value>(line) else {
            continue;
        };
        let g = value["generation"].as_u64().filter(|g| *g > 0);
        match value["event"].as_str() {
            Some("capture_started") if g.is_some() => {
                generation = g;
                frame = None;
                ended = None;
                status_unavailable = false;
            }
            Some("capture_ended") if g.is_some() && g == generation => {
                ended = Some((frame.is_some(), value["producer_exit"].as_i64()));
                generation = None;
                frame = None;
            }
            Some("capture_frame_accepted") if g.is_some() && g == generation => {
                if let (Some(w), Some(h), Some(pid), Some(ticks)) = (
                    value["width"].as_u64(),
                    value["height"].as_u64(),
                    value["peer_pid"].as_u64(),
                    value["peer_start_ticks"].as_u64(),
                ) {
                    if (2..=4096).contains(&w)
                        && (2..=4096).contains(&h)
                        && (w | h) & 1 == 0
                        && (1..=u32::MAX as u64).contains(&pid)
                        && ticks > 0
                        && value["sequence"] == 1
                    {
                        frame = Some((w, h, pid as u32, ticks));
                    }
                }
            }
            Some("capture_status_unavailable") if g.is_some() && g == generation => {
                status_unavailable = true
            }
            _ => (),
        }
    }
    match (generation, frame, ended) {
        (Some(g), Some((w, h, pid, ticks)), _) if peer_alive(pid, ticks) =>
            format!("原生采集：第 {g} 代已确认首帧 · {w} × {h}\n采集接收进程仍存在；首帧不证明当前画面或远端 FPS。\n"),
        (Some(_), Some(_), _) => "原生采集：首帧所属进程已退出或身份变化，旧记录不能证明当前连接。\n".into(),
        (Some(g), None, _) if status_unavailable => format!("原生采集：第 {g} 代首帧状态不可核实，不能确认画面正常。\n"),
        (Some(g), None, _) => format!("原生采集：第 {g} 代等待 GPU 首帧，尚未确认画面。\n"),
        (None, _, Some((false, code))) => format!("原生采集：上次连接结束前未收到首帧确认（采集进程退出码：{}）。\n若 UU 仍显示 Wine 桌面，请重连 UU 并检查此处首帧状态。\n", code.map(|v| v.to_string()).unwrap_or_else(|| "未知".into())),
        (None, _, Some((true, Some(code)))) if code > 0 => format!("原生采集：收到过首帧，但采集进程随后异常结束（退出码 {code}）。\n首帧记录不能证明后续画面或编码正常。\n"),
        (None, _, Some((true, _))) => "原生采集：上次连接已结束，等待新的连接。\n".into(),
        _ => "原生采集：尚无已记录的连接；服务运行不等于远端正在显示 Linux 桌面。\n".into(),
    }
}

pub(crate) fn collect() -> String {
    let mut text = String::from("服务状态\n");
    text.push_str(&services());
    let Ok(parent) = directory() else {
        text.push_str("\n无法访问私有配置目录。\n");
        return text;
    };
    let Ok(config) = runtime(&parent) else {
        text.push_str("\n运行配置不可用；未读取 UU 账号或原始日志。\n");
        return text;
    };
    text.push_str("\n配置与版本\n");
    text.push_str(&text_backend_state(&parent, &config));
    text.push_str(&display_capability_state(&config));
    text.push_str(match config["cursor_mode"].as_str() {
        Some("embedded") => "已保存光标：视频内真实光标\n",
        Some("composited") => "已保存光标：GPU 合成真实光标（不等于手机画面跟随）\n",
        _ => "已保存光标：独立元数据（实验性）\n",
    });
    if matches!(config["cursor_mode"].as_str(), Some("embedded" | "composited")) {
        text.push_str(CURSOR_FOLLOW_HELP.trim_end_matches('\0'));
    }
    if let Ok(p) = crate::load_preferences(&parent) {
        text.push_str(&format!(
            "已保存速度：相对鼠标 {}% · 滚轮 {}% · {}\n",
            p.relative_percent,
            p.wheel_percent,
            if p.invert_wheel == 1 {
                "反向滚动"
            } else {
                "正常方向"
            }
        ));
    }
    if let Some(state_parent) = config["state_parent"]
        .as_str()
        .filter(|p| Path::new(p).is_absolute())
    {
        let state_parent = Path::new(state_parent);
        if let Ok(pointer) = read_private(&state_parent.join("active-runtime.json")) {
            if let Some(name) = pointer["directory"]
                .as_str()
                .filter(|n| n.starts_with("trial-") && !n.contains('/') && !n.contains(".."))
            {
                let state = state_parent.join(name);
                if let Ok(j) = read_private(&state.join("journal.json")) {
                    if let Some(release) = j["release_id"]
                        .as_str()
                        .filter(|v| v.len() == 64 && v.bytes().all(|c| c.is_ascii_hexdigit()))
                    {
                        text.push_str(&format!("最近运行记录的 bridge：{}\n", &release[..16]));
                    }
                    text.push_str(match j["capture_cursor_mode"].as_str() {
                        Some("embedded") => "该次启动光标：视频内真实光标\n",
                        Some("metadata") => "该次启动光标：独立元数据\n",
                        Some("composited") => "该次启动光标：GPU 合成真实光标\n",
                        _ => "该次启动光标：未知\n",
                    });
                    if matches!(j["capture_cursor_mode"].as_str(), Some("embedded" | "composited")) {
                        text.push_str("已知限制：当前适配版本隐藏独立光标时，UU 会丢弃该通道的位置；手机画面跟随尚未通过实测。\n");
                    }
                    let mode = &j["initial_topology"]["modes"][0];
                    if let (Some(w), Some(h)) = (mode["width"].as_u64(), mode["height"].as_u64()) {
                        if w <= 8192 && h <= 8192 {
                            text.push_str(&format!("启动时的采集尺寸：{w} × {h}\n"));
                        }
                    }
                }
                text.push_str("\n视频连接\n");
                text.push_str(
                    &capture_state(&state.join("capture.log"))
                        .unwrap_or_else(|| "采集状态：无法读取\n".into()),
                );
                text.push_str(&encoder_state(&state.join("wine.log")));
            }
        }
        if let Ok(record) = read_private(&state_parent.join("display-transaction.json")) {
            text.push_str(if record["pending"].is_null() {
                "显示切换：无待确认事务\n"
            } else {
                "显示切换：待确认，回滚保护仍有效\n"
            });
        }
    }
    text.push_str("连接路径（P2P/中继）：未知，未接入可核实接口\n实时编码/远端 FPS、丢帧率：尚未测得\n首帧确认不等于当前远端画面持续流畅。\n");
    text.push_str("远程终端：Linux PTY 接入尚未部署；官方终端会话/控制帧尚未接入监测，不宣称已支持。\n");
    text.push_str("\n主机身份\n");
    if let Ok(host) = std::fs::read_to_string("/proc/sys/kernel/hostname") {
        let host = host.trim();
        if host.len() <= 253
            && host
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || ".-_".contains(c))
        {
            text.push_str(&format!("Linux 系统主机名：{host}（只读）\n"));
        }
    }
    text.push_str("UU 设备显示名/改名：尚未接入官方接口，不用本地备注假冒改名。\n\n操作边界\n仅管理本机 bridge；不重启 RustDesk、GNOME 或 Portal。\n本页不显示账号、令牌、剪贴板内容或原始厂商日志。");
    text
}

#[no_mangle]
pub unsafe extern "C" fn uurb_console_status(output: *mut u8, capacity: usize) -> i32 {
    if output.is_null() || capacity < 2 {
        return 1;
    }
    let text = collect();
    if text.len() >= capacity {
        return 1;
    }
    std::ptr::copy_nonoverlapping(text.as_ptr(), output, text.len());
    *output.add(text.len()) = 0;
    0
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn phone_follow_help_is_shared_read_only_and_does_not_claim_acceptance() {
        let text = unsafe { std::ffi::CStr::from_ptr(uurb_cursor_follow_help().cast()) }
            .to_str().unwrap();
        assert_eq!(text, CURSOR_FOLLOW_HELP.trim_end_matches('\0'));
        for expected in ["Android 4.39.1", "iPhone 未验证", "只关闭", "智能鼠标",
                         "无需重连", "待实测", "不能代改"] {
            assert!(text.contains(expected));
        }
        assert!(text.len() < 2048);
    }

    #[test]
    fn display_capability_status_reports_schema36_without_claiming_phone_acceptance() {
        use std::os::unix::fs::PermissionsExt;
        let root = std::env::temp_dir().join(format!("uurb-display-cap-{}", std::process::id()));
        let bundle = root.join("bundle");
        std::fs::create_dir_all(&bundle).unwrap();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700)).unwrap();
        std::fs::set_permissions(&bundle, std::fs::Permissions::from_mode(0o700)).unwrap();
        std::fs::write(bundle.join("manifest.json"), r#"{
            "contract": {
                "schema_version": 36,
                "dpi_boundary": "DISPLAYCONFIG_DEVICE_INFO_GET_DPI_SCALE_and_SET_DPI_SCALE_to_native_guardian",
                "virtual_display_emulation": false
            }
        }"#).unwrap();
        std::fs::set_permissions(bundle.join("manifest.json"), std::fs::Permissions::from_mode(0o600)).unwrap();
        let status = display_capability_state(&json!({"bundle": bundle}));
        assert!(status.contains("离散比例虚报风险"));
        assert!(status.contains("手机 DPI 菜单尚未完成验收"));
        assert!(status.contains("超级屏"));
        std::fs::write(bundle.join("manifest.json"), serde_json::to_vec(&json!({
            "contract": {"schema_version": 37, "display_native_abi_version": 2,
                         "dpi_boundary": "native_guardian", "virtual_display_emulation": false}
        })).unwrap()).unwrap();
        let status = display_capability_state(&json!({"bundle": bundle}));
        assert!(status.contains("精确离散缩放 ABI v2"));
        assert!(status.contains("稀疏比例时明确不支持"));
        assert!(status.contains("手机 DPI 菜单尚未完成验收"));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn native_text_status_does_not_confuse_socket_presence_with_insertion() {
        use std::os::unix::fs::PermissionsExt;
        let parent = std::env::temp_dir().join(format!("uurb-text-status-{}", std::process::id()));
        std::fs::create_dir(&parent).unwrap();
        std::fs::set_permissions(&parent, std::fs::Permissions::from_mode(0o700)).unwrap();
        let config = json!({"state_parent": parent, "text_socket": parent.join("text.sock")});
        assert!(text_backend_state(&parent, &config).contains("Portal"));
        let settings = parent.join("text-backend.json");
        std::fs::write(&settings, r#"{"version":1,"backend":"fcitx"}"#).unwrap();
        std::fs::set_permissions(&settings, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert!(text_backend_state(&parent, &config).contains("原生 Fcitx"));
        assert!(text_backend_state(&parent, &config).contains("尚未出现"));
        let socket_path = parent.join("ime.sock");
        let socket = std::os::unix::net::UnixDatagram::bind(&socket_path).unwrap();
        std::fs::set_permissions(&socket_path, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert!(text_backend_state(&parent, &config).contains("不能据此判定输入成功"));
        drop(socket);
        std::fs::remove_file(&socket_path).unwrap();
        std::os::unix::fs::symlink(&settings, &socket_path).unwrap();
        assert!(text_backend_state(&parent, &config).contains("类型或权限不可核实"));
        std::fs::write(&settings, r#"{"version":1,"backend":"auto"}"#).unwrap();
        assert!(text_backend_state(&parent, &config).contains("配置不可核实"));
        std::fs::remove_file(&socket_path).unwrap();
        std::fs::remove_file(&settings).unwrap();
        std::fs::remove_dir(&parent).unwrap();
    }

    #[test]
    fn text_socket_listener_probe_matches_path_and_is_read_only() {
        let proc = concat!(
            "Num RefCount Protocol Flags Type St Inode Path\n",
            "0000000000000000: 00000002 00000000 00010000 0005 01 123 /tmp/uurb-text.sock\n",
            "0000000000000000: 00000002 00000000 00010000 0005 03 456 /tmp/connected.sock\n",
        );
        assert_eq!(parse_unix_socket_listener_state(proc, Path::new("/tmp/uurb-text.sock")), Some(true));
        assert_eq!(parse_unix_socket_listener_state(proc, Path::new("/tmp/connected.sock")), Some(false));
        assert_eq!(parse_unix_socket_listener_state(proc, Path::new("/tmp/missing.sock")), None);
        assert_eq!(parse_unix_socket_listener_state(proc, Path::new("/tmp/uurb-text.sock.backup")), None);
        let path = Path::new("/tmp/uurb-text.sock");
        assert_eq!(parse_unix_socket_listener_state(&proc.replace("00010000", "00000000"), path), Some(false));
        assert_eq!(parse_unix_socket_listener_state(&proc.replace("0005", "0002"), path), Some(false));
        assert_eq!(parse_unix_socket_listener_state(&proc.replace("/tmp/uurb-text.sock", "/tmp/other /tmp/uurb-text.sock"), path), None);
        let spaced = proc.replace("/tmp/uurb-text.sock", "/tmp/with space/ime.sock");
        assert_eq!(parse_unix_socket_listener_state(&spaced, Path::new("/tmp/with space/ime.sock")), Some(true));
        let duplicate = format!("{proc}0000000000000000: 00000002 00000000 00000000 0005 01 999 /tmp/uurb-text.sock\n");
        assert_eq!(parse_unix_socket_listener_state(&duplicate, path), None);
        assert_eq!(parse_unix_socket_listener_state(proc.trim_end_matches('\n'), path), None);
    }

    #[test]
    fn text_socket_live_probe_requires_listen_and_does_not_connect() {
        use std::os::{fd::{AsRawFd, FromRawFd, OwnedFd}, unix::fs::PermissionsExt};
        let parent = std::env::temp_dir().join(format!("uurb-listener-status-{}", std::process::id()));
        std::fs::create_dir(&parent).unwrap();
        std::fs::set_permissions(&parent, std::fs::Permissions::from_mode(0o700)).unwrap();
        let path = parent.join("ime.sock");
        let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
        address.sun_family = libc::AF_UNIX as _;
        let bytes = path.as_os_str().as_encoded_bytes();
        assert!(bytes.len() < address.sun_path.len());
        for (out, byte) in address.sun_path.iter_mut().zip(bytes) { *out = *byte as _; }
        let raw = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC | libc::SOCK_NONBLOCK, 0) };
        assert!(raw >= 0);
        let socket = unsafe { OwnedFd::from_raw_fd(raw) };
        assert_eq!(unsafe { libc::bind(socket.as_raw_fd(), (&address as *const libc::sockaddr_un).cast(),
            (std::mem::offset_of!(libc::sockaddr_un, sun_path) + bytes.len() + 1) as _) }, 0);
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(endpoint_listener_state(&path), Some(false));
        assert_eq!(unsafe { libc::listen(socket.as_raw_fd(), 1) }, 0);
        assert_eq!(endpoint_listener_state(&path), Some(true));
        let accepted = unsafe { libc::accept4(socket.as_raw_fd(), std::ptr::null_mut(), std::ptr::null_mut(), libc::SOCK_CLOEXEC) };
        if accepted >= 0 { unsafe { libc::close(accepted); } }
        assert_eq!(accepted, -1, "status must never connect to the listener");
        assert_eq!(std::io::Error::last_os_error().raw_os_error(), Some(libc::EAGAIN));
        drop(socket);
        assert_eq!(endpoint_listener_state(&path), None);
        std::fs::remove_file(&path).unwrap();
        std::fs::remove_dir(&parent).unwrap();
    }

    #[test]
    fn audio_status_requires_each_unique_active_host_unit() {
        let properties = "Id=pipewire.service\nActiveState=active\n\nId=pipewire-pulse.service\nActiveState=active\n\nId=wireplumber.service\nActiveState=active\n";
        let status = audio_status(properties);
        assert!(status.contains("均运行") && status.contains("尚无可核实接口"));
        for invalid in [String::new(), properties.replace("wireplumber.service", "unrelated.service"),
            properties.replace("ActiveState=active", "ActiveState=failed"),
            format!("{properties}\nId=pipewire.service\nActiveState=active\n")] {
            assert!(audio_status(&invalid).contains("不可完整核实"));
        }
    }

    fn records(values: &[Value]) -> Vec<u8> {
        values
            .iter()
            .map(|v| format!("{v}\n"))
            .collect::<String>()
            .into_bytes()
    }
    fn start() -> Value {
        json!({"event":"capture_started","generation":3})
    }
    fn accepted() -> Value {
        json!({"event":"capture_frame_accepted","generation":3,"width":1920,
        "height":1080,"peer_pid":123,"peer_start_ticks":456,"sequence":1})
    }

    #[test]
    fn idle_and_unconfirmed_are_not_online_claims() {
        assert!(
            capture_summary(&records(&[json!({"event":"ready"})]), false, |_, _| true)
                .contains("尚无")
        );
        assert!(capture_summary(&records(&[start()]), false, |_, _| true).contains("尚未确认"));
    }

    #[test]
    fn encoder_status_accepts_only_bounded_successful_codec_records() {
        let data = b"noise UURB_NATIVE_ENCODER {\"event\":\"initialize\",\"status\":0,\"width\":1024,\"height\":768,\"rate_num\":60,\"rate_den\":1,\"codec\":2}\nUURB_NATIVE_ENCODER {\"event\":\"first_frame\",\"status\":0,\"width\":1024,\"height\":768,\"rate_num\":60,\"rate_den\":1,\"codec\":2}\n";
        let text = encoder_summary(data, false);
        assert!(text.contains("HEVC") && text.contains("首帧确认") && text.contains("1024 × 768"));
        let first_only = encoder_summary(&encoder_records(&[encoder_record("first_frame", 0, 2)]), false);
        assert!(first_only.contains("首帧确认") && !first_only.contains("初始化"));
        assert!(first_only.contains("记录") && first_only.contains("不证明编码器当前仍在运行"));
        let invalid = b"UURB_NATIVE_ENCODER {\"event\":\"first_frame\",\"status\":1,\"width\":1024,\"height\":768,\"rate_num\":60,\"rate_den\":1,\"codec\":1}\n";
        assert!(encoder_summary(invalid, false).contains("尚无"));
        let secret = b"UURB_NATIVE_ENCODER {\"event\":\"first_frame\",\"status\":0,\"width\":8192,\"height\":768,\"rate_num\":60,\"rate_den\":1,\"codec\":1,\"secret\":\"PRIVATE\"}\n";
        let text = encoder_summary(secret, false);
        assert!(text.contains("尚无") && !text.contains("PRIVATE"));
    }
    fn encoder_record(event: &str, status: u32, codec: u32) -> Value {
        json!({"event":event,"status":status,"width":1024,"height":768,
               "rate_num":60,"rate_den":1,"codec":codec})
    }
    fn encoder_records(values: &[Value]) -> Vec<u8> {
        values
            .iter()
            .map(|v| format!("UURB_NATIVE_ENCODER {v}\n"))
            .collect::<String>()
            .into_bytes()
    }
    #[test]
    fn encoder_failed_or_invalid_first_frame_never_confirms_success() {
        let initialized = encoder_record("initialize", 0, 2);
        let failed = encoder_record("first_frame", 1, 2);
        let text = encoder_summary(&encoder_records(&[initialized.clone(), failed]), false);
        assert!(text.contains("初始化确认") && !text.contains("首帧确认"));
        let mut malformed = encoder_record("first_frame", 0, 2);
        malformed["width"] = json!(8192);
        let text = encoder_summary(&encoder_records(&[initialized, malformed]), false);
        assert!(text.contains("初始化确认") && !text.contains("首帧确认"));
    }
    #[test]
    fn encoder_new_initialization_discards_previous_first_frame() {
        let old = [encoder_record("initialize", 0, 2), encoder_record("first_frame", 0, 2)];
        let mut records = old.to_vec();
        records.push(encoder_record("initialize", 0, 1));
        let text = encoder_summary(&encoder_records(&records), false);
        assert!(text.contains("H.264") && text.contains("初始化确认"));
        assert!(!text.contains("HEVC") && !text.contains("首帧确认"));
        *records.last_mut().unwrap() = encoder_record("initialize", 1, 1);
        assert!(encoder_summary(&encoder_records(&records), false).contains("尚无"));
    }
    #[test]
    fn encoder_partial_tail_does_not_promote_or_clear_confirmed_evidence() {
        let initialized = encoder_record("initialize", 0, 2);
        let frame = encoder_record("first_frame", 0, 2);
        let mut data = encoder_records(&[initialized.clone()]);
        data.extend_from_slice(format!("UURB_NATIVE_ENCODER {frame}").as_bytes());
        let text = encoder_summary(&data, false);
        assert!(text.contains("初始化确认") && !text.contains("首帧确认"));
        let mut data = encoder_records(&[initialized, frame]);
        data.extend_from_slice(format!("UURB_NATIVE_ENCODER {}", encoder_record("initialize", 1, 1)).as_bytes());
        assert!(encoder_summary(&data, false).contains("首帧确认"));
        assert!(encoder_summary(b"UURB_NATIVE_ENCODER {}", false).contains("尚无"));
        assert!(encoder_summary(&encoder_records(&[encoder_record("first_frame", 0, 2)]), true).contains("尚无"));
    }
    #[test]
    fn first_frame_requires_exact_live_peer_and_current_generation() {
        let data = records(&[start(), accepted()]);
        assert!(capture_summary(&data, false, |p, t| p == 123 && t == 456).contains("1920 × 1080"));
        assert!(capture_summary(&data, false, |_, _| false).contains("旧记录不能证明"));
        let mut old = accepted();
        old["generation"] = json!(2);
        assert!(
            capture_summary(&records(&[start(), old]), false, |_, _| true)
                .contains("等待 GPU 首帧")
        );
        assert!(capture_summary(&records(&[accepted()]), false, |_, _| true).contains("尚无"));
    }
    #[test]
    fn failed_first_frame_is_distinct_from_normal_end() {
        let end = json!({"event":"capture_ended","generation":3,"producer_exit":1});
        assert!(
            capture_summary(&records(&[start(), end.clone()]), false, |_, _| true)
                .contains("未收到首帧确认")
        );
        assert!(
            capture_summary(&records(&[start(), accepted(), end]), false, |_, _| true)
                .contains("随后异常结束")
        );
        assert!(capture_summary(
            &records(&[
                start(),
                accepted(),
                json!({"event":"capture_ended","generation":3,"producer_exit":0})
            ]),
            false,
            |_, _| true
        )
        .contains("等待新的连接"));
        assert!(capture_summary(
            &records(&[
                start(),
                json!({"event":"capture_status_unavailable","generation":3})
            ]),
            false,
            |_, _| true
        )
        .contains("不可核实"));
    }
    #[test]
    fn partial_tail_and_unrelated_fields_never_become_status() {
        let mut data = records(&[start()]);
        data.extend_from_slice(accepted().to_string().as_bytes());
        assert!(capture_summary(&data, false, |_, _| true).contains("等待 GPU 首帧"));
        let mut value = accepted();
        value["width"] = json!(8192);
        value["secret"] = json!("FIXTURE_PRIVATE_SENTINEL");
        let text = capture_summary(&records(&[start(), value]), false, |_, _| true);
        assert!(text.contains("等待 GPU 首帧") && !text.contains("FIXTURE_PRIVATE_SENTINEL"));
        assert!(
            capture_summary(&records(&[start(), accepted()]), true, |_, _| true).contains("尚无")
        );
    }
    #[test]
    fn reused_pid_is_not_evidence_of_the_old_capture_peer() {
        let stat = std::fs::read_to_string("/proc/self/stat").unwrap();
        let ticks = stat
            .rsplit_once(") ")
            .unwrap()
            .1
            .split_whitespace()
            .nth(19)
            .unwrap()
            .parse::<u64>()
            .unwrap();
        let pid = std::process::id();
        assert!(peer_matches(pid, ticks));
        assert!(!peer_matches(pid, ticks + 1));
        assert!(!peer_matches(0, ticks));
    }
}
