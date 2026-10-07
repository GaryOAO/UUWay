import json
import os
from pathlib import Path
import subprocess
import unittest
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('UURB_TEST_CAPTURE_BINARY', str(ROOT / 'build/native-presenter/uu-pipewire-native-probe')))


class NativePipewireProbeTests(unittest.TestCase):
    @unittest.skipUnless(BINARY.is_file(), 'Build the native PipeWire probe first')
    def test_invalid_cursor_composition_rejected_before_gpu_initialization(self):
        for value in ('', '0', 'true', '2', ' 1', '1 '):
            result = subprocess.run([str(BINARY)], env=dict(os.environ, UURB_CURSOR_COMPOSITE=value),
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertIn('UURB_CURSOR_COMPOSITE', result.stderr)

    @unittest.skipUnless(BINARY.is_file(), 'Build the native PipeWire probe first')
    def test_invalid_source_serial_rejected_before_gpu_initialization(self):
        for serial in ('','0','-1','+1',' 1','1 ','1.0','node-id','18446744073709551616'):
            result=subprocess.run([str(BINARY)],env=dict(os.environ,UURB_CAPTURE_TARGET_SERIAL=serial),
                                  capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,2)
            self.assertIn('UURB_CAPTURE_TARGET_SERIAL',result.stderr)

    def test_first_frame_status_does_not_block_or_raise_sigpipe(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = str(Path(temporary) / 'status')
            subprocess.run(['gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                '-I', str(ROOT / 'src'), str(ROOT / 'tests/probes/native_capture_status.c'),
                '-o', executable], check=True, capture_output=True, timeout=15)
            subprocess.run([executable], check=True, capture_output=True, timeout=5)

    @unittest.skipUnless((ROOT / 'build/portal-recovery/sysroot/usr/include/spa-0.2').is_dir(),
                         'Build the portal sysroot first (scripts/build-portal-lifetime.sh)')
    def test_bounded_cursor_metadata_without_video_pixel_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = str(Path(temporary) / 'cursor')
            subprocess.run(['gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                '-I', str(ROOT / 'src'), '-isystem', str(ROOT / 'build/portal-recovery/sysroot/usr/include/spa-0.2'),
                str(ROOT / 'src/native_cursor_metadata.c'), str(ROOT / 'tests/probes/native_cursor_metadata.c'),
                '-o', executable], check=True, capture_output=True, timeout=15)
            subprocess.run([executable], check=True, capture_output=True, timeout=5)

    @unittest.skipUnless(BINARY.is_file(), 'Build the native PipeWire probe first')
    def test_invalid_capture_size_rejected_before_gpu_initialization(self):
        for size in ['', '0x1080', '1921x1080', '1920x1081', '4098x2160', '-2x2',
                     '1920X1080', '1920x1080extra', '1920', '999999999999x2']:
            result = subprocess.run([str(BINARY)], env=dict(os.environ, UURB_CAPTURE_SIZE=size),
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertIn('UURB_CAPTURE_SIZE', result.stderr)

    @unittest.skipUnless(BINARY.is_file(), 'Build the native PipeWire probe first')
    def test_invalid_capture_rate_rejected_before_gpu_initialization(self):
        for rate in ['', '0', '-1', '121', '60/1', '60x']:
            result = subprocess.run([str(BINARY)], env=dict(os.environ, UURB_CAPTURE_MAX_FPS=rate),
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertIn('UURB_CAPTURE_MAX_FPS', result.stderr)

    def test_source_timing_handles_jitter_discontinuities_and_large_timestamps(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = str(Path(temporary) / 'timing')
            subprocess.run(['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT / 'src'),
                            str(ROOT / 'tests/probes/capture_timing_test.c'), '-o', executable],
                           check=True, capture_output=True, timeout=15)
            subprocess.run([executable], check=True, capture_output=True, timeout=5)

    @unittest.skipUnless(BINARY.is_file(), 'Build the native PipeWire probe first')
    def test_invalid_fd_and_node_rejected(self):
        for args in [[], ['0', '1'], ['-1', '1'], ['3', '4294967295'],
                     ['999999', '1'], ['1x', '3'], ['0', '1', '--cpu-fallback']]:
            with self.subTest(args=args):
                result = subprocess.run([str(BINARY), *args], timeout=5,
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn('usage:', result.stderr)

    def test_capture_does_not_use_default_core_or_pixel_mapping(self):
        source = (ROOT / 'src/uu_pipewire_native_probe.c').read_text()
        self.assertIn('pw_context_connect_fd(', source)
        self.assertNotIn('pw_context_connect(', source)
        self.assertNotIn('PW_STREAM_FLAG_MAP_BUFFERS', source)
        self.assertNotIn('mmap(', source)
        self.assertNotIn('SPA_DATA_MemPtr', source)
        self.assertNotIn('SPA_DATA_MemFd', source)
        self.assertIn('SPA_PARAM_BUFFERS_dataType, SPA_POD_Int(1u << SPA_DATA_DmaBuf)', source)

    def test_build_inputs_are_hash_pinned_and_not_installed(self):
        data = json.loads((ROOT / 'config/portal-recovery-dependencies.json').read_text())
        self.assertEqual(len(data['source_sha256']), 64)
        packages = data['packages'] + data['validation_packages']
        names = [p[0] for p in packages]
        self.assertEqual(len(names), len(set(names)))
        for package, version, arch, digest in packages:
            self.assertRegex(digest, r'^[0-9a-f]{64}$')
            self.assertIn(arch, ['amd64', 'all'])
        source = (ROOT / 'scripts/build-portal-lifetime.sh').read_text()
        self.assertIn('sha256sum -c -', source)
        self.assertIn('--fuzz=0', source)
        self.assertIn('--wrap-mode=nofallback', source)
        self.assertNotIn('apt-get install', source)
        self.assertNotIn('systemctl ', source)

    def test_import_check_does_not_submit_or_map_pixels(self):
        source = (ROOT / 'src/native_vk_dmabuf_import.c').read_text()
        self.assertIn('F_DUPFD_CLOEXEC', source)
        self.assertIn('vkBindImageMemory', source)
        self.assertIn('VK_EXTERNAL_MEMORY_FEATURE_IMPORTABLE_BIT', source)
        self.assertNotIn('vkQueueSubmit(', source)
        self.assertNotIn('vkMapMemory(', source)
        self.assertNotIn('mmap(', source)

    def test_dmabuf_fence_wait_covers_foreign_ownership_acquire(self):
        source = (ROOT / 'src/native_vk_capture_encode.c').read_text()
        self.assertIn('VkPipelineStageFlags wait_stage = VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;', source)
        self.assertNotIn('VkPipelineStageFlags wait_stage = VK_PIPELINE_STAGE_TRANSFER_BIT;', source)


    def test_queued_frames_are_superseded_so_the_picture_never_falls_behind(self):
        source = (ROOT / 'src/uu_pipewire_native_probe.c').read_text()
        process = source[source.index('static void process(void *opaque)'):source.index('static void timer(')]
        # Every queued buffer is taken first, then only the newest desktop image goes on to the encoder.
        self.assertLess(process.index('pw_stream_dequeue_buffer'), process.index('carries_video'))
        self.assertLess(process.index('supersede_frame'), process.index('process_frame'))
        self.assertIn('for (unsigned i = count; i-- > 0;)', process)  # newest first, so later cursor-only buffers still run
        self.assertIn('if (p->negotiated)', process)  # before negotiation nothing is judged or dropped
        # A superseded buffer is recycled, never encoded, and still feeds its cursor metadata.
        supersede = source[source.index('static void supersede_frame'):source.index('static void process(void *opaque)')]
        self.assertIn('uurb_cursor_metadata', supersede)
        self.assertLess(supersede.index('uurb_cursor_metadata'), supersede.index('pw_stream_queue_buffer'))
        for forbidden in ('uurb_capture_encoder_frame', 'uurb_capture_encoder_cursor_frame', 'record_delivery'):
            self.assertNotIn(forbidden, supersede)
        self.assertIn('\\"superseded_frames\\":%u', source)


if __name__ == '__main__':
    unittest.main()
