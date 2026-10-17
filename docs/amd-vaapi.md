# AMD VA-API compatibility backend

This optional backend adds AMD VA-API encoding while keeping the original
NVIDIA source and build path. The AMD runtime needs no CUDA headers or NVIDIA
driver.

## Architecture and limits

The original PipeWire DMA-BUF import, synchronization, cursor handling and
Vulkan blit remain. The AMD producer then copies BGRA pixels into a size-sealed
memfd. Protocol **v4** distinguishes this from upstream v3's opaque GPU memory;
mixing producers and consumers fails rather than treating GPU memory as pixels.
The existing acknowledgment still prevents the producer from overwriting an
in-flight frame. The Windows capture adapter uploads this memory to a D3D11
texture and implements the same Desktop Duplication interface.

The NVENC Windows function table is a compatibility interface, not a claim that
AMD implements NVENC. The AMD adapter reads back the registered D3D11 texture,
converts BGRA to limited-range BT.709 NV12, and encodes with FFmpeg's VA-API
H.264/HEVC hardware encoders. The real AMD GPU is matched by Vulkan UUID to its
DRM render node. Native Vulkan entry points are loaded from `libvulkan.so.1`
explicitly to avoid collisions with Wine's Windows Vulkan thunks.

The bundle-local DXVK configuration presents vendor ID `10de` to the Windows
application so it selects the implemented NVENC interface. The actual Vulkan
device, VA-API driver and encoding hardware remain AMD. No system-wide adapter
override is installed.

This is **not a zero-copy path**. CPU memory bandwidth, color conversion and
uploads add overhead. Hardware compression, no B frames, forced IDR, sequence
headers, CBR/VBR and bitrate/framerate reconfiguration are implemented.
Reconfiguration reopens the encoder between frames and starts a new IDR.
Spatial AQ uses 32 non-overlapping regions selected from luma variance and
submitted as VA-API QP deltas. Flatter areas receive more bits; the requested
strength controls the bounded QP adjustment. This is this backend's algorithm,
not NVIDIA's proprietary AQ. Drivers lacking 32 QP-delta ROI regions reject AQ.
Temporal AQ, multipass, HDR/10-bit, asynchronous encoding and dynamic resolution within
an encoder session are rejected. Display-size changes require the existing
session-recreation mechanism. Multiple AMD GPUs require future explicit device
selection. The initial validation target is Radeon 780M, Ubuntu 26.04, GNOME
Wayland, and WineHQ 11.0; other hardware remains unverified.

## Build

Install the upstream `uuway` package first; its existing patched DXVK, input,
display and terminal artifacts are reused. This build does not overwrite
`/usr/lib/uuway`. WineHQ 11.0 must be at `/opt/wine-stable`.

```bash
sudo apt install gcc gcc-mingw-w64-x86-64 libwine-dev libavcodec-dev libavutil-dev \
  libswscale-dev libva-dev libvulkan-dev libdrm-dev libpipewire-0.3-dev \
  libspa-0.2-dev glslang-tools pkg-config ffmpeg xvfb
bash scripts/build-amd-runtime.sh
python3 scripts/test-amd-runtime.py
```

The build fetches the pinned upstream NVENC interface headers, not NVIDIA
drivers. It writes `build/encoder-backend` only after compilation succeeds.
The AMD and NVIDIA runtime outputs should use separate checkouts/build trees.

## Setup and use

```bash
bash scripts/uuway-amd.sh setup --installer ~/Downloads/uuyc_4.42.1.exe
bash scripts/uuway-amd.sh doctor
bash scripts/uuway-amd.sh refresh
bash scripts/uuway-amd.sh open
```

Log in to UU when prompted, completely quit its tray application, then grant
the GNOME screen-sharing prompt and remember the selected display. The normal
UUWay user services and console are used. Setup checks actual hardware encoding
instead of `nvidia-smi` when the AMD runtime marker is present.

`open` starts the Windows service bootstrap needed by UU 4.42 for login
when the bridge is stopped. With the bridge running, `open` reports how to
re-enter setup instead of trying to activate an invisible window. Setup's
launcher step repairs recognized vendor shortcuts and adds an AMD menu entry.
The AMD background service keeps UU on an isolated Xvfb display, just like
the NVIDIA backend. This preserves the clipboard bridge and deferred phone-file
paste: Wine must retain its virtual-file clipboard while the desktop offers
Linux file paths. The running service's account/settings window is therefore
not shown on the desktop. To log in again, run `bash scripts/uuway-amd.sh setup --login`;
setup stops the bridge before opening the login window and restarts it afterwards.
The text supervisor validates the system Python executable by resolved path,
including Ubuntu 26.04's Python 3.14.

The original `/usr/bin/uuway` still belongs to the upstream package. Use
`uuway-amd` for this checkout; upstream `uuway update install` or `uuway refresh`
would deploy the NVIDIA runtime. The AMD wrapper blocks its `update` command.
Remove the user services with `uuway-amd uninstall` if no longer needed.
Uninstall also removes the AMD menu entry and restores backed-up vendor shortcuts.

## Validation

`scripts/test-amd-runtime.py` records evidence under `build/amd-evidence/`:

- Real Vulkan image readback, four changing frames checked pixel by pixel,
  and memfd size sealing.
- H.264 and HEVC hardware encoding: 64 frames each, independent CPU decode,
  color checks, sequence headers, forced IDR, bitrate changes, stale timestamps
  and locked-packet lifecycle checks.
- True Windows PE client under Wine 11: BGRA and NV12 source textures plus
  UAV binding; 64 frames per codec in each scenario, independent decoding of
  every frame, ABI errors, source/output handle isolation and cleanup.

These synthetic checks passed on the local Radeon 780M. Live login, portal
authorization and all three background services also passed. An authorized
1920x1080 PipeWire DMA-BUF capture encoded and independently decoded all 193
frames with H.264. On 2026-10-17 the user completed the live controller test
and confirmed successful connection, visible desktop and working remote input.
UU 4.42.1's actual detector also initialized and encoded a first
3840x2160 frame with both H.264 and HEVC, including its default spatial AQ,
infinite GOP, one reference, single slice and absent color metadata requests.
The Windows acceptance probe exercises the same AQ/GOP/slice settings and
bitrate reconfiguration. The author also tested a visible desktop window.
Review subsequently restored
Xvfb isolation for background operation to preserve clipboard file transfers;
the visible login window remains available while the bridge is stopped.
