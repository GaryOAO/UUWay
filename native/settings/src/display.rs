//! Fixed, same-UID display-guardian RPC. No shell, retries, or auto-confirmation.
use crate::{directory, invalid, private_parent, runtime};
use serde_json::{json, Value};
use std::{
    io,
    mem::{size_of, zeroed},
    os::{
        fd::{AsRawFd, FromRawFd, OwnedFd},
        unix::{
            ffi::OsStrExt,
            fs::{FileTypeExt, MetadataExt},
        },
    },
    path::Path,
    time::{Duration, Instant},
};

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Mode {
    width: u32,
    height: u32,
    refresh: f64,
    scale_count: u32,
    scales: [f64; 64],
}
#[repr(C)]
pub struct Snapshot {
    serial: u32,
    width: u32,
    height: u32,
    pending: u32,
    count: u32,
    refresh: f64,
    scale: f64,
    modes: [Mode; 256],
}
#[repr(C)]
pub struct Reply {
    changed: u32,
    serial: u32,
    seconds: u32,
    transaction: [u8; 33],
}

fn ready(fd: &OwnedFd, events: i16, deadline: Instant) -> io::Result<()> {
    loop {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .ok_or_else(invalid)?;
        let mut poll = libc::pollfd {
            fd: fd.as_raw_fd(),
            events,
            revents: 0,
        };
        let result = unsafe { libc::poll(&mut poll, 1, remaining.as_millis().max(1) as i32) };
        if result > 0 && poll.revents & events != 0 {
            return Ok(());
        }
        if result < 0 && io::Error::last_os_error().kind() == io::ErrorKind::Interrupted {
            continue;
        }
        return Err(invalid());
    }
}

