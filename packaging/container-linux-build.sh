#!/usr/bin/env bash
# Run only as root in a disposable target container (see build-linux.sh).
set -Eeuo pipefail
target="${1:?target}" version="${2:?version}" output="${3:?output directory}"
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
((EUID == 0)) || { echo 'Use packaging/build-linux.sh to build in a container.' >&2; exit 1; }
# shellcheck source=/dev/null
. /etc/os-release
case "$target:${ID:-}:${VERSION_ID:-}" in
    debian-13:debian:13)
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends ca-certificates curl git ripgrep python3 \
            build-essential gcc-mingw-w64-x86-64 g++-mingw-w64-x86-64 gcc-mingw-w64-i686 \
            g++-mingw-w64-i686 meson ninja-build pkg-config flex bison xz-utils dpkg-dev \
            libjson-c-dev libx11-dev libxfixes-dev libpipewire-0.3-dev libspa-0.2-dev libvulkan-dev \
            glslang-tools libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libfcitx5core-dev \
            libfcitx5utils-dev libavcodec-dev libavutil-dev libswscale-dev libva-dev \
            libfreetype-dev libfontconfig-dev libgnutls28-dev libxext-dev libxrandr-dev \
            libxrender-dev libxinerama-dev libxcursor-dev libxi-dev libasound2-dev libpulse-dev libudev-dev
        ;;
    fedora-44:fedora:44)
        dnf install -y --setopt=install_weak_deps=False ca-certificates curl git ripgrep python3 gcc gcc-c++ \
            make mingw64-gcc mingw64-gcc-c++ mingw32-gcc mingw32-gcc-c++ meson ninja-build \
            pkgconf-pkg-config flex bison xz dpkg rpm-build python3-rpm \
            json-c-devel libX11-devel libXfixes-devel pipewire-devel vulkan-headers vulkan-loader-devel \
            glslang glslang-devel gstreamer1-devel gstreamer1-plugins-base-devel fcitx5-devel \
            ffmpeg-free-devel libva-devel freetype-devel fontconfig-devel gnutls-devel \
            libXext-devel libXrandr-devel libXrender-devel libXinerama-devel libXcursor-devel \
            libXi-devel alsa-lib-devel pulseaudio-libs-devel systemd-devel
        ;;
    arch:arch:*)
        pacman -Syu --noconfirm --needed ca-certificates curl git ripgrep python base-devel \
            mingw-w64-gcc meson ninja pkgconf flex bison xz dpkg \
            json-c libx11 libxfixes pipewire vulkan-headers vulkan-icd-loader glslang \
            gstreamer gst-plugins-base-libs fcitx5 ffmpeg libva freetype2 fontconfig gnutls \
            libxext libxrandr libxrender libxinerama libxcursor libxi alsa-lib libpulse systemd
        ;;
    *) echo "Target $target does not match ${PRETTY_NAME:-unknown}" >&2; exit 2 ;;
esac
cd "$repo_dir"
bash packaging/build-wine11.sh
# CUDA link stubs and headers are build inputs, never installed in a runtime package.
mkdir -p build/multidistro/downloads
cuda_base=https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64
while read -r file sha; do
    archive="$repo_dir/build/multidistro/downloads/$file"
    [[ -f "$archive" ]] || curl -fsSL --proto '=https' --proto-redir '=https' --retry 3 --max-time 300 "$cuda_base/$file" -o "$archive"
    printf '%s  %s\n' "$sha" "$archive" | sha256sum --strict -c -
    dpkg-deb -x "$archive" /
done <<'CUDA'
cuda-cudart-dev-12-1_12.1.105-1_amd64.deb 531c1cc54bc76e383a4a43f04a60f5bb1b6e34e79fc3a4156422057e87031240
cuda-driver-dev-12-1_12.1.105-1_amd64.deb 49a0807b1f37da920a9417b3fa16d5e281c306595412e40ba4af04195d1655f4
CUDA
ln -sfn cuda-12.1 /usr/local/cuda
export UURB_WINE_BIN=/opt/uuway-wine11/bin
export UURB_USE_SYSTEM_TOOLCHAIN=1 UURB_PIPEWIRE_HEADERS=/usr/include
UURB_GLSLANG="$(command -v glslangValidator)"
export UURB_GLSLANG
export LIBRARY_PATH=/usr/local/cuda/lib64/stubs
for script in build-dxvk-capture build-dxgi-capture-probe build-uu-native-bootstrap build-uu-native-input \
              build-uu-native-display build-native-ime build-helpers build-pipewire-probe; do
    printf '\nBuilding %s for %s\n' "$script" "$target"
    bash "scripts/$script.sh"
done
mkdir -p build/backends/nvidia
cp -a build/dxvk-capture build/helpers build/native-presenter build/backends/nvidia/
UURB_AMD_BASE="$repo_dir" bash scripts/build-amd-runtime.sh
mkdir -p build/backends/amd
cp -a build/dxvk-capture build/helpers build/native-presenter build/backends/amd/
rm -rf build/dxvk-capture build/helpers build/native-presenter build/encoder-backend
cp -a build/backends/nvidia/dxvk-capture build/backends/nvidia/helpers build/backends/nvidia/native-presenter build/
unset LIBRARY_PATH
python3 packaging/stage-linux.py --target "$target" --version "$version" --output-dir "$output"
