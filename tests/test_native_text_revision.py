import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('native_text_revision',
    Path(__file__).resolve().parents[1] / 'scripts/native_text_revision.py')
revision = importlib.util.module_from_spec(spec)
spec.loader.exec_module(revision)


class RevisionTests(unittest.TestCase):
    def test_unavailable_bus_never_enters_native_atspi_initialization(self):
        with patch.object(revision, 'accessibility_available', return_value=False), \
                patch.object(revision, 'Atspi') as native:
            with self.assertRaisesRegex(RuntimeError, 'accessibility bus unavailable'):
                revision.RevisionWitness()
            native.set_timeout.assert_not_called()
            native.EventListener.new.assert_not_called()

    def make(self, text='prefix候选'):
        witness = revision.RevisionWitness.__new__(revision.RevisionWitness)
        target = Mock()
        target.get_caret_offset.return_value = len(text)
        target.get_n_selections.return_value = 0
        target.get_text.side_effect = lambda start, end: text[start:end]
        witness.focus = target
        witness.focus_epoch = 1
        witness.current = lambda: target
        witness.eligible = lambda obj: obj is target
        witness.record = None
        witness.read_text = lambda obj, start, end: obj.get_text(start, end)
        witness.selected_range = lambda obj: obj.get_selection(0)
        return witness, target

    def test_credit_requires_actual_inserted_suffix_and_caret_advance(self):
        witness, target = self.make()
        before = (target, 1, 6, 6)
        self.assertTrue(witness.after(before, '候选'))
        self.assertEqual(witness.record['text'], '候选')
        witness.record = None
        target.get_caret_offset.return_value = 6
        self.assertFalse(witness.after(before, '候选'))
        self.assertIsNone(witness.record)

    def test_oversized_revision_never_selects_existing_field_prefix(self):
        witness, target = self.make()
        witness.after((target, 1, 6, 6), '候选')
        with self.assertRaisesRegex(RuntimeError, 'exceeds'):
            witness.select(3)
        target.add_selection.assert_not_called()

    def test_caret_move_or_expiration_refuses_before_selection(self):
        for changed in ('caret', 'time', 'focus', 'text'):
            witness, target = self.make()
            witness.after((target, 1, 6, 6), '候选')
            if changed == 'caret': target.get_caret_offset.return_value = 7
            if changed == 'time': witness.record['time'] = time.monotonic() - 3
            if changed == 'focus': witness.focus_epoch = 2
            if changed == 'text': target.get_text.side_effect = lambda start, end: 'changed'
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                witness.select(2)
            target.add_selection.assert_not_called()

    def test_selection_is_bounded_and_does_not_delete(self):
        witness, target = self.make()
        witness.after((target, 1, 6, 6), '候选')
        def select(start, end):
            target.get_n_selections.return_value = 1
            target.get_selection.return_value = Mock(start_offset=start, end_offset=end)
            return True
        target.add_selection.side_effect = select
        selected = witness.select(2)
        self.assertEqual(selected, (target, 1, 6, 8))
        target.add_selection.assert_called_once_with(6, 8)
        target.delete_text.assert_not_called()
        self.assertIsNone(witness.record)
        witness.cancel(selected)
        target.remove_selection.assert_called_once_with(0)
        target.set_caret_offset.assert_called_once_with(8)


if __name__ == '__main__':
    unittest.main()
