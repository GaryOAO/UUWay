#!/usr/bin/env bash
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cargo build --locked --release --manifest-path "$repo_dir/native/settings/Cargo.toml"
