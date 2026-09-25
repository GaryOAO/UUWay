#!/usr/bin/env bash
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
stage="$repo_dir/build/native-presenter"
mkdir -p "$stage"
cc -std=c11 -O2 -Wall -Wextra -Werror -o "$stage/uu-native-input" "$repo_dir/src/uu_native_input.c" "$repo_dir/src/native_text_client.c" "$repo_dir/src/native_input_settings.c" -ljson-c
x86_64-w64-mingw32-gcc -O2 -Wall -Wextra -Werror -DUURB_NATIVE_INPUT_ONLY \
    -shared -o "$stage/uu-native-input-bridge.dll" "$repo_dir/src/uu_input_bridge.c" -luser32 -lgdi32
x86_64-w64-mingw32-gcc -O2 -Wall -Wextra -Werror -DUURB_NATIVE_INPUT_ONLY -municode -mwindows \
    -o "$stage/uu-native-input-broker.exe" "$repo_dir/src/uu_input_broker.c" -luser32 -lws2_32
x86_64-w64-mingw32-gcc -O2 -Wall -Wextra -Werror -municode \
    -o "$stage/uu-native-input-injector.exe" "$repo_dir/src/uu_injector.c"
x86_64-w64-mingw32-gcc -O2 -Wall -Wextra -Werror -municode \
    -o "$stage/uu-native-input-send.exe" "$repo_dir/tests/probes/native_input_send.c"
