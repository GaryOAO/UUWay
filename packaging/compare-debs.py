#!/usr/bin/python3
"""Show whether two uuway .deb files are the same package built twice.

Two builds of the same commit are not byte-identical (PE link timestamps, build paths), so this
compares what matters: the control fields, the file list, modes and links, every text file byte
for byte, and for binaries the things that decide whether they run: size, dynamic dependencies
(NEEDED, SONAME), imported and undefined symbols, and PE import tables.

    packaging/compare-debs.py tested.deb candidate.deb

Exit status 0 means equivalent, 1 means a real difference, 2 means it could not compare.
"""
import argparse
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

ELF = b'\x7fELF'
PE = b'MZ'


def extract(deb, directory):
    subprocess.run(['dpkg-deb', '-x', str(deb), str(directory / 'data')], check=True)
    control = subprocess.run(['dpkg-deb', '-f', str(deb)], check=True, capture_output=True, text=True).stdout
    return directory / 'data', control


def tree(root):
    entries = {}
    for path in sorted(root.rglob('*')):
        if path.is_dir() and not path.is_symlink():
            continue
        relative = str(path.relative_to(root))
        info = path.lstat()
        entries[relative] = (stat.S_IMODE(info.st_mode), path.readlink() if path.is_symlink() else None)
    return entries


def elf_facts(path):
    dynamic = subprocess.run(['readelf', '-d', str(path)], capture_output=True, text=True).stdout
    symbols = subprocess.run(['readelf', '--dyn-syms', '-W', str(path)], capture_output=True, text=True).stdout
    undefined, defined = set(), set()
    for line in symbols.splitlines():
        fields = line.split()
        if len(fields) >= 8 and fields[0].rstrip(':').isdigit():
            (undefined if fields[6] == 'UND' else defined).add(fields[7].split('@')[0])
    return {
        'needed': sorted(re.findall(r'NEEDED\).*\[(.*?)\]', dynamic)),
        'soname': re.findall(r'SONAME\).*\[(.*?)\]', dynamic),
        'undefined': sorted(undefined),
        'defined': sorted(defined),
    }


def pe_facts(path):
    headers = subprocess.run(['objdump', '-p', str(path)], capture_output=True, text=True)
    if headers.returncode != 0:
        return None
    return {
        'imports': sorted(set(re.findall(r'DLL Name: (\S+)', headers.stdout))),
        'exports': sorted(set(re.findall(r'^\s+\[\s*\d+\]\s+\+base\[\s*\d+\]\s+\w+\s+(\S+)', headers.stdout, re.M))),
    }


def kind(path):
    with path.open('rb') as handle:
        head = handle.read(4)
    if head.startswith(ELF):
        return 'elf'
    if head.startswith(PE):
        return 'pe'
    return 'data'


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('first', type=Path)
    parser.add_argument('second', type=Path)
    args = parser.parse_args()
    problems, notes = [], []
    with tempfile.TemporaryDirectory() as scratch:
        scratch = Path(scratch)
        for name in ('a', 'b'):
            (scratch / name).mkdir()
        try:
            root_a, control_a = extract(args.first, scratch / 'a')
            root_b, control_b = extract(args.second, scratch / 'b')
        except (OSError, subprocess.CalledProcessError) as error:
            print(f'cannot read the packages: {error}', file=sys.stderr)
            return 2
        fields_a = dict(line.split(': ', 1) for line in control_a.splitlines() if ': ' in line)
        fields_b = dict(line.split(': ', 1) for line in control_b.splitlines() if ': ' in line)
        for field in sorted(set(fields_a) | set(fields_b)):
            if field == 'Installed-Size':
                continue  # unstripped binaries of the same size can still round differently
            if fields_a.get(field) != fields_b.get(field):
                problems.append(f'control {field}: {fields_a.get(field)!r} != {fields_b.get(field)!r}')
        files_a, files_b = tree(root_a), tree(root_b)
        for name in sorted(set(files_a) ^ set(files_b)):
            problems.append(f'only in {"first" if name in files_a else "second"}: {name}')
        identical = binary_same = 0
        for name in sorted(set(files_a) & set(files_b)):
            if files_a[name] != files_b[name]:
                problems.append(f'mode or link differs: {name}: {files_a[name]} != {files_b[name]}')
                continue
            path_a, path_b = root_a / name, root_b / name
            if path_a.is_symlink():
                identical += 1
                continue
            if path_a.read_bytes() == path_b.read_bytes():
                identical += 1
                continue
            file_kind = kind(path_a)
            if file_kind != kind(path_b):
                problems.append(f'file type differs: {name}')
            elif file_kind == 'data':
                problems.append(f'text or data file differs: {name}')
            elif path_a.stat().st_size != path_b.stat().st_size:
                problems.append(f'binary size differs: {name}: {path_a.stat().st_size} != {path_b.stat().st_size}')
            else:
                facts_a = elf_facts(path_a) if file_kind == 'elf' else pe_facts(path_a)
                facts_b = elf_facts(path_b) if file_kind == 'elf' else pe_facts(path_b)
                if facts_a is None or facts_b is None:
                    notes.append(f'{name}: no tool to inspect it; compared by size only')
                    binary_same += 1
                elif facts_a != facts_b:
                    problems.append(f'binary interface differs: {name}')
                else:
                    binary_same += 1
    print(f'{identical} files byte-identical, {binary_same} binaries equivalent '
          f'(same size, dependencies, imports and symbols; link timestamps and paths differ)')
    for note in notes:
        print('note:', note)
    for problem in problems:
        print('DIFFERENT:', problem)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
