import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from tests.test_native_display_config import FakeBackend

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    spec = importlib.util.spec_from_file_location('native_display_guardian', ROOT / 'scripts/native_display_guardian.py')
    guardian = importlib.util.module_from_spec(spec); spec.loader.exec_module(guardian)
finally:
    sys.path.pop(0)


class Backend(FakeBackend):
    def identity(self):
        return ['fixture-boot', ':fixture.1']


class GuardianTests(unittest.TestCase):
    def test_uu_global_default_is_staged_rolled_back_or_committed_with_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            backend, instance = self.make(directory)
            request = dict(version=1, op='apply', serial=7, width=3840, height=2160,
                           default_scope='global', force_reset=True, confirmation='gpu_frame')
            result = instance.request(request)
            state = instance.request(dict(version=1, op='inspect'))
            self.assertEqual(state['registry']['width'], 3840)
            self.assertIsNone(guardian.read_private(instance.journal)['registry_default'])
            instance.deadline = 0
            self.assertEqual(instance.expire(), 'rolled_back')
            self.assertEqual(instance.request(dict(version=1, op='inspect'))['registry']['width'], 1920)
            request['serial'] = backend.state['serial']
            result = instance.request(request)
            instance.request(dict(version=1, op='confirm', serial=backend.state['serial'], transaction=result['transaction']))
            record = guardian.read_private(instance.journal)
            self.assertEqual(record['registry_default']['width'], 3840)
            self.assertEqual(record['registry_default']['scope'], 'global')
            self.assertIsNone(record['registry_candidate'])
            backend.identity = lambda: ['new-boot', ':new.1']
            recovered = guardian.DisplayGuardian(backend, instance.journal)
            self.assertEqual(recovered.registry_default['width'], 3840)
            self.assertIsNone(recovered.transaction.pending)

    def test_default_checkpoint_failure_does_not_apply_and_confirmation_failure_keeps_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            backend, instance = self.make(directory)
            request = dict(version=1, op='apply', serial=7, width=3840, height=2160, default_scope='user')
            with patch.object(guardian, 'write_private', side_effect=OSError):
                with self.assertRaises(guardian.DisplayDefaultWriteError):
                    instance.request(request)
            self.assertEqual(backend.calls, [True])
            self.assertIsNone(instance.registry_candidate)
            result = instance.request(request)
            with patch.object(guardian, 'write_private', side_effect=OSError):
                with self.assertRaises(guardian.DisplayDefaultWriteError):
                    instance.request(dict(version=1, op='confirm', serial=8, transaction=result['transaction']))
            self.assertIsNone(instance.registry_default)
            self.assertIsNotNone(instance.registry_candidate)
            instance.deadline = 0
            self.assertEqual(instance.expire(), 'rolled_back')
            self.assertIsNone(instance.registry_candidate)

    def test_reset_reapplies_current_mode_and_invalid_scopes_never_change_display(self):
        with tempfile.TemporaryDirectory() as directory:
            backend, instance = self.make(directory)
            request = dict(version=1, op='apply', serial=7, width=1920, height=1080)
            for extra in ({'default_scope':'linux-system'}, {'force_reset':1}, {'op':'verify','default_scope':'user'}):
                with self.assertRaises(ValueError):
                    instance.request(dict(request, **extra))
            self.assertEqual(backend.calls, [])
            result = instance.request(dict(request, force_reset=True, default_scope='global'))
            self.assertTrue(result['changed'])
            self.assertEqual(backend.calls, [True, False])
            instance.deadline = 0
            self.assertEqual(instance.expire(), 'rolled_back')

    def test_gpu_confirmation_policy_is_explicit_and_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            backend, instance = self.make(directory)
            instance.request(dict(version=1, op='apply', serial=7, width=3840, height=2160, confirmation='gpu_frame'))
            self.assertEqual(guardian.read_private(instance.journal)['confirmation_policy'], 'gpu_frame')
            instance.deadline = 0
            self.assertEqual(instance.expire(), 'rolled_back')
            instance.request(dict(version=1, op='apply', serial=backend.state['serial'], width=3840, height=2160))
            self.assertEqual(guardian.read_private(instance.journal)['confirmation_policy'], 'manual')

    def test_repeated_pending_mode_requests_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            backend, instance = self.make(directory)
            first = instance.request(dict(version=1, op='apply', serial=7,
                                          width=3840, height=2160, refresh=17,
                                          scale=2, confirmation='gpu_frame'))
            verify = instance.request(dict(version=1, op='verify', serial=8,
                                           width=3840, height=2160, refresh=17, scale=2))
            repeat = instance.request(dict(version=1, op='apply', serial=8,
                                           width=3840, height=2160, refresh=17,
                                           scale=2, confirmation='gpu_frame'))
            self.assertEqual(verify, {'verified': True, 'desktop_changed': True})
            self.assertEqual(repeat['transaction'], first['transaction'])
            self.assertEqual(repeat['serial'], 8)
            self.assertEqual(backend.calls, [True, False])

    def test_normal_apply_persists_confirmed_mode_as_owned_default(self):
        with tempfile.TemporaryDirectory() as directory:
            backend, instance = self.make(directory)
            result = self.apply(instance)
            self.assertEqual(instance.request(dict(version=1, op='inspect'))['registry']['width'], 3840)
            instance.request(dict(version=1, op='confirm', serial=backend.state['serial'],
                                  transaction=result['transaction']))
            record = guardian.read_private(instance.journal)
            self.assertEqual(record['registry_default']['width'], 3840)
            self.assertEqual(record['registry_default']['scope'], 'user')
            self.assertIsNone(record['registry_candidate'])

    def test_unknown_or_verify_confirmation_policy_cannot_mutate_display(self):
        with tempfile.TemporaryDirectory() as directory:
            backend, instance = self.make(directory)
            for op, policy in [('apply', 'unknown'), ('verify', 'gpu_frame')]:
                with self.assertRaises(ValueError):
                    instance.request(dict(version=1, op=op, serial=7, width=3840, height=2160, confirmation=policy))
            self.assertEqual(backend.calls, [])

    def make(self, directory):
        backend = Backend()
        return backend, guardian.DisplayGuardian(backend, Path(directory) / 'pending.json', timeout=5)

    def apply(self, instance):
        return instance.request(dict(version=1, op='apply', serial=7, width=3840, height=2160))

    def test_restart_restores_only_observed_owned_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            backend, instance = self.make(directory)
            result = self.apply(instance)
            self.assertTrue(result['changed'])
            recovered = guardian.DisplayGuardian(backend, instance.journal)
            self.assertEqual(recovered.expire(), 'rolled_back')
            self.assertEqual(backend.state['mode'], '1080p')
            self.assertIsNone(guardian.read_private(instance.journal)['pending'])

    def test_expiry_preserves_later_local_change(self):
        with tempfile.TemporaryDirectory() as directory:
            backend, instance = self.make(directory)
            self.apply(instance); backend.state['serial'] += 1
            calls = len(backend.calls); instance.deadline = 0
            self.assertEqual(instance.expire(), 'later_display_change_preserved')
            self.assertEqual(len(backend.calls), calls)

    def test_previous_compositor_record_cannot_reconfigure_new_desktop(self):
        with tempfile.TemporaryDirectory() as directory:
            backend, instance = self.make(directory); self.apply(instance)
            backend.identity = lambda: ['next-boot', ':fixture.1']
            calls = len(backend.calls)
            recovered = guardian.DisplayGuardian(backend, instance.journal)
            self.assertIsNone(recovered.transaction.pending)
            self.assertEqual(len(backend.calls), calls)

    def test_expired_or_stale_confirmation_cannot_cancel_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            backend, instance = self.make(directory); result = self.apply(instance)
            request = dict(version=1, op='confirm', transaction='wrong', serial=8)
            with self.assertRaises(ValueError):
                instance.request(request)
            request['transaction'] = result['transaction']; instance.deadline = 0
            with self.assertRaises(RuntimeError):
                instance.request(request)
            self.assertIsNotNone(instance.transaction.pending)

    def test_failed_durable_checkpoint_prevents_display_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            backend, instance = self.make(directory)
            with patch.object(guardian, 'write_private', side_effect=OSError):
                with self.assertRaises((OSError, guardian.DisplayDefaultWriteError)):
                    self.apply(instance)
            self.assertEqual(backend.calls, [True])
            self.assertEqual(backend.state['mode'], '1080p')
            self.assertIsNone(instance.transaction.pending)

    def test_failed_confirmation_checkpoint_retains_live_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            backend, instance = self.make(directory); result = self.apply(instance)
            with patch.object(guardian, 'write_private', side_effect=OSError):
                with self.assertRaises((OSError, guardian.DisplayDefaultWriteError)):
                    instance.request(dict(version=1, op='confirm', transaction=result['transaction'], serial=8))
            self.assertIsNotNone(instance.transaction.pending)
            instance.deadline = 0
            self.assertEqual(instance.expire(), 'rolled_back')


if __name__ == '__main__':
    unittest.main()
