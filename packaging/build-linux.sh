#!/usr/bin/env bash
# Build distribution-native UUWay using official prebuilt Wine 11.0.
# Usage: packaging/build-linux.sh --target debian-13|fedora|arch --version X.Y.Z
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
case "$target" in fedora-44|fedora-45) target=fedora ;; esac
output_dir="${output_dir:-$repo_dir/build/linux/$target}"
image="$(python3 - "$repo_dir" "$target" <<'PY'
import json, pathlib, sys
targets = json.loads((pathlib.Path(sys.argv[1]) / 'packaging/linux-targets.json').read_text())
if sys.argv[2] not in targets:
    raise SystemExit('Unknown target. Choose: ' + ', '.join(targets))
print(targets[sys.argv[2]]['image'])
PY
)"
name="uuway-$target-$$"
containers=()
start_builder() {
    local container="$1" build_image="$2"
    docker run -d --name "$container" --cpus "${UUWAY_BUILD_CPUS:-8}" "$build_image" sleep infinity >/dev/null
    containers+=("$container")
    git -C "$repo_dir" ls-files -z --cached --others --exclude-standard --deduplicate |
        tar -C "$repo_dir" --null -T - -cf - |
        docker exec -i "$container" sh -c 'mkdir -p /work/UUWay && tar xf - -C /work/UUWay'
}
cleanup() {
    if ((!keep && ${#containers[@]})); then docker rm -f "${containers[@]}" >/dev/null || true; fi
}
trap cleanup EXIT
start_builder "$name" "$image"
if [[ "$target" == fedora ]]; then
    compat="$name-compat"
    start_builder "$compat" fedora:45
    docker exec -w /work/UUWay "$compat" bash packaging/container-fedora-compat.sh /work/compat
    compat_dir=/work/UUWay/build/compat/fedora45
    docker exec "$name" mkdir -p "$compat_dir"
    docker exec -w /work/compat "$compat" tar cf - . | docker exec -i -w "$compat_dir" "$name" tar xf -
    if ((!keep)); then
        docker rm -f "$compat" >/dev/null
        containers=("$name")
    fi
fi
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
cleanup
containers=()
# Resolve runtime dependencies in a pristine image for each supported release.
output_dir="$(cd "$output_dir" && pwd)"
install_check() {
    docker run --rm -i --mount "type=bind,source=$output_dir,target=/packages,readonly" \
        --mount "type=bind,source=$repo_dir,target=/source,readonly" \
        "$1" bash -s -- "$2" /packages < "$repo_dir/packaging/container-linux-test.sh"
}
if [[ "$target" == fedora ]]; then
    install_check "$image" fedora-44
    install_check fedora:45 fedora-45
else
    install_check "$image" "$target"
fi
printf '\nBuilt and install-checked %s packages in %s\n' "$target" "$output_dir"
