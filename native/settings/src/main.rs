use serde_json::{json, Value};
mod display;
mod status;
use std::{
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    os::unix::{
        fs::{MetadataExt, OpenOptionsExt},
        io::AsRawFd,
    },
    path::{Path, PathBuf},
};

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Preferences {
    relative_percent: u32,
    wheel_percent: u32,
    invert_wheel: u32,
    cursor_metadata: u32,
}
impl Default for Preferences {
    fn default() -> Self {
        Self {
            relative_percent: 100,
            wheel_percent: 100,
            invert_wheel: 0,
            cursor_metadata: 0,
        }
    }
}
fn invalid() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "Invalid private bridge settings",
    )
}
fn directory() -> io::Result<PathBuf> {
    if unsafe { libc::getuid() } == 0 {
        return Err(invalid());
    }
    let home = std::env::var_os("HOME").ok_or_else(invalid)?;
    let path = PathBuf::from(home).join(".config/uurb");
    private_parent(&path)?;
    Ok(path)
}
fn private_parent(path: &Path) -> io::Result<()> {
    let m = fs::symlink_metadata(path)?;
    if !path.is_absolute()
        || !m.is_dir()
        || m.uid() != unsafe { libc::getuid() }
        || m.mode() & 0o077 != 0
    {
        return Err(invalid());
    }
    Ok(())
}
fn read_private(path: &Path) -> io::Result<Value> {
    private_parent(path.parent().ok_or_else(invalid)?)?;
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)?;
    let m = file.metadata()?;
    if !m.is_file()
        || m.uid() != unsafe { libc::getuid() }
        || m.mode() & 0o077 != 0
        || m.len() == 0
        || m.len() > 65536
    {
        return Err(invalid());
    }
    let mut data = Vec::new();
    file.take(65537).read_to_end(&mut data)?;
    if data.len() > 65536 {
        return Err(invalid());
    }
    serde_json::from_slice(&data).map_err(|_| invalid())
}
fn parse_preferences(value: &Value) -> io::Result<Preferences> {
    if value.as_object().map(|v| v.len()) != Some(4) || value["version"].as_u64() != Some(1) {
        return Err(invalid());
    }
    let relative = value["relative_percent"].as_u64().ok_or_else(invalid)?;
    let wheel = value["wheel_percent"].as_u64().ok_or_else(invalid)?;
    let invert = value["invert_wheel"].as_bool().ok_or_else(invalid)?;
    if !(25..=400).contains(&relative) || !(25..=400).contains(&wheel) {
        return Err(invalid());
    }
    Ok(Preferences {
        relative_percent: relative as u32,
        wheel_percent: wheel as u32,
        invert_wheel: invert as u32,
        cursor_metadata: 0,
    })
}
fn load_preferences(parent: &Path) -> io::Result<Preferences> {
    match read_private(&parent.join("input-settings.json")) {
        Ok(v) => parse_preferences(&v),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(Preferences::default()),
        Err(e) => Err(e),
    }
}
fn runtime(parent: &Path) -> io::Result<Value> {
    let value = read_private(&parent.join("native-runtime.json"))?;
    let expected = [
        "schema",
        "prefix",
        "bundle",
        "restore_state",
        "state_parent",
        "text_socket",
        "cursor_mode",
    ];
    let object = value.as_object().ok_or_else(invalid)?;
    if object.len() != expected.len()
        || expected.iter().any(|k| !object.contains_key(*k))
        || value["schema"].as_u64() != Some(1)
        || !matches!(
            value["cursor_mode"].as_str(),
            Some("embedded" | "metadata" | "composited")
        )
    {
        return Err(invalid());
    }
    Ok(value)
}

fn cursor_index(mode: &Value) -> io::Result<u32> {
    match mode.as_str() {
        Some("embedded") => Ok(0),
        Some("metadata") => Ok(1),
        Some("composited") => Ok(2),
        _ => Err(invalid()),
    }
}

