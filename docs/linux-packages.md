# Experimental distribution packages

Debian 13, Fedora 44 / 45 and Arch Linux have experimental support, not yet tested on
real hardware. Compatibility reports via [Issues](https://github.com/GaryOAO/UUWay/issues/new?template=compatibility-report.yml)
and code contributions via [PRs](https://github.com/GaryOAO/UUWay/compare) are welcome.

| Target | Build image | Outputs |
| --- | --- | --- |
| `debian-13` | Debian 13 | UUWay `.deb` |
| Fedora | Fedora 44 + 45 | one shared UUWay `.rpm` |
| `arch` | Arch Linux | UUWay `.pkg.tar.zst`, generated PKGBUILD |

All builds are x86_64 and carry NVIDIA NVENC and AMD VA-API backends. GPU drivers
and the UU Windows program are not included. Arch derivatives require separate
validation; sharing a package manager does not establish desktop compatibility.
KDE needs its own backend. Hyprland has a separate [single-monitor experiment](hyprland.md).

## Build

Docker, Git, Python 3 and tar are required on the host. Only tracked and
non-ignored source files, including local edits, enter the build container.
Compiled host artifacts and host packages are never used or changed.

```bash
packaging/build-linux.sh --target debian-13 --version 1.5.0
packaging/build-linux.sh --target arch --version 1.5.0
```

Outputs go to `build/linux/<target>/`. Use `--output-dir DIR` to override it;
`--keep` preserves the build container for debugging. `UUWAY_BUILD_JOBS` and
`UUWAY_BUILD_CPUS` default to 8; set both to 4 for concurrent local builds. The
build downloads official prebuilt Wine 11.0 and compiles only UUWay.
The **Experimental Linux packages** GitHub Actions workflow builds Ubuntu and all
the native targets and uploads artifacts. Fedora builds are combined into one RPM,
then that same package is install-checked on both Fedora 44 and 45.
The **Release** workflow runs these checks before assembling a complete draft release.

A second, clean container installs the resulting packages without the build
dependencies present. Checks cover CLI/console entry points, both user-owned GPU
bundles, and ELF library resolution. CUDA comes from the user's NVIDIA driver;
the optional Fcitx5 add-on needs its libraries only when Fcitx5 is used. These
exceptions do not replace real GPU or logged-in desktop tests.

## Install

Install the package for your distribution:

```bash
# Debian 13
sudo apt install ./uuway_*_trixie_amd64.deb

# Fedora 44 / 45 (same package)
sudo dnf install ./uuway-[0-9]*.fedora.x86_64.rpm

# Arch Linux
sudo pacman -U ./uuway-[0-9]*.pkg.tar.zst
```

Run `uuway setup` as your regular user in GNOME Wayland. Setup searches Downloads
for a Windows installer, then downloads one from
`https://api.nrd.nie.163.com/api/v1/release/dl/1?channel=gwqd` if none is found.
Use `uuway setup --installer /path/to/UURemote_Setup.exe` for a local file.
Downloads use HTTPS, accept only the official API and NetEase download CDN, and
check completeness and the Windows executable header before installation.

## Wine and updates

The adapter is compiled against Wine 11.0's private server protocol 930.
Debian and Fedora 44 use WineHQ's official prebuilt packages, pinned to 11.0.
Fedora 44 and 45 share one RPM. Three AMD components have separate FFmpeg builds;
UUWay selects them automatically. Other files are shared.
Fedora 45 and Arch use Arch's official Wine 11.0 archive in a separate directory
and verify its SHA256 checksum. Fedora 45 uses `/opt/uuway-wine11`; Arch uses the
user's UUWay data directory. No target compiles Wine or follows rolling Wine
updates. Newer Wine versions are outside this change's scope.

Use `uuway update install` or the console to update. Package metadata selects the
matching distribution asset; an Ubuntu deb cannot replace a native package.
SHA256SUMS remains shared across all assets so existing Ubuntu clients can update.

Updates back up the old package before installation and retain it for offline
rollback. Debian/Ubuntu rebuild it from installed files; Fedora/Arch first download
and verify the installed version's release asset. If that backup is unavailable,
the update stops before installation. Unreleased development packages require one
manual installation of a public release. `uuway update rollback` restores the saved
version. Actions exercises real package upgrades and automatic/manual rollback in
disposable containers, with desktop health simulated.
