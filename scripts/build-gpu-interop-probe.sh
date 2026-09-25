#!/usr/bin/env bash
# Experimental, pinned Wine 11.0/DXVK 3.1 native CUDA interoperability test.
# Downloads development inputs into build/, never installs system/prefix files.
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
work="$repo_dir/build/gpu-relay"
stage="$repo_dir/build/native-presenter"
wine_revision=db11d0fe6a169c457e23d007e20404643d067aa8
codec_revision=e844e5b26f46bb77479f063029595293aa8f812d
header_archive="$work/downloads/libwine-dev_9.0~repack-4build3_amd64.deb"
header_sha=b9599eb86a630087248394795cb09abe023745f1230057f9762e152f83101f0a
for tool in gcc git curl sha256sum dpkg-deb /opt/wine-stable/bin/winegcc; do
    command -v "$tool" >/dev/null || { printf 'Missing dependency: %s\n' "$tool" >&2; exit 1; }
done
[[ "$(/opt/wine-stable/bin/wine --version)" == wine-11.0 ]] || {
    printf 'Private server protocol requires Wine 11.0.\n' >&2; exit 1;
}
[[ -f /usr/local/cuda/include/cuda.h && -f /usr/include/vulkan/vulkan.h ]] || {
    printf 'CUDA and Vulkan development headers are required.\n' >&2; exit 1;
}
if [[ ! -d "$work/wine-review/.git" ]]; then
    git clone --depth 1 --branch wine-11.0 --filter=blob:none --sparse \
        https://github.com/wine-mirror/wine.git "$work/wine-review"
    git -C "$work/wine-review" sparse-checkout set include
fi
if [[ ! -d "$work/nv-codec-headers/.git" ]]; then
    git clone --depth 1 --branch n13.0.19.0 \
        https://github.com/FFmpeg/nv-codec-headers.git "$work/nv-codec-headers"
fi
for source in wine-review nv-codec-headers; do
    expected="$wine_revision"
    [[ "$source" != nv-codec-headers ]] || expected="$codec_revision"
    [[ "$(git -C "$work/$source" rev-parse HEAD)" == "$expected" ]] || {
        printf 'Unexpected dependency revision: %s\n' "$source" >&2; exit 1;
    }
    git -C "$work/$source" diff --exit-code HEAD -- include/
    [[ -z "$(git -C "$work/$source" ls-files --others -- include/)" ]] || {
        printf 'Untracked dependency headers: %s\n' "$source" >&2; exit 1;
    }
done
mkdir -p "$work/downloads" "$stage"
if [[ ! -f "$header_archive" ]]; then
    curl --fail --location --proto '=https' --proto-redir '=https' --retry 3 \
        'https://archive.ubuntu.com/ubuntu/pool/universe/w/wine/libwine-dev_9.0~repack-4build3_amd64.deb' \
        -o "$header_archive.part"
    printf '%s  %s\n' "$header_sha" "$header_archive.part" | sha256sum --strict -c -
    mv -- "$header_archive.part" "$header_archive"
fi
printf '%s  %s\n' "$header_sha" "$header_archive" | sha256sum --strict -c -
# Wine 9 generated COM headers provide the unchanged D3D11 declarations. Wine 11
# private server headers are compiled in a separate native translation unit.
dpkg-deb -x "$header_archive" "$work/wine-dev"
bash "$repo_dir/scripts/build-native-presenter.sh"
common=(-std=c11 -O2 -Wall -Wextra -Werror)
gcc "${common[@]}" -fPIC -I /usr/local/cuda/include -I "$work/nv-codec-headers/include" \
    -c "$repo_dir/src/native_cuda_encode_session.c" -o "$stage/native_cuda_encode_session.o"
gcc "${common[@]}" -fPIC -I /usr/local/cuda/include \
    -c "$repo_dir/src/native_cuda_encode_probe.c" -o "$stage/native_cuda_encode_probe.o"
gcc "${common[@]}" -fPIC -I "$work/nv-codec-headers/include" \
    -c "$repo_dir/tests/probes/nvenc_symbol_collision.c" -o "$stage/nvenc_symbol_collision.o"
gcc "${common[@]}" -fPIC -I "$work/wine-review/include" \
    -c "$repo_dir/src/native_wine11_gpu_fd.c" -o "$stage/native_wine11_gpu_fd.o"
/opt/wine-stable/bin/winegcc -m64 "${common[@]}" \
    -I "$work/wine-dev/usr/include/wine" -I "$work/wine-dev/usr/include/wine/wine/windows" \
    -I "$work/nv-codec-headers/include" \
    -o "$stage/uu-gpu-interop-probe.exe" "$repo_dir/src/uu_gpu_interop_probe.c" \
    "$repo_dir/src/uu_d3d11_frame_adapter.c" \
    "$repo_dir/src/uu_d3d11_encode_session.c" \
    "$stage/native_cuda_encode_session.o" "$stage/native_cuda_encode_probe.o" "$stage/native_wine11_gpu_fd.o" \
    "$stage/nvenc_symbol_collision.o" \
    -ld3d11 -ld3dcompiler -ldxgi -ldxguid -luuid -lntdll -lvulkan-1 -ldl -lcuda -pthread
printf 'Built synthetic GPU interop probe. No UU or Wine prefix was changed.\n'
