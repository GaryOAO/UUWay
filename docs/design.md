# Design notes

UUWay keeps one rule: **implement the Windows interface UU uses, faithfully, on Linux** — never
patch UU's behaviour or depend on offsets inside its binaries. That is why UU releases can change
underneath it. Each section below describes one interface, how it is served, and the Wine
behaviour that shaped it.

## Process layout

`uu-native-bridge.service` (a user service) owns everything below and tears it down on stop:

- **UU under Wine** on a private Xvfb display. UU's own windows are never shown; the desktop users
  see is the real GNOME session.
- **Capture broker** — holds the screen-cast session (restored from a saved portal token) and
  feeds GPU frames and cursor metadata to UU's process.
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

On Windows, duplicated frames never contain the cursor: DXGI reports the pointer position per frame
and its image through `GetFramePointerShape`, and the streaming application draws it. UU relies on
exactly that — it ignores cursor shapes from `GetCursorInfo` and draws a fixed arrow when it has no
DXGI shape.

UUWay therefore captures with the portal's *metadata* cursor mode (no pointer in the video) and
reports position, visibility and the 32-bit colour shape with its hotspot through DXGI whenever
they change. The controller draws the pointer locally with your real theme, and zoomed views
follow it. Alternative policies that composite the pointer into the video remain available but
cannot drive UU's follow-the-pointer view.

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
   Returning viewers get a `SIGWINCH` redraw (full-screen programs repaint; a plain shell reprints
   its prompt).

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

Wine's ANSI code page follows the locale, and UU reads outgoing text in that code page, so the
service runs Wine with the session `LANG` rather than a desktop-wide `LC_ALL` override — otherwise
CJK text would arrive on the phone as `?`.

## Not supported

- **Super Screen** creates virtual monitors through UU's IddCx driver, a Windows kernel-mode
  driver that Wine cannot load.
- **Port mapping** listens on the *controller*, so it works as in UU, but nothing on the host side
  can create or observe a mapping.