fn exchange_at(path: &Path, request: Value) -> io::Result<Value> {
    private_parent(path.parent().ok_or_else(invalid)?)?;
    let info = std::fs::symlink_metadata(path)?;
    if !info.file_type().is_socket()
        || info.uid() != unsafe { libc::getuid() }
        || info.mode() & 0o077 != 0
    {
        return Err(invalid());
    }
    let mut address: libc::sockaddr_un = unsafe { zeroed() };
    let bytes = path.as_os_str().as_bytes();
    if !path.is_absolute() || bytes.len() >= address.sun_path.len() || bytes.contains(&0) {
        return Err(invalid());
    }
    address.sun_family = libc::AF_UNIX as _;
    for (a, b) in address.sun_path.iter_mut().zip(bytes) {
        *a = *b as _;
    }
    let raw = unsafe {
        libc::socket(
            libc::AF_UNIX,
            libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC | libc::SOCK_NONBLOCK,
            0,
        )
    };
    if raw < 0 {
        return Err(io::Error::last_os_error());
    }
    let fd = unsafe { OwnedFd::from_raw_fd(raw) };
    if unsafe {
        libc::connect(
            raw,
            &address as *const _ as _,
            size_of::<libc::sockaddr_un>() as _,
        )
    } != 0
    {
        return Err(io::Error::last_os_error());
    }
    let mut peer: libc::ucred = unsafe { zeroed() };
    let mut length = size_of::<libc::ucred>() as libc::socklen_t;
    if unsafe {
        libc::getsockopt(
            raw,
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            &mut peer as *mut _ as _,
            &mut length,
        )
    } != 0
        || length as usize != size_of::<libc::ucred>()
        || peer.uid != unsafe { libc::getuid() }
    {
        return Err(invalid());
    }
    let encoded = serde_json::to_vec(&request).map_err(|_| invalid())?;
    if encoded.len() > 2048 {
        return Err(invalid());
    }
    let deadline = Instant::now() + Duration::from_secs(12);
    ready(&fd, libc::POLLOUT, deadline)?;
    // A lost reply is ambiguous: never retry any display mutation.
    if unsafe {
        libc::send(
            raw,
            encoded.as_ptr() as _,
            encoded.len(),
            libc::MSG_NOSIGNAL,
        )
    } != encoded.len() as isize
    {
        return Err(invalid());
    }
    ready(&fd, libc::POLLIN, deadline)?;
    let mut bytes = vec![0u8; 65536];
    let received =
        unsafe { libc::recv(raw, bytes.as_mut_ptr() as _, bytes.len(), libc::MSG_TRUNC) };
    if received <= 0 || received as usize > bytes.len() {
        return Err(invalid());
    }
    let value: Value =
        serde_json::from_slice(&bytes[..received as usize]).map_err(|_| invalid())?;
    if value["ok"].as_bool() != Some(true) {
        return Err(invalid());
    }
    Ok(value)
}
fn exchange(request: Value) -> io::Result<Value> {
    let config = runtime(&directory()?)?;
    let state = config["state_parent"].as_str().ok_or_else(invalid)?;
    exchange_at(&Path::new(state).join("display.sock"), request)
}
fn integer(v: &Value, low: u32, high: u32) -> io::Result<u32> {
    let n = v
        .as_u64()
        .filter(|n| *n >= low as u64 && *n <= high as u64)
        .ok_or_else(invalid)?;
    Ok(n as u32)
}
fn number(v: &Value, low: f64, high: f64) -> io::Result<f64> {
    v.as_f64()
        .filter(|n| n.is_finite() && *n >= low && *n <= high)
        .ok_or_else(invalid)
}
fn mode(v: &Value) -> io::Result<Mode> {
    let width = integer(&v["width"], 2, 4096)?;
    let height = integer(&v["height"], 2, 4096)?;
    if width % 2 != 0 || height % 2 != 0 {
        return Err(invalid());
    }
    let values = v["scales"]
        .as_array()
        .filter(|v| !v.is_empty() && v.len() <= 64)
        .ok_or_else(invalid)?;
    let mut mode = Mode {
        width,
        height,
        refresh: number(&v["refresh"], 1.0, 120.0)?,
        scale_count: values.len() as u32,
        scales: [0.0; 64],
    };
    for (i, v) in values.iter().enumerate() {
        mode.scales[i] = number(v, 0.5, 4.0)?;
    }
    Ok(mode)
}
fn snapshot(v: Value) -> io::Result<Snapshot> {
    let mut out: Snapshot = unsafe { zeroed() };
    out.serial = integer(&v["serial"], 0, u32::MAX)?;
    out.width = integer(&v["width"], 2, 4096)?;
    out.height = integer(&v["height"], 2, 4096)?;
    out.refresh = number(&v["refresh"], 1.0, 120.0)?;
    out.scale = number(&v["scale"], 0.5, 4.0)?;
    out.pending = v["pending"].as_bool().ok_or_else(invalid)? as u32;
    let modes = v["modes"]
        .as_array()
        .filter(|v| !v.is_empty() && v.len() <= 256)
        .ok_or_else(invalid)?;
    out.count = modes.len() as u32;
    for (i, v) in modes.iter().enumerate() {
        out.modes[i] = mode(v)?;
    }
    if !out.modes[..modes.len()].iter().any(|m| {
        m.width == out.width
            && m.height == out.height
            && (m.refresh - out.refresh).abs() < 0.00001
            && m.scales[..m.scale_count as usize]
                .iter()
                .any(|s| (s - out.scale).abs() < 0.00001)
    }) {
        return Err(invalid());
    }
    Ok(out)
}
pub fn check() -> io::Result<()> {
    let state = snapshot(exchange(json!({"version":1,"op":"inspect"}))?)?;
    println!("display_guardian_read_only width={} height={} refresh={} scale={} modes={} pending={} desktop_changed=false",
             state.width, state.height, state.refresh, state.scale, state.count, state.pending);
    Ok(())
}
#[no_mangle]
pub unsafe extern "C" fn uurb_display_inspect(output: *mut Snapshot) -> i32 {
    if output.is_null() {
        return 1;
    }
    match exchange(json!({"version":1,"op":"inspect"})).and_then(snapshot) {
        Ok(value) => {
            output.write(value);
            0
        }
        Err(_) => 1,
    }
}
#[no_mangle]
pub unsafe extern "C" fn uurb_display_apply(
    serial: u32,
    width: u32,
    height: u32,
    refresh: f64,
    scale: f64,
    output: *mut Reply,
) -> i32 {
    if output.is_null()
        || !refresh.is_finite()
        || !scale.is_finite()
        || width < 2
        || width > 4096
        || height < 2
        || height > 4096
        || width % 2 != 0
        || height % 2 != 0
        || !(1.0..=120.0).contains(&refresh)
        || !(0.5..=4.0).contains(&scale)
    {
        return 1;
    }
    let result = (|| -> io::Result<Reply> {
        // No gpu_frame policy: only the user's Keep button confirms this UI change.
        let v = exchange(
            json!({"version":1,"op":"apply","serial":serial,"width":width,"height":height,"refresh":refresh,"scale":scale}),
        )?;
        let mut reply: Reply = zeroed();
        reply.changed = v["changed"].as_bool().ok_or_else(invalid)? as u32;
        reply.serial = integer(&v["serial"], 0, u32::MAX)?;
        if reply.changed == 1 {
            reply.seconds = integer(&v["confirmation_seconds"], 5, 60)?;
            let token = v["transaction"].as_str().ok_or_else(invalid)?;
            if !valid_transaction(token.as_bytes()) {
                return Err(invalid());
            }
            reply.transaction[..32].copy_from_slice(token.as_bytes());
        }
        Ok(reply)
    })();
    match result {
        Ok(value) => {
            output.write(value);
            0
        }
        Err(_) => 1,
    }
}
fn valid_transaction(token: &[u8]) -> bool {
    token.len() == 32 && token.iter().all(|b| b.is_ascii_hexdigit())
}
#[no_mangle]
pub unsafe extern "C" fn uurb_display_finish(confirm: u32, serial: u32, token: *const u8) -> i32 {
    if confirm > 1 || token.is_null() {
        return 1;
    }
    let token = std::slice::from_raw_parts(token, 33);
    if token[32] != 0 || !valid_transaction(&token[..32]) {
        return 1;
    }
    let mut request = json!({"version":1,"op":if confirm == 1 {"confirm"} else {"rollback"},"transaction":std::str::from_utf8(&token[..32]).unwrap()});
    if confirm == 1 {
        request["serial"] = json!(serial);
    }
    match exchange(request) {
        Ok(v)
            if v[if confirm == 1 {
                "confirmed"
            } else {
                "rolled_back"
            }]
            .as_bool()
                == Some(true) =>
        {
            0
        }
        _ => 1,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{DirBuilderExt, PermissionsExt};

    fn fixture_exchange(reply: Option<Vec<u8>>) -> io::Result<Value> {
        let unique = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let parent = std::env::temp_dir().join(format!("uurb-rpc-{}-{unique}", std::process::id()));
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&parent)
            .unwrap();
        let path = parent.join("display.sock");
        let mut address: libc::sockaddr_un = unsafe { zeroed() };
        address.sun_family = libc::AF_UNIX as _;
        let path_bytes = path.as_os_str().as_bytes();
        assert!(path_bytes.len() < address.sun_path.len());
        for (a, b) in address.sun_path.iter_mut().zip(path_bytes) {
            *a = *b as _;
        }
        let raw = unsafe {
            libc::socket(
                libc::AF_UNIX,
                libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC | libc::SOCK_NONBLOCK,
                0,
            )
        };
        assert!(raw >= 0);
        let listener = unsafe { OwnedFd::from_raw_fd(raw) };
        assert_eq!(
            unsafe {
                libc::bind(
                    raw,
                    &address as *const _ as _,
                    size_of::<libc::sockaddr_un>() as _,
                )
            },
            0
        );
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(unsafe { libc::listen(raw, 1) }, 0);
        let thread = std::thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(3);
            ready(&listener, libc::POLLIN, deadline).unwrap();
            let raw = unsafe {
                libc::accept4(
                    listener.as_raw_fd(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    libc::SOCK_CLOEXEC | libc::SOCK_NONBLOCK,
                )
            };
            assert!(raw >= 0);
            let peer = unsafe { OwnedFd::from_raw_fd(raw) };
            ready(&peer, libc::POLLIN, deadline).unwrap();
            let mut data = [0u8; 2048];
            let count = unsafe { libc::recv(raw, data.as_mut_ptr() as _, data.len(), 0) };
            assert!(count > 0);
            let request: Value = serde_json::from_slice(&data[..count as usize]).unwrap();
            assert_eq!(request, json!({"version":1,"op":"inspect"}));
            if let Some(bytes) = reply {
                ready(&peer, libc::POLLOUT, deadline).unwrap();
                assert_eq!(
                    unsafe {
                        libc::send(raw, bytes.as_ptr() as _, bytes.len(), libc::MSG_NOSIGNAL)
                    },
                    bytes.len() as isize
                );
            }
        });
        let result = exchange_at(&path, json!({"version":1,"op":"inspect"}));
        thread.join().unwrap();
        std::fs::remove_file(path).unwrap();
        std::fs::remove_dir(parent).unwrap();
        result
    }

    #[test]
    fn same_uid_rpc_bounds_and_lost_reply() {
        let value = fixture_exchange(Some(br#"{"ok":true,"pending":false}"#.to_vec())).unwrap();
        assert_eq!(value["pending"], false);
        assert!(fixture_exchange(Some(vec![b' '; 65537])).is_err());
        assert!(fixture_exchange(Some(br#"{"ok":false}"#.to_vec())).is_err());
        assert!(fixture_exchange(None).is_err());
    }

    #[test]
    fn advertised_modes_and_bounds() {
        let v = json!({"serial":7,"width":1920,"height":1080,"refresh":60,"scale":1,"pending":false,
            "modes":[{"width":1920,"height":1080,"refresh":60,"scales":[1,1.5,2]}]});
        let out = snapshot(v.clone()).unwrap();
        assert_eq!(out.modes[0].scale_count, 3);
        for field in ["serial", "pending", "scale", "modes"] {
            let mut bad = v.clone();
            bad[field] = Value::Null;
            assert!(snapshot(bad).is_err());
        }
        let mut bad = v.clone();
        bad["scale"] = json!(1.25);
        assert!(snapshot(bad).is_err());
        let mut bad = v;
        bad["modes"][0]["width"] = json!(1919);
        assert!(snapshot(bad).is_err());
        assert!(!valid_transaction(b"bad-token"));
        assert!(!valid_transaction(&[b'x'; 32]));
        assert!(valid_transaction(&[b'a'; 32]));
        assert_eq!(size_of::<Mode>(), 536);
        assert_eq!(size_of::<Snapshot>(), 137256);
        assert_eq!(size_of::<Reply>(), 48);
    }
}
