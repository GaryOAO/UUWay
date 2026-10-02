#!/usr/bin/python3
"""Apply the explicit Wine-to-Linux path mapping phase.

This command is intentionally separate from ``uu-native-service.py``.  It is
run while the UU bridge is stopped, so directory moves and the desktop
registry update never race Wine startup or a live bridge process.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
import importlib.util

spec = importlib.util.spec_from_file_location('uu_native_service', ROOT / 'scripts/uu-native-service.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


def all_applied(value):
    if isinstance(value, dict):
        return all(all_applied(item) for item in value.values())
    return value is True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, required=True)
    parser.add_argument('--config-directory', type=Path,
                        default=Path.home() / '.config/uurb')
    args = parser.parse_args(argv)
    mappings = service.LinuxWineMappings(args.prefix, args.config_directory)
    result = mappings.apply()
    result['desktop_image'] = service.DesktopImageMapping(
        args.prefix, args.config_directory).apply()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if all_applied(result) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'UUWay Linux path mapping failed: {error}', file=sys.stderr)
        raise SystemExit(1)
