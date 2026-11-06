<div align="center">

<img src="docs/images/hero.en.svg" alt="UUWay: a Linux host for NetEase UU Remote" width="100%">

[简体中文](README.md) · [English](README.en.md)

![Ubuntu](https://img.shields.io/badge/Ubuntu-E95420?logo=ubuntu&logoColor=white)
[![Debian](https://img.shields.io/badge/Debian-A81D33?logo=debian&logoColor=white)](docs/linux-packages.md)
[![Fedora](https://img.shields.io/badge/Fedora-51A2DA?logo=fedora&logoColor=white)](docs/linux-packages.md)
[![Arch Linux](https://img.shields.io/badge/Arch_Linux-1793D1?logo=archlinux&logoColor=white)](docs/linux-packages.md)
<br>
![GNOME Wayland](https://img.shields.io/badge/GNOME-Wayland-4A86CF?logo=gnome&logoColor=white)
![NVIDIA NVENC](https://img.shields.io/badge/NVIDIA-NVENC-76B900?logo=nvidia&logoColor=white)
![AMD VA-API](https://img.shields.io/badge/AMD-VA--API-ED1C24?logo=amd&logoColor=white)
![Wine](https://img.shields.io/badge/Wine-A30000)
![License AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-0ea5e9)

</div>

UUWay runs the official Windows UU Remote host on Linux so you can control your desktop from a phone or another device. UU's files stay unmodified; native Linux components provide capture, input and display management.

## UU feature support

The following common UU features are adapted for Linux. See [supported scope](#supported-scope) for limitations.

| UU feature | Linux support |
| --- | --- |
| Video streaming | H.264 / HEVC through NVIDIA NVENC or AMD VA-API |
| Mouse pointer | Native cursor theme, position and shape |
| Keyboard and mouse | Remote input, including Win / ⌘ keys |
| Phone input method | Commit Chinese and other text to the focused field through Fcitx5 |
| Multiple displays | Independent capture and display selection from the controller |
| Resolution, refresh rate and scale | Select modes exposed by Linux, with automatic rollback on failure |
| Super Screen | Virtual displays and exclusive mode; restore original displays on exit or failure |
| Remote terminal | Linux login shells, multiple sessions that keep running when you leave the page |
| Clipboard | Bidirectional text sync |
| File transfer | Phone ↔ Linux; files from other devices arrive in the mapped Linux receive directory |

**UUWay Console** also manages services, input, displays, file directories and software updates. It supports manual and automatic update checks; installation requires confirmation.

## Install

Mainline targets **GNOME Wayland**, with an NVIDIA GPU supporting NVENC or an AMD GPU supporting H.264/HEVC VA-API. Ubuntu 24.04 / GNOME 46 is the reference environment; Ubuntu 26.04 also has compatibility fixes and hardware validation. Keep the desktop logged in. A machine without a monitor still needs a desktop output, such as a display dummy plug.

1. Download the `.deb` from [Releases](https://github.com/GaryOAO/UUWay/releases/latest).
2. Install the package and run setup as your normal user:

   ```bash
   sudo apt install ./uuway_*_amd64.deb
   uuway setup
   ```

3. Setup looks for a UU Windows installer in your Downloads folder, then downloads it from UU's official endpoint if none is found. Use `--installer PATH` to supply a local file. Sign in to UU and allow screen sharing and the text service when prompted. Open **UUWay Console** from the application menu, then connect from your controller.

Ubuntu packages are built on Ubuntu 24.04; other distributions have [native packages](docs/linux-packages.md). [Build from source](docs/build.md) if dependencies differ. Setup installs and pins Wine 11, verifies the encoder and configures user services. It can be run again. NVIDIA is tried first, then AMD; GPU drivers are not installed or switched. After first enabling the Fcitx5 add-on, restart the input method with `fcitx5 -r`. The console and wizard currently use Chinese.

## Common commands

```bash
uuway doctor                   # Check environment and services
uuway update                   # Check for updates
uuway update install           # Install an update
uuway update status            # View the result
uuway update rollback          # Roll back an update
uuway refresh --restart-display # Reload display service; removes Super Screens
```

The console checks for updates every six hours while open; automatic checks can be disabled. Updating briefly disconnects remote sessions. Package installations select the matching distribution's update asset; source installations should rebuild from updated source.

For remote-only use, `uuway autologin on` enables GDM login and relogin after the desktop exits. Anyone with physical access can also enter the desktop; consider that before enabling it. Disable with `uuway autologin off`.

To uninstall, run `uuway uninstall`, then `sudo apt remove uuway`. The Wine prefix and UU login are retained.

## Supported scope

The following **native x86_64 packages** include both NVIDIA NVENC and AMD VA-API backends:

| Distribution | Package | Current support |
| --- | --- | --- |
| Ubuntu 24.04 | `.deb` | Primary development and daily-use environment |
| Ubuntu 26.04 | `.deb` | Community hardware validation available |
| Debian 13 (trixie) | `.deb` | Experimental support |
| Fedora 44 / 45 | One shared `.rpm` | Experimental support |
| Arch Linux | `.pkg.tar.zst` | Experimental support |

Debian, Fedora and Arch pass build, clean-install, library-dependency and update/rollback checks; desktop and GPU streaming still need hardware feedback. See the [distribution installation guide](docs/linux-packages.md) for package selection and installation. Arch derivatives require separate validation.

Desktop support:

- **GNOME Wayland**: the main backend, including multiple displays, resolution/refresh-rate/scaling changes and Super Screen.
- **Hyprland**: [experimental single-monitor support](docs/hyprland.md) for capture and basic keyboard/mouse input; remote display-mode changes, multiple displays and Super Screen are not supported yet.
- **KDE Plasma / X11**: outside the current mainline support scope. Early community KDE-branch feedback is recorded in [#14](https://github.com/GaryOAO/UUWay/issues/14).

<details>
<summary>Historical community tests and troubleshooting</summary>

These reports describe their original versions and environments. The list above describes current support.

| Environment | Versions and feedback at the time | Source |
| --- | --- | --- |
| Ubuntu 26.04.1 / GNOME Wayland / AMD Radeon 780M | Wine 11.0 / UU 4.42.1; Python 3.14 text-service support and the OSC 3008 terminal fix have been merged | [#4](https://github.com/GaryOAO/UUWay/pull/4) |
| Debian 13 / GNOME 48.7 / NVIDIA RTX 3090 | UUWay 1.2.0; a startup hang was resolved through local source changes whose details were not provided | [#15](https://github.com/GaryOAO/UUWay/issues/15) |
| Manjaro / Plasma KWin 6.7.4 / NVIDIA RTX 4060 Laptop | Community KDE branch `60f5c8c`; only a single display was validated, with extra displays temporarily disabled | [#14](https://github.com/GaryOAO/UUWay/issues/14) |

</details>

[Compatibility reports](https://github.com/GaryOAO/UUWay/issues/new?template=compatibility-report.yml) and [PRs](https://github.com/GaryOAO/UUWay/compare) are welcome.

- Up to eight active displays. Mirroring, rotation and Intel-only encoding are unsupported. AMD uses host-memory copies; NVIDIA uses a zero-copy GPU path.
- Super Screen can still show two cursors. Display modes, drivers and hardware limit resolution, refresh rate and streaming performance; a 144 Hz mode does not guarantee a 144 fps stream.
- Works inside a logged-in desktop session, not at the GDM login screen. Layout changes are temporary; save permanent layouts in GNOME Settings.

## How it works

UU handles accounts, connections and bitrate negotiation. The zero-copy GPU diagram describes NVIDIA; AMD uses host-memory copies.

<img src="docs/images/architecture.en.svg" alt="UUWay architecture" width="100%">

<img src="docs/images/video-pipeline.en.svg" alt="UUWay video pipeline (NVIDIA)" width="100%">

## Develop

```bash
git clone https://github.com/GaryOAO/UUWay.git
cd UUWay
./install.sh --installer ~/Downloads/UURemote_Setup.exe
```

[Build and troubleshooting](docs/build.md) · [Design](docs/design.md) · [Super Screen and recovery](docs/super-screen-lifecycle.md) · [AMD backend](docs/amd-vaapi.md)

## License and credits

[GNU AGPL-3.0](LICENSE). UUWay is an independent, unofficial project, unaffiliated with NetEase.

Derived from [uu-remote-ubuntu-bridge](https://github.com/lachlanchen/uu-remote-ubuntu-bridge), and built on [Wine](https://www.winehq.org/), [DXVK](https://github.com/doitsujin/dxvk), PipeWire and Mutter.
