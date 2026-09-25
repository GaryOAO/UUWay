# Build and install

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
sudo apt install build-essential gcc-mingw-w64-x86-64 meson ninja-build pkg-config jq curl \
    libjson-c-dev libx11-dev libxfixes-dev libpipewire-0.3-dev libvulkan-dev glslang-tools \
    libei-dev fcitx5-modules-dev xvfb xclip python3-gi cargo libgtk-3-dev
```

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
sandbox and reports the server's hash and patch candidates:

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
scripts/build-helpers.sh               # terminal broker/proxy, conpty.dll, clipboard bridge
scripts/build-uu-settings.sh           # control console
scripts/build-pipewire-probe.sh        # capture check used to grant screen-cast permission
```

`build-dxgi-capture-probe.sh` also builds the PipeWire capture worker, cursor shaders and the
patched portal it depends on.

Install the helpers into the prefix:

```bash
install -m 0755 build/helpers/* ~/.local/share/wineprefixes/uu-remote/compat/
```

### Optional: 60 FPS capture pacing

Stock Mutter 46.2 drops about a third of 60 Hz screen-cast frames because its frame limiter
rejects frames that arrive a few microseconds early. `patches/mutter-46.2-capture-jitter-candidate.patch`
tolerates that jitter:

```bash
scripts/build-mutter-capture.sh
python3 scripts/stage-mutter-capture.py build/mutter-build ~/.local/lib/uurb/mutter-46.2-jitter
mkdir -p ~/.config/systemd/user/org.gnome.Shell@wayland.service.d
printf '[Service]\nEnvironment=LD_LIBRARY_PATH=%s\n' ~/.local/lib/uurb/mutter-46.2-jitter \
    > ~/.config/systemd/user/org.gnome.Shell@wayland.service.d/92-uurb-mutter-jitter.conf
```

Log out and back in to load it. Delete the drop-in to return to the distribution library.

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

python3 scripts/install-uu-native-service.py \
    --bundle "$PWD/build/uu-native-releases/<sha256>" \
    --prefix ~/.local/share/wineprefixes/uu-remote \
    --restore-state ~/.local/state/uurb/portal-probe.json \
    --state-parent ~/.local/state/uurb \
    --text-socket ~/.local/state/uurb/text.sock \
    --cursor-mode metadata

printf '{"version":1,"backend":"fcitx"}' > ~/.config/uurb/text-backend.json   # or "portal"
systemctl --user daemon-reload
systemctl --user enable --now uu-native-display uu-native-text uu-native-bridge
```

`--cursor-mode metadata` keeps the pointer out of the video and hands it to UU as DXGI pointer
data, which is what makes the controller draw your real cursor. Install the console with
`python3 scripts/install-uu-settings.py`.

## 6. Connect

The host appears in your UU device list. Check the service with
`systemctl --user status uu-native-bridge` and `journalctl --user -u uu-native-bridge`; terminal and
clipboard helpers log to `~/.local/state/uurb/terminal-bridge.log` and `clipboard-bridge.log`.

## Updating UU

UUWay does not pin a UU version. For a new release: stage it with `stage-uu-release.sh`, stop
`uu-native-bridge`, copy the prefix aside, run the new installer with `/S` in the prefix, copy
`compat/uu-terminal-proxy.exe` to `bin/powershell.exe` if the release ships none, and start the
service again. See [the 4.42 review](releases/4.42.0.2770-native-review.md) for what to compare.

## Tests

```bash
python3 -m unittest tests.test_native_service tests.test_native_terminal_broker   # etc.
```

Most suites build small C probes with gcc/mingw; a few need the `build/` outputs above and skip
otherwise.
