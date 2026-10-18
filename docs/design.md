# Design notes

UUWay keeps one rule: **implement the Windows interface UU uses, faithfully, on Linux** — never
patch UU's behaviour or depend on offsets inside its binaries. That is why UU releases can change
underneath it. Each section below describes one interface, how it is served, and the Wine
behaviour that shaped it.

## Process layout

`uu-native-bridge.service` (a user service) owns everything below and tears it down on stop:

- **UU under Wine** on a private Xvfb display. UU's own windows are never shown; the desktop users
  see is the real GNOME session.
- **Capture broker** — owns one concurrent Mutter `RecordMonitor` producer per selected connector
  and feeds GPU frames and cursor metadata to UU's process. Setup checks portal screen sharing;
  the legacy single-output path can still restore a portal token. The installed desktop service
  uses explicit connectors, including the primary output: a token invalidated by a mode change
  must not leave a remote reconnect waiting for a new consent dialog.
- **Input worker** — owns `/dev/uinput`/libei on the desktop session.
- **Terminal broker** and **clipboard bridge** — Linux helpers in `<prefix>/compat`.

`uu-native-display.service` guards display-mode changes; `uu-native-text.service` commits IME text.
Runtime components are packaged as a content-addressed **bundle** (`build/uu-native-releases/<sha256>`)
whose manifest records every file hash and capability, so the service only ever runs a verified set.

## Video: DXGI Desktop Duplication + NVENC

UU captures with `IDXGIOutputDuplication` and encodes through the NVENC function table.

- The screen cast arrives from PipeWire as DMA-BUF. It is imported with Vulkan into memory shared
  with a D3D11 texture that a patched DXVK exposes to UU. `AcquireNextFrame` hands UU that texture;
  `ReleaseFrame` acknowledges it to the producer. One GPU copy, no readback.
- UU's NVENC calls go to the Linux NVENC library through CUDA on the same device. UU still chooses
  codec (H.264/HEVC), bitrate, frame rate and IDR requests; UUWay validates and forwards them.
- Frame timestamps come from the producer's GPU-ready time, converted to QPC, because Mutter's PTS
  can be a future presentation time.
- **Mutter pacing.** Mutter 46's screen-cast frame limiter drops frames that arrive 1–62 µs early,
  which cost a third of all frames at 60 Hz. The bundled Mutter patch tolerates that jitter.

## Pointer

Physical and virtual outputs use the same native cursor contract. Mutter's *metadata* mode
keeps the pointer out of captured video. USER32 supplies desktop coordinates, visibility and
a real cursor handle with its hotspot. DXGI supplies monitor-local pointer coordinates and
the image through `GetFramePointerShape`, following the
[Desktop Duplication API](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api).
Both APIs retain the actual image, visibility and movement. Presentation must not
be implemented by hiding metadata or replacing its image with transparent pixels:
UU also uses these results in its independent cursor channel.

`streamer.dll` constructs cursor metadata as well as supporting video rendering. Treating all
of its USER32 queries as an extra GPU sprite, or reporting every DXGI pointer update as hidden,
removes information from UU's actual cursor channel. Every module's USER32 queries return
the real handle and DXGI retains the actual shape. Startup clears inherited presentation
overrides. The recovery journal retains older bundles' policy so cleanup removes exactly
the Windows environment values owned by that invocation.

Cursor-only acquisitions reuse the last desktop texture without a producer GPU ACK, so a
static desktop still supplies movement, visibility and shape changes. Alternative policies
that composite the pointer into the video remain available.

Each output has a persistent `cursor.<n>.state` snapshot, including visibility when the pointer
leaves that output. A merged
`cursor.state` converts the visible stream's coordinates to desktop coordinates for USER32. Switching outputs assigns a fresh shape serial even if both source streams
use the same serial. DXGI also invalidates the delivered shape when a producer generation
changes, even if its shape serial restarts at the same value. The bootstrap carries the legacy
path and per-output directory into Windows service processes, whose environment is rebuilt by Wine.
Hidden snapshots remain hidden, including Mutter's empty-bitmap updates while typing. The adapter
retains the old handle privately for reuse instead of reporting it as a visible cursor. Per-role
logs distinguish server and capture-module queries, including process/thread IDs and
module binding, without logging cursor pixels. If a writer briefly holds an odd sequence, USER32
can reuse a committed snapshot for up to 100 ms rather than return a transient API failure; a
committed hide is applied immediately and an abandoned write expires.

