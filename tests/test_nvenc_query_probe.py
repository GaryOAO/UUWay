import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('nvenc_query_probe', ROOT / 'tests/probes/nvenc_query_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class NvencQueryProbeTests(unittest.TestCase):
    @staticmethod
    def rows():
        return [dict(cycle=cycle, codec=codec, formats=[1, 0x1000000], profiles=3,
                     presets=7, max_width=8192, preset_config_ex=True)
                for cycle in range(4) for codec in ['h264', 'hevc']]

    def test_all_cycles_must_be_present(self):
        rows = self.rows()
        probe.validate(rows)
        with self.assertRaises(RuntimeError):
            probe.validate(rows[:-1])

    def test_wrong_codec_cycle_or_incomplete_result_is_rejected(self):
        for field, value in [('codec', 'h264'), ('cycle', 4), ('formats', []),
                             ('profiles', 0), ('presets', 0), ('max_width', 2048), ('preset_config_ex', False)]:
            rows = self.rows()
            rows[1][field] = value
            with self.assertRaises(RuntimeError):
                probe.validate(rows)

    def test_capability_results_cannot_change_between_identical_sessions(self):
        rows = copy.deepcopy(self.rows())
        rows[-1]['formats'].append(0x10000)
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            probe.validate(rows)

    def test_query_module_is_not_named_like_a_production_nvenc_replacement(self):
        build = (ROOT / 'scripts/build-nvenc-query-probe.sh').read_text()
        self.assertIn('uurb-nvenc-query.dll', build)
        self.assertNotIn('"$stage/nvEncodeAPI64.dll"', build)
        source = (ROOT / 'src/uu_nvenc_query.c').read_text()
        self.assertIn('NV_ENC_ERR_UNIMPLEMENTED', source)
        self.assertNotIn('enableEncodeAsync = 0', source)
        self.assertNotIn('.nvEncEncodePicture =', source)
        self.assertIn('params->version != NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS_VER', source)

    def test_native_driver_entry_points_do_not_interpose_windows_abi_exports(self):
        native = (ROOT / 'src/native_nvenc_query.c').read_text()
        self.assertIn('dlsym(library, "NvEncodeAPICreateInstance")', native)
        self.assertIn('status = native_instance(&s->api)', native)
        self.assertNotIn('status = NvEncodeAPICreateInstance(', native)
        self.assertIn('cuCtxPopCurrent', native)
        self.assertIn('candidate.bytes, uuid, 16', native)

    def test_pe_loader_has_explicit_adjacent_backend_and_serialized_initialization(self):
        source = (ROOT / 'src/uu_nvenc_query_loader.c').read_text()
        self.assertIn('GetModuleFileNameW(self,', source)
        self.assertIn('LoadLibraryW(location)', source)
        self.assertIn('InitOnceExecuteOnce(', source)
        self.assertIn('GET_MODULE_HANDLE_EX_FLAG_PIN', source)
        self.assertIn('uurb-nvenc-query.dll.so', source)
        self.assertNotIn('GetEnvironmentVariable', source)

    def test_missing_backend_mode_requires_named_pe_client(self):
        for pe, named in [(False, False), (True, False), (False, True)]:
            with self.subTest(pe=pe, named=named), self.assertRaises(ValueError):
                probe.run(pe_client=pe, named_load=named, missing_backend=True)


if __name__ == '__main__':
    unittest.main()
