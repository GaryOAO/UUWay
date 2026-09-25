#!/usr/bin/env bash
# Private Ubuntu 24.04 Mutter build. Never installs packages or restarts services.
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
rg_path=$(command -v rg || true)
export PATH=/usr/bin:/bin
work="$repo_dir/build/mutter-build"
review="$repo_dir/build/mutter-review"
manifest="$repo_dir/config/mutter-build-dependencies.json"
mkdir -p "$work/downloads"
for tool in jq curl tar patch meson ninja pkg-config; do command -v "$tool" >/dev/null; done
printf '%s  %s\n' "$(jq -r .source_sha256 "$repo_dir/config/mutter-capture-review.json")" "$review/mutter-46.2.tar.xz" | sha256sum -c -
printf '%s  %s\n' "$(jq -r .debian_patches_sha256 "$repo_dir/config/mutter-capture-review.json")" "$review/mutter_46.2-1ubuntu0.24.04.9.debian.tar.xz" | sha256sum -c -
while IFS=$'\t' read -r package version arch digest url; do
    archive="$work/downloads/${package}_${version}_${arch}.deb"
    if [[ ! -f "$archive" ]]; then
        curl --fail --location --proto '=https' --proto-redir '=https' --retry 2 --max-time 90 "$url" -o "$archive.part"
        printf '%s  %s\n' "$digest" "$archive.part" | sha256sum -c -
        mv -- "$archive.part" "$archive"
    fi
    printf '%s  %s\n' "$digest" "$archive" | sha256sum -c -
done < <(jq -r '.packages[] | @tsv' "$manifest")
build_dir=$(mktemp -d "$work/rebuild.XXXXXX")
headers="$build_dir/sysroot"
mkdir -p "$headers"
while IFS=$'\t' read -r package version arch digest; do
    archive="$repo_dir/build/portal-recovery/${package}_${version}_${arch}.deb"
    printf '%s  %s\n' "$digest" "$archive" | sha256sum -c -
    dpkg-deb -x "$archive" "$headers"
done < <(jq -r '.packages[] | @tsv' "$repo_dir/config/portal-recovery-dependencies.json")
while IFS=$'\t' read -r package version arch digest url; do
    dpkg-deb -x "$work/downloads/${package}_${version}_${arch}.deb" "$headers"
done < <(jq -r '.packages[] | @tsv' "$manifest")
# Only rewrite extracted .pc metadata; link against installed runtime libraries.
if [[ -n "$rg_path" ]]; then
    pc_files=$("$rg_path" --files --hidden --no-ignore "$headers" -g '*.pc')
else
    pc_files=$(find "$headers" -name '*.pc' -type f)
fi
while IFS= read -r pc; do
    sed -i -e "s|^prefix=/usr$|prefix=$headers/usr|" -e "s|=/usr/|=$headers/usr/|" \
        -e "s|-I/usr/|-I$headers/usr/|" "$pc"
done <<< "$pc_files"
for library in "$headers"/usr/lib/x86_64-linux-gnu/*.so; do
    target=$(readlink "$library" || true)
    if [[ -n "$target" && -e "/usr/lib/x86_64-linux-gnu/$(basename "$target")" ]]; then
        ln -sfn "/usr/lib/x86_64-linux-gnu/$(basename "$target")" "$library"
    fi
done
tar -xJf "$review/mutter-46.2.tar.xz" -C "$build_dir"
source_dir="$build_dir/mutter-46.2"
tar -xJf "$review/mutter_46.2-1ubuntu0.24.04.9.debian.tar.xz" -C "$source_dir"
while IFS= read -r entry; do
    [[ -z "$entry" || "$entry" == \#* ]] && continue
    [[ "$entry" != *' '* && "$entry" != /* && "$entry" != *'..'* ]] || { printf 'Unexpected patch entry\n' >&2; exit 1; }
    patch --batch --fuzz=0 -d "$source_dir" -p1 -i "$source_dir/debian/patches/$entry"
done < "$source_dir/debian/patches/series"
patch --batch --fuzz=0 -d "$source_dir" -p1 -i "$repo_dir/patches/mutter-46.2-capture-jitter-candidate.patch"
if [[ "${UURB_MUTTER_VIRTUAL_TIMESTAMPS:-0}" == 1 ]]; then
    patch --batch --fuzz=0 -d "$source_dir" -p1 -i "$repo_dir/patches/mutter-46.2-virtual-presentation-candidate.patch"
fi
export PKG_CONFIG_PATH="$headers/usr/lib/x86_64-linux-gnu/pkgconfig:$headers/usr/share/pkgconfig"
export CFLAGS='-O2 -g -fstack-protector-strong -Wformat -Werror=format-security'
export LDFLAGS='-Wl,-z,relro,-z,now'
meson setup "$build_dir/compiled" "$source_dir" --prefix=/usr --libdir=lib/x86_64-linux-gnu \
    --buildtype=debugoptimized --wrap-mode=nofallback -Dlibdisplay_info=disabled \
    -Degl_device=true -Dwayland_eglstream=true -Dremote_desktop=true \
    -Dtests=false -Dintrospection=false -Ddocs=false
ninja -C "$build_dir/compiled" -j 4
printf 'Private build complete; nothing installed. Retained build: %s\n' "$build_dir"
