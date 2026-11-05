#!/usr/bin/env bash
# Build distribution-native UUWay and its private Wine 11.0 in a clean container.
# Usage: packaging/build-linux.sh --target debian-13|fedora-44|arch --version 1.4.3
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
target='' version='' output_dir='' keep=0
while (($#)); do
    case "$1" in
        --target) target="${2:?}"; shift 2 ;;
        --version) version="${2:?}"; shift 2 ;;
        --output-dir) output_dir="${2:?}"; shift 2 ;;
        --keep) keep=1; shift ;;
        -h|--help) sed -n '2,3p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
done
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.+~][A-Za-z0-9.]+)?$ ]] || { echo '--version must be X.Y.Z (optionally +/~ suffix)' >&2; exit 2; }
image="$(python3 - "$repo_dir" "$target" <<'PY'
import json, pathlib, sys
targets = json.loads((pathlib.Path(sys.argv[1]) / 'packaging/linux-targets.json').read_text())
if sys.argv[2] not in targets:
    raise SystemExit('Unknown target. Choose: ' + ', '.join(targets))
print(targets[sys.argv[2]]['image'])
PY
)"
output_dir="${output_dir:-$repo_dir/build/linux/$target}"
name="uuway-$target-$$"
cleanup() { ((keep)) || docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --name "$name" --cpus "${UUWAY_BUILD_CPUS:-8}" "$image" sleep infinity >/dev/null
git -C "$repo_dir" ls-files -z --cached --others --exclude-standard --deduplicate |
    tar -C "$repo_dir" --null -T - -cf - |
    docker exec -i "$name" sh -c 'mkdir -p /work/UUWay && tar xf - -C /work/UUWay'
# Cache downloads only; compiled host artifacts never enter a distribution build.
if [[ -d "$repo_dir/build/multidistro/downloads" ]]; then
    docker exec "$name" mkdir -p /work/UUWay/build/multidistro
    docker cp "$repo_dir/build/multidistro/downloads" "$name:/work/UUWay/build/multidistro/"
fi
docker exec -e "SOURCE_DATE_EPOCH=$(git -C "$repo_dir" log -1 --format=%ct)" \
    -e "UUWAY_BUILD_JOBS=${UUWAY_BUILD_JOBS:-8}" -w /work/UUWay "$name" \
    bash packaging/container-linux-build.sh "$target" "$version" /work/out
mkdir -p "$output_dir"
docker cp "$name:/work/out/." "$output_dir/"
# Resolve runtime dependencies again in a pristine image, without build packages.
output_dir="$(cd "$output_dir" && pwd)"
docker run --rm -i --mount "type=bind,source=$output_dir,target=/packages,readonly" \
    "$image" bash -s -- "$target" /packages < "$repo_dir/packaging/container-linux-test.sh"
printf '\nBuilt and install-checked %s packages in %s\n' "$target" "$output_dir"
