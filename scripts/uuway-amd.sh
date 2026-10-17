#!/usr/bin/env bash
set -Eeuo pipefail
root="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/.." && pwd)"
if [[ ! -f "$root/build/encoder-backend" ]] || [[ "$(cat "$root/build/encoder-backend")" != amd-vaapi ]]; then
    printf 'AMD runtime is not built. Run: bash %q/scripts/build-amd-runtime.sh\n' "$root" >&2
    exit 1
fi
if [[ "${1:-}" == update ]]; then
    printf 'This is a local AMD fork. Upstream update would replace its backend. Rebuild this checkout instead.\n' >&2
    exit 1
fi
if (($# == 0)); then set -- doctor; fi
exec /usr/bin/python3 "$root/scripts/uuway_cli.py" "$@"
