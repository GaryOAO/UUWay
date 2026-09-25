#!/usr/bin/python3
"""One authorized capture/encode diagnostic with its own visible refresh target.
Never changes resolution, services, permissions or production Wine prefixes.
"""
import argparse
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def validate_fixture_samples(report, samples, source):
    rates = [sample['fixture_draw_fps'] for sample in samples if 'fixture_draw_fps' in sample]
    if not rates:
        raise RuntimeError('No measured fixture refresh interval')
    if source == 'virtual' and not any(sample.get('fixture_virtual_monitor_selected') for sample in samples):
        raise RuntimeError('Fixture never selected the new virtual output')
    if source == 'virtual' and not any(
            [sample.get('fixture_width'), sample.get('fixture_height')] == [report['width'], report['height']]
            for sample in samples):
        raise RuntimeError('Fixture never filled the virtual output; invalid fullscreen measurement')
    return dict(fixture_draw_fps_samples=rates,
                fixture_sizes=[[sample['fixture_width'], sample['fixture_height']] for sample in samples
                               if 'fixture_width' in sample])


def capture_command(command):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        output, errors = process.communicate(timeout=40)
    except subprocess.TimeoutExpired:
        # The portal script owns native/decoder children. Kill only this private
        # process group, not just the parent which would orphan a blocked worker.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=5)
        raise RuntimeError('Capture process group exceeded forty seconds')
    return subprocess.CompletedProcess(command, process.returncode, output, errors)


def run(mode, restore_state, fixture_interval_ms=None, source='monitor'):
    if mode not in ('metadata', 'h264', 'hevc'):
        raise ValueError('Expected metadata, h264 or hevc')
    if source not in ('monitor', 'virtual'):
        raise ValueError('Expected monitor or virtual source')
    if fixture_interval_ms is not None and (type(fixture_interval_ms) is not int or not 1 <= fixture_interval_ms <= 1000):
        raise ValueError('Fixture interval must be an integer from 1 to 1000 ms')
    command = ['/usr/bin/python3', str(ROOT / 'scripts/probe-wayland-portal.py'),
               '--restore-state', str(restore_state), '--source', source]
    command += ['--native-dmabuf'] if mode == 'metadata' else ['--encode', mode, '--check-color-fixture']
    fixture_args = ['--frame-clock'] if fixture_interval_ms is None else ['--interval-ms', str(fixture_interval_ms)]
    if source == 'virtual':
        fixture_args += ['--virtual-output', '--gpu']
    fixture = subprocess.Popen(['/usr/bin/python3', str(ROOT / 'tests/probes/wayland_color_fixture.py'), *fixture_args],
        env=dict(os.environ, GDK_BACKEND='wayland'), stdout=subprocess.PIPE, text=True)
    try:
        # Do not start the eight-second measurement before the window was drawn.
        with selectors.DefaultSelector() as selector:
            selector.register(fixture.stdout, selectors.EVENT_READ)
            ready = json.loads(fixture.stdout.readline()) if selector.select(timeout=5) else {}
            if not ready.get('fixture_ready'):
                raise RuntimeError('Wayland fixture was not ready within five seconds')
        capture = capture_command(command)
        if fixture.poll() is not None:
            raise RuntimeError('Fixture exited during capture/validation; throughput result is invalid')
        if capture.returncode:
            raise RuntimeError('Capture failed: ' + capture.stderr[-4000:])
        report = json.loads(capture.stdout)
    finally:
        # Terminate only the fixture created by this invocation, never the desktop
        # or other capture clients. Its stdout contains counters, not pixels.
        if fixture.poll() is None:
            fixture.terminate()
        try:
            counters, _ = fixture.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            fixture.kill()
            counters, _ = fixture.communicate(timeout=5)
    samples = [json.loads(line) for line in counters.splitlines() if line]
    report.update(validate_fixture_samples(report, samples, source))
    report.update(fixture_lived_through_capture=True,
                  fixture_gpu_renderer=ready.get('fixture_gpu_renderer'),
                  fixture_clock='frame-clock' if fixture_interval_ms is None else 'timer',
                  fixture_interval_ms=fixture_interval_ms)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['metadata', 'h264', 'hevc'], required=True)
    parser.add_argument('--restore-state', type=Path, required=True)
    parser.add_argument('--fixture-interval-ms', type=int, help='Use timer-driven fixture instead of GTK frame clock')
    parser.add_argument('--source', choices=['monitor', 'virtual'], default='monitor')
    options = parser.parse_args()
    print(json.dumps(run(options.mode, options.restore_state, options.fixture_interval_ms, options.source), indent=2))
