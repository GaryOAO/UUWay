use std::{env, path::PathBuf, process::Command};
fn main() {
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    let flags = Command::new("pkg-config")
        .args(["--cflags", "--libs", "gtk+-3.0"])
        .output()
        .unwrap();
    assert!(
        flags.status.success(),
        "GTK 3 development headers are required"
    );
    let flags = String::from_utf8(flags.stdout).unwrap();
    let mut cc = Command::new("cc");
    cc.args([
        "-std=c11", "-O2", "-fPIC", "-Wall", "-Wextra", "-Werror", "-c", "src/ui.c", "-o",
    ])
    .arg(out.join("ui.o"));
    for flag in flags
        .split_whitespace()
        .filter(|f| !f.starts_with("-l") && !f.starts_with("-L"))
    {
        cc.arg(flag);
    }
    assert!(cc.status().unwrap().success());
    assert!(Command::new("ar")
        .arg("crs")
        .arg(out.join("libuurb_ui.a"))
        .arg(out.join("ui.o"))
        .status()
        .unwrap()
        .success());
    println!("cargo:rustc-link-search=native={}", out.display());
    println!("cargo:rustc-link-lib=static=uurb_ui");
    for flag in flags.split_whitespace() {
        if let Some(name) = flag.strip_prefix("-l") {
            println!("cargo:rustc-link-lib={name}");
        }
        if let Some(path) = flag.strip_prefix("-L") {
            println!("cargo:rustc-link-search=native={path}");
        }
    }
    println!("cargo:rerun-if-changed=src/ui.c");
    println!("cargo:rerun-if-changed=src/display_ui.h");
}
