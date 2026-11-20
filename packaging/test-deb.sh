#!/usr/bin/env bash
# Install a built uuway .deb into a throwaway, clean Ubuntu docker container and run
# packaging/container-test.sh against it.
#
#   packaging/test-deb.sh [--target ubuntu-24.04|ubuntu-26.04] path/to/uuway_X_amd64.deb
#                        [--keep] [--no-lint]
set -Eeuo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
deb=""
target="ubuntu-24.04"
keep=0
lint=1
while (($#)); do
    case "$1" in
        --target) target="${2:?--target needs a value}"; shift 2 ;;
        --keep) keep=1; shift ;;
        --no-lint) lint=0; shift ;;
        -h|--help) sed -n '2,7p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) deb="$1"; shift ;;
    esac
done
[[ -f "$deb" ]] || { printf 'usage: %s path/to/uuway.deb\n' "$0" >&2; exit 2; }
command -v docker >/dev/null || { printf 'docker is required\n' >&2; exit 1; }
case "$target" in
    ubuntu-24.04) image=ubuntu:24.04 ;;
    ubuntu-26.04) image=ubuntu:26.04 ;;
    *) printf 'unsupported Ubuntu target: %s\n' "$target" >&2; exit 2 ;;
esac

name="uuway-test-$$"
cleanup() { ((keep)) || docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "$name" "$image" sleep infinity >/dev/null
docker cp "$deb" "$name:/tmp/uuway.deb"
docker cp "$repo_dir/packaging/container-test.sh" "$name:/tmp/container-test.sh"
docker cp "$repo_dir/packaging/update-e2e.py" "$name:/tmp/update-e2e.py"
docker exec -e "UUWAY_LINT=$lint" "$name" bash /tmp/container-test.sh /tmp/uuway.deb
