# UU Super Screen lifecycle

Review target: the installed UU 4.42.0.2770 `GameViewerServer.exe`. The observations
below come from local disassembly and actual public-API traces. Vendor addresses
are diagnostic references only; UUWay does not ship or patch vendor offsets.

## Native behavior and mapping

UU separates manual virtual displays (`manual_virtual_display.cpp`), Super Screen
mode (`super_screen.cpp`) and headless fallback (`headless_virtual_display.cpp`).
Adding a manual display and entering exclusive Super Screen mode are different
operations. A successful IDD plug alone does not mean exclusive mode was entered.

Observed flow on this machine:

1. Local `NetWkstaGetInfo(100)` must report Windows 10 or later; otherwise UU
   returns 501 before creating a display.
2. UU supplies a 48-entry mode catalogue, including 144 Hz, through `0x30c010`,
   then plugs its slot through `0x30c004`.
3. To enter Super Screen, UU calls `SetDisplayConfig` with flags `0x16a0`
   (`USE_SUPPLIED_DISPLAY_CONFIG | APPLY | SAVE_TO_DATABASE | ALLOW_CHANGES |
   FORCE_MODE_ENUMERATION`). Its path list contains only the virtual output,
   positioned at the origin. The mode array still contains unused physical
   source/target modes. Rejecting that valid array previously left an extra
   monitor running without entering Super Screen mode.
4. On exit UU submits its saved physical paths with the same flags and removes
   virtual slots through `0x30c008`. These operations can be repeated.

UU's video-session cleanup calls the display-feature cleanup with separate
Super Screen/secondary-screen state. Manual display code also has a separate
configuration-save flag. Those preferences do not establish ownership of a
running Linux virtual output. UUWay deliberately bounds live outputs to the
active remote/bridge lifetime requested by the user.

Relevant diagnostic RVAs: Super Screen mode initialization `0x595700`, native
video-session enter `0x45ef20`, quit `0x460310`, capture cleanup `0x452c90` and
display-feature cleanup `0x4856d0`.

## UUWay guarantees and boundaries

- IDD plug creates one owned Mutter `RecordVirtual` session. A manual/console
  display extends the desktop until UU requests an exclusive topology.
- That lifetime/mode-negotiation source always hides its cursor. The actual
  `RecordMonitor` capture uses the configured embedded, metadata or GPU-composited
  policy for both physical and virtual outputs. It clears inherited cursor
  settings before enabling the selected policy, preventing a second baked sprite.
- Metadata mode uses the same complete USER32/DXGI contract on physical and
  virtual outputs. `streamer.dll` also constructs cursor metadata; its queries
  must retain actual visibility and a real USER32 cursor handle. Metadata startup
  clears inherited presentation overrides. Both public cursor APIs retain the
  real image and hotspot. GPU draw suppression is excluded from release builds;
  Super Screen can still show an additional cursor. Hiding or replacing metadata
  with a transparent image interferes with UU's independent cursor channel.
  Pointer-only movement, hide/show and shape updates remain active on static
  desktops without a false producer GPU ACK. Recovery records the invocation's
  policy and removes exactly the environment settings owned by that invocation,
  including older bundles' former suppression policy. Hidden/empty bitmap updates do not
  revive a cached sprite. An in-progress writer can reuse a recent committed
  snapshot for up to 100 ms; a committed hide is immediate. Diagnostics record
  client and capture roles separately.
- The reviewed CCD path selection disables other desktop outputs temporarily.
  `SAVE_TO_DATABASE` is translated to this owned-session state; it never writes
  a persistent Linux display layout. Only owned virtual paths or the saved
  physical topology are eligible.
- GDI display names, CCD source/target IDs and cursor-file names retain their
  identity when physical outputs become inactive. Active output indices remain
  contiguous for display transactions and frame confirmation.
- If a later layout disables an output with a pending mode confirmation, its
  obsolete transaction is cleared without restoring that output. The current
  layout and the virtual output now occupying its active index remain available
  to inspection and capture.
- Physical outputs are restored before the last virtual session is stopped.
  Removal is idempotent and restores pre-entry resolution, position and scale.
  The restore baseline is captured before virtual-mode negotiation, since a
  mode hotplug can temporarily reactivate an old physical/kernel output.
- The capture broker reports its active video-track count every 0.5 seconds.
  After all tracks close, a 15-second quiet window allows mode changes and
  track recreation before outputs are reclaimed. A pending display-mode
  confirmation delays this quiet-window cleanup. This is an inference from
  video-track lifetime, not a vendor remote-session notification; a connected
  controller that intentionally stops every video track is treated as idle.
- Linux pidfds independently cover the actual IDD caller, capture broker and
  broker supervisor. Process death triggers cleanup without waiting for the
  quiet window. A 30-second lost-heartbeat limit covers a wedged broker.
- The display service journals the physical baseline before disabling it.
  Normal shutdown restores it; `ExecStopPost --recover` and startup recovery
  cover forced termination. A boot/session/Mutter-owner identity check prevents
  replay into a different desktop session.
- Closing the display service's D-Bus connection releases its own virtual
  monitors. VKMS outputs are kernel devices, not Mutter ScreenCast sessions;
  their activation is managed as physical topology, without unloading drivers.
- The console lists active and disabled outputs and provides per-output controls
  plus restore-and-cleanup. Output toggles, Super Screen entry, recovery and
  disconnect cleanup never persist a desktop layout. Permanent layouts belong
  in GNOME display settings, including their confirmation flow.

The retired VKMS experiment is documented in
[the archive note](archive/legacy-vkms.md); it is not part of the Super Screen backend.

Diagnostics: `virtual_exclusive_*`, `virtual_session_owner`, `virtual_owner_lost`,
`virtual_uu_process_exited`, `virtual_remote_tracks_closed`,
`virtual_crash_layout_restored` and `virtual_release_failed` in
`journalctl --user -u uu-native-display.service`. API/control details remain in
`~/.local/state/uurb/trial-*/display-trace.log`.
