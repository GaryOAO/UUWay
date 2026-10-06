#!/usr/bin/env bash
# Build the uuway .deb in a throwaway Ubuntu 24.04 docker container.
#
#   packaging/build-deb.sh [--version 1.0.0] [--output-dir DIR] [--keep]
#
# The source is the git-tracked and untracked-but-not-ignored files of this checkout, so
# local edits are included; build/ and the host toolchain are never used. --keep leaves
# the container running afterwards for inspection.
set -Eeuo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="$repo_dir/build/deb"
version=""
keep=0
while (($#)); do
    case "$1" in
        --version) version="${2:?--version needs a value}"; shift 2 ;;
        --output-dir) output_dir="${2:?--output-dir needs a path}"; shift 2 ;;
        --keep) keep=1; shift ;;
        -h|--help) sed -n '2,8p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done
command -v docker >/dev/null || { printf 'docker is required\n' >&2; exit 1; }

if [[ -z "$version" ]]; then
    if tag="$(git -C "$repo_dir" describe --tags --exact-match 2>/dev/null)"; then
        version="${tag#v}"
    else
        version="0.0.0~git$(git -C "$repo_dir" rev-parse --short HEAD)"
    fi
fi

name="uuway-build-$$"
cleanup() { ((keep)) || docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

printf 'Building uuway %s in container %s\n' "$version" "$name"
docker run -d --name "$name" ubuntu:24.04 sleep infinity >/dev/null
git -C "$repo_dir" ls-files -z --cached --others --exclude-standard --deduplicate |
    tar -C "$repo_dir" --null -T - -cf - |
    docker exec -i "$name" sh -c 'mkdir -p /work/UUWay && tar xf - -C /work/UUWay'
docker exec -e "SOURCE_DATE_EPOCH=$(git -C "$repo_dir" log -1 --format=%ct)" -w /work/UUWay "$name" \
    packaging/container-build.sh --version "$version" --output-dir /work/out
mkdir -p "$output_dir"
docker cp "$name:/work/out/." "$output_dir/"
printf '\nBuilt: %s\n' "$output_dir/uuway_${version}_amd64.deb"
