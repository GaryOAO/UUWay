"""Candidate patch checks only: not a running-compositor validation."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'build/mutter-review/mutter-46.2/src/backends/meta-screen-cast-stream-src.c'
PATCH = ROOT / 'patches/mutter-46.2-capture-jitter-candidate.patch'
FINISH_PATCH = ROOT / 'patches/mutter-46.2-screencast-dmabuf-finish.patch'


class MutterCapturePacingTests(unittest.TestCase):
    @unittest.skipUnless(SOURCE.is_file(), 'Download audited Mutter source to test patch application')
    def test_actual_patched_rate_check_with_synthetic_jitter(self):
        with tempfile.TemporaryDirectory(prefix='uurb-mutter-pacing-') as temporary:
            directory = Path(temporary)
            target = directory / 'src/backends/meta-screen-cast-stream-src.c'
            target.parent.mkdir(parents=True)
            shutil.copyfile(SOURCE, target)
            subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(PATCH)],
                           cwd=directory, check=True, capture_output=True, timeout=5)
            code = target.read_text()
            assignment = re.search(r'jitter_tolerance_us = [^;]+;', code).group(0)
            condition = re.search(r'if \((time_since_last_frame_us < min_interval_us[^)]+)\)', code).group(1)
            # Compile the expressions extracted from the patched source itself.
            harness = '''
#include <stdint.h>
#include <assert.h>
#define MIN(a,b) ((a)<(b)?(a):(b))
static int early(int64_t min_interval_us, int64_t time_since_last_frame_us) {
    int64_t jitter_tolerance_us;
''' + assignment + '\nreturn ' + condition + ''';
}
int main(void) {
    assert(!early(16666, 16666));
    assert(!early(16666, 16604)); /* largest observed 62 us shortfall */
    assert(!early(16666, 16566));
    assert(early(16666, 16565)); /* bounded, not an unlimited bypass */
    assert(early(16666, 8333)); /* 120 Hz -> 60 FPS still throttles */
    assert(early(16949, 16666)); /* 59 cap is not silently treated as 60 */
    assert(early(16666, -1));
    assert(!early(1000, 990));
    assert(early(1000, 989)); /* at most 1% for high-rate sources */
    assert(early(1, 0));
    assert(!early(16666, 1000000)); /* a stall does not block resumption */
    int old_count = 1, new_count = 1;
    int64_t old_last = 1000000, new_last = 1000000;
    uint32_t random = 12345;
    for (int64_t i = 1; i < 600; i++) {
        random = random * 1664525u + 1013904223u;
        int jitter = (int)(random % 81) - 40;
        int64_t timestamp = 1000000 + i * 1000000 / 60 + jitter;
        if (timestamp - old_last >= 16666) { old_count++; old_last = timestamp; }
        if (!early(16666, timestamp - new_last)) { new_count++; new_last = timestamp; }
    }
    assert(old_count < 500);
    assert(new_count == 600); /* model only, not a measured desktop FPS */
    return 0;
}
'''
            executable = directory / 'pacing-test'
            subprocess.run(['gcc', '-x', 'c', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-o', str(executable), '-'], input=harness, text=True,
                           check=True, capture_output=True, timeout=10)
            subprocess.run([str(executable)], check=True, capture_output=True, timeout=5)

    @unittest.skipUnless(SOURCE.is_file(), 'Download audited Mutter source to test patch application')
    def test_dmabuf_finish_patch_applies_after_pacing_and_waits_before_queueing(self):
        with tempfile.TemporaryDirectory(prefix='uurb-mutter-finish-') as temporary:
            directory = Path(temporary)
            target = directory / 'src/backends/meta-screen-cast-stream-src.c'
            target.parent.mkdir(parents=True)
            shutil.copyfile(SOURCE, target)
            for patch in (PATCH, FINISH_PATCH):  # the order scripts/build-mutter-capture.sh applies them
                subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(patch)],
                               cwd=directory, check=True, capture_output=True, timeout=5)
            code = target.read_text()
            branch = code[code.index('else if (spa_data->type == SPA_DATA_DmaBuf)'):]
            branch = branch[:branch.index('Unknown SPA buffer type')]
            self.assertLess(branch.index('meta_screen_cast_stream_src_record_to_framebuffer'),
                            branch.index('cogl_framebuffer_finish (dmabuf_fbo)'))
            self.assertLess(branch.index('return FALSE;'), branch.index('cogl_framebuffer_finish (dmabuf_fbo)'))
            self.assertEqual(code.count('cogl_framebuffer_finish'), 1)  # memfd/CPU buffers are already synchronous

    def test_dmabuf_finish_patch_is_built_and_required_for_staging(self):
        # NVIDIA attaches no implicit fence to the dma-bufs Mutter renders into, so a busy GPU lets the
        # consumer read stale ring contents. A build or bundle without the patch must not be staged.
        self.assertEqual(FINISH_PATCH.read_text().count('\n+++ '), 1)
        self.assertIn('+++ b/src/backends/meta-screen-cast-stream-src.c', FINISH_PATCH.read_text())
        build = (ROOT / 'scripts/build-mutter-capture.sh').read_text()
        self.assertLess(build.index('mutter-46.2-capture-jitter-candidate.patch'),
                        build.index('mutter-46.2-screencast-dmabuf-finish.patch'))
        self.assertLess(build.index('mutter-46.2-screencast-dmabuf-finish.patch'), build.index('meson setup'))
        stage = (ROOT / 'scripts/stage-mutter-capture.py').read_text()
        self.assertLess(stage.index("'cogl_framebuffer_finish (dmabuf_fbo)' not in"), stage.index('output.mkdir('))
        self.assertIn('dmabuf_finish_patch_sha256', stage)

    def test_readonly_trace_is_hash_pinned_and_bounded(self):
        source = (ROOT / 'tests/probes/mutter_capture_pacing.py').read_text()
        self.assertIn('hashlib.sha256(LIBRARY.read_bytes()).hexdigest() != SHA256', source)
        self.assertIn('time.sleep(20)', source)
        self.assertIn('trace.cleanup()', source)
        for forbidden in ['bpf_probe_write_user', 'override_return', 'os.system', 'ptrace(']:
            self.assertNotIn(forbidden, source)


if __name__ == '__main__':
    unittest.main()
