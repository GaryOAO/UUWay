# Build and install

`./install.sh` in the repository root runs every step below in order and stops only where you must
act (signing in to UU, choosing the monitor to share); `./install.sh --help` lists its options. This
guide is the same procedure by hand, for when you want to see or change a step.

**Most users do not need this guide.** The `.deb` on the
[Releases page](https://github.com/GaryOAO/UUWay/releases/latest) contains every component built
below: install it with `sudo apt install ./uuway_*_amd64.deb` and run `uuway setup`. This guide is for
building from source. The reference build environment is `packaging/container-build.sh`, which builds
everything in a clean Ubuntu 24.04 container (gcc 11, WineHQ stable 11.0.0.0, CUDA 12.1 headers);
`packaging/build-deb.sh` runs it under docker and produces the package. Other compilers are untested.

This is the procedure UUWay is developed and run with. It assumes Ubuntu 24.04, GNOME 46 on
Wayland, an NVIDIA GPU with the proprietary driver, and a user session that stays logged in
(auto-login plus a display dummy plug for headless machines). Nothing here replaces a system
package: patched components live under your home directory and are loaded through systemd user
drop-ins, so removing a drop-in restores the distribution build.

Paths below use the defaults the tooling expects:

| Path | Purpose |
| --- | --- |
| `~/.local/share/wineprefixes/uu-remote` | Wine prefix holding UU and the UUWay helpers (`compat/`) |
| `~/.local/state/uurb` | runtime state: portal restore token, logs, sockets |
| `~/.config/uurb` | service configuration |
| `build/` | build outputs and content-addressed runtime bundles |

## 1. Dependencies

```bash
sudo apt install build-essential gcc-mingw-w64-x86-64 meson ninja-build pkg-config jq curl git ripgrep \
    libjson-c-dev libx11-dev libxfixes-dev libpipewire-0.3-dev libspa-0.2-dev libvulkan-dev glslang-tools \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libfcitx5core-dev libfcitx5utils-dev \
    libei-dev fcitx5-modules-dev xvfb xclip python3-gi python3-dbus gir1.2-gtk-3.0 gir1.2-atspi-2.0
```

### CUDA headers

The build scripts read the CUDA driver-API header from the fixed path `/usr/local/cuda/include/cuda.h`
and link `-lcuda`. Use the **12.1** headers: newer ones map calls such as `cuCtxCreate` to newer symbol
versions that older drivers do not export. Two small packages from NVIDIA's CUDA repository provide
them (12.1.105, about 1 MB); `packaging/container-build.sh` shows the URLs and SHA-256 sums and how
they are unpacked. The second one also ships the official `libcuda.so` link stub, which lets a machine
without the NVIDIA driver link the binaries (`LIBRARY_PATH=/usr/local/cuda/lib64/stubs`). Never ship
that stub; the driver provides `libcuda.so.1` on the machine that runs UUWay.

Install **WineHQ stable 11** so that `/opt/wine-stable/bin/wine` exists
([WineHQ instructions](https://gitlab.winehq.org/wine/wine/-/wikis/Debian-Ubuntu)). CUDA comes from
the NVIDIA driver (`libcuda.so`, `libnvidia-encode.so`).

## 2. Install UU into its own prefix

```bash
export WINEPREFIX=~/.local/share/wineprefixes/uu-remote
/opt/wine-stable/bin/wineboot -u
/opt/wine-stable/bin/wine /path/to/UURemote_Setup.exe /S
```

Optionally inspect a new installer first without touching any prefix — it runs in a networkless
sandbox and reports the server's hash and what a read-only audit finds in it (UUWay never patches
UU):

```bash
scripts/stage-uu-release.sh --installer /path/to/UURemote_Setup.exe --sandbox-install
```

Sign in once. From the Wayland desktop (Wine uses Xwayland):

```bash
/opt/wine-stable/bin/wine "C:\\Program Files\\Netease\\GameViewer\\GameViewer.exe"
```

Log in, then quit UU completely. Finally let Wine load the terminal proxy instead of its built-in
PowerShell placeholder:

```bash
/opt/wine-stable/bin/wine reg add 'HKCU\Software\Wine\DllOverrides' /v powershell.exe /t REG_SZ /d native /f
```

## 3. Build the native runtime

The build scripts download pinned sources (DXVK, Mutter, Ubuntu's Wine headers, the portal) and
verify every archive against `config/*.json` or a pinned hash before use. Run the Python tooling
with the system interpreter (`/usr/bin/python3`), which provides `python3-gi`.

```bash
scripts/build-dxvk-capture.sh          # DXVK with the capture hook (d3d11.dll, dxgi.dll)
scripts/build-dxgi-capture-probe.sh    # DXGI duplication + NVENC + GPU channel backends
scripts/build-uu-native-bootstrap.sh   # Wine-side bootstrap and winlogon stand-in
scripts/build-uu-native-input.sh       # input bridge, broker and uinput worker
scripts/build-uu-native-display.sh     # display-mode and DPI backend
scripts/build-native-ime.sh            # Fcitx5 text-commit add-on
scripts/build-helpers.sh               # terminal broker/proxy, conpty.dll, clipboard bridge and files helper
scripts/build-uu-settings.sh           # Python/GTK UUWay control console (syntax check)
scripts/build-pipewire-probe.sh        # capture check used to grant screen-cast permission
```

`build-dxgi-capture-probe.sh` also builds the PipeWire capture worker, cursor shaders and the
patched portal it depends on.

Install the helpers into the prefix:

```bash
install -m 0755 build/helpers/* ~/.local/share/wineprefixes/uu-remote/compat/
```

### Optional: capture pacing and complete frames

The private Mutter build carries two patches:

- `patches/mutter-46.2-capture-jitter-candidate.patch`: stock Mutter 46.2 drops about a third of
  60 Hz screen-cast frames because its frame limiter rejects frames that arrive a few microseconds
  early. The patch tolerates that jitter.
- `patches/mutter-46.2-screencast-dmabuf-finish.patch`: the NVIDIA driver attaches no implicit
  fence to the dma-bufs Mutter renders into, and Mutter queues a buffer to PipeWire after only a
  flush. While another process keeps the GPU busy, UUWay can therefore read a buffer before Mutter's
  blit has run and capture what it held a whole ring (16 buffers) ago: the remote picture jumps
  between the current frame and frames from seconds earlier, then slowly catches up. The patch
  makes Mutter wait for its blit before queueing the buffer. The price is that the compositor
  blocks for that wait once per recorded frame, only while a remote session streams, so with a
  saturated GPU the local desktop refreshes more slowly.
  `tests/probes/dmabuf_implicit_fence_probe.c` reproduces the stale read on your driver without
  touching the desktop session.

The private build replaces only `libmutter-14` and keeps the system Clutter and Cogl, so it must
come from the Ubuntu revision that is installed. `config/mutter-capture-review.json` pins that
revision, and the build refuses to start when `libmutter-14-0` differs. After a Mutter update,
download the new `.debian.tar.xz` from Launchpad, check it against the `.dsc`, and update
`installed_package_version`, the two URLs and `debian_patches_sha256`.

```bash
bash scripts/build-mutter-capture.sh      # prints the retained build directory
bundle=~/.local/lib/uurb/mutter-$(dpkg-query -W -f='${Version}' libmutter-14-0)-jitter-core
python3 scripts/stage-mutter-capture.py build/mutter-build/rebuild.XXXXXX "$bundle"
python3 tests/probes/mutter_bundle_probe.py "$bundle"   # a private headless Shell must start
mkdir -p ~/.config/systemd/user/org.gnome.Shell@wayland.service.d
printf '[Service]\nExecStart=\nExecStart=%s/start-gnome-shell\n' "$bundle" \
    > ~/.config/systemd/user/org.gnome.Shell@wayland.service.d/92-uurb-mutter-jitter.conf
systemctl --user daemon-reload
```

Log out and back in to load it. `start-gnome-shell` checks the system Mutter libraries against the
bundle's `system-mutter.sha256` and loads the private library only while they match. After a
Mutter update it starts the stock library and logs `system Mutter changed` to the journal; rebuild
when you see that. Delete the drop-in to return to the distribution library for good.

Never point `LD_LIBRARY_PATH` at a bundle directly. Beside a newer system Clutter the Shell dies at
every start (`MetaStageView` class size smaller than `ClutterStageView`) and the desktop never
comes up.

### Optional: portal fixes

`scripts/build-portal-lifetime.sh` builds xdg-desktop-portal-gnome 46.2 with two upstream
dialog-lifetime fixes. Point the portal at it with a drop-in in
`~/.config/systemd/user/xdg-desktop-portal-gnome.service.d/`:

```ini
[Service]
ExecStart=
ExecStart=%h/.local/libexec/uurb/xdg-desktop-portal-gnome-46.2-lifetime
Environment=GSK_RENDERER=cairo
```

## 4. Allow input and screen capture

Native input needs access to `/dev/uinput` for the active desktop user:

```bash
sudo install -m 0644 config/70-uurb-native-input.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

Grant screen-cast permission once and store the restore token, so later starts need no dialog:

```bash
mkdir -p -m 0700 ~/.local/state/uurb
python3 scripts/probe-wayland-portal.py --capture-check --restore-state ~/.local/state/uurb/portal-probe.json
```

Choose the monitor UU should stream and tick “remember”.

## 5. Package and install the services

```bash
python3 scripts/package-uu-native-runtime.py package --with-input build/uu-native-releases
# prints {"bundle": "build/uu-native-releases/<sha256>", ...}

systemctl --user stop uu-native-bridge 2>/dev/null || true
python3 scripts/install-uu-native-service.py \
    --bundle "$PWD/build/uu-native-releases/<sha256>" \
    --prefix ~/.local/share/wineprefixes/uu-remote \
    --restore-state ~/.local/state/uurb/portal-probe.json \
    --state-parent ~/.local/state/uurb \
    --text-socket ~/.local/state/uurb/text.sock \
    --cursor-mode metadata --preserve-display-session

# Stop the bridge before changing Wine's path view, then reap the short-lived
# Wine server started by the desktop registry update.
systemctl --user stop uu-native-bridge 2>/dev/null || true
python3 scripts/configure-uu-wine-mappings.py \
    --prefix ~/.local/share/wineprefixes/uu-remote \
    --config-directory ~/.config/uurb
WINEPREFIX="$HOME/.local/share/wineprefixes/uu-remote" \
    /opt/wine-stable/bin/wineserver -k
WINEPREFIX="$HOME/.local/share/wineprefixes/uu-remote" \
    /opt/wine-stable/bin/wineserver -w

printf '{"version":1,"backend":"fcitx"}' > ~/.config/uurb/text-backend.json   # or "portal"
systemctl --user daemon-reload
systemctl --user restart uu-native-display
systemctl --user enable --now uu-native-display uu-native-text uu-native-bridge
```

`--cursor-mode metadata` keeps the pointer out of the video and exposes complete USER32/DXGI
cursor metadata to UU, using the same contract for physical and virtual outputs. USER32
retains the real cursor's visibility, position, image and hotspot, including queries from
`streamer.dll`. DXGI also retains the actual image and normal shape notifications.
Startup clears inherited presentation overrides. GPU cursor suppression is excluded from
release builds: Super Screen can still show an additional cursor. Metadata hiding and
transparent images are not used because they also affect the independent cursor channel.
Pointer-only updates remain active on static desktops without a producer GPU ACK. Recovery
restores the policy recorded for that bundle when cleaning its owned settings.
Running `python3 scripts/install-uu-settings.py` installs the
`uuway-console` Python/GTK application and
the `UUWay 控制台` menu entry. It does not compile Rust or restart a service.

The console is intentionally installed as Python source instead of a GitHub Actions-produced
single binary: GTK and PyGObject are supplied by the distribution, so this keeps the download
portable across Ubuntu updates and avoids bundling a second native runtime. Reinstall after
updating the console or its artwork; the release identity includes the Python source, icon
and desktop image.

The console separates overview, display/appearance, input, file transfer, software updates and diagnostics.
Mouse speed settings require an explicit save and apply without restarting. Cursor, text,
cover and receive-directory changes show a persistent restart notice. Display changes from
the console use manual confirmation with the guardian's 30-second rollback deadline.
Select a monitor before changing its mode: the console pins that output through apply,
confirmation and rollback. Other outputs keep their modes. The Super Screen card creates and
removes owned Mutter virtual monitors, using the same service as UU's IDD device compatibility
interface. Virtual monitors are bound to the UU bridge and live display-service/desktop-session
lifetime, not on disk. The bridge sends an idempotent release during UU disconnect/crash cleanup, so a
remote session cannot leave a stale super screen behind.
The output catalogue includes inactive physical outputs and external virtual outputs. Owned screens
can be removed; external/physical outputs can be enabled or disabled, with a guard against disabling
the last active output. Console output toggles, Super Screen entry/exit and resolution trials all
use temporary compositor changes. Save permanent layouts through GNOME display settings.

The software-updates page uses the installed CLI's JSON check/status endpoints. Manual checks and
the automatic-check preference never install anything. With the console open, automatic checks run
at most every six hours; a failed attempt uses the same backoff. A new available version prompts
once per release, showing the current/new versions; cancelling never installs it. Installation and manual rollback
require confirmation and use the existing verified download, detached system update, backup and
health-check rollback path. Status polling is offline and detects updates launched from a terminal.
Source builds show release information but cannot install OTA packages. Source-console installations
record their CLI location in private `console-tools.json`; immutable user copies use that mapping
before the system package. A packaged console prefers its own CLI.
Package refresh also retires an old user menu entry so it cannot shadow a newly installed console.
The Wine compatibility layer records bounded PID/control/API diagnostics and handles the vendor
IDD driver-install handshake in user space; a failed Windows driver installation must not be
reported as UU error 501.
For Super Screen debugging, correlate `net_wksta_version` and `DeviceIoControl` in
`~/.local/state/uurb/trial-*/display-trace.log` with
`journalctl --user -u uu-native-display`. A local NetAPI version below 10 triggers
UU 4.42's 501 before creation; a rejected `0x30c010` mode catalogue can produce 301.
The mode-list decoder accepts up to 64 entries, including the observed 44-entry
(536-byte) and 48-entry (584-byte, maximum 144 Hz) requests. The display/capture
path accepts rates through 144 Hz without clamping the requested rate. The service
records the bounded numeric catalogue, an explicit `virtual_modes_accepted` or
`virtual_modes_rejected` event (with rejected index/mode), and the failed creation
stage. `virtual_negotiate_result` distinguishes a failed capture consumer from a
Mutter layout/identity failure. These logs contain no desktop pixels.
Removing one keeps the remaining displays' live modes and positions. One existing desktop output
is required; mirrored and rotated layouts are outside the current input/topology scope.

Both installation paths ship `mutter-direct-capture.py` and `native_virtual_display.py` along with
the matched native bridge, display loader and Winelib backend. Runtime schemas 40 (NVIDIA) and
41 (AMD) verify all three virtual-control exports before advertising support. A source-only
repack preserves the installed native capability; it cannot turn old DLLs into a virtual-screen
runtime. Runtime upgrades reload the display service, including fixes within the same API
version. Use `uuway refresh --restart-display` to reload it explicitly even when the runtime
is unchanged. Existing virtual monitors are removed; the text/clipboard service keeps running.
File selection offers a `text/uri-list` through the installed `xclip` helper; finish receiving
on the phone before replacing the clipboard.

To check the console without changing a live desktop or restarting services:

```bash
env -u WAYLAND_DISPLAY GDK_BACKEND=x11 xvfb-run -a /usr/bin/python3 scripts/uuway_console.py --smoke-test
env -u WAYLAND_DISPLAY GDK_BACKEND=x11 xvfb-run -a env UUWAY_UI_TESTS=1 /usr/bin/python3 -m unittest \
  tests.test_uuway_console tests.test_uuway_console_ui tests.test_uuway_console_install
```

The interaction tests use temporary settings and simulated service/display responses.

## 6. Connect

The host appears in your UU device list. Check the service with
`systemctl --user status uu-native-bridge` and `journalctl --user -u uu-native-bridge`; terminal and
clipboard helpers log to `~/.local/state/uurb/terminal-bridge.log` and `clipboard-bridge.log`.

## Updating UU

UUWay does not pin a UU version. For a new release: stage it with `stage-uu-release.sh`, stop
`uu-native-bridge`, copy the prefix aside, run the new installer with `/S` in the prefix, copy
`compat/uu-terminal-proxy.exe` to `bin/powershell.exe` if the release ships none, and start the
service again. See [the 4.42 review](releases/4.42.0.2770-native-review.md) for what to compare.

## Troubleshooting

- **The controller shows only UU's Wine desktop.** The bridge runs but capture fails. Read the newest
  `~/.local/state/uurb/trial-*/capture.log`. `gpu_driver_mismatch` means the NVIDIA packages were
  updated without a reboot, so the loaded kernel module no longer matches the user-space driver:
  reboot. `capture_ended` without `capture_frame_accepted` for another reason: check step 4.
- **The remote picture jumps back to frames from a few seconds ago while the GPU is busy (a render,
  a training or OCR job), then slowly catches up.** Stale screen-cast buffers: Mutter queues a
  buffer before the NVIDIA GPU has finished drawing into it. Build and load the private Mutter with
  `mutter-46.2-screencast-dmabuf-finish.patch` (see "capture pacing and complete frames"), then log
  out and back in. `./dmabuf_implicit_fence_probe` (build line in its header) shows whether your
  driver is affected.
- **No desktop after a system update, and `gnome-shell` segfaults in `libmutter-14.so` every few
  seconds.** A private Mutter library that no longer matches the system Mutter. The gated drop-in
  above falls back by itself; for an older drop-in that sets `LD_LIBRARY_PATH`, move it out of
  `org.gnome.Shell@wayland.service.d` over SSH and run `systemctl --user daemon-reload`.
- NVIDIA driver, kernel and Mutter updates all need a reboot, and Mutter ones a pacing rebuild.
  Install them when you can reboot, not from the Software Updater during a remote session.

## Tests

```bash
python3 -m unittest tests.test_native_service tests.test_native_terminal_broker   # etc.
```

Most suites build small C probes with gcc/mingw; a few need the `build/` outputs above and skip
otherwise.
