# Experimental distribution packages

Debian 13, Fedora 44 and Arch Linux have experimental support, not yet tested on
real hardware. Compatibility reports via [Issues](https://github.com/GaryOAO/UUWay/issues/new?template=compatibility-report.yml)
and code contributions via [PRs](https://github.com/GaryOAO/UUWay/compare) are welcome.

| Target | Build image | Outputs |
| --- | --- | --- |
| `debian-13` | Debian 13 | UUWay and Wine 11.0 `.deb` |
| `fedora-44` | Fedora 44 | UUWay and Wine 11.0 `.rpm` |
| `arch` | Arch Linux | UUWay and Wine 11.0 `.pkg.tar.zst`, generated PKGBUILDs |

All builds are x86_64 and carry NVIDIA NVENC and AMD VA-API backends. GPU drivers
and the UU Windows program are not included. Arch derivatives require separate
validation; sharing a package manager does not establish desktop compatibility.
KDE needs its own backend. Hyprland has a separate [single-monitor experiment](hyprland.md).

## Build

Docker, Git, Python 3 and tar are required on the host. Only tracked and
non-ignored source files, including local edits, enter the build container.
Compiled host artifacts and host packages are never used or changed.

```bash
packaging/build-linux.sh --target debian-13 --version 1.4.3
packaging/build-linux.sh --target fedora-44 --version 1.4.3
packaging/build-linux.sh --target arch --version 1.4.3
```

Outputs go to `build/linux/<target>/`. Use `--output-dir DIR` to override it;
`--keep` preserves the build container for debugging. `UUWAY_BUILD_JOBS` and
`UUWAY_BUILD_CPUS` default to 8; set both to 4 for concurrent local builds. The
first build also compiles Wine, so it takes longer than repackaging binaries.
The **Experimental Linux packages** GitHub Actions workflow builds all three
targets on demand, uploads artifacts, and does not publish a release.

A second, clean container installs the resulting packages without the build
dependencies present. Checks cover CLI/console entry points, both user-owned GPU
bundles, and ELF library resolution. CUDA comes from the user's NVIDIA driver;
the optional Fcitx5 add-on needs its libraries only when Fcitx5 is used. These
exceptions do not replace real GPU or logged-in desktop tests.

## Install

Install UUWay and its accompanying `uuway-wine11` package together:

```bash
# Debian 13
sudo apt install ./uuway-wine11_*_trixie_amd64.deb ./uuway_*_trixie_amd64.deb

# Fedora 44
sudo dnf install ./uuway-wine11-*.fedora44.x86_64.rpm ./uuway-[0-9]*.fedora44.x86_64.rpm

# Arch Linux
sudo pacman -U ./uuway-wine11-*.pkg.tar.zst ./uuway-[0-9]*.pkg.tar.zst
```

Run `uuway setup` as your regular user in GNOME Wayland. Setup searches Downloads
for a Windows installer, then downloads one from
`https://api.nrd.nie.163.com/api/v1/release/dl/1?channel=gwqd` if none is found.
Use `uuway setup --installer /path/to/UURemote_Setup.exe` for a local file.
Downloads use HTTPS, accept only the official API and NetEase download CDN, and
check completeness and the Windows executable header before installation.

## Wine and updates

The adapter is compiled against Wine 11.0's private server protocol 930.
The companion package installs source-built Wine 11.0 in `/opt/uuway-wine11`.
It neither replaces `/usr/bin/wine` nor uses a rolling Wine version. The
x86/x86_64 WoW64 build uses 64-bit host libraries. Newer Wine versions are outside
this change's scope.

The unmodified Wine source archive, SHA256 checksum and build script accompany
the binary packages. Build options are in `packaging/build-wine11.sh`.

Install future versions through the same package manager. Built-in automatic
installation and rollback are Ubuntu-specific and disabled by package metadata
on these three targets, so an Ubuntu deb cannot replace a native package.
The existing Ubuntu OTA behavior is retained.
