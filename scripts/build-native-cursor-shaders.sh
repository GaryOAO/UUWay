#!/usr/bin/env bash
# Offline build only. Shader compiler is a build dependency, never a runtime one.
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
compiler="${UURB_GLSLANG:-$repo_dir/build/cursor-shader-tools/usr/bin/glslangValidator}"
stage="$repo_dir/build/native-presenter/cursor-shaders"
if [[ ! -x "$compiler" ]]; then
    printf 'Missing glslangValidator; set UURB_GLSLANG to the build compiler.\n' >&2
    exit 1
fi
mkdir -p "$stage"
for kind in vert frag; do
    "$compiler" -V --target-env vulkan1.0 --vn "uurb_cursor_${kind}_spv" \
        -o "$stage/native_cursor_${kind}.h" "$repo_dir/src/shaders/native_cursor.$kind"
done
