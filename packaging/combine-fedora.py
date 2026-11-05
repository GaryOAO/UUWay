#!/usr/bin/python3
"""Make one Fedora RPM from CI's Fedora 44 baseline and three Fedora 45 AMD files."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def extract(package, destination):
    destination.mkdir()
    with subprocess.Popen(['rpm2cpio', str(package)], stdout=subprocess.PIPE) as producer:
        subprocess.run(['cpio', '-idmu', '--quiet', '--no-absolute-filenames'], cwd=destination,
                       stdin=producer.stdout, check=True)
        producer.stdout.close()
        if producer.wait():
            raise RuntimeError('rpm2cpio failed: ' + str(package))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fedora44', type=Path, required=True)
    parser.add_argument('--fedora45', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    stage = module('stage_linux', ROOT / 'packaging/stage-linux.py')
    checks = module('update_checks', ROOT / 'scripts/uuway_update_apply.py')
    platform = module('platform_files', ROOT / 'scripts/uuway_platform.py')
    for package, target in ((args.fedora44, 'fedora-44'), (args.fedora45, 'fedora-45')):
        control = checks.package_control(package, target)
        if (control.get('Package'), control.get('Version'), control.get('Architecture')) != ('uuway', args.version, 'x86_64'):
            raise ValueError('Mismatched input package: ' + str(package))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='uuway-fedora-') as temporary:
        work = Path(temporary)
        primary, secondary = work / 'fedora44', work / 'fedora45'
        extract(args.fedora44.resolve(), primary)
        extract(args.fedora45.resolve(), secondary)
        lib = primary / 'usr/lib/uuway'
        compat = lib / 'build/compat/fedora45'
        compat.mkdir(parents=True)
        for name in sorted(platform.FEDORA_AMD_VARIANTS):
            shutil.copy2(secondary / 'usr/lib/uuway/build/backends/amd/native-presenter' / name, compat / name)
        info = lib / 'package-info.json'
        metadata = json.loads(info.read_text())
        metadata.update(target='fedora', fedora_versions=['44', '45'])
        info.write_text(json.dumps(metadata, indent=2) + '\n')
        stage.build_rpm(primary, work, args.output_dir, 'uuway', args.version,
                        stage.RUNTIME['rpm'], 'fedora', runtime=True)
    package = args.output_dir / checks.package_name(args.version, 'fedora')
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    (args.output_dir / 'SHA256SUMS').write_text(f'{digest}  {package.name}\n')


if __name__ == '__main__':
    main()
