#!/usr/bin/env bash
# Install official prebuilt Wine 11.0; never compile Wine.
set -Eeuo pipefail
target="${1:?target}"; shift
tools=0
[[ "${1:-}" != --build-tools ]] || tools=1
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
((EUID == 0)) || { echo 'Run this helper as root, or use uuway setup.' >&2; exit 1; }
# shellcheck source=/dev/null
. /etc/os-release
case "$target:${ID:-}:${VERSION_ID:-}" in
    debian-13:debian:13|fedora-44:fedora:44|arch:arch:*) ;;
    *) echo "Wine target $target does not match ${PRETTY_NAME:-unknown}" >&2; exit 2 ;;
esac
case "$target" in
    debian-13)
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y --no-install-recommends ca-certificates curl
        dpkg --add-architecture i386
        install -d -m 0755 /etc/apt/keyrings
        curl -fsSL --proto '=https' --proto-redir '=https' https://dl.winehq.org/wine-builds/winehq.key -o /etc/apt/keyrings/winehq-archive.asc
        curl -fsSL --proto '=https' --proto-redir '=https' https://dl.winehq.org/wine-builds/debian/dists/trixie/winehq-trixie.sources -o /etc/apt/sources.list.d/winehq-trixie.sources
        sed -i 's@winehq-archive.key@winehq-archive.asc@' /etc/apt/sources.list.d/winehq-trixie.sources
        install -m 0644 "$repo_dir/config/uuway-wine.preferences" /etc/apt/preferences.d/uuway-wine
        apt-get update -qq
        apt-get install -y --no-install-recommends 'winehq-stable=11.0.*' 'wine-stable=11.0.*' 'wine-stable-amd64=11.0.*' 'wine-stable-i386:i386=11.0.*'
        /opt/wine-stable/bin/wine --version
        ;;
    fedora-44)
        command -v curl >/dev/null || dnf install -y curl
        if [[ ! -f /etc/yum.repos.d/winehq.repo ]]; then
            curl -fsSL --proto '=https' --proto-redir '=https' https://dl.winehq.org/wine-builds/fedora/44/winehq.repo -o /etc/yum.repos.d/winehq.repo
        fi
        packages=('winehq-stable-11.0*' dnf5-plugins)
        ((tools == 0)) || packages+=('wine-stable-devel-11.0*')
        dnf install -y --setopt=install_weak_deps=False "${packages[@]}"
        dnf versionlock add winehq-stable wine-stable
        /opt/wine-stable/bin/wine --version
        ;;
    arch)
        python3 "$repo_dir/scripts/uuway_wine_archive.py" --destination /opt/uuway-wine11
        ;;
    *) echo "Unknown Wine target: $target" >&2; exit 2 ;;
esac