// UI availability check only; the service still verifies the exact release
// contract and every component hash before it can start this mode.
fn composited_available(value: &Value) -> io::Result<()> {
    let bundle = Path::new(value["bundle"].as_str().ok_or_else(invalid)?);
    if !bundle.is_absolute() {
        return Err(invalid());
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(bundle.join("manifest.json"))?;
    let info = file.metadata()?;
    if !info.is_file()
        || info.uid() != unsafe { libc::getuid() }
        || info.mode() & 0o022 != 0
        || info.len() > 65536
    {
        return Err(invalid());
    }
    let mut data = Vec::new();
    file.take(65537).read_to_end(&mut data)?;
    if data.len() > 65536 {
        return Err(invalid());
    }
    let manifest: Value = serde_json::from_slice(&data).map_err(|_| invalid())?;
    if manifest["contract"]["cursor_composited_policy_field"]
        != "reserved[0]=2; metadata_source; GPU_single_sprite; native_USER32_positions"
    {
        return Err(invalid());
    }
    Ok(())
}
fn lock(parent: &Path) -> io::Result<File> {
    private_parent(parent)?;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(parent.join(".settings.lock"))?;
    let m = file.metadata()?;
    if !m.is_file() || m.uid() != unsafe { libc::getuid() } || m.mode() & 0o077 != 0 {
        return Err(invalid());
    }
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(file)
}
fn write_private(path: &Path, value: &Value) -> io::Result<()> {
    let parent = path.parent().ok_or_else(invalid)?;
    private_parent(parent)?;
    match read_private(path) {
        Ok(_) => (),
        Err(e) if e.kind() == io::ErrorKind::NotFound => (),
        Err(e) => return Err(e),
    }
    let mut random = [0u8; 8];
    File::open("/dev/urandom")?.read_exact(&mut random)?;
    let temporary = parent.join(format!(".settings-{:016x}", u64::from_ne_bytes(random)));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(&temporary)?;
    let result = (|| {
        let data = serde_json::to_vec(value).map_err(|_| invalid())?;
        file.write_all(&data)?;
        file.sync_all()?;
        fs::rename(&temporary, path)?;
        File::open(parent)?.sync_all()
    })();
    if temporary.exists() {
        let _ = fs::remove_file(temporary);
    }
    result
}
fn save_preferences(parent: &Path, prefs: Preferences) -> io::Result<()> {
    let value = json!({"version":1,"relative_percent":prefs.relative_percent,"wheel_percent":prefs.wheel_percent,"invert_wheel":prefs.invert_wheel == 1});
    if prefs.invert_wheel > 1 {
        return Err(invalid());
    }
    parse_preferences(&value)?;
    let _lock = lock(parent)?;
    load_preferences(parent)?;
    write_private(&parent.join("input-settings.json"), &value)
}
#[no_mangle]
pub unsafe extern "C" fn uurb_preferences_load(output: *mut Preferences) -> i32 {
    let result = (|| {
        if output.is_null() {
            return Err(invalid());
        }
        let parent = directory()?;
        let mut prefs = load_preferences(&parent)?;
        prefs.cursor_metadata = cursor_index(&runtime(&parent)?["cursor_mode"])?;
        output.write(prefs);
        Ok(())
    })();
    if result.is_ok() {
        0
    } else {
        1
    }
}
#[no_mangle]
pub extern "C" fn uurb_preferences_save(
    relative_percent: u32,
    wheel_percent: u32,
    invert_wheel: u32,
) -> i32 {
    let result = directory().and_then(|p| {
        save_preferences(
            &p,
            Preferences {
                relative_percent,
                wheel_percent,
                invert_wheel,
                cursor_metadata: 0,
            },
        )
    });
    if result.is_ok() {
        0
    } else {
        1
    }
}
#[no_mangle]
pub extern "C" fn uurb_cursor_save(metadata: u32) -> i32 {
    let result = (|| {
        if metadata > 2 {
            return Err(invalid());
        }
        let parent = directory()?;
        let _lock = lock(&parent)?;
        let mut value = runtime(&parent)?;
        if metadata == 2 {
            composited_available(&value)?;
        }
        value["cursor_mode"] = json!(if metadata == 2 {
            "composited"
        } else if metadata == 1 {
            "metadata"
        } else {
            "embedded"
        });
        write_private(&parent.join("native-runtime.json"), &value)
    })();
    if result.is_ok() {
        0
    } else {
        1
    }
}
extern "C" {
    fn uurb_settings_gui(smoke: i32) -> i32;
}
fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let smoke = match args.as_slice() {
        [] => false,
        [arg] if arg == "--smoke-test" => true,
        [arg] if arg == "--status" => {
            println!("{}", status::collect());
            return;
        }
        [arg] if arg == "--display-check" => {
            if display::check().is_err() {
                eprintln!("Display guardian read-only check failed");
                std::process::exit(1);
            }
            return;
        }
        [arg] if arg == "--help" => {
            println!(
                "uuway-console [--smoke-test|--display-check|--status] — UUWay 控制台"
            );
            return;
        }
        _ => {
            eprintln!("Unsupported settings argument");
            std::process::exit(2);
        }
    };
    std::process::exit(unsafe { uurb_settings_gui(smoke as i32) });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{symlink, PermissionsExt};
    #[test]
    fn cursor_policy_indices_are_distinct() {
        assert_eq!(cursor_index(&json!("embedded")).unwrap(), 0);
        assert_eq!(cursor_index(&json!("metadata")).unwrap(), 1);
        assert_eq!(cursor_index(&json!("composited")).unwrap(), 2);
        assert!(cursor_index(&json!("unknown")).is_err());
    }
    #[test]
    fn preferences_bounds_and_types() {
        let valid =
            json!({"version":1,"relative_percent":100,"wheel_percent":25,"invert_wheel":false});
        assert_eq!(parse_preferences(&valid).unwrap().wheel_percent, 25);
        for value in [
            json!(24),
            json!(401),
            json!(true),
            json!(100.5),
            json!(null),
        ] {
            let mut bad = valid.clone();
            bad["wheel_percent"] = value;
            assert!(parse_preferences(&bad).is_err());
        }
    }
    #[test]
    fn private_atomic_roundtrip_and_symlink_preservation() {
        let mut random = [0u8; 8];
        File::open("/dev/urandom")
            .unwrap()
            .read_exact(&mut random)
            .unwrap();
        let parent = std::env::temp_dir().join(format!(
            "uuway-console-test-{:016x}",
            u64::from_ne_bytes(random)
        ));
        fs::create_dir(&parent).unwrap();
        fs::set_permissions(&parent, fs::Permissions::from_mode(0o700)).unwrap();
        assert_eq!(load_preferences(&parent).unwrap(), Preferences::default());
        save_preferences(
            &parent,
            Preferences {
                wheel_percent: 150,
                ..Preferences::default()
            },
        )
        .unwrap();
        let path = parent.join("input-settings.json");
        assert_eq!(load_preferences(&parent).unwrap().wheel_percent, 150);
        assert_eq!(path.metadata().unwrap().mode() & 0o777, 0o600);
        fs::rename(&path, parent.join("saved")).unwrap();
        symlink(parent.join("saved"), &path).unwrap();
        assert!(save_preferences(&parent, Preferences::default()).is_err());
        assert_eq!(
            read_private(&parent.join("saved")).unwrap()["wheel_percent"],
            150
        );
        fs::remove_file(path).unwrap();
        fs::remove_file(parent.join("saved")).unwrap();
        fs::remove_file(parent.join(".settings.lock")).unwrap();
        fs::remove_dir(parent).unwrap();
    }
}
