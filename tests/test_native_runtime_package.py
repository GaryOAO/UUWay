"""Packaging isolation checks with synthetic bytes, not executable acceptance."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import subprocess
import struct
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runtime_package', ROOT / 'scripts/package-uu-native-runtime.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class PackageIsolationTests(unittest.TestCase):
    @staticmethod
    def pe_input_bridge(marker=True, machine=0x8664, export=None):
        """Return a small, non-loadable PE fixture with an export table.

        The package verifier deliberately parses the PE export table rather
        than accepting a string in an arbitrary file.  Keeping this fixture
        self-contained makes the package tests independent of Wine/cross
        toolchains while still exercising the same checks as a real DLL.
        """
        marker_name = (export.encode() if export is not None else
                       (b'UurbInputBridgeDisplaySetVersion' if marker else b'NotTheDisplaySetter'))
        image = bytearray(0x600)
        image[:2] = b'MZ'
        image[0x3c:0x40] = (0x80).to_bytes(4, 'little')
        image[0x80:0x84] = b'PE\0\0'
        coff = 0x84
        image[coff:coff + 2] = machine.to_bytes(2, 'little')
        image[coff + 2:coff + 4] = (2).to_bytes(2, 'little')
        image[coff + 16:coff + 18] = (0xf0).to_bytes(2, 'little')
        image[coff + 18:coff + 20] = (0x2022).to_bytes(2, 'little')
        optional = coff + 20
        image[optional:optional + 2] = (0x20b).to_bytes(2, 'little')
        image[optional + 60:optional + 64] = (0x200).to_bytes(4, 'little')
        image[optional + 108:optional + 112] = (16).to_bytes(4, 'little')
        # IMAGE_DIRECTORY_ENTRY_EXPORT (RVA, size).
        image[optional + 112:optional + 120] = (0x2000).to_bytes(4, 'little') + (0x200).to_bytes(4, 'little')
        section = optional + 0xf0
        # .text: the exported function points into this executable section.
        image[section:section + 8] = b'.text\0\0\0'
        image[section + 8:section + 12] = (0x100).to_bytes(4, 'little')
        image[section + 12:section + 16] = (0x1000).to_bytes(4, 'little')
        image[section + 16:section + 20] = (0x200).to_bytes(4, 'little')
        image[section + 20:section + 24] = (0x200).to_bytes(4, 'little')
        image[section + 36:section + 40] = (0x60000020).to_bytes(4, 'little')
        # .edata: export directory and tables.
        section += 40
        image[section:section + 8] = b'.edata\0\0'
        image[section + 8:section + 12] = (0x200).to_bytes(4, 'little')
        image[section + 12:section + 16] = (0x2000).to_bytes(4, 'little')
        image[section + 16:section + 20] = (0x200).to_bytes(4, 'little')
        image[section + 20:section + 24] = (0x400).to_bytes(4, 'little')
        image[section + 36:section + 40] = (0x40000040).to_bytes(4, 'little')
        export = 0x400
        image[export + 16:export + 20] = (1).to_bytes(4, 'little')  # Base
        image[export + 20:export + 24] = (1).to_bytes(4, 'little')  # NumberOfFunctions
        image[export + 24:export + 28] = (1).to_bytes(4, 'little')  # NumberOfNames
        image[export + 28:export + 32] = (0x2060).to_bytes(4, 'little')
        image[export + 32:export + 36] = (0x2064).to_bytes(4, 'little')
        image[export + 36:export + 40] = (0x2068).to_bytes(4, 'little')
        image[0x460:0x464] = (0x1000).to_bytes(4, 'little')
        image[0x464:0x468] = (0x2070).to_bytes(4, 'little')
        image[0x468:0x46a] = (0).to_bytes(2, 'little')
        image[0x450:0x455] = b'uurb\0'
        image[0x470:0x470 + len(marker_name) + 1] = marker_name + b'\0'
        return bytes(image)

    @staticmethod
    def elf_query_v2():
        """Minimal ELF64 DYN carrying only a defined dynsym query export."""
        image = bytearray(0x500)
        image[:4] = b'\x7fELF'; image[4:6] = b'\x02\x01'
        struct.pack_into('<HHIQQQIHHHHHH', image, 16, 3, 0x3e, 1, 0, 0,
                         0x100, 0, 64, 0, 0, 64, 3, 0)
        dynstr = b'\0UurbDisplayQueryV2\0'
        image[0x300:0x300 + len(dynstr)] = dynstr
        # section 1: .dynstr; section 2: .dynsym linked to section 1
        struct.pack_into('<IIQQQQIIQQ', image, 0x100 + 64, 0, 3, 0, 0,
                         0x300, len(dynstr), 0, 0, 1, 0)
        struct.pack_into('<IIQQQQIIQQ', image, 0x100 + 128, 0, 11, 0, 0,
                         0x340, 48, 1, 0, 8, 24)
        struct.pack_into('<IBBHQQ', image, 0x340 + 24, 1, 0x12, 0, 1, 0x100, 1)
        return bytes(image)

    def test_deferred_start_requires_explicit_backend_and_bundle_capability(self):
        spec = importlib.util.spec_from_file_location('deferred_service', ROOT / 'scripts/uu-native-service.py')
        service = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(service)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = dict(schema=1, prefix=str(root / 'prefix'), bundle=str(root / 'bundle'),
                          restore_state=str(root / 'restore.json'), state_parent=str(root / 'state'),
                          text_socket=str(root / 'state/text.sock'), cursor_mode='composited')
            service.trial.state_tools.write_private(root / 'native-runtime.json', config)
            for backend, capable in [('fcitx', True), ('fcitx', False), ('portal', True)]:
                service.trial.state_tools.write_private(root / 'text-backend.json', dict(version=1, backend=backend))
                with self.subTest(backend=backend, capable=capable), \
                        patch.object(service.trial.bundle_tools, 'verify', return_value={
                            'native_ime_deferred_start_included': capable,
                            'native_display_included': True}), \
                        patch.object(service.trial, 'run') as launch:
                    service.run(root / 'native-runtime.json', preserve_display_session=True,
                                allow_display_reconfigure=True)
                    args, keywords = launch.call_args
                    self.assertEqual(args[6], 'composited')
                    self.assertEqual(args[7], root / 'state' / ('ime.sock' if backend == 'fcitx' else 'text.sock'))
                    self.assertTrue(args[10])
                    self.assertEqual(keywords['pending_native_ime'], backend == 'fcitx' and capable)
                    self.assertEqual(args[9], root / 'state' / 'display.sock')

            # A remote UU negotiation must not be allowed to mutate Mutter by
            # merely passing the legacy preserve-display policy.
            with patch.object(service.trial.bundle_tools, 'verify', return_value={
                'native_ime_deferred_start_included': True,
                'native_display_included': True}), \
                    patch.object(service.trial, 'run') as launch:
                service.run(root / 'native-runtime.json', preserve_display_session=True)
                args, _ = launch.call_args
                self.assertEqual(args[9], root / 'state' / 'display.sock')
                self.assertFalse(args[10])
                self.assertFalse(launch.call_args.kwargs['allow_display_reconfigure'])

    def test_expected_display_remap_reconnects_in_process(self):
        spec = importlib.util.spec_from_file_location('remap_service', ROOT / 'scripts/uu-native-service.py')
        service = importlib.util.module_from_spec(spec); spec.loader.exec_module(service)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = dict(schema=1, prefix=str(root / 'prefix'), bundle=str(root / 'bundle'),
                          restore_state=str(root / 'restore.json'), state_parent=str(root / 'state'),
                          text_socket=str(root / 'state/text.sock'), cursor_mode='composited')
            service.trial.state_tools.write_private(root / 'native-runtime.json', config)
            with patch.object(service.trial.bundle_tools, 'verify', return_value={}), \
                 patch.object(service.trial, 'run', side_effect=[RuntimeError(service.DISPLAY_REMAP_FAILURE), None]) as launch, \
                 patch.object(service.trial, 'event') as event, \
                 patch.object(service.trial.state_tools, 'notify'):
                service.run(root / 'native-runtime.json', preserve_display_session=False)
            self.assertEqual(launch.call_count, 2)
            event.assert_called_once_with('native_service_display_reconnect', attempt=1)
            self.assertIn('stop_requested', launch.call_args.kwargs)

    def test_source_only_update_keeps_every_native_component_byte_identical(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self.fixture(root)
            with patch.object(package, 'ROOT', root):
                base = Path(package.package(root / 'releases', with_input=True)['bundle'])
                self.assertEqual(base.stat().st_mode & 0o777, 0o700)
                self.assertEqual((base / 'manifest.json').stat().st_mode & 0o777, 0o600)
                for name, source in dict(package.INPUTS, **package.INPUT_RUNTIME, **package.PINNED_CAPTURE).items():
                    (root / source).write_bytes(b'undeployed-development-build')
                updated = Path(package.package(root / 'releases', with_input=True, reuse_runtime_from=base)['bundle'])
                package.verify(base)
                for name in dict(package.INPUTS, **package.INPUT_RUNTIME, **package.PINNED_CAPTURE):
                    self.assertEqual((updated / name).read_bytes(), (base / name).read_bytes())

    def test_schema32_33_and_erroneous_schema34_are_accepted_as_legacy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self.fixture(root)
            with patch.object(package, 'ROOT', root):
                base = Path(package.package(root / 'releases', with_input=True)['bundle'])
                original = json.loads((base / 'manifest.json').read_text())
                for contract in (package.COMPOSITED_INPUT_CONTRACT,
                                 package.DEFERRED_IME_INPUT_CONTRACT,
                                 *package.LEGACY_DISPLAY_SET_CONTRACTS):
                    manifest = dict(original, contract=contract)
                    (base / 'manifest.json').write_text(json.dumps(manifest))
                    result = package.verify(base)
                    self.assertFalse(result['native_display_set_config_included'])
                    if contract in package.LEGACY_DISPLAY_SET_CONTRACTS:
                        self.assertTrue(result['legacy_display_set_config_claim_unverified'])
                    else:
                        self.assertFalse(result['legacy_display_set_config_claim_unverified'])

    def test_source_only_reuse_of_schema34_downgrades_claim_and_does_not_promote_native(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self.fixture(root)
            with patch.object(package, 'ROOT', root):
                base = Path(package.package(root / 'releases', with_input=True)['bundle'])
                manifest = json.loads((base / 'manifest.json').read_text())
                manifest['contract'] = package.LEGACY_DISPLAY_SET_INPUT_CONTRACT
                (base / 'manifest.json').write_text(json.dumps(manifest))
                native_before = (base / package.INPUT_BRIDGE_COMPONENT).read_bytes()
                updated = Path(package.package(root / 'releases', with_input=True,
                                               reuse_runtime_from=base)['bundle'])
                self.assertEqual(json.loads((updated / 'manifest.json').read_text())['contract'],
                                 package.DEFERRED_IME_INPUT_CONTRACT)
                result = package.verify(updated)
                self.assertFalse(result['native_display_set_config_included'])
                self.assertEqual((updated / package.INPUT_BRIDGE_COMPONENT).read_bytes(), native_before)

    def test_explicit_bridge_replacement_changes_only_input_bridge_and_promotes_schema35(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self.fixture(root)
            with patch.object(package, 'ROOT', root):
                base = Path(package.package(root / 'releases', with_input=True)['bundle'])
                manifest = json.loads((base / 'manifest.json').read_text())
                manifest['contract'] = package.DEFERRED_IME_INPUT_CONTRACT
                (base / 'manifest.json').write_text(json.dumps(manifest))
                replacement = root / 'replacement.dll'
                replacement_bytes = bytearray(self.pe_input_bridge())
                replacement_bytes[0x210] = 0x91  # Different function-section bytes, same export.
                replacement.write_bytes(replacement_bytes)
                updated = Path(package.package(root / 'releases', with_input=True,
                                               reuse_runtime_from=base,
                                               replace_input_bridge=replacement)['bundle'])
                self.assertEqual(json.loads((updated / 'manifest.json').read_text())['contract'],
                                 package.INPUT_CONTRACT)
                self.assertEqual((updated / package.INPUT_BRIDGE_COMPONENT).read_bytes(),
                                 replacement.read_bytes())
                for name in dict(package.INPUTS, **package.INPUT_RUNTIME, **package.PINNED_CAPTURE):
                    if name != package.INPUT_BRIDGE_COMPONENT:
                        self.assertEqual((updated / name).read_bytes(), (base / name).read_bytes(), name)
                self.assertTrue(package.verify(updated)['native_display_set_config_included'])

    def test_explicit_bridge_replacement_preserves_schema37_display_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self.fixture(root)
            with patch.object(package, 'ROOT', root):
                runtime = root / 'display-runtime-v2'; runtime.mkdir()
                for name, data in {
                    'runtime/native-input-bridge.dll': self.pe_input_bridge(
                        export='UurbInputBridgeDisplayDpiV2Version'),
                    'runtime/uurb-native-display-loader.dll': self.pe_input_bridge(
                        export='UurbDisplayQueryV2'),
                    'runtime/uurb-native-display.dll.so': self.elf_query_v2(),
                }.items():
                    (runtime / package.DISPLAY_RUNTIME_SOURCE_NAMES[name]).write_bytes(data)
                original = Path(package.package(root / 'releases', with_input=True)['bundle'])
                base = Path(package.package(root / 'releases', with_input=True,
                                            reuse_runtime_from=original,
                                            replace_display_runtime=runtime)['bundle'])
                replacement = root / 'replacement-v2.dll'
                replacement.write_bytes(self.pe_input_bridge(
                    export='UurbInputBridgeDisplayDpiV2Version'))
                updated = Path(package.package(root / 'releases', with_input=True,
                                               reuse_runtime_from=base,
                                               replace_input_bridge=replacement)['bundle'])
                manifest = json.loads((updated / 'manifest.json').read_text())
                self.assertEqual(manifest['contract'], package.DPI_V2_INPUT_CONTRACT)
                result = package.verify(updated)
                self.assertEqual(result['native_display_abi_version'], 2)
                self.assertTrue(result['native_display_dpi_v2_included'])
                self.assertEqual((updated / package.INPUT_BRIDGE_COMPONENT).read_bytes(),
                                 replacement.read_bytes())

    def test_bridge_replacement_rejects_v2_bridge_on_v1_display_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self.fixture(root)
            with patch.object(package, 'ROOT', root):
                base = Path(package.package(root / 'releases', with_input=True)['bundle'])
                replacement = root / 'replacement-v2.dll'
                replacement.write_bytes(self.pe_input_bridge(
                    export='UurbInputBridgeDisplayDpiV2Version'))
                with self.assertRaisesRegex(ValueError, 'matching DPI v2 display runtime'):
                    package.package(root / 'releases', with_input=True,
                                    reuse_runtime_from=base,
                                    replace_input_bridge=replacement)

    def test_explicit_bridge_replacement_rejects_invalid_pe_or_missing_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self.fixture(root)
            with patch.object(package, 'ROOT', root):
                base = Path(package.package(root / 'releases', with_input=True)['bundle'])
                manifest = json.loads((base / 'manifest.json').read_text())
                manifest['contract'] = package.DEFERRED_IME_INPUT_CONTRACT
                (base / 'manifest.json').write_text(json.dumps(manifest))
                for suffix, data in (('bad', b'not a PE'),
                                     ('missing', self.pe_input_bridge(marker=False)),
                                     ('wrong-machine', self.pe_input_bridge(machine=0x14c))):
                    candidate = root / (suffix + '.dll'); candidate.write_bytes(data)
                    with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, 'x64 PE DLL'):
                        package.package(root / 'releases', with_input=True,
                                        reuse_runtime_from=base,
                                        replace_input_bridge=candidate)

    def test_explicit_display_runtime_replacement_publishes_dpi_and_changes_only_three_components(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self.fixture(root)
            with patch.object(package, 'ROOT', root):
                base = Path(package.package(root / 'releases', with_input=True)['bundle'])
                runtime = root / 'display-runtime'; runtime.mkdir()
                for name in package.DISPLAY_RUNTIME_COMPONENTS:
                    source = base / name
                    target = runtime / package.DISPLAY_RUNTIME_SOURCE_NAMES[name]
                    target.write_bytes(source.read_bytes() + b'\0')
                updated = Path(package.package(root / 'releases', with_input=True,
                                               reuse_runtime_from=base,
                                               replace_display_runtime=runtime)['bundle'])
                manifest = json.loads((updated / 'manifest.json').read_text())
                self.assertEqual(manifest['contract'], package.DPI_INPUT_CONTRACT)
                self.assertEqual(package.verify(updated)['native_display_set_config_included'], True)
                for name in dict(package.INPUTS, **package.INPUT_RUNTIME, **package.PINNED_CAPTURE):
                    if name in package.DISPLAY_RUNTIME_COMPONENTS:
                        self.assertEqual((updated / name).read_bytes(),
                                         (runtime / package.DISPLAY_RUNTIME_SOURCE_NAMES[name]).read_bytes())
                    else:
                        self.assertEqual((updated / name).read_bytes(), (base / name).read_bytes(), name)

    def test_explicit_display_runtime_v2_requires_matching_three_exports_and_publishes_schema37(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self.fixture(root)
            with patch.object(package, 'ROOT', root):
                base = Path(package.package(root / 'releases', with_input=True)['bundle'])
                runtime = root / 'display-runtime-v2'; runtime.mkdir()
                v2 = {
                    'runtime/native-input-bridge.dll': self.pe_input_bridge(
                        export='UurbInputBridgeDisplayDpiV2Version'),
                    'runtime/uurb-native-display-loader.dll': self.pe_input_bridge(
                        export='UurbDisplayQueryV2'),
                    'runtime/uurb-native-display.dll.so': self.elf_query_v2(),
                }
                for name, data in v2.items():
                    (runtime / package.DISPLAY_RUNTIME_SOURCE_NAMES[name]).write_bytes(data)
                updated = Path(package.package(root / 'releases', with_input=True,
                                               reuse_runtime_from=base,
                                               replace_display_runtime=runtime)['bundle'])
                manifest = json.loads((updated / 'manifest.json').read_text())
                self.assertEqual(manifest['contract'], package.DPI_V2_INPUT_CONTRACT)
                result = package.verify(updated)
                self.assertTrue(result['native_display_dpi_v2_included'])
                self.assertEqual(result['native_display_abi_version'], 2)
                for name in dict(package.INPUTS, **package.INPUT_RUNTIME, **package.PINNED_CAPTURE):
                    if name in package.DISPLAY_RUNTIME_COMPONENTS:
                        self.assertEqual((updated / name).read_bytes(), v2[name])
                    else:
                        self.assertEqual((updated / name).read_bytes(), (base / name).read_bytes(), name)

    def test_display_runtime_v2_rejects_mixed_query_exports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self.fixture(root)
            with patch.object(package, 'ROOT', root):
                base = Path(package.package(root / 'releases', with_input=True)['bundle'])
                runtime = root / 'display-runtime-mixed'; runtime.mkdir()
                (runtime / 'uu-native-input-bridge.dll').write_bytes(
                    self.pe_input_bridge(export='UurbInputBridgeDisplayDpiV2Version'))
                (runtime / 'uurb-native-display-loader.dll').write_bytes(
                    self.pe_input_bridge(export='UurbDisplayQueryV2'))
                # A v1/invalid Winelib paired with a v2 bridge must fail closed.
                (runtime / 'uurb-native-display.dll.so').write_bytes(b'not-an-elf')
                with self.assertRaisesRegex(ValueError, 'matching UurbDisplayQueryV2'):
                    package.package(root / 'releases', with_input=True,
                                    reuse_runtime_from=base,
                                    replace_display_runtime=runtime)

    def test_display_runtime_replacement_rejects_partial_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self.fixture(root)
            with patch.object(package, 'ROOT', root):
                base = Path(package.package(root / 'releases', with_input=True)['bundle'])
                runtime = root / 'display-runtime'; runtime.mkdir()
                (runtime / package.DISPLAY_RUNTIME_SOURCE_NAMES[package.DISPLAY_RUNTIME_COMPONENTS[0]]).write_bytes(b'x')
                with self.assertRaisesRegex(ValueError, 'missing'):
                    package.package(root / 'releases', with_input=True,
                                    reuse_runtime_from=base,
                                    replace_display_runtime=runtime)

    def test_source_only_update_cannot_claim_new_native_cursor_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); self.fixture(root)
            with patch.object(package, 'ROOT', root):
                base = Path(package.package(root / 'releases', with_input=True)['bundle'])
                manifest = json.loads((base / 'manifest.json').read_text())
                manifest['contract'] = package.NATIVE_IME_INPUT_CONTRACT
                (base / 'manifest.json').write_text(json.dumps(manifest))
                updated = Path(package.package(root / 'releases', with_input=True, reuse_runtime_from=base)['bundle'])
                self.assertFalse(package.verify(updated)['native_composited_cursor_included'])
                self.assertEqual(json.loads((updated / 'manifest.json').read_text())['contract'], package.NATIVE_IME_INPUT_CONTRACT)

    def test_explicit_ime_backend_does_not_change_clipboard_supervisor_endpoint(self):
        spec = importlib.util.spec_from_file_location('service_backend', ROOT / 'scripts/uu-native-service.py')
        service = importlib.util.module_from_spec(spec); spec.loader.exec_module(service)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = dict(state_parent=str(root), text_socket=str(root / 'text.sock'))
            self.assertEqual(service.text_endpoint(config, root), root / 'text.sock')
            for backend, expected in (('fcitx', 'ime.sock'), ('portal', 'text.sock')):
                service.trial.state_tools.write_private(root / 'text-backend.json', dict(version=1, backend=backend))
                self.assertEqual(service.text_endpoint(config, root), root / expected)
                self.assertEqual(config['text_socket'], str(root / 'text.sock'))
            for value in (dict(version=True, backend='fcitx'), dict(version=1, backend='auto'),
                          dict(version=1, backend='fcitx', fallback=True)):
                service.trial.state_tools.write_private(root / 'text-backend.json', value)
                with self.assertRaises(ValueError): service.text_endpoint(config, root)

    def fixture(self, root):
        for name, source in dict(package.INPUTS, **package.INPUT_RUNTIME, **package.PINNED_CAPTURE, **package.PINNED_MAIN_SCRIPTS).items():
            path = root / source
            path.parent.mkdir(parents=True, exist_ok=True)
            if name in package.PINNED_MAIN_SCRIPTS:
                path.write_bytes((ROOT / source).read_bytes())
            elif name == package.INPUT_BRIDGE_COMPONENT:
                path.write_bytes(self.pe_input_bridge())
            else:
                path.write_bytes(('fixture:' + name).encode())

    def test_released_main_entrypoints_and_imports_are_closed_over_the_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary);self.fixture(root)
            with patch.object(package, 'ROOT', root):
                release = package.package(root / 'releases', with_input=True)
                bundle = Path(release['bundle'])
                self.assertTrue(package.verify(bundle)['native_main_scripts_included'])
                original = {name:(bundle/name).read_bytes() for name in package.PINNED_MAIN_SCRIPTS}
                for name in package.PINNED_MAIN_SCRIPTS:
                    (root/name).write_bytes(b'raise RuntimeError("mutable development source must not run")\n')
                for entry in ('uu-native-service.py','uu-native-trial.py','uu-native-capture-broker.py','probe-wayland-portal.py'):
                    result=subprocess.run(['/usr/bin/python3',str(bundle/'scripts'/entry),'--help'],
                        cwd='/tmp',capture_output=True,timeout=5)
                    self.assertEqual(result.returncode,0,result.stderr.decode())
                for name, expected in original.items():self.assertEqual((bundle/name).read_bytes(),expected)
                self.assertTrue(package.verify(bundle)['native_main_scripts_included'])
                # The broker's subprocess path must also stay in this bundle.
                spec=importlib.util.spec_from_file_location('pinned_fixture_broker',bundle/'scripts/uu-native-capture-broker.py')
                module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
                command=module.producer_command(Path('/private/restore.json'),3,0,'embedded',
                    capture_binary=bundle/'capture/uu-pipewire-native-probe')
                self.assertEqual(command[1],str(bundle/'scripts/probe-wayland-portal.py'))
                (bundle/'scripts/native_display_confirmation.py').write_bytes(b'changed')
                with self.assertRaises(ValueError):package.verify(bundle)

    def test_service_install_pins_only_main_without_changing_independent_daemon_paths(self):
        spec=importlib.util.spec_from_file_location('pinned_service_installer',ROOT/'scripts/install-uu-native-service.py')
        installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);self.fixture(root)
            with patch.object(package,'ROOT',root):
                bundle=Path(package.package(root/'releases',with_input=True)['bundle'])
            prefix=root/'prefix';prefix.mkdir(mode=0o700)
            state=root/'state';state.mkdir(mode=0o700)
            with patch.object(installer.Path,'home',return_value=root), contextlib.redirect_stdout(io.StringIO()):
                installer.install(bundle,prefix,state/'portal.json',state,state/'text.sock','embedded')
            units=root/'.config/systemd/user'
            self.assertIn(str(bundle/'scripts/uu-native-service.py'),(units/'uu-native-bridge.service').read_text())
            for name in ('text','display'):
                unit=(units/f'uu-native-{name}.service').read_text()
                self.assertIn(str(ROOT/f'scripts/uu-native-{name}-service.py'),unit)
                self.assertNotIn(str(bundle),unit)
            main = units / 'uu-native-bridge.service'
            self.assertNotIn('@DISPLAY_POLICY@', main.read_text())
            with patch.object(installer.Path, 'home', return_value=root), contextlib.redirect_stdout(io.StringIO()):
                installer.install(bundle, prefix, state/'portal.json', state, state/'text.sock',
                                  'composited', preserve_display_session=True)
                self.assertTrue(installer.existing_display_policy(main.read_text()))
                # An upgrade without policy arguments preserves both user choices.
                installer.install(bundle, prefix, state/'portal.json', state, state/'text.sock')
                config_path = root / '.config/uurb/native-runtime.json'
                self.assertEqual(json.loads(config_path.read_text())['cursor_mode'], 'composited')
                self.assertTrue(installer.existing_display_policy(main.read_text()))
                before = (config_path.read_bytes(), main.read_bytes())
                incompatible = installer.trial.bundle_tools.verify(bundle)
                incompatible['native_display_geometry_included'] = False
                with patch.object(installer.trial.bundle_tools, 'verify', return_value=incompatible):
                    with self.assertRaisesRegex(ValueError, 'incompatible'):
                        installer.install(bundle, prefix, state/'portal.json', state, state/'text.sock')
                self.assertEqual(before, (config_path.read_bytes(), main.read_bytes()))
                installer.install(bundle, prefix, state/'portal.json', state, state/'text.sock',
                                  preserve_display_session=False)
                self.assertFalse(installer.existing_display_policy(main.read_text()))

    def test_managed_display_policy_does_not_guess_from_comments_or_unknown_options(self):
        spec = importlib.util.spec_from_file_location('policy_installer', ROOT/'scripts/install-uu-native-service.py')
        installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
        command = 'ExecStart=/usr/bin/python3 "/owned/scripts/uu-native-service.py" --config "%h/.config/uurb/native-runtime.json"'
        self.assertFalse(installer.existing_display_policy('[Service]\n# --preserve-display-session\n' + command))
        self.assertTrue(installer.existing_display_policy('[Service]\n' + command + ' --preserve-display-session'))
        for text in ('[Service]\n'+command+' --unknown', '[Service]\n'+command+'\n'+command,
                     '[Unit]\n'+command, '[Service]\n'+command+'\\\n --preserve-display-session'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                installer.existing_display_policy(text)


    def test_build_changes_cannot_replace_released_producer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            with patch.object(package, 'ROOT', root):
                release = package.package(root / 'releases', with_input=True)
                bundle = Path(release['bundle'])
                producer = bundle / 'capture/uu-pipewire-native-probe'
                before = producer.read_bytes()
                self.assertEqual(producer.stat().st_mode & 0o777, 0o700)
                (root / package.PINNED_CAPTURE['capture/uu-pipewire-native-probe']).write_bytes(b'new incompatible build')
                self.assertEqual(producer.read_bytes(), before)
                self.assertTrue(package.verify(bundle)['native_capture_producer_included'])
                newer = package.package(root / 'releases', with_input=True)
                self.assertNotEqual(newer['release_id'], release['release_id'])
                self.assertEqual(producer.read_bytes(), before)

    def test_modified_or_missing_released_producer_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            with patch.object(package, 'ROOT', root):
                release = package.package(root / 'releases', with_input=True)
                bundle = Path(release['bundle'])
                producer = bundle / 'capture/uu-pipewire-native-probe'
                producer.write_bytes(b'changed')
                with self.assertRaises(ValueError):
                    package.verify(bundle)
                producer.unlink()
                with self.assertRaises(OSError):
                    package.verify(bundle)


if __name__ == '__main__':
    unittest.main()
