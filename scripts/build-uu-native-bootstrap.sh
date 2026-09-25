#!/usr/bin/env bash
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
stage="$repo_dir/build/native-presenter"
mkdir -p "$stage"
x86_64-w64-mingw32-gcc -std=c11 -O2 -Wall -Wextra -Werror -municode \
    -o "$stage/uu-native-bootstrap.exe" "$repo_dir/src/uu_native_wine_bootstrap.c" -ladvapi32
x86_64-w64-mingw32-gcc -std=c11 -O2 -Wall -Wextra -Werror -mwindows \
    -o "$stage/uu-native-winlogon.exe" "$repo_dir/src/winlogon.c"
