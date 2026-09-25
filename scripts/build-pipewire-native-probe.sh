#!/usr/bin/env bash
# Isolated native PipeWire consumer, linked to the installed runtime library.
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
headers="${UURB_PIPEWIRE_HEADERS:-$repo_dir/build/portal-recovery/sysroot/usr/include}"
stage="$repo_dir/build/native-presenter"
for required_header in "$headers/pipewire-0.3/pipewire/pipewire.h" \
    "$headers/spa-0.2/spa/param/video/format-utils.h" /usr/include/vulkan/vulkan.h \
    /usr/local/cuda/include/cuda.h "$repo_dir/build/gpu-relay/nv-codec-headers/include/ffnvcodec/nvEncodeAPI.h"; do
    if [[ ! -f "$required_header" ]]; then
        printf 'Missing native GPU probe header: %s\nPrepare CUDA/Vulkan development headers, run scripts/build-gpu-interop-probe.sh for pinned NVENC headers and scripts/build-portal-lifetime.sh for isolated PipeWire headers.\n' "$required_header" >&2
        exit 1
    fi
done
mkdir -p "$stage"
bash "$repo_dir/scripts/build-native-cursor-shaders.sh"
gcc -std=gnu11 -O2 -Wall -Wextra -Werror \
    -isystem "$headers/pipewire-0.3" -isystem "$headers/spa-0.2" \
    "$repo_dir/src/uu_pipewire_native_probe.c" \
    "$repo_dir/src/native_vk_dmabuf_import.c" \
    "$repo_dir/src/native_vk_capture_encode.c" "$repo_dir/src/native_cuda_encode_session.c" \
    "$repo_dir/src/native_gpu_frame_channel.c" \
    "$repo_dir/src/native_cursor_metadata.c" \
    "$repo_dir/src/native_vk_cursor_composite.c" -I "$stage/cursor-shaders" \
    -I /usr/local/cuda/include -I "$repo_dir/build/gpu-relay/nv-codec-headers/include" \
    -o "$stage/uu-pipewire-native-probe" \
    -l:libpipewire-0.3.so.0 -lvulkan -lcuda -ldl -pthread
gcc -std=c11 -O2 -Wall -Wextra -Werror \
    "$repo_dir/src/native_gpu_receiver_probe.c" "$repo_dir/src/native_gpu_frame_channel.c" \
    "$repo_dir/src/native_cuda_encode_session.c" \
    -I /usr/local/cuda/include -I "$repo_dir/build/gpu-relay/nv-codec-headers/include" \
    -o "$stage/uu-gpu-receiver-probe" -lcuda -ldl -pthread
