<div align="center">

<img src="docs/images/hero.en.svg" alt="UUWay: a native Linux host for NetEase UU Remote" width="100%">

[简体中文](README.md) · [English](README.en.md)

![Ubuntu 24.04](https://img.shields.io/badge/Ubuntu-24.04-E95420?logo=ubuntu&logoColor=white)
![GNOME 46 Wayland](https://img.shields.io/badge/GNOME_46-Wayland-4A86CF?logo=gnome&logoColor=white)
![NVIDIA NVENC](https://img.shields.io/badge/NVIDIA-NVENC-76B900?logo=nvidia&logoColor=white)
![Wine 11](https://img.shields.io/badge/Wine-11-A30000)
![Status](https://img.shields.io/badge/status-developer_preview-f59e0b)
![License AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-0ea5e9)

</div>

**NetEase UU Remote only ships a Windows host — there is no Linux client.** UUWay runs the official
host under Wine, with its files unmodified on disk, and serves every Windows interface it depends on
from a native Linux implementation. Open UU Remote on your phone and drive your Linux desktop just as
you would a Windows PC.

## ✨ Nearly every UU feature works

| UU feature | On Linux | Notes |
| --- | :---: | --- |
| 🎞️ Screen streaming | ✅ | Zero-copy GPU capture + NVENC, up to 60 FPS at 1080p |
| 🖱️ Pointer | ✅ | The controller draws your real cursor theme, and zoomed views follow it |
| ⌨️ Keyboard & mouse | ✅ | Including the Super / ⌘ key, injected through libei / uinput |
| 🀄 Phone IME | ✅ | Chinese text is committed straight into the focused field (Fcitx5) |
| 🖥️ Resolution · refresh rate · scale | ✅ | Applied to Mutter, rolled back if the new mode never reaches the stream |
| 💻 **Remote terminal** | ✅ | Opens your Linux login shell; sessions persist and can run side by side |
| 📋 Clipboard | ✅ | Text in both directions, in real time |
| 📁 Files: phone → desktop | ✅ | Transferred when you paste; the paste waits for the download |
| 📁 Files: desktop → phone | 🧪 | Implemented, awaiting acceptance |
| 🪟 Super Screen (virtual displays) | ❌ | Needs a Windows kernel driver (IddCx) that Wine cannot load |
| 📺 1440p / 4K at 60 Hz | ⚠️ | Limited by the refresh rates your display (or dummy plug) advertises |

Verified with UU 4.42.0.2770 and no patch at all ([review](docs/releases/4.42.0.2770-native-review.md)).
UUWay implements public Windows interfaces rather than offsets inside UU, so UU updates rarely matter.

### 💻 Highlight: UU's remote terminal opens a Linux shell

On Windows, UU's remote terminal opens PowerShell. With UUWay it opens **your Linux login shell**.

- Bytes flow **raw** between the PTY and UU with no console layer in between, so full-screen programs
  such as vim and htop render correctly and CJK text stays intact.
- Leave the terminal page and **the session keeps running**; it redraws when you return, and several
  can run at once.
- Closing a terminal in UU ends its shell, exactly as on Windows.

## 🔧 How it works

<p align="center">
  <img src="docs/images/architecture.en.svg" alt="Architecture: every Windows interface the official UU host calls is served by a native Linux implementation" width="100%">
</p>

UUWay is an **adaptation layer**: UU calls the same APIs it would call on any Windows PC, and each
call lands on a Linux implementation. UU still handles accounts, relays, and codec and bitrate
negotiation.

<p align="center">
  <img src="docs/images/video-pipeline.en.svg" alt="Video path: pixels stay in video memory from compositor to encoder" width="100%">
</p>

See the [design notes](docs/design.md) for details.

## 🚀 One-step install

**You need:**

- Ubuntu 24.04 with a GNOME 46 **Wayland** session that stays logged in (a display dummy plug for
  headless machines)
- An **NVIDIA** GPU with NVENC and the proprietary driver
- The official UU Remote **Windows** installer and a UU account

```bash
git clone https://github.com/GaryOAO/UUWay.git && cd UUWay
./install.sh --installer ~/Downloads/UURemote_Setup.exe
```

The script checks the system, installs the dependencies and WineHQ, installs UU into its own Wine
prefix, builds the native runtime, grants input and screen-cast access, then packages and starts the
services. Only two things need you: **sign in** in the UU window it opens, and in the screen-sharing
dialog **pick the monitor and tick "remember"**. Then open UU Remote on your phone — this computer is
in your device list. The installer's messages are in Chinese.

- Resume after an interruption with `./install.sh --from STEP`; `./install.sh --dry-run` only prints
  what it would run.
- For a manual install, the optional 60 FPS pacing patch and troubleshooting, see the
  [build and install guide](docs/build.md).

> UUWay is a **developer preview**: it is in daily use on its development machine, but so far only
> verified on Ubuntu 24.04 with an RTX 3090.

## 📦 Repository layout

```
install.sh  one-step installer
src/        native backends (Linux and Windows/Wine sides) and helpers
scripts/    service, packaging, build and release-audit tooling
patches/    DXVK capture, Mutter pacing, portal session-lifetime patches
config/     pinned build inputs and the udev rule
systemd/    user service templates
native/     control console (Rust + GTK)
tests/      unit tests and probes
docs/       design notes, build guide, release reviews
```

## ⚖️ License

UUWay is released under the **[GNU AGPL-3.0](LICENSE)** (or any later version). Anyone who
distributes UUWay or a modified version, or offers a modified version to others over a network, must
publish the complete source under the same license — using it inside closed-source or commercial
software means opening that software's source. Portions from the upstream project keep their original
MIT license; see [NOTICE](NOTICE).

## Disclaimer

UUWay is an independent, unofficial project. It is not affiliated with or endorsed by NetEase.
"UU" and "UU Remote" are trademarks of their respective owners. UUWay distributes no UU program
files; you install the client yourself from the official source.

## Acknowledgements

UUWay grew out of [uu-remote-ubuntu-bridge](https://github.com/lachlanchen/uu-remote-ubuntu-bridge)
by Lachlan Chen, whose RDP-relay design it replaces with a native Wayland pipeline. It also builds
on [DXVK](https://github.com/doitsujin/dxvk), [Wine](https://www.winehq.org/), PipeWire and Mutter.
