#!/usr/bin/env bash
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
headers="${UURB_FCITX5_HEADERS:-$repo_dir/build/fcitx-ime-deps/usr/include/Fcitx5}"
# Without a private header tree, use the distribution's libfcitx5core-dev.
[[ -d "$headers/Core" ]] || headers=/usr/include/Fcitx5
[[ -d "$headers/Core" ]] || {
    printf 'Missing Fcitx5 headers; install libfcitx5core-dev or set UURB_FCITX5_HEADERS.\n' >&2
    exit 1
}
stage="$repo_dir/build/native-ime"
mkdir -p "$stage"
c++ -std=c++17 -O2 -fPIC -shared -Wall -Wextra -Werror \
    -I"$headers/Core" -I"$headers/Utils" -I"$headers/Config" \
    "$repo_dir/src/uu_fcitx_native_ime.cpp" \
    -l:libFcitx5Core.so.7 -l:libFcitx5Utils.so.2 \
    -o "$stage/libuurb-native-ime.so"
