#!/usr/bin/env bash
# Build every native artifact and the uuway .deb inside a clean Ubuntu 24.04.
#
# Run as root from the repository root, in a container or a CI job:
#     packaging/container-build.sh --version 1.0.0
# packaging/build-deb.sh runs this in a throwaway local docker container.
#
# The environment is pinned to what the shipped binaries were validated with:
# gcc 11 (the compiler of the live-tested builds), WineHQ stable 11.0.0.0 and the
# CUDA 12.1 headers.  Newer CUDA headers would map cuCtxCreate and friends to
# newer symbol versions and break older drivers at load time.
set -Eeuo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
version=""
output_dir="$repo_dir/build/deb"

wine_version='11.0.0.0~noble-1'
cuda_base='https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64'
cuda_debs=(
    'cuda-cudart-dev-12-1_12.1.105-1_amd64.deb 531c1cc54bc76e383a4a43f04a60f5bb1b6e34e79fc3a4156422057e87031240'
    'cuda-driver-dev-12-1_12.1.105-1_amd64.deb 49a0807b1f37da920a9417b3fa16d5e281c306595412e40ba4af04195d1655f4'
)
packages=(ca-certificates curl gnupg git ripgrep python3 build-essential gcc-11 g++-11
          gcc-mingw-w64-x86-64 g++-mingw-w64-x86-64 meson ninja-build pkg-config
          libjson-c-dev libx11-dev libxfixes-dev libpipewire-0.3-dev libspa-0.2-dev libvulkan-dev
          glslang-tools libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
          libfcitx5core-dev libfcitx5utils-dev dpkg-dev xz-utils)

die() { printf '\n✗ %s\n' "$*" >&2; exit 1; }
step() { printf '\n━━ %s\n' "$*"; }

while (($#)); do
    case "$1" in
        --version) version="${2:?--version needs a value}"; shift 2 ;;
        --output-dir) output_dir="${2:?--output-dir needs a path}"; shift 2 ;;
        -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done
[[ -n "$version" ]] || die "--version is required (for example 1.0.0)"
((EUID == 0)) || die "run as root inside a disposable Ubuntu 24.04 container"
# shellcheck source=/dev/null
. /etc/os-release
[[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04 ]] || die "needs Ubuntu 24.04 (found ${PRETTY_NAME:-unknown})"
export DEBIAN_FRONTEND=noninteractive

step "Build packages"
apt-get update -qq
apt-get install -y -qq --no-install-recommends "${packages[@]}"

step "WineHQ stable $wine_version (winegcc and import libraries)"
if ! dpkg-query -W -f='${Version}' winehq-stable 2>/dev/null | grep -qx "$wine_version"; then
    dpkg --add-architecture i386
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSLo /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key
    curl -fsSLo /etc/apt/sources.list.d/winehq-noble.sources \
        https://dl.winehq.org/wine-builds/ubuntu/dists/noble/winehq-noble.sources
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends "winehq-stable=$wine_version" \
        "wine-stable=$wine_version" "wine-stable-amd64=$wine_version" "wine-stable-i386:i386=$wine_version"
fi
[[ "$(/opt/wine-stable/bin/wine --version)" == wine-11.0 ]] || die "Wine is not 11.0"

step "CUDA 12.1 headers and the official libcuda link stub"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
for entry in "${cuda_debs[@]}"; do
    read -r file sha <<<"$entry"
    curl -fsSL --retry 3 --proto '=https' -o "$scratch/$file" "$cuda_base/$file"
    printf '%s  %s\n' "$sha" "$scratch/$file" | sha256sum --strict -c - >/dev/null || die "hash mismatch: $file"
    dpkg-deb -x "$scratch/$file" /
done
ln -sfn cuda-12.1 /usr/local/cuda
[[ -f /usr/local/cuda/include/cuda.h && -f /usr/local/cuda/lib64/stubs/libcuda.so ]] || die "CUDA headers missing"

step "Toolchain: gcc 11 first on PATH"
for tool in gcc cc; do ln -sf /usr/bin/gcc-11 "/usr/local/bin/$tool"; done
for tool in g++ c++; do ln -sf /usr/bin/g++-11 "/usr/local/bin/$tool"; done
export PATH="/usr/local/bin:$PATH"
gcc --version | head -1

step "Pinned DXVK build tools (archive.ubuntu.com first, the configured mirror as fallback)"
/usr/bin/python3 - "$repo_dir" <<'PY'
import hashlib, json, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
manifest = json.loads((root / 'config/dxvk-capture-dependencies.json').read_text())
downloads = root / 'build/gpu-relay/downloads'
downloads.mkdir(parents=True, exist_ok=True)
for package in manifest['packages']:
    target = downloads / package['file']
    if target.is_file():
        continue
    configured = package['url']
    sources = [configured.replace('mirrors.tuna.tsinghua.edu.cn/ubuntu', 'archive.ubuntu.com/ubuntu'), configured]
    for url in dict.fromkeys(sources):
        partial = target.with_suffix('.deb.part')
        done = subprocess.run(['curl', '--fail', '--location', '--proto', '=https', '--retry', '3',
                               '--max-time', '300', '-o', str(partial), url])
        if done.returncode == 0 and hashlib.sha256(partial.read_bytes()).hexdigest() == package['sha256']:
            partial.rename(target)
            break
        partial.unlink(missing_ok=True)
    else:
        raise SystemExit('could not fetch ' + package['file'])
PY

step "Native runtime (same order as install.sh step 3)"
cd "$repo_dir"
export UURB_PIPEWIRE_HEADERS=/usr/include
export LIBRARY_PATH=/usr/local/cuda/lib64/stubs
export UURB_GLSLANG="$repo_dir/build/dxvk-capture/toolchain/usr/bin/glslangValidator"
for script in build-dxvk-capture build-dxgi-capture-probe build-uu-native-bootstrap build-uu-native-input \
              build-uu-native-display build-native-ime build-helpers build-pipewire-probe; do
    printf '\n── %s\n' "$script"
    bash "scripts/$script.sh"
done

step "Package"
unset LIBRARY_PATH
/usr/bin/python3 packaging/stage-deb.py --version "$version" --output-dir "$output_dir"
