#!/usr/bin/env python3
"""Windows function-table calls to native NVENC queries, in a disposable prefix.
No UU account, desktop capture, RDP service or production DLL is used.
"""
import json
import argparse
import os
from pathlib import Path
import signal
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
STAGE = ROOT / 'build/native-presenter'


def validate(entries):
    if len(entries) != 8:
        raise RuntimeError('Expected four H264/HEVC query-session cycles')
    for cycle in range(4):
        pair = entries[cycle * 2:cycle * 2 + 2]
        if {p['codec'] for p in pair} != {'h264', 'hevc'} or any(p['cycle'] != cycle for p in pair):
            raise RuntimeError('Missing or out-of-order capability query')
        for row in pair:
            if not row['formats'] or row['max_width'] < 3840 or not row['profiles'] or not row['presets'] or row.get('preset_config_ex') is not True:
                raise RuntimeError('Incomplete native capability result')
            first = next(p for p in entries[:2] if p['codec'] == row['codec'])
            if any(row[k] != first[k] for k in ['formats', 'max_width', 'profiles', 'presets']):
                raise RuntimeError('Capabilities changed between identical sessions')


def run(pe_client=False, named_load=False, missing_backend=False):
    if missing_backend and not (pe_client and named_load):
        raise ValueError('Missing-backend test requires the PE client and named loader')
    executable = STAGE / ('uu-nvenc-pe-query-probe.exe' if pe_client else 'uu-nvenc-query-probe.exe')
    if (not executable.is_file() or not (STAGE / 'uurb-nvenc-query.dll.so').is_file() or
        (named_load and not (STAGE / 'uurb-nvenc-query-loader.dll').is_file())):
        raise RuntimeError('Build scripts/build-nvenc-query-probe.sh first')
    if pe_client:
        with executable.open('rb') as binary:
            if binary.read(2) != b'MZ':
                raise RuntimeError('PE query client is not a Windows executable')
    with tempfile.TemporaryDirectory(prefix='uurb-nvenc-query-') as temporary:
        directory = Path(temporary)
        if named_load and pe_client:
            app = directory / 'app'
            app.mkdir()
            for filename in [executable.name, 'uurb-nvenc-query-loader.dll', 'd3d11.dll', 'dxgi.dll']:
                shutil.copy2(STAGE / filename, app / filename)
            if not missing_backend:
                shutil.copy2(STAGE / 'uurb-nvenc-query.dll.so', app / 'uurb-nvenc-query.dll.so')
            executable = app / executable.name
        env = dict(os.environ, WINEPREFIX=str(directory / 'wine'), WINELOADER='/opt/wine-stable/bin/wine',
            WINEDEBUG='-all', WINEDLLOVERRIDES='mscoree,mshtml=;d3d11,dxgi=n;uurb-nvenc-query-loader=n',
            WINEDLLPATH=str(STAGE), DXVK_CONFIG_FILE=str(ROOT / 'config/dxvk-native.conf'),
            DXVK_LOG_PATH=str(directory))
        for name in ['DXVK_CONFIG', 'DXVK_FILTER_DEVICE_NAME', 'VK_INSTANCE_LAYERS']:
            env.pop(name, None)
        try:
            command = ['xvfb-run', '-a', '--server-args=-screen 0 640x360x24 -nolisten tcp',
                       *(['/opt/wine-stable/bin/wine'] if pe_client else []),
                       str(executable), 'uurb-nvenc-query-loader.dll' if named_load else str(STAGE / 'uurb-nvenc-query.dll.so')]
            if missing_backend:
                command.append('--missing-backend')
            with (directory / 'probe.log').open('w') as log:
                process = subprocess.Popen(command, cwd=directory, env=env, stdout=log,
                                           stderr=log, start_new_session=True)
                try:
                    result = process.wait(timeout=60)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=5)
            log = (directory / 'probe.log').read_text()
            marker = 'ABI missing backend checks passed;' if missing_backend else 'ABI query checks passed;'
            if result or marker not in log:
                relevant = '\n'.join(line for line in log.splitlines()
                    if any(key in line for key in ['uurb-nvenc', 'ABI check', 'LoadLibrary failed', 'err:']))
                raise RuntimeError(f'Windows NVENC ABI query probe failed ({result}):\n{relevant[-6000:]}')
            entries = [json.loads(line[6:]) for line in log.splitlines() if line.startswith('QUERY ')]
            if missing_backend:
                if entries:
                    raise RuntimeError('Missing backend unexpectedly advertised native capabilities')
                return dict(query_only=True, windows_pe_client=True, missing_backend_rejected=True,
                            native_driver_called=False, uu_session_tested=False)
            validate(entries)
            return dict(query_only=True, native_driver_called=True, encoding_implemented=False,
                        uu_session_tested=False, windows_pe_client=pe_client,
                        dll_load_mode='adjacent-pe-loader-basename' if named_load else 'absolute-elf-path', cycles=4, query_results=entries)
        finally:
            subprocess.run(['/opt/wine-stable/bin/wineserver', '-k'], env=env,
                           capture_output=True, timeout=10)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pe-client', action='store_true', help='Use a real MinGW Windows executable instead of Winelib')
    parser.add_argument('--named-load', action='store_true', help='Load an adjacent PE loader DLL by basename')
    parser.add_argument('--missing-backend', action='store_true', help='Verify absent adjacent backend fails without modifying caller output')
    options = parser.parse_args()
    if options.missing_backend and not (options.pe_client and options.named_load):
        parser.error('--missing-backend requires --pe-client --named-load')
    print(json.dumps(run(pe_client=options.pe_client, named_load=options.named_load,
                         missing_backend=options.missing_backend), indent=2))
