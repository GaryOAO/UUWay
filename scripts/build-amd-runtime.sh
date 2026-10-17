#!/usr/bin/env bash
# Build an explicit AMD runtime using the installed UUWay support artifacts.
# No CUDA headers, CUDA stubs, NVIDIA driver or global DXVK overrides required.
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
base="${1:-/usr/lib/uuway}"
winegcc="${UURB_WINEGCC:-/opt/wine-stable/bin/winegcc}"
stage="$repo_dir/build/native-presenter"
headers="$repo_dir/build/gpu-relay/nv-codec-headers"
for tool in gcc x86_64-w64-mingw32-gcc "$winegcc" pkg-config glslangValidator; do
    command -v "$tool" >/dev/null || { echo "Missing build dependency: $tool" >&2; exit 1; }
done
[[ -d "$base/build/dxvk-capture/stage" ]] || { echo 'Install the upstream uuway package first.' >&2; exit 1; }
[[ -d "$headers/.git" ]] || git clone --depth 1 --branch n13.0.19.0 https://github.com/FFmpeg/nv-codec-headers.git "$headers"
[[ "$(git -C "$headers" rev-parse HEAD)" == e844e5b26f46bb77479f063029595293aa8f812d ]] || exit 1
git -C "$headers" diff --exit-code HEAD -- include/
mkdir -p "$stage"
rm -f "$repo_dir/build/encoder-backend"
cp -a "$base/build/dxvk-capture" "$repo_dir/build/"
cp -a "$base/build/helpers" "$repo_dir/build/"
cp -a "$base/build/native-presenter/." "$stage/"
bash "$repo_dir/scripts/build-uu-native-bootstrap.sh"
common=(-std=c11 -O2 -Wall -Wextra -Werror -DUURB_VAAPI -I "$headers/include")
if [[ "${UURB_AMD_TRACE:-0}" == 1 ]]; then common+=(-DUURB_NVENC_TRACE); fi
win=(-I /usr/include/wine -I /usr/include/wine/wine/windows)
native=(-lavcodec -lavutil -lswscale -lvulkan -lva -ldl -pthread)
for source in native_vaapi_encoder native_vaapi_query native_gpu_frame_channel native_host_frame; do
    gcc "${common[@]}" -fPIC -c "$repo_dir/src/$source.c" -o "$stage/$source.o"
done
"$winegcc" -m64 -shared "${common[@]}" "${win[@]}" \
    -o "$stage/uurb-nvenc-encode.dll" "$repo_dir/src/uu_nvenc_query.spec" \
    "$repo_dir/src/uu_nvenc_encode.c" "$repo_dir/src/uu_d3d11_vaapi_session.c" \
    "$repo_dir/src/uu_d3d11_frame_adapter.c" "$stage/native_vaapi_encoder.o" "$stage/native_vaapi_query.o" \
    -ld3d11 -ld3dcompiler -ldxgi -ldxguid -luuid -lvulkan-1 "${native[@]}"
x86_64-w64-mingw32-gcc "${common[@]}" -shared \
    '-DUURB_NVENC_BACKEND_FILENAME=L"uurb-nvenc-encode.dll.so"' \
    -o "$stage/uurb-nvenc-encode-loader.dll" "$repo_dir/src/uu_nvenc_query_loader.c"
"$winegcc" -m64 -shared "${common[@]}" "${win[@]}" \
    -o "$stage/uurb-dxgi-capture.dll" "$repo_dir/src/uu_dxgi_duplication.spec" \
    "$repo_dir/src/uu_dxgi_duplication.c" "$repo_dir/src/uu_d3d11_host_capture.c" "$stage/native_gpu_frame_channel.o" \
    -ld3d11 -ldxgi -ldxguid -luuid
UURB_GLSLANG="$(command -v glslangValidator)" bash "$repo_dir/scripts/build-native-cursor-shaders.sh"
read -r -a pipewire_cflags <<< "$(pkg-config --cflags libpipewire-0.3)"
gcc "${common[@]}" -std=gnu11 "${pipewire_cflags[@]}" \
    "$repo_dir/src/uu_pipewire_native_probe.c" "$repo_dir/src/native_vk_dmabuf_import.c" \
    "$repo_dir/src/native_vk_capture_encode.c" "$repo_dir/src/native_host_frame.c" \
    "$repo_dir/src/native_gpu_frame_channel.c" "$repo_dir/src/native_cursor_metadata.c" \
    "$repo_dir/src/native_vk_cursor_composite.c" "$stage/native_vaapi_encoder.o" \
    -I "$stage/cursor-shaders" -o "$stage/uu-pipewire-native-probe" -lpipewire-0.3 "${native[@]}"
gcc "${common[@]}" -I "$repo_dir/src" "$repo_dir/tests/probes/vaapi_encode.c" \
    "$stage/native_vaapi_encoder.o" -o "$stage/uu-vaapi-probe" "${native[@]}"
x86_64-w64-mingw32-gcc "${common[@]}" -o "$stage/uu-nvenc-pe-encode-probe.exe" \
    "$repo_dir/src/uu_nvenc_encode_probe.c" -ld3d11 -ld3dcompiler -ldxgi -ldxguid
gcc -std=c11 -O2 -Wall -Wextra -Werror -D_GNU_SOURCE -I "$repo_dir/src" \
    "$repo_dir/tests/probes/host_frame.c" "$stage/native_host_frame.o" \
    -lvulkan -o "$stage/uu-host-frame-probe"
# Test the same DXVK binaries that will be packaged.
cp "$repo_dir/build/dxvk-capture/stage/d3d11.dll" "$repo_dir/build/dxvk-capture/stage/dxgi.dll" "$stage/"
printf 'amd-vaapi\n' > "$repo_dir/build/encoder-backend"
printf 'Built AMD VA-API runtime. Run scripts/test-amd-runtime.py before setup.\n'
