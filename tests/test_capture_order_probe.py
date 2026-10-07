import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('capture_order_probe', ROOT / 'tests/probes/capture_order_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def synthetic_stream(rewind):
    """H.264 of the counter window, offset inside a larger canvas; optionally rewinds 30 frames once."""
    pieces = ('[p]split=3[a][b][c];[a]trim=start_frame=0:end_frame=60,setpts=PTS-STARTPTS[a1];'
              '[b]trim=start_frame=30:end_frame=60,setpts=PTS-STARTPTS[b1];'
              '[c]trim=start_frame=60:end_frame=90,setpts=PTS-STARTPTS[c1];[a1][b1][c1]concat=n=3[v]'
              if rewind else '[p]trim=start_frame=0:end_frame=90,setpts=PTS-STARTPTS[v]')
    command = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=0x303030:s=1280x1024:r=60',
               '-f', 'lavfi', '-i', probe.pattern_graph(960, 540),
               '-filter_complex', f'[1:v]format=rgb24[p];{pieces};[0:v][v]overlay=237:113:shortest=1,format=yuv420p[o]',
               '-map', '[o]', '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', '4M', '-bf', '0', '-f', 'h264', '-']
    stream = tempfile.TemporaryFile()
    stream.write(subprocess.run(command, capture_output=True, check=True, timeout=120).stdout)
    stream.flush()
    stream.seek(0)
    return stream


class CaptureOrderProbeTests(unittest.TestCase):
    def test_analysis_counts_every_frame_older_than_one_already_seen(self):
        clean = probe.analyse([0, 1, 1, 2, 5, 9])
        self.assertEqual((clean['backward_steps'], clean['deepest_rewind_frames'], clean['repeated']), (0, 0, 1))
        stale = probe.analyse([10, 11, 12, 13, 5, 6, 14, 15, 7, 16])
        self.assertEqual(stale['backward_steps'], 2)
        self.assertEqual(stale['frames_older_than_newest_seen'], 3)
        self.assertEqual(stale['deepest_rewind_frames'], 8)
        self.assertEqual(stale['deepest_rewind_seconds'], round(8 / probe.PATTERN_FPS, 2))
        self.assertEqual(probe.analyse([None, 3, None])['readable'], 1)  # unreadable frames are not evidence

    def test_probe_never_touches_the_service_token_or_other_desktop_state(self):
        source = (ROOT / 'tests/probes/capture_order_probe.py').read_text()
        self.assertIn("'.local/state/uurb/order-probe.json'", source)
        self.assertNotIn('portal-probe.json', source)
        for forbidden in ('systemctl', 'uurb/trial', 'xdotool', 'ydotool'):
            self.assertNotIn(forbidden, source)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is needed to build and decode the synthetic capture')
    def test_decoder_finds_the_offset_window_and_detects_one_rewind(self):
        with synthetic_stream(False) as stream:  # the descriptor must outlive the ffmpeg runs
            clean = probe.analyse(probe.decode(None, stream.fileno(), 1280, 1024))
        self.assertEqual((clean['readable'], clean['backward_steps']), (90, 0))
        with synthetic_stream(True) as stream:
            rewound = probe.analyse(probe.decode(None, stream.fileno(), 1280, 1024))
        self.assertEqual((rewound['readable'], rewound['backward_steps'], rewound['deepest_rewind_frames']), (120, 1, 29))


if __name__ == '__main__':
    unittest.main()
