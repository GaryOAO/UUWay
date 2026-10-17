#!/usr/bin/env bash
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
stage="$repo_dir/build/native-presenter"
mkdir -p "$stage"
wine_headers="${UURB_WINE_HEADERS:-$repo_dir/build/gpu-relay/wine-dev/usr/include/wine}"
if [[ ! -d "$wine_headers/wine/windows" ]]; then wine_headers=/usr/include/wine; fi
[[ -d "$wine_headers/wine/windows" ]] || { echo 'Missing Wine development headers' >&2; exit 1; }
cc -std=c11 -O2 -Wall -Wextra -Werror -fPIC $(pkg-config --cflags json-c) \
  -c "$repo_dir/src/native_display_client.c" -o "$stage/native_display_client.o"
/opt/wine-stable/bin/winegcc -m64 -shared -std=c11 -O2 -Wall -Wextra -Werror \
  -I "$wine_headers" \
  -I "$wine_headers/wine/windows" \
  -o "$stage/uurb-native-display.dll" "$repo_dir/src/uu_native_display.spec" \
  "$repo_dir/src/uu_native_display.c" "$stage/native_display_client.o" $(pkg-config --libs json-c)
x86_64-w64-mingw32-gcc -std=c11 -O2 -Wall -Wextra -Werror -shared \
  -o "$stage/uurb-native-display-loader.dll" "$repo_dir/src/uu_native_display_loader.c"
