#!/usr/bin/env bash
# Private Wine 11.0; new WoW64 needs no 32-bit host libraries or system Wine changes.
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
work="$repo_dir/build/multidistro"
archive="$work/downloads/wine-11.0.tar.xz"
sha=c07a6857933c1fc60dff5448d79f39c92481c1e9db5aa628db9d0358446e0701
mkdir -p "$work/downloads" "$work/wine-build"
if [[ ! -f "$archive" ]]; then
    curl -fsSL --proto '=https' --proto-redir '=https' --retry 3 --max-time 300 \
        https://dl.winehq.org/wine/source/11.0/wine-11.0.tar.xz -o "$archive.part"
    printf '%s  %s\n' "$sha" "$archive.part" | sha256sum --strict -c -
    mv "$archive.part" "$archive"
fi
printf '%s  %s\n' "$sha" "$archive" | sha256sum --strict -c -
[[ -d "$work/wine-11.0" ]] || tar -xJf "$archive" -C "$work"
cd "$work/wine-build"
if [[ ! -f Makefile ]] || ! ./config.status --config | grep -q -- --disable-tests; then
    ../wine-11.0/configure --prefix=/opt/uuway-wine11 --libdir=/opt/uuway-wine11/lib \
        --enable-archs=i386,x86_64 --with-mingw --disable-tests --without-oss --without-cups --without-pcap \
        --without-sane --without-gphoto --without-opencl --without-gssapi \
        > configure.log 2>&1 || { tail -60 configure.log >&2; exit 1; }
fi
make -j"${UUWAY_BUILD_JOBS:-8}" > build.log 2>&1 || { tail -80 build.log >&2; exit 1; }
make install > install.log 2>&1 || { tail -60 install.log >&2; exit 1; }
[[ "$(/opt/uuway-wine11/bin/wine --version)" == wine-11.0 ]]
install -Dm644 "$work/wine-11.0/COPYING.LIB" /opt/uuway-wine11/share/doc/wine/COPYING.LIB
install -Dm644 "$work/wine-11.0/LICENSE" /opt/uuway-wine11/share/doc/wine/LICENSE
install -Dm644 "$work/wine-11.0/AUTHORS" /opt/uuway-wine11/share/doc/wine/AUTHORS
printf 'Private Wine 11.0 installed in /opt/uuway-wine11\n'
