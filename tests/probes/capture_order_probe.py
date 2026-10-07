#!/usr/bin/python3
"""Count stale (backward) frames in a real capture of this desktop while the GPU is busy.

A window shows a frame counter as 16 black/white blocks. UUWay's own capture and NVENC path records
the screen, ffmpeg decodes it, and the counters are read back. Frames only ever move forward on
screen, so every frame whose counter is lower than its predecessor's was a stale buffer: the capture
read a buffer before the compositor finished drawing into it.

    /usr/bin/python3 tests/probes/capture_order_probe.py --load burner --burner ./dmabuf_implicit_fence_probe

The first run asks for screen sharing through the portal; its restore token is kept in --state, apart
from the one the UUWay service uses. Needs build/native-presenter/uu-pipewire-native-probe, ffmpeg
and ffplay. Nothing is saved except the summary printed at the end. Keep the pattern window visible
and uncovered while it runs."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
BLOCK, BITS, BAR_Y, BAR_HEIGHT, BORDER = 48, 16, 24, 48, 8
BAR_X = 64
PATTERN_FPS = 60
# From the capture worker's own report: how many queued frames one wake-up had to drain (1 = no backlog,
# 16 = the whole PipeWire queue), and what each frame cost it.
BACKLOG_KEYS = ('frames', 'delivery_fps', 'maximum_batch', 'process_callbacks', 'superseded_frames', 'frame_processing_us_avg',
                'frame_processing_us_max', 'delivery_gap_us_max')


def pattern_graph(width, height):
    border = f'lt(X,{BORDER})+gte(X,W-{BORDER})+lt(Y,{BORDER})+gte(Y,H-{BORDER})'
    in_bar = f'between(Y,{BAR_Y},{BAR_Y + BAR_HEIGHT - 1})*between(X,{BAR_X},{BAR_X + BITS * BLOCK - 1})'
    bit = f'255*bitand(floor(N/pow(2,floor((X-{BAR_X})/{BLOCK}))),1)'
    channel = lambda edge: f"'if({border},{edge},if({in_bar},{bit},128))'"
    return (f'color=c=black:s={width}x{height}:r={PATTERN_FPS},format=rgb24,'
            f'geq=r={channel(255)}:g={channel(0)}:b={channel(255)}')


def decode(args, stream_fd, width, height):
    """Frame counters in decode order; None where the pattern is not visible."""
    # passthrough: one output frame per decoded frame, never duplicated or dropped to a nominal rate
    source = ['ffmpeg', '-v', 'error', '-nostdin', '-f', 'h264', '-i', f'/proc/self/fd/{stream_fd}', '-fps_mode', 'passthrough']
    probe = subprocess.run(source + ['-vf', 'select=eq(n\\,30)', '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                           pass_fds=(stream_fd,), capture_output=True, timeout=60, check=True).stdout
    if len(probe) != width * height * 3:
        raise RuntimeError('Capture shorter than 31 frames; cannot locate the pattern window')
    red, green, blue = probe[0::3], probe[1::3], probe[2::3]
    hits = [i for i in range(width * height) if red[i] > 200 and green[i] < 80 and blue[i] > 200]
    if len(hits) < 500:
        raise RuntimeError('Pattern window not visible in the capture; keep it uncovered on the captured monitor')
    left, top = min(i % width for i in hits), min(i // width for i in hits)
    strip_x, strip_y = left + BAR_X, top + BAR_Y
    frames = subprocess.run(source + ['-vf', f'crop={BITS * BLOCK}:{BAR_HEIGHT}:{strip_x}:{strip_y}',
                                       '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
                            pass_fds=(stream_fd,), capture_output=True, timeout=120, check=True).stdout
    size = BITS * BLOCK * BAR_HEIGHT
    result = []
    for start in range(0, len(frames) - size + 1, size):
        frame = frames[start:start + size]
        samples = [frame[(BAR_HEIGHT // 2) * BITS * BLOCK + i * BLOCK + BLOCK // 2] for i in range(BITS)]
        if any(60 <= value <= 190 for value in samples):
            result.append(None)
        else:
            result.append(sum((value > 128) << i for i, value in enumerate(samples)))
    return result


def analyse(counters):
    valid = [value for value in counters if value is not None]
    if len(valid) < 2:
        return dict(frames=len(counters), readable=len(valid))
    best, backward, below_best, worst = valid[0], 0, 0, 0
    for previous, value in zip(valid, valid[1:]):
        backward += value < previous
        below_best += value < best
        worst = max(worst, best - value)
        best = max(best, value)
    return dict(frames=len(counters), readable=len(valid), repeated=sum(a == b for a, b in zip(valid, valid[1:])),
                backward_steps=backward, frames_older_than_newest_seen=below_best,
                deepest_rewind_frames=worst, deepest_rewind_seconds=round(worst / PATTERN_FPS, 2))


def load_portal_probe():
    spec = importlib.util.spec_from_file_location('portal_probe', ROOT / 'scripts/probe-wayland-portal.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def capture_once(portal, args, results):
    def validate(stream, report, color_fixture=False, fixture_warmup=False):
        stream.seek(0)
        counters = decode(args, stream.fileno(), report['width'], report['height'])
        results.append((counters, {key: report.get(key) for key in BACKLOG_KEYS}))
        return {}
    portal.validate_encoded_capture = validate
    sys.argv = ['probe-wayland-portal.py', '--encode', 'h264', '--restore-state', str(args.state),
                '--cursor-mode', 'hidden']
    with contextlib.redirect_stdout(io.StringIO()):
        portal.main()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--runs', type=int, default=4, help='8-second captures, back to back (default 4, about 32 s)')
    parser.add_argument('--load', choices=['none', 'burner'], default='none',
                        help='"burner" runs the GPU burner for the whole test; "none" leaves load to you (e.g. your OCR job)')
    parser.add_argument('--burner', type=Path, help='compiled tests/probes/dmabuf_implicit_fence_probe')
    parser.add_argument('--burn-iterations', type=int, default=4000, help='shader loop length per draw; short batches keep the capture '
                        'alive while still saturating the GPU (long ones starve it past the probe\'s 20 s limit)')
    parser.add_argument('--burn-draws', type=int, default=2)
    parser.add_argument('--state', type=Path, default=Path.home() / '.local/state/uurb/order-probe.json',
                        help='portal restore token for this probe only')
    parser.add_argument('--pattern-size', default='960x540')
    args = parser.parse_args()
    if not 1 <= args.runs <= 10:
        parser.error('--runs must be 1 to 10')
    if args.load == 'burner' and not (args.burner and args.burner.is_file()):
        parser.error('--load burner needs --burner with the compiled dmabuf_implicit_fence_probe')
    width, height = (int(value) for value in args.pattern_size.split('x'))
    if width < BAR_X + BITS * BLOCK + 2 * BORDER or height < BAR_Y + BAR_HEIGHT + BORDER:
        parser.error('--pattern-size is too small for the counter bar')
    portal = load_portal_probe()
    helpers = []
    try:
        helpers.append(subprocess.Popen(['ffplay', '-v', 'error', '-f', 'lavfi', '-i', pattern_graph(width, height),
                                         '-window_title', 'uurb-order-pattern'], stdin=subprocess.DEVNULL))
        utilisation = subprocess.Popen(['nvidia-smi', '--query-gpu=utilization.gpu', '--format=csv,noheader,nounits', '-l', '1'],
                                       stdout=subprocess.PIPE, text=True)
        helpers.append(utilisation)
        time.sleep(3)
        scratch = []
        if not args.state.exists():
            print('First run: approve the screen-sharing dialog (choose the monitor, tick remember).', file=sys.stderr, flush=True)
            capture_once(portal, args, scratch)  # authorisation only, before the load starts
        if args.load == 'burner':
            helpers.append(subprocess.Popen([str(args.burner), 'burn', str(args.burn_iterations), str(args.burn_draws)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            time.sleep(2)
        runs = []
        begin = time.monotonic()
        for index in range(args.runs):
            print(f'capture {index + 1}/{args.runs} ...', file=sys.stderr, flush=True)
            capture_once(portal, args, runs)
        elapsed = time.monotonic() - begin
    finally:
        for helper in reversed(helpers):
            helper.send_signal(signal.SIGTERM)
        for helper in helpers:
            try:
                helper.wait(timeout=5)
            except subprocess.TimeoutExpired:
                helper.kill()
    load = [int(line) for line in (utilisation.stdout.read().split() if utilisation.stdout else []) if line.isdigit()]
    counters = [value for run, _ in runs for value in run]
    summary = analyse(counters)
    reports = [report for _, report in runs]
    summary['capture_worker'] = dict(
        frames_per_second=round(sum(r['frames'] for r in reports) / (8 * len(reports)), 1),
        most_queued_frames_drained_at_once=max(r['maximum_batch'] for r in reports),
        stale_queued_frames_skipped=sum(r['superseded_frames'] or 0 for r in reports),
        wakeups_that_drained_several=None,
        frame_processing_ms_avg=round(sum(r['frame_processing_us_avg'] for r in reports) / len(reports) / 1000, 1),
        frame_processing_ms_max=round(max(r['frame_processing_us_max'] for r in reports) / 1000, 1),
        longest_gap_between_frames_ms=round(max(r['delivery_gap_us_max'] for r in reports) / 1000))
    del summary['capture_worker']['wakeups_that_drained_several']
    summary.update(captures=len(runs), seconds=round(elapsed, 1), load=args.load,
                   gpu_utilisation_percent=dict(mean=round(sum(load) / len(load)), min=min(load), max=max(load)) if load else None)
    print(json.dumps(summary, indent=2))
    ok = summary.get('backward_steps') == 0 and summary.get('readable', 0) >= 100
    print('PASS: no stale frames' if ok else 'FAIL: the capture went back to older frames' if summary.get('backward_steps') else
          'INCONCLUSIVE: too few readable frames', file=sys.stderr)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
