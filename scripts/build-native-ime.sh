#!/usr/bin/env bash
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
headers="$repo_dir/build/fcitx-ime-deps/usr/include/Fcitx5"
stage="$repo_dir/build/native-ime"
mkdir -p "$stage"
c++ -std=c++17 -O2 -fPIC -shared -Wall -Wextra -Werror \
    -I"$headers/Core" -I"$headers/Utils" -I"$headers/Config" \
    "$repo_dir/src/uu_fcitx_native_ime.cpp" \
    -l:libFcitx5Core.so.7 -l:libFcitx5Utils.so.2 \
    -o "$stage/libuurb-native-ime.so"
