#!/usr/bin/env bash
# Private source build, no install into the official UU prefix or system Wine.
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source_dir="$repo_dir/build/dxvk-capture/source-v3.1"
build_dir="$repo_dir/build/dxvk-capture/win64"
stage="$repo_dir/build/dxvk-capture/stage"
revision=70d7508c01201ed3d4bfb33da42ba834eafe3857
mkdir -p "$repo_dir/build/dxvk-capture"
exec 9>"$repo_dir/build/dxvk-capture/build.lock"
flock -n 9 || { printf 'Private DXVK build already running\n' >&2; exit 1; }
/usr/bin/python3 "$repo_dir/scripts/prepare-dxvk-capture.py"
if [[ ! -d "$source_dir/.git" ]]; then
    git clone --depth 1 --branch v3.1 https://github.com/doitsujin/dxvk.git "$source_dir"
fi
[[ "$(git -C "$source_dir" rev-parse HEAD)" == "$revision" ]] || { printf 'Wrong DXVK revision\n' >&2; exit 1; }
git -C "$source_dir" submodule update --init --recursive --depth 1
[[ -z "$(git -C "$source_dir" submodule status --recursive | rg '^[+-U]' || true)" ]] || exit 1
git -C "$source_dir" submodule foreach --quiet --recursive 'git diff --exit-code && git diff --cached --exit-code'
patch_file="$repo_dir/patches/dxvk-3.1-private-capture.patch"
if ! git -C "$source_dir" apply --reverse --check "$patch_file" 2>/dev/null; then
    git -C "$source_dir" apply --check "$patch_file"
    git -C "$source_dir" apply "$patch_file"
fi
git -C "$source_dir" diff -- src/dxgi/dxgi_output.cpp | cmp - "$patch_file"
git -C "$source_dir" diff --exit-code -- . ':(exclude)src/dxgi/dxgi_output.cpp'
git -C "$source_dir" diff --cached --exit-code
if ! cmp -s "$repo_dir/src/dxvk_capture_hook.h" "$source_dir/src/dxgi/dxgi_uurb_capture.h"; then
    cp -- "$repo_dir/src/dxvk_capture_hook.h" "$source_dir/src/dxgi/dxgi_uurb_capture.h"
fi
export PATH="$repo_dir/scripts:$repo_dir/build/dxvk-capture/toolchain/usr/bin:$PATH"
if [[ ! -f "$build_dir/build.ninja" ]]; then
    meson setup "$build_dir" "$source_dir" --cross-file "$repo_dir/config/dxvk-capture-win64.ini" \
        --buildtype=release -Denable_d3d8=false -Denable_d3d9=false -Denable_d3d10=false \
        -Denable_d3d11=true -Denable_dxgi=true -Ddxbc-spirv:enable_tests=false -Ddxbc-spirv:enable_tools=false
fi
nice -n 10 ninja -C "$build_dir" -j 4
mkdir -p "$stage"
cp -- "$build_dir/src/dxgi/dxgi.dll" "$stage/dxgi.dll"
cp -- "$build_dir/src/d3d11/d3d11.dll" "$stage/d3d11.dll"
cp -- "$source_dir/LICENSE" "$stage/DXVK_LICENSE"
/usr/bin/python3 "$repo_dir/scripts/report-dxvk-capture-build.py" > "$repo_dir/build/dxvk-capture/build-report.json"
printf 'Private DXVK capture build staged at %s; no production DLLs changed.\n' "$stage"
