#!/usr/bin/env bash
# Query-only Windows ABI test DLL. Does NOT install/replace nvEncodeAPI64.dll.
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
bash "$repo_dir/scripts/build-gpu-interop-probe.sh"
work="$repo_dir/build/gpu-relay"
stage="$repo_dir/build/native-presenter"
common=(-std=c11 -O2 -Wall -Wextra -Werror)
gcc "${common[@]}" -fPIC -I /usr/local/cuda/include -I "$work/nv-codec-headers/include" \
    -c "$repo_dir/src/native_nvenc_query.c" -o "$stage/native_nvenc_query.o"
winincludes=(-I "$work/wine-dev/usr/include/wine" -I "$work/wine-dev/usr/include/wine/wine/windows"
    -I "$work/nv-codec-headers/include")
/opt/wine-stable/bin/winegcc -m64 -shared "${common[@]}" "${winincludes[@]}" \
    -o "$stage/uurb-nvenc-query.dll" "$repo_dir/src/uu_nvenc_query.spec" \
    "$repo_dir/src/uu_nvenc_query.c" "$stage/native_nvenc_query.o" \
    -ld3d11 -ldxguid -lvulkan-1 -ldl -lpthread -lcuda
/opt/wine-stable/bin/winegcc -m64 "${common[@]}" "${winincludes[@]}" \
    -o "$stage/uu-nvenc-query-probe.exe" "$repo_dir/src/uu_nvenc_query_probe.c" \
    -ld3d11 -ldxgi -ldxguid
x86_64-w64-mingw32-gcc "${common[@]}" -I "$work/nv-codec-headers/include" \
    -o "$stage/uu-nvenc-pe-query-probe.exe" "$repo_dir/src/uu_nvenc_query_probe.c" \
    -ld3d11 -ldxgi -ldxguid
x86_64-w64-mingw32-gcc "${common[@]}" -shared -I "$work/nv-codec-headers/include" \
    -o "$stage/uurb-nvenc-query-loader.dll" "$repo_dir/src/uu_nvenc_query_loader.c"
printf 'Built query-only Windows ABI probe; production UU and RDP remain untouched.\n'
