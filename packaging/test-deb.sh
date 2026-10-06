#!/usr/bin/env bash
# Install a built uuway .deb into a throwaway, clean Ubuntu 24.04 docker container and run
# packaging/container-test.sh against it.
#
#   packaging/test-deb.sh path/to/uuway_X_amd64.deb [--keep] [--no-lint]
set -Eeuo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
deb=""
keep=0
lint=1
for argument in "$@"; do
    case "$argument" in
        --keep) keep=1 ;;
        --no-lint) lint=0 ;;
        -h|--help) sed -n '2,6p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) deb="$argument" ;;
    esac
done
[[ -f "$deb" ]] || { printf 'usage: %s path/to/uuway.deb\n' "$0" >&2; exit 2; }
command -v docker >/dev/null || { printf 'docker is required\n' >&2; exit 1; }

name="uuway-test-$$"
cleanup() { ((keep)) || docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "$name" ubuntu:24.04 sleep infinity >/dev/null
docker cp "$deb" "$name:/tmp/uuway.deb"
docker cp "$repo_dir/packaging/container-test.sh" "$name:/tmp/container-test.sh"
docker exec -e "UUWAY_LINT=$lint" "$name" bash /tmp/container-test.sh /tmp/uuway.deb
