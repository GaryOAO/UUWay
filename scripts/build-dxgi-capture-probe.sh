#!/usr/bin/env bash
# Only experimental backend/loader names, never a production dxgi.dll hook.
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
bash "$repo_dir/scripts/build-d3d11-capture-probe.sh"
work="$repo_dir/build/gpu-relay"
stage="$repo_dir/build/native-presenter"
common=(-std=c11 -O2 -Wall -Wextra -Werror)
/opt/wine-stable/bin/winegcc -m64 -shared "${common[@]}" \
    -I "$work/wine-dev/usr/include/wine" -I "$work/wine-dev/usr/include/wine/wine/windows" \
    -o "$stage/uurb-dxgi-capture.dll" "$repo_dir/src/uu_dxgi_duplication.spec" \
    "$repo_dir/src/uu_dxgi_duplication.c" "$repo_dir/src/uu_d3d11_capture_texture.c" \
    "$stage/native_cuda_frame_copy.o" "$stage/native_gpu_frame_channel.o" "$stage/native_wine11_gpu_fd.o" \
    -ld3d11 -ldxgi -ldxguid -luuid -lntdll -lvulkan-1 -lcuda
x86_64-w64-mingw32-gcc "${common[@]}" -shared \
    -o "$stage/uurb-dxgi-capture-loader.dll" "$repo_dir/src/uu_dxgi_duplication_loader.c"
x86_64-w64-mingw32-gcc "${common[@]}" -I "$work/nv-codec-headers/include" \
    -o "$stage/uu-dxgi-pe-capture-probe.exe" "$repo_dir/src/uu_dxgi_capture_probe.c" \
    -ld3d11 -ldxgi -ldxguid -luuid
/opt/wine-stable/bin/winegcc -m64 "${common[@]}" -I "$repo_dir/src" \
    -I "$work/wine-dev/usr/include/wine" -I "$work/wine-dev/usr/include/wine/wine/windows" \
    -o "$stage/uu-dxgi-state-probe.exe" "$repo_dir/tests/probes/dxgi_duplication_state.c" \
    "$repo_dir/src/uu_dxgi_duplication.c" "$stage/native_gpu_frame_channel.o" -ldxguid -luuid
printf 'Built experimental DXGI COM backend and true PE consumer. No UU files changed.\n'
x86_64-w64-mingw32-gcc "${common[@]}" -municode \
    -o "$stage/wine-capture-child.exe" "$repo_dir/tests/probes/wine_capture_child.c"
