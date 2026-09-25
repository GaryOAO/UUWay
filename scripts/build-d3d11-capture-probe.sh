#!/usr/bin/env bash
# Build only isolated test names. Does not modify production Wine/UU files.
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
bash "$repo_dir/scripts/build-nvenc-encode-probe.sh"
bash "$repo_dir/scripts/build-pipewire-native-probe.sh"
work="$repo_dir/build/gpu-relay"
stage="$repo_dir/build/native-presenter"
common=(-std=c11 -O2 -Wall -Wextra -Werror)
gcc "${common[@]}" -fPIC -I /usr/local/cuda/include \
    -c "$repo_dir/src/native_cuda_frame_copy.c" -o "$stage/native_cuda_frame_copy.o"
gcc "${common[@]}" -fPIC -c "$repo_dir/src/native_gpu_frame_channel.c" -o "$stage/native_gpu_frame_channel.o"
/opt/wine-stable/bin/winegcc -m64 "${common[@]}" \
    -I "$work/wine-dev/usr/include/wine" -I "$work/wine-dev/usr/include/wine/wine/windows" \
    -I "$work/nv-codec-headers/include" \
    -o "$stage/uu-d3d11-receiver-probe.exe" "$repo_dir/src/uu_gpu_receiver_probe.c" \
    "$repo_dir/src/uu_d3d11_capture_texture.c" "$stage/native_cuda_frame_copy.o" \
    "$stage/native_gpu_frame_channel.o" "$stage/native_wine11_gpu_fd.o" \
    -ld3d11 -ldxgi -ldxguid -lntdll -lvulkan-1 -lcuda
printf 'Built isolated D3D11 GPU-frame receiver. No UU session started.\n'