Super Screen can still render an additional cursor alongside UU's independent cursor.
The release retains the verified metadata path; experimental GPU draw suppression is
excluded until it has been validated in a real remote session.

## Input and text

- `SendInput` calls from UU are intercepted in its process and forwarded to the input worker, which
  injects them on the Wayland session. Logo-key scan codes without the extended flag are normalised.
- Phone IME text arrives as `KEYEVENTF_UNICODE` batches. They are committed as whole strings by an
  Fcitx5 add-on into the focused input context (or through the portal clipboard as a fallback
  backend), never replayed as key presses.

## Display modes

`EnumDisplaySettings`, `ChangeDisplaySettingsEx`, `QueryDisplayConfig` and the DPI device-info
calls are answered from Mutter's real monitor configuration. A change is applied temporarily and
committed only once a fresh GPU frame of the new size has reached UU; otherwise the guardian rolls
it back after 30 seconds.

Output-specific transactions retain the complete layout and move adjacent outputs when a resized
screen would otherwise overlap them or leave a gap. Transactions are serialized across outputs;
only the selected output's fresh capture can confirm its change. Other outputs keep streaming.

## Virtual displays / Super Screen

UU's reviewed IDD device interface is implemented in user space through SetupAPI discovery,
`CreateFile`, `DeviceIoControl` and real owned handles. Its bounded plug, unplug and mode-list
messages go to the same-UID display service. Unknown device classes and handles pass through.
The vendor's `UpdateDriverForPlugAndPlayDevicesW`/SetupAPI installation handshake is also
translated to the same plug operation on Wine; it is never allowed to fall through to a missing
kernel/UMDF driver. No Windows driver is installed and no vendor offsets are used at runtime.

UU 4.42 checks Windows 10 support through local `NetWkstaGetInfo(100)` before sending
any IDD requests. Wine reports 6.2 through that API even when `RtlGetVersion` reports
Windows 10, and UU returns 501 immediately. While the native virtual-display backend
is enabled, the vendor import sees consistent local version fields; allocations,
identity fields, remote queries and NetAPI failures retain their original behavior.
HTTP status codes are never rewritten. Mode-list messages accept up to 64 entries,
matching the display ABI; UU's observed 44- and 48-entry catalogues must reach the create
stage. The 48-entry catalogue contains 144 Hz modes. Display parsing, Win32 changes,
saved defaults and PipeWire negotiation consistently accept rates through 144 Hz;
the selected rate is preserved. This is a mode limit, not a delivered-frame-rate guarantee.

The display service owns a Mutter `RecordVirtual` session and negotiates its size with a short
metadata-only PipeWire consumer. It establishes ownership from the actual added connector, then
exposes that output to the existing Windows topology and capture APIs. The session outlives
individual video consumers. Changing the size negotiates the owned source again; closing it stops
only that owned session. A monitor hotplug can make GNOME reload saved physical settings, so
creation and removal explicitly retain the live layout of all other outputs.

The console uses the same service for creation/removal and offers explicit monitor selection for
mode changes. Virtual monitors last until removed or the display service/desktop session stops.
At least one active desktop output is required to initialize the current input/topology path.
The reviewed Super Screen CCD path selection, independent process ownership, remote-track quiet
window and forced-stop physical-layout recovery are described in
[Super Screen lifecycle](super-screen-lifecycle.md).

## Terminal

UU 4.39+ opens terminals like this:

```
GameViewerServer → conpty_bridge.exe → CreatePseudoConsole (bundled conpty.dll)
                 → powershell -Command "uuyc-mux new … ; … ; uuyc-mux attach …"
```

`uuyc-mux` is a fork of the psmux terminal multiplexer. Under Wine every console host re-renders
the terminal and loses VT sequences, the bundled Microsoft `conpty.dll` cannot start its host, Wine's
own pseudoconsole ends clients immediately when `PSEUDOCONSOLE_INHERIT_CURSOR` is set, and
`powershell.exe` is a placeholder that exits. UUWay therefore:

1. **Replaces `conpty.dll`.** Inside `conpty_bridge` it joins the bridge's input/output pipes
   directly to the **PTY broker**: raw bytes both ways, as over SSH. A real Wine pseudoconsole on
   private pipes still hosts the bridge's child process.
