#!/usr/bin/python3
"""Hardware acceptance for the explicit AMD backend. No UU login needed."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / 'build/native-presenter'
sys.path.insert(0, str(ROOT / 'tests/probes'))
from nvenc_encode_probe import run, validate_sequence_headers  # noqa: E402


def main():
    evidence = ROOT / 'build/amd-evidence'
    evidence.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(STAGE / 'uu-host-frame-probe')], check=True, timeout=30)
    with tempfile.TemporaryDirectory(prefix='uuway-vaapi-') as directory:
        subprocess.run([str(STAGE / 'uu-vaapi-probe'), directory], check=True, timeout=60)
        for codec in ('h264', 'hevc'):
            stream = Path(directory) / (codec + '.bin')
            info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
                'stream=codec_name,width,height,color_space,color_transfer,color_primaries,color_range:frame=key_frame,pict_type',
                '-of', 'json', str(stream)], timeout=30))
            assert len(info['frames']) == 64
            assert all(info['frames'][i]['key_frame'] for i in (0, 21, 32))
            validate_sequence_headers((Path(directory) / (codec + '.headers')).read_bytes(), stream.read_bytes(), codec)
            decoded = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(stream), '-frames:v', '1',
                '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'], timeout=30)
            for x, expected in ((160, (255, 0, 0)), (480, (0, 0, 255))):
                offset = (180 * 640 + x) * 3
                assert all(abs(a-b) < 12 for a, b in zip(decoded[offset:offset+3], expected))
            (evidence / (codec + '-native.json')).write_text(json.dumps(info, indent=2))
    reports = []
    for pixel_format, binding in (('bgra', 'sampled'), ('nv12', 'sampled'), ('bgra', 'uav')):
        report = run(pixel_format, input_bind=binding, amd=True)
        reports.append(report)
        (evidence / 'windows-abi.json').write_text(json.dumps(reports, indent=2))
        print(f'Windows ABI {pixel_format}/{binding}: passed', flush=True)
    print('AMD hardware and Windows interface checks passed. Live UU connection is a separate acceptance step.')


if __name__ == '__main__':
    main()
