import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_display_config', ROOT / 'scripts/native_display_config.py')
display = importlib.util.module_from_spec(spec); spec.loader.exec_module(display)


def reply():
    spec = ('DP-fixture', 'private-vendor', 'private-product', 'private-serial')
    return (7, [(spec, [('1080p', 1920, 1080, 60., 1., [1., 2.], {'is-current': True}),
                        ('4k17', 3840, 2160, 17., 2., [1., 2., 3., 4.], {})], {})],
            [(0, 0, 1., 0, True, [spec], {})], {'layout-mode': 1})


def multi_reply(virtual_size=(1280, 720), virtual_serial=''):
    spec = ('DP-fixture', 'private-vendor', 'private-product', 'private-serial')
    virtual = ('Meta-0', 'MetaVendor', 'Virtual remote monitor', '0x000001')
    return (8, [(spec, [('1080p', 1920, 1080, 60., 1., [1.], {'is-current': True}),
                        ('4k17', 3840, 2160, 17., 1., [1.], {})], {}),
                (virtual, [('virtual', virtual_size[0], virtual_size[1], 60., 1., [1.],
                            {'is-current': True})], {'display-name': 'Virtual ' + virtual_serial})],
            [(0, 0, 1., 0, True, [spec], {}),
             (1920, 0, 1., 0, False, [virtual], {})], {'layout-mode': 1})


class FakeBackend:
    def __init__(self):
        self.state = display.decode_state(reply())
        self.calls = []
        self.fail = False

    def current(self):
        return copy.deepcopy(self.state)

    def configure(self, state, target, verify_only):
        if state['serial'] != self.state['serial']:
            raise RuntimeError('stale compositor serial')
        self.calls.append(verify_only)
        if self.fail:
            raise RuntimeError('lost reply')
        if not verify_only:
            self.state.update(mode=target['mode'], scale=target['scale'], serial=self.state['serial'] + 1)


class DisplayTests(unittest.TestCase):
    def test_144_hz_current_mode_remains_visible_and_selectable(self):
        data = copy.deepcopy(reply())
        modes = data[1][0][1]
        modes[0] = ('1080p144', 1920, 1080, 144., 1., [1., 2.], {'is-current': True})
        state = display.decode_state(data)
        self.assertEqual(display.choose(state, 1920, 1080, 144)['refresh'], 144.)
        self.assertEqual(display.decode_monitors(data)[0]['refresh'], 144.)
        modes[0] = ('1080p145', 1920, 1080, 145., 1., [1.], {'is-current': True})
        with self.assertRaises(ValueError):
            display.decode_state(data)

    def test_extra_output_is_visible_but_never_rewritten_by_single_output_backend(self):
        state = display.decode_state(multi_reply())
        self.assertTrue(state['multi_output'])
        self.assertEqual(state['connector'], 'DP-fixture')
        self.assertEqual(len(state['topology']), 2)
        self.assertEqual(state['topology'][1]['connector'], 'Meta-0')
        monitors = display.decode_monitors(multi_reply())
        self.assertEqual([item['gdi_name'] for item in monitors], ['\\\\.\\DISPLAY1', '\\\\.\\DISPLAY2'])
        self.assertEqual([item['index'] for item in monitors], [0, 1])
        self.assertEqual(monitors[1]['connector'], 'Meta-0')
        self.assertTrue(monitors[1]['virtual'])
        self.assertEqual((monitors[1]['x'], monitors[1]['y']), (1920, 0))
        self.assertNotIn('private-serial', str(monitors))
        self.assertTrue(monitors[0]['modes'])
        self.assertNotIn('is-current', monitors[0]['modes'][0])
        backend = display.MutterDisplay.__new__(display.MutterDisplay)
        with self.assertRaises(ValueError):
            backend.configure(state, dict(mode='4k17', scale=1.), True)
        # Adding/removing the second output invalidates an in-flight
        # transaction, but decoding the origin output still succeeds.
        other = display.decode_state(multi_reply(virtual_size=(1920, 1080)))
        self.assertNotEqual(display.signature(state), display.signature(other))
        self.assertEqual(display.decode_state(multi_reply())['connector'], 'DP-fixture')

    def test_real_advertised_rates_and_scales_only_no_serial_export(self):
        state = display.decode_state(reply())
        self.assertNotIn('private-serial', str(state))
        self.assertEqual(display.choose(state, 3840, 2160)['refresh'], 17.)
        for values in [(3840, 2160, 60, 1), (1920, 1080, 60, 1.25), (1234, 5678, 0, 1)]:
            with self.assertRaises(ValueError):
                display.choose(state, *values)

    def test_validate_never_applies_then_exact_serial_rollback(self):
        backend = FakeBackend(); transaction = display.DisplayTransaction(backend)
        before = backend.current()
        plan = transaction.prepare(7, 3840, 2160, 17, 2)
        self.assertEqual(backend.calls, [True])
        self.assertEqual(backend.current(), before)
        self.assertEqual(transaction.apply(plan), {'changed': True, 'serial': 8})
        self.assertTrue(transaction.rollback())
        self.assertEqual(backend.state['mode'], before['mode'])
        self.assertEqual(backend.state['scale'], before['scale'])

    def test_later_local_change_is_never_rolled_back(self):
        backend = FakeBackend(); transaction = display.DisplayTransaction(backend)
        transaction.apply(transaction.prepare(7, 3840, 2160))
        backend.state['serial'] += 1
        before = backend.current(); calls = len(backend.calls)
        self.assertFalse(transaction.rollback())
        self.assertEqual(backend.current(), before)
        self.assertEqual(len(backend.calls), calls)

    def test_stale_plan_and_double_transaction_are_rejected(self):
        backend = FakeBackend(); transaction = display.DisplayTransaction(backend)
        plan = transaction.prepare(7, 3840, 2160)
        backend.state['serial'] += 1
        with self.assertRaises(RuntimeError):
            transaction.apply(plan)
        plan = transaction.prepare(8, 3840, 2160)
        transaction.apply(plan)
        with self.assertRaises(RuntimeError):
            transaction.prepare(9, 1920, 1080)
        transaction.confirm(9)
        self.assertIsNone(transaction.pending)

    def test_ambiguous_write_is_not_replayed_or_blindly_reversed(self):
        backend = FakeBackend(); transaction = display.DisplayTransaction(backend)
        plan = transaction.prepare(7, 3840, 2160); backend.fail = True
        with self.assertRaises(RuntimeError):
            transaction.apply(plan)
        self.assertEqual(backend.calls, [True, False])
        self.assertIsNotNone(transaction.pending)
        with self.assertRaises(RuntimeError):
            transaction.rollback()
        self.assertEqual(backend.calls, [True, False])

    def test_same_mode_does_not_trigger_reconnect(self):
        backend = FakeBackend(); transaction = display.DisplayTransaction(backend)
        self.assertEqual(transaction.apply(transaction.prepare(7, 1920, 1080)), {'changed': False, 'serial': 7})
        self.assertEqual(backend.calls, [True])


if __name__ == '__main__':
    unittest.main()