2. **Stands in for PowerShell.** A small proxy runs the fixed launch script — only `uuyc-mux` and
   `chcp` calls are accepted — so the psmux session and an attached client exist, which UU checks
   before it reports the terminal as ready. The `attach` client renders to the private console,
   invisible.
3. **Persists sessions.** Broker protocol v2 names a session after UU's psmux session. The first
   viewer creates a holder process that owns the PTY; later viewers are handed to it with
   `SCM_RIGHTS` over an abstract socket checked with `SO_PEERCRED`. The psmux pane anchors the
   session, so closing the terminal in UU ends the shell and `exit` in the shell closes it in UU.
   A viewer that is still attached keeps the shell when the pane goes away first; the session ends
   when the last of viewer and pane has gone. Returning viewers get a `SIGWINCH` redraw
   (full-screen programs repaint; a plain shell reprints its prompt). A resize frame with an
   out-of-range size is ignored rather than treated as a protocol error, because a controller can
   send one transiently while its terminal window initializes.
4. **Removes OSC 3008 from the shell's output.** Ubuntu 26.04 (systemd 258 and later) ships
   `/etc/profile.d/80-systemd-osc-context.sh`, which makes every interactive bash print
   `ESC ] 3008 ; start=… ESC \` and `ESC ] 3008 ; end=… ESC \` around each prompt and command
   (UAPI.15 "hierarchical context signalling"). UU's terminal does not know the sequence and shows
   it as text, on the PC and on the phone. The broker drops these strings before they reach the
   viewer (`src/terminal_osc_filter.h`), the way a terminal drops an OSC string it does not know,
   and passes every other sequence through. The filter is a small state machine because a sequence
   can be split across two reads of the PTY. It follows the usual rules for an OSC string: ST or
   BEL ends it; an ESC followed by anything else, a control character, or a string longer than
   8 KiB cancels it and the rest is ordinary output, so a damaged sequence cannot swallow the
   session. Up to six bytes that could still turn out to be the start of a sequence are held back
   and released as soon as the shell goes quiet for 20 ms.
   The sequence is only printed by bash. To stop it at the source instead, remove the symlink
   `/etc/profile.d/80-systemd-osc-context.sh` and mask its tmpfiles snippet
   (`ln -s /dev/null /etc/tmpfiles.d/20-systemd-osc-context.conf`), as the script itself documents.

## Clipboard

UU talks to the Windows clipboard, which Wine mirrors onto UU's private X display. The clipboard
bridge watches `CLIPBOARD` ownership with XFixes on that display and on Xwayland, and copies UTF-8
text, `text/uri-list`, `x-special/gnome-copied-files` and `image/png` across. Wine converts file
lists to `CF_HDROP` and PNG to bitmaps; Mutter mirrors Xwayland's clipboard to Wayland apps.

While a transfer waits on one display, the bridge keeps answering requests on both and queues
owner changes instead of dropping them. The newest change wins, and content a side already holds
is never offered to it again, so the two clipboards cannot echo. Each transfer is logged with its
direction, sizes and a short digest, never its content.

Wine 11 needs one workaround. When UU writes the Windows clipboard (a copy on the phone),
winex11 takes `CLIPBOARD` with an X request that its clipboard thread never flushes, and win32u
only runs the X driver when its connection has input. The request therefore waited for the next X
event — typically the bridge offering the next desktop copy, which then flushed the stale phone
copy on top of it. Every Wine thread watches root-window properties, so the bridge touches one on
UU's display four times a second; phone copies now arrive within about 250 ms.

A file copied on the phone reaches UU as Windows *virtual files* — `FileGroupDescriptorW` plus
one `FileContents` item per file, fetched from the phone only when a reader asks for that index,
as Explorer does on paste. X cannot carry them, because Wine renders `FileContents` without an
index. The bridge reads the file names at once and offers the desktop their future paths under
`~/.cache/uurb-clipboard` as a file list only (`x-special/gnome-copied-files` and
`text/uri-list`), which clipboard managers do not read, so nothing is transferred on copy. When a
file manager pastes, the bridge runs `uu-clipboard-files.exe` in UU's prefix; it reads every file
through OLE (at most 1 GiB in total), and the paste waits until it has finished, with a desktop
notification if that takes more than a second. Saved files are removed ten minutes after their
last use (a later paste fetches them again), when a newer copy replaces them, and when the bridge
starts.

When phone text falls back to the portal, it is pasted through the desktop clipboard; that
selection is marked `application/x-uurb-transient`, and the bridge does not carry it to the phone.

## File transfer

UU's file-transfer receiver defaults to `C:\\Program Files\\Netease\\GameViewer\\Download`.
On Linux the native service maps that Wine path to the user's XDG download directory before UU
starts, so files sent from another device arrive in `~/Downloads` (or the directory configured by
`~/.config/user-dirs.dirs`). The UUWay console can save an explicit destination in
`~/.config/uurb/download-directory.json`. An existing Wine receive directory is kept as a
`.uurb-wine` backup before the mapping is created.

Wine's ANSI code page follows the locale, and UU reads outgoing text in that code page, so the
service runs Wine with the session `LANG` rather than a desktop-wide `LC_ALL` override — otherwise
CJK text would arrive on the phone as `?`.

## Updates

`uuway update` is built around one fact: a machine that is only used remotely is usually updated
from UU's own remote terminal, and an update restarts the service that terminal belongs to.

- **It outlives the terminal.** The shell the user types in sits in the bridge service's cgroup
  (`KillMode=mixed`), so a child of that shell dies when the bridge restarts. The root side is a
  transient *system* unit (`systemd-run`), which is in nobody's cgroup, held by a `shutdown:sleep`
  inhibitor. A user-level unit would not survive a crash of the desktop session, and apt needs root
  anyway. The unit is started with one authorization, in the terminal (sudo), because later nobody can
  answer a polkit dialog: a shell opened through UU's terminal inherits `DISPLAY`, so `pkexec` would put
  its dialog on a desktop nobody is watching.
- **It installs what the project's GitHub release serves.** There is no release signature or key: the
  release is fetched only from `github.com/GaryOAO/UUWay` over https (redirects may lead only to
  GitHub's own hosts), so whoever controls that repository's releases controls what installs. The
  checks (`uuway_update_apply.py`, first half) run twice: as the user before anything is asked, and as
  root on a copy that only root can change. A release counts when the deb matches the SHA-256 that the
  same release lists in `SHA256SUMS` (this catches a damaged or cut-off download, it is not
  authentication), says it is `uuway`/`amd64` at the version in the tag, and that version is newer
  than the installed one. To rehearse without the network, lay a deb and its `SHA256SUMS` out in a
  directory and run `uuway update install --source DIR --tag vX.Y.Z` (an option that is not listed in
  `--help`).
- **It can go back.** Before installing, the installed package is rebuilt as a deb from the files on
  disk, so a rollback needs neither the old release nor the network. The user's configuration is
  snapshotted too: the old installer refuses configuration it does not recognise, so a rollback restores
  the files first and only then refreshes.
- **It checks that the machine came back, against what it looked like before.** The three user services
  stay up for 90 seconds without a restart, UU's processes run (recognised by the file name in `argv[0]`,
  where wine shows the Windows path), and UU holds an established connection if it did before the update
  and the network is up. `uuway doctor` is not used for this: it asks for a desktop environment that a root
  process does not have, and would call a healthy machine broken. What cannot be seen from the machine is
  whether a viewer gets a picture.
- **It survives a reboot.** While an update runs, a one-shot unit under `/etc/systemd/system` (created
  at run time, never shipped in the package) lets the machine pick it up again after a power cut or reboot:
  repair dpkg, then carry on or roll back. It is removed when the update ends.
- **It never reboots on its own** unless `--reboot-if-stuck` was given for that run, and then once.

## Not supported

- **The GDM login screen** is a separate session owned by the `gdm` user. The portal screen cast,
  the input worker and the user services all belong to *your* session, so UU goes offline when
  nobody is logged in. `uuway autologin on` sets two GDM keys: `AutomaticLogin` logs the account in
  when GDM starts (a boot, a power cut), and `TimedLogin` logs it in again ten seconds after the login
  screen appears, which is what brings the desktop back after a Shell crash or a logout, because GDM
  does the first only once per start. Showing the greeter itself would need a second, system-level
  capture and input path that does not exist.
- **Port mapping** listens on the *controller*, so it works as in UU, but nothing on the host side
  can create or observe a mapping.
