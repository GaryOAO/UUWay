#!/usr/bin/env bash
# Native metadata-only DMA-BUF probe; no RDP or Wine dependency.
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
stage="$repo_dir/build/native-presenter"
mkdir -p "$stage"
pkg-config --atleast-version=1.24 gstreamer-video-1.0
read -r -a capture_flags <<<"$(pkg-config --cflags --libs gstreamer-app-1.0 gstreamer-allocators-1.0 gstreamer-video-1.0)"
gcc -std=c11 -O2 -Wall -Wextra -Werror "$repo_dir/src/uu_pipewire_dmabuf_probe.c" \
    -o "$stage/uu-pipewire-dmabuf-probe" "${capture_flags[@]}"
printf 'Built native DMA-BUF probe. Running it requires a user-approved portal session.\n'
