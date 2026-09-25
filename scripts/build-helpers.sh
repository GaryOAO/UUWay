#!/usr/bin/env bash
# Build the helpers the native service loads from <prefix>/compat:
#   uu-terminal-bridge   Linux PTY broker (persistent terminal sessions)
#   uu-terminal-proxy.exe PowerShell stand-in for UU's terminal launcher
#   uu-conpty.dll        conpty.dll stand-in joining UU's terminal to the PTY
#   uu-clipboard-bridge  clipboard bridge between UU's X display and the desktop
set -Eeuo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="${1:-$repo_dir/build/helpers}"
common=(-std=c11 -O2 -Wall -Wextra -Werror)
pe_link=(-Wl,--no-insert-timestamp)
mkdir -p "$output_dir"

x86_64-w64-mingw32-gcc "${common[@]}" "${pe_link[@]}" -I "$repo_dir/src" \
    -o "$output_dir/uu-terminal-proxy.exe" \
    "$repo_dir/src/uu_terminal_proxy.c" -lws2_32
x86_64-w64-mingw32-gcc "${common[@]}" "${pe_link[@]}" -shared -I "$repo_dir/src" \
    -o "$output_dir/uu-conpty.dll" \
    "$repo_dir/src/uu_conpty_shim.c" "$repo_dir/src/uu_conpty_shim.def" -lws2_32
cc "${common[@]}" -I "$repo_dir/src" \
    -o "$output_dir/uu-terminal-bridge" \
    "$repo_dir/src/uu_terminal_bridge.c" -lutil
cc "${common[@]}" \
    -o "$output_dir/uu-clipboard-bridge" \
    "$repo_dir/src/uu_clipboard_bridge.c" -lX11 -lXfixes

x86_64-w64-mingw32-strip "$output_dir/uu-terminal-proxy.exe" "$output_dir/uu-conpty.dll"
strip "$output_dir/uu-terminal-bridge" "$output_dir/uu-clipboard-bridge"
printf 'Built helpers in %s\n' "$output_dir"
