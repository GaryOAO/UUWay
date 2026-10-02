#!/usr/bin/env bash
set -Eeuo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# The console uses the same Python + GTK stack as the native bridge.  Keep this
# historical script name so existing one-step installs keep working, but no
# Rust toolchain or GTK development headers are needed for the console.
/usr/bin/python3 -m py_compile "$repo_dir/scripts/uuway_console.py"
chmod 0755 "$repo_dir/scripts/uuway_console.py"
echo "UUWay 控制台已通过 Python/GTK 语法检查"
