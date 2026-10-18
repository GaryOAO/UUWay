<div align="center">

<img src="docs/images/hero.en.svg" alt="UUWay: a Linux host for NetEase UU Remote" width="100%">

[简体中文](README.md) · [English](README.en.md)

![Ubuntu 24.04](https://img.shields.io/badge/Ubuntu-24.04-E95420?logo=ubuntu&logoColor=white)
![GNOME 46 Wayland](https://img.shields.io/badge/GNOME_46-Wayland-4A86CF?logo=gnome&logoColor=white)
![NVIDIA NVENC](https://img.shields.io/badge/NVIDIA-NVENC-76B900?logo=nvidia&logoColor=white)
![AMD VA-API](https://img.shields.io/badge/AMD-VA--API-ED1C24?logo=amd&logoColor=white)
![Wine 11](https://img.shields.io/badge/Wine-11-A30000)
![License AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-0ea5e9)

</div>

UUWay runs the official Windows UU Remote host on Linux so you can control your desktop from a phone or another device. UU's files stay unmodified; native Linux components provide capture, input and display management.

## Features

- **Streaming and input**: NVIDIA NVENC or AMD VA-API H.264/HEVC, keyboard and mouse, native cursor images, and Fcitx5 text input.
- **Multiple displays and Super Screen**: independent capture, resolution, refresh rate and scale. Exclusive Super Screen restores the original displays on exit or failure.
- **Terminal and files**: Linux login shells, persistent terminal sessions, bidirectional text clipboard and file transfer.
- **Console**: service, input, display, file-directory and update settings. Manual and automatic update checks; installation requires confirmation.

## Install

Requires Ubuntu 24.04, a GNOME 46 **Wayland** desktop, and an NVIDIA GPU with NVENC or an AMD GPU with H.264/HEVC VA-API. Keep the desktop logged in. A machine without a monitor still needs an available desktop output, such as a display dummy plug.

1. Download the `.deb` from [Releases](https://github.com/GaryOAO/UUWay/releases/latest) and obtain the official Windows UU installer.
2. Install the package and run setup as your normal user:

   ```bash
   sudo apt install ./uuway_*_amd64.deb
   uuway setup --installer ~/Downloads/UURemote_Setup.exe
   ```

3. Sign in to UU and allow screen sharing and the text service when prompted. Open **UUWay Console** from the application menu, then connect from your controller.

Setup installs and pins Wine 11, verifies the encoder and configures user services. It can be run again. NVIDIA is tried first, then AMD; GPU drivers are not installed or switched. After first enabling the Fcitx5 add-on, restart the input method with `fcitx5 -r`. The console and wizard currently use Chinese.

## Common commands

```bash
uuway doctor                   # Check environment and services
uuway update                   # Check for updates
uuway update install           # Install an update
uuway update status            # View the result
uuway update rollback          # Roll back an update
uuway refresh --restart-display # Reload display service; removes Super Screens
```

The console checks for updates every six hours while open; automatic checks can be disabled. Updating briefly disconnects remote sessions. OTA installation requires a `.deb` install; source installations should rebuild from updated source.

For remote-only use, `uuway autologin on` enables GDM login and relogin after the desktop exits. Anyone with physical access can also enter the desktop; consider that before enabling it. Disable with `uuway autologin off`.

To uninstall, run `uuway uninstall`, then `sudo apt remove uuway`. The Wine prefix and UU login are retained.

## Supported scope

- Validated on Ubuntu 24.04 + GNOME 46 Wayland + RTX 3090 + UU 4.42.0.2770. Reports from other setups are welcome through the [compatibility template](https://github.com/GaryOAO/UUWay/issues/new?template=compatibility-report.yml).
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

[GNU AGPL-3.0](LICENSE). UUWay is an independent, unofficial project, unaffiliated with NetEase. It does not distribute UU program files.

Derived from [uu-remote-ubuntu-bridge](https://github.com/lachlanchen/uu-remote-ubuntu-bridge), and built on [Wine](https://www.winehq.org/), [DXVK](https://github.com/doitsujin/dxvk), PipeWire and Mutter.
