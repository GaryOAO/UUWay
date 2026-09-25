#!/usr/bin/env bash
# Build/stage an experimental renderer. Does not change any installed prefix.
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
version=3.1
digest=30f9cc326874be344285582275446968cfa4c069db31ce56df312d6644179154
archive="$repo_dir/build/gpu-relay/downloads/dxvk-$version.tar.gz"
stage="$repo_dir/build/native-presenter"
for tool in x86_64-w64-mingw32-gcc curl sha256sum tar; do
    command -v "$tool" >/dev/null || { printf 'Missing tool: %s\n' "$tool" >&2; exit 1; }
done
mkdir -p "$(dirname "$archive")" "$stage"
if [[ ! -f "$archive" ]]; then
    curl --fail --location --proto '=https' --proto-redir '=https' --retry 3 \
        "https://github.com/doitsujin/dxvk/releases/download/v$version/dxvk-$version.tar.gz" \
        -o "$archive.part"
    printf '%s  %s\n' "$digest" "$archive.part" | sha256sum --strict -c -
    mv -- "$archive.part" "$archive"
fi
printf '%s  %s\n' "$digest" "$archive" | sha256sum --strict -c -
tar -xzf "$archive" -C "$stage" --strip-components=2 \
    "dxvk-$version/x64/d3d11.dll" "dxvk-$version/x64/dxgi.dll"
x86_64-w64-mingw32-gcc -std=c11 -O2 -Wall -Wextra -Werror -municode \
    -Wl,--no-insert-timestamp -o "$stage/uu-native-presenter.exe" \
    "$repo_dir/src/uu_native_presenter.c" \
    -ld3d11 -ldxgi -ldxguid -ld3dcompiler -lgdi32 -luser32 -lshell32
cp -- "$repo_dir/config/dxvk-native.conf" "$stage/dxvk.conf"
printf 'Built experimental DXVK presenter: %s\n' "$stage"
