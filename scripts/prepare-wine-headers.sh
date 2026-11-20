#!/usr/bin/env bash
# The native builds use Wine 9's generated D3D11 COM declarations with Wine 11.
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
work="$repo_dir/build/gpu-relay"
name='libwine-dev_9.0~repack-4build3_amd64.deb'
archive="$work/downloads/$name"
sha=b9599eb86a630087248394795cb09abe023745f1230057f9762e152f83101f0a
mkdir -p "$work/downloads"
if [[ ! -f "$archive" ]]; then
    curl --fail --location --proto '=https' --proto-redir '=https' --retry 3 \
        "https://archive.ubuntu.com/ubuntu/pool/universe/w/wine/$name" -o "$archive.part"
    printf '%s  %s\n' "$sha" "$archive.part" | sha256sum --strict -c -
    mv -- "$archive.part" "$archive"
fi
printf '%s  %s\n' "$sha" "$archive" | sha256sum --strict -c -
dpkg-deb -x "$archive" "$work/wine-dev"
