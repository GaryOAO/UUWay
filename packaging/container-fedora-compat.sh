#!/usr/bin/env bash
# Build the Fedora 45 components that link to the distribution's FFmpeg.
set -Eeuo pipefail
output="${1:?output directory}"
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
. /etc/os-release
[[ $EUID == 0 && "$ID:$VERSION_ID" == fedora:45 ]] || { echo 'Run in a Fedora 45 build container.' >&2; exit 2; }
dnf install -y --setopt=install_weak_deps=False \
    ca-certificates curl git gcc gcc-c++ mingw64-gcc pkgconf-pkg-config dpkg \
    pipewire-devel libdrm-devel vulkan-headers vulkan-loader-devel glslang glslang-devel \
    ffmpeg-free-devel libva-devel
bash "$repo_dir/scripts/install-wine11.sh" fedora-45 --build-tools
bash "$repo_dir/scripts/prepare-wine-headers.sh"
UURB_WINE_BIN=/opt/uuway-wine11/usr/bin bash "$repo_dir/scripts/build-amd-runtime.sh" --ffmpeg-only "$output"
