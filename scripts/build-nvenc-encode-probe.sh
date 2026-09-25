#!/usr/bin/env bash
# Experimental encoder ABI under test-only names, never installed into UU.
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
bash "$repo_dir/scripts/build-nvenc-query-probe.sh"
work="$repo_dir/build/gpu-relay"
stage="$repo_dir/build/native-presenter"
common=(-std=c11 -O2 -Wall -Wextra -Werror)
winincludes=(-I "$work/wine-dev/usr/include/wine" -I "$work/wine-dev/usr/include/wine/wine/windows"
    -I "$work/nv-codec-headers/include")
/opt/wine-stable/bin/winegcc -m64 -shared "${common[@]}" "${winincludes[@]}" \
    -o "$stage/uurb-nvenc-encode.dll" "$repo_dir/src/uu_nvenc_query.spec" \
    "$repo_dir/src/uu_nvenc_encode.c" "$repo_dir/src/uu_d3d11_encode_session.c" \
    "$repo_dir/src/uu_d3d11_frame_adapter.c" "$stage/native_nvenc_query.o" \
    "$stage/native_cuda_encode_session.o" "$stage/native_wine11_gpu_fd.o" \
    -ld3d11 -ld3dcompiler -ldxgi -ldxguid -luuid -lntdll -lvulkan-1 -ldl -pthread -lcuda
x86_64-w64-mingw32-gcc "${common[@]}" -shared -I "$work/nv-codec-headers/include" \
    '-DUURB_NVENC_BACKEND_FILENAME=L"uurb-nvenc-encode.dll.so"' \
    -o "$stage/uurb-nvenc-encode-loader.dll" "$repo_dir/src/uu_nvenc_query_loader.c"
x86_64-w64-mingw32-gcc "${common[@]}" -I "$work/nv-codec-headers/include" \
    -o "$stage/uu-nvenc-pe-encode-probe.exe" "$repo_dir/src/uu_nvenc_encode_probe.c" \
    -ld3d11 -ld3dcompiler -ldxgi -ldxguid
printf 'Built isolated Windows encoder-ABI prototype. No production DLL changed.\n'
