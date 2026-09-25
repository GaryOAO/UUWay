#!/usr/bin/env bash
# Ubuntu 24.04 x86_64 local backport. No sudo, package installation or service restart.
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
work="$repo_dir/build/portal-recovery"
manifest="$repo_dir/config/portal-recovery-dependencies.json"
mkdir -p "$work"
cd "$work"
archive=xdg-desktop-portal-gnome-46.2.tar.xz
if [[ ! -f "$archive" ]]; then
    curl --fail --location --retry 2 --max-time 60 \
        "https://download.gnome.org/sources/xdg-desktop-portal-gnome/46/$archive" -o "$archive"
fi
printf '%s  %s\n' "$(jq -r '.source_sha256' "$manifest")" "$archive" | sha256sum -c -
while IFS=$'\t' read -r package version arch digest; do
    deb="${package}_${version}_${arch}.deb"
    if [[ ! -f "$deb" ]]; then apt-get download "$package=$version"; fi
    printf '%s  %s\n' "$digest" "$deb" | sha256sum -c -
done < <(jq -r '.packages[] | @tsv' "$manifest")

# Always use a fresh build tree; never overwrite a developer's patched source.
build_dir=$(mktemp -d "$work/rebuild.XXXXXX")
headers="$build_dir/sysroot"
mkdir -p "$headers"
while IFS=$'\t' read -r package version arch digest; do
    dpkg-deb -x "${package}_${version}_${arch}.deb" "$headers"
done < <(jq -r '.packages[] | @tsv' "$manifest")
while IFS= read -r pc; do
    sed -i -e "s|^prefix=/usr$|prefix=$headers/usr|" \
           -e "s|=/usr/|=$headers/usr/|" -e "s|-I/usr/|-I$headers/usr/|" "$pc"
done < <(rg --files "$headers" | rg '\.pc$')
for library in "$headers"/usr/lib/x86_64-linux-gnu/*.so; do
    target=$(readlink "$library")
    if [[ -n "$target" && -e "/usr/lib/x86_64-linux-gnu/$target" ]]; then
        ln -sfn "/usr/lib/x86_64-linux-gnu/$target" "$library"
    fi
done
tar -xJf "$archive" -C "$build_dir"
source_dir="$build_dir/xdg-desktop-portal-gnome-46.2"
patch --batch --fuzz=0 -d "$source_dir" -p1 < "$repo_dir/patches/xdg-desktop-portal-gnome-46.2-session-lifetime.patch"
export PKG_CONFIG_PATH="$headers/usr/lib/x86_64-linux-gnu/pkgconfig:$headers/usr/share/pkgconfig"
meson setup "$build_dir/compiled" "$source_dir" --prefix=/usr \
    --buildtype=debugoptimized --wrap-mode=nofallback -Dsystemd=disabled
ninja -C "$build_dir/compiled" -j 4
binary="$build_dir/compiled/src/xdg-desktop-portal-gnome"
"$headers/usr/bin/patchelf" --remove-rpath "$binary"
mkdir -p "$work/artifacts"
install -m 0755 "$binary" "$work/artifacts/xdg-desktop-portal-gnome-46.2-lifetime"
sha256sum "$work/artifacts/xdg-desktop-portal-gnome-46.2-lifetime"
UURB_PIPEWIRE_HEADERS="$headers/usr/include" bash "$repo_dir/scripts/build-pipewire-native-probe.sh"
printf 'Built only; no services changed. Retained source/build: %s\n' "$build_dir"
