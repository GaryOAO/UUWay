import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mutter_stage', ROOT / 'scripts/stage-mutter-capture.py')
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


class MutterPrivateToolTests(unittest.TestCase):
    def test_selected_display_mutation_requires_private_runtime_and_pid(self):
        source = (ROOT / 'tests/probes/mutter_selected_display.py').read_text()
        self.assertLess(source.index("runtime.parent.parent != Path('/tmp')"), source.index('transaction.apply('))
        self.assertLess(source.index('owner_pid != compositor_pid'), source.index('transaction.apply('))
        self.assertIn('transaction.rollback()', source)
        self.assertIn('uu_super_screen_tested=False', source)
        self.assertNotIn('/run/user/1000', source)

    def test_symbol_inventory_keeps_names_and_ignores_empty_lines(self):
        with patch.object(stage.subprocess, 'check_output', return_value='\n0001 T public_a\n0002 D public_b\n'):
            self.assertEqual(stage.symbols(Path('/unused')), {'public_a', 'public_b'})

    def test_private_build_dependencies_are_hash_and_version_pinned(self):
        data = json.loads((ROOT / 'config/mutter-build-dependencies.json').read_text())
        names = []
        for name, version, architecture, digest, url in data['packages']:
            self.assertTrue(name and version)
            self.assertIn(architecture, ('amd64', 'all'))
            self.assertRegex(digest, r'^[0-9a-f]{64}$')
            self.assertTrue(url.startswith('https://'))
            names.append(name)
        self.assertEqual(len(names), len(set(names)))

    def test_build_script_is_syntactically_valid_and_does_not_install(self):
        script = ROOT / 'scripts/build-mutter-capture.sh'
        subprocess.run(['bash', '-n', str(script)], check=True, capture_output=True, timeout=5)
        source = script.read_text()
        self.assertIn('debian/patches/series', source)
        self.assertIn('--fuzz=0', source)
        self.assertIn('--wrap-mode=nofallback', source)
        self.assertLess(source.index('rg_path='), source.index('export PATH='))
        for forbidden in ('apt-get install', 'dpkg -i', 'systemctl restart', 'ninja install'):
            self.assertNotIn(forbidden, source)

    def test_private_capture_never_connects_to_login_runtime(self):
        source = (ROOT / 'tests/probes/mutter_private_virtual_capture.py').read_text()
        self.assertIn('GetConnectionUnixProcessID', source)
        self.assertIn('owner != shell_pid', source)
        self.assertIn("runtime.parent.parent != Path('/tmp')", source)
        self.assertIn("remote.connect(str(runtime / 'pipewire-0'))", source)
        self.assertNotIn('/run/user/1000', source)

    def test_gpu_stop_check_is_explicit_and_behind_private_owner_guard(self):
        source = (ROOT / 'tests/probes/mutter_private_virtual_capture.py').read_text()
        self.assertLess(source.index('owner != shell_pid'), source.index('return capture_stop('))
        runner = (ROOT / 'tests/probes/mutter_bundle_probe.py').read_text()
        self.assertIn("mode.add_argument('--capture-stop'", runner)
        self.assertIn("env['GIO_USE_VFS'] = 'local'", runner)
        probe = (ROOT / 'tests/probes/mutter_private_capture_stop.py').read_text()
        self.assertIn("runtime.parent.parent != Path('/tmp')", probe)
        self.assertIn('producer.send_signal(termination)', probe)
        self.assertIn("('h264', signal.SIGTERM), ('hevc', signal.SIGINT)", probe)
        self.assertIn('public_portal_tested=False', probe)
        self.assertNotIn('/run/user/1000', probe)

    def test_virtual_lifecycle_is_explicit_and_stays_after_private_owner_guards(self):
        source = (ROOT / 'tests/probes/mutter_private_virtual_capture.py').read_text()
        self.assertLess(source.index('owner != shell_pid'), source.index('return capture_lifecycle(bus, runtime)'))
        self.assertIn('output_survived_consumer_exit=True', source)
        self.assertIn('output_removed_with_session=True', source)
        runner = (ROOT / 'tests/probes/mutter_bundle_probe.py').read_text()
        self.assertIn('add_mutually_exclusive_group', runner)
        self.assertIn("'--virtual-lifecycle'", runner)
        self.assertIn('lifecycle=virtual_lifecycle', runner)

    def test_shell_gate_loads_private_library_only_beside_the_mutter_it_was_built_for(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            system, bundle = directory / 'libmutter-clutter-14.so.0.0.0', directory / 'bundle'
            system.write_bytes(b'clutter 46.2-1ubuntu0.24.04.16')
            bundle.mkdir()
            shell = directory / 'gnome-shell'
            shell.write_text('#!/bin/sh\nprintf "%s|%s" "$LD_LIBRARY_PATH" "$*"\n')
            shell.chmod(0o755)
            gate = bundle / 'start-gnome-shell'
            gate.write_text(stage.gate_script(str(shell)))
            gate.chmod(0o755)
            digest = subprocess.check_output(['sha256sum', str(system)], text=True)
            (bundle / 'system-mutter.sha256').write_text(digest)
            run = lambda: subprocess.run([str(gate), '--mode=user'], capture_output=True, text=True, timeout=5,
                                         env=dict(os.environ, LD_LIBRARY_PATH=''))
            result = run()
            self.assertEqual(result.stdout, f'{bundle}|--mode=user')
            self.assertEqual(result.stderr, '')
            system.write_bytes(b'clutter 46.2-1ubuntu0.24.04.17')  # a Mutter update
            result = run()
            self.assertEqual(result.stdout, '|--mode=user')
            self.assertIn('system Mutter changed', result.stderr)
            (bundle / 'system-mutter.sha256').unlink()
            self.assertEqual(run().stdout, '|--mode=user')

    def test_staging_pins_every_system_mutter_library_and_the_package_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            changelog = Path(temporary) / 'changelog'
            changelog.write_text('mutter (46.2-1ubuntu0.24.04.16) noble; urgency=medium\n\n  * Fixes.\n')
            self.assertEqual(stage.debian_version(changelog), '46.2-1ubuntu0.24.04.16')
        self.assertEqual(stage.system_library('libmutter-14.so.0.0.0'),
                         Path('/usr/lib/x86_64-linux-gnu/libmutter-14.so.0.0.0'))
        self.assertEqual(stage.system_library('libmutter-clutter-14.so.0.0.0'),
                         Path('/usr/lib/x86_64-linux-gnu/mutter-14/libmutter-clutter-14.so.0.0.0'))
        source = (ROOT / 'scripts/stage-mutter-capture.py').read_text()
        self.assertLess(source.index("parser.error(f'Build is Mutter"), source.index('output.mkdir('))
        self.assertIn('for name in LIBRARIES}', source)
        build = (ROOT / 'scripts/build-mutter-capture.sh').read_text()
        self.assertLess(build.index("dpkg-query -W -f='${Version}' libmutter-14-0"), build.index('curl --fail'))
        self.assertNotIn('0ubuntu0.24.04.', build)
        review = json.loads((ROOT / 'config/mutter-capture-review.json').read_text())
        self.assertIn(review['installed_package_version'], review['debian_patches_url'])

    def test_staging_is_core_only_and_never_overwrites_a_bundle(self):
        source = (ROOT / 'scripts/stage-mutter-capture.py').read_text()
        self.assertIn("action='store_true', default=True", source)
        self.assertIn('output.exists() or output.is_symlink()', source)
        self.assertIn('built_source_sha256', source)
        self.assertNotIn('systemctl', source)

    def test_eis_private_owner_guard_and_no_input_injection(self):
        source = (ROOT / 'tests/probes/mutter_private_virtual_capture.py').read_text()
        self.assertLess(source.index('input_owner != shell_pid'), source.index('return capture_lifecycle(bus, runtime, joint_input=True)'))
        self.assertIn("'ConnectToEIS'", source)
        self.assertIn('input_rebound_after_resize=input_rebound_after_resize', source)
        self.assertLess(source.index('controller.suspend()'),source.index("UURB_CAPTURE_SIZE=f'{width}x{height}'"))
        self.assertIn('public_portal_tested=False', source)
        self.assertIn('desktop_input_sent=False', source)
        self.assertNotIn('NotifyPointer', source)
        self.assertNotIn('NotifyKeyboard', source)
        self.assertIn("mode.add_argument('--virtual-eis'", (ROOT / 'tests/probes/mutter_bundle_probe.py').read_text())
