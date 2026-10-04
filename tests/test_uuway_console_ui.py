"""GTK interaction regressions. Run under Xvfb with UUWAY_UI_TESTS=1."""
import copy
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tests.test_uuway_console import console

Gtk = GLib = Gio = None
if os.environ.get("UUWAY_UI_TESTS") == "1":
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk, GLib, Gio
    except (ImportError, ValueError):
        pass


def settle(seconds=.06):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(.002)


@unittest.skipUnless(Gtk is not None, "GTK interaction tests require UUWAY_UI_TESTS=1 and an isolated display")
class ConsoleInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Gtk.Application(application_id="io.uuway.InteractionTest", flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Path(self.temporary.name)
        self.patch = patch.object(console, "CONFIG_DIR", self.config)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.runtime = dict(schema=1, prefix="/fixture/wine", bundle="/fixture/bundle",
                            restore_state="/fixture/restore", state_parent="/fixture/state",
                            text_socket="/fixture/text", cursor_mode="metadata")
        console._write_json(self.config / "native-runtime.json", self.runtime)
        self.display = dict(serial=1, width=1920, height=1080, refresh=60, scale=1, pending=False,
                            modes=[dict(width=1920, height=1080, refresh=60, scales=[1, 1.5, 2]),
                                   dict(width=2560, height=1440, refresh=60, scales=[1, 1.5, 2])])
        self.snapshot = dict(runtime=self.runtime, display=self.display, display_error=None, mappings=[],
                             download=Path("/fixture/Downloads"),
                             services={"uu-native-bridge.service": dict(ActiveState="active")})
        snapshot_patch = patch.object(console, "_collect_snapshot", side_effect=lambda: copy.deepcopy(self.snapshot))
        snapshot_patch.start(); self.addCleanup(snapshot_patch.stop)
        self.ui = console.Console(Gtk, GLib, Gio)
        self.ui._async = self.inline
        self.ui.build(self.app)
        self.addCleanup(self.ui.window.destroy)
        settle()

    @staticmethod
    def inline(work, done):
        try:
            value, error = work(), None
        except Exception as problem:
            value, error = None, problem
        done(value, error)

    def test_input_draft_survives_navigation_and_requires_save(self):
        self.ui.relative.set_value(175)
        self.ui.invert.set_active(True)
        self.ui.navigate("files")
        self.ui.refresh()
        self.ui.navigate("input")
        self.assertFalse((self.config / "input-settings.json").exists())
        self.assertTrue(self.ui.input_save.get_sensitive())
        self.ui.input_save.clicked()
        saved = console._read_json(self.config / "input-settings.json")
        self.assertEqual(saved["relative_percent"], 175)
        self.assertTrue(saved["invert_wheel"])
        self.assertFalse(self.ui.input_save.get_sensitive())
        self.assertFalse(self.ui.restart_revealer.get_reveal_child())

    def test_close_with_draft_can_be_cancelled(self):
        self.ui.wheel.set_value(140)
        with patch.object(self.ui, "_confirm", return_value=False):
            self.assertTrue(self.ui._close_requested())
        self.ui.save_input()
        self.assertFalse(self.ui._close_requested())

    def test_auto_save_rolls_back_selection_when_write_fails(self):
        with patch.object(console, "_write_json", side_effect=OSError("fixture full disk")):
            self.ui.backend.set_active(1)
            self.ui.cursor.set_active(2)
        self.assertEqual(self.ui.backend.get_active(), 0)
        self.assertEqual(self.ui.cursor.get_active(), 1)
        self.assertFalse(self.ui.restart_revealer.get_reveal_child())

    def test_restart_notice_survives_navigation_and_polling(self):
        self.ui.backend.set_active(1)
        self.ui.navigate("overview")
        self.ui.refresh()
        self.assertTrue(self.ui.restart_revealer.get_reveal_child())
        self.assertIn("文字输入", self.ui.restart_label.get_text())
        self.assertEqual(console._read_json(self.config / "text-backend.json")["backend"], "fcitx")

    def test_poll_does_not_reset_unsaved_display_selection(self):
        self.ui.display_modes.set_active(1)
        self.ui.display_scales.set_active(1)
        self.ui.refresh()
        self.assertEqual(self.ui.display_modes.get_active(), 1)
        self.assertEqual(self.ui.display_scales.get_active(), 1)
        self.assertTrue(self.ui.display_apply_button.get_sensitive())
        self.ui.display_inspect()
        self.assertEqual(self.ui.display_modes.get_active(), 0)
        self.assertFalse(self.ui.display_apply_button.get_sensitive())

    def test_unavailable_display_disables_stale_apply(self):
        self.ui.display_modes.set_active(1)
        self.snapshot["display"] = None
        self.ui.refresh()
        self.assertFalse(self.ui.display_apply_button.get_sensitive())
        self.assertFalse(self.ui.display_modes.get_sensitive())
        self.assertIsNone(self.ui.display_state)

    def test_display_trial_requires_manual_confirmation_with_fresh_serial(self):
        requests = []
        def request(runtime, payload):
            requests.append(payload)
            if payload["op"] == "apply":
                self.snapshot["display"] = dict(self.display, serial=2, pending=True, width=2560, height=1440)
                return dict(transaction="fixture-trial", confirmation_seconds=30)
            self.snapshot["display"]["pending"] = False
            return {"ok": True}
        self.ui.display_modes.set_active(1)
        with patch.object(console, "_display_request", side_effect=request), patch.object(self.ui, "_confirm", return_value=True):
            self.ui.display_apply()
            self.assertNotIn("confirmation", requests[0])
            self.assertTrue(self.ui.confirm_revealer.get_reveal_child())
            self.assertTrue(self.ui.keep_button.get_sensitive())
            self.ui.keep_button.clicked()
        self.assertEqual(requests[1], dict(version=1, op="confirm", transaction="fixture-trial", serial=2))
        self.assertFalse(self.ui.confirm_revealer.get_reveal_child())

    def test_expired_trial_and_invalid_selection_cannot_be_confirmed(self):
        self.ui.display_transaction = "fixture-trial"
        self.ui.display_deadline = time.monotonic() - 1
        self.ui._tick_confirmation()
        self.assertFalse(self.ui.keep_button.get_sensitive())
        self.ui.display_modes.set_active(-1)
        self.assertIsNone(self.ui._display_choice())
        self.assertFalse(self.ui.display_apply_button.get_sensitive())
        self.ui.refresh()
        self.assertIsNone(self.ui.display_transaction)

    def test_stop_or_restart_cancellation_never_calls_systemctl(self):
        with patch.object(self.ui, "_confirm", return_value=False), patch.object(console.subprocess, "run") as run:
            self.ui.service_action(None, "stop")
            self.ui.service_action(None, "restart")
        run.assert_not_called()

    def test_refresh_coalesces_requests_and_rejects_pre_mutation_display(self):
        jobs = []
        self.ui._async = lambda work, done: jobs.append((work, done))
        self.ui.refresh()
        self.ui.refresh(force_display=True)
        self.assertEqual(len(jobs), 1)
        self.ui.display_generation += 1
        stale = copy.deepcopy(self.snapshot)
        stale["display"]["width"] = 999
        jobs[0][1](stale, None)
        self.assertEqual(self.ui.display_state["width"], 1920)
        settle()
        self.assertEqual(len(jobs), 2)
        jobs[1][1](copy.deepcopy(self.snapshot), None)
        self.assertFalse(self.ui.refreshing)

    def test_narrow_layout_keeps_settings_inside_window(self):
        self.ui.window.resize(780, 600)
        for name, widget in (("input", self.ui.relative), ("display", self.ui.display_modes),
                             ("files", self.ui.download_label)):
            self.ui.navigate(name)
            settle(.25)
            x, y = widget.translate_coordinates(self.ui.window, 0, 0)
            self.assertLessEqual(x + widget.get_allocated_width(), 780, name)
        self.assertLess(self.ui.sidebar.get_allocated_width(), 100)
        self.assertFalse(self.ui.nav_labels[0].get_visible())
        self.ui.window.resize(1120, 800)
        settle(.25)
        self.assertTrue(self.ui.nav_labels[0].get_visible())


if __name__ == "__main__":
    unittest.main()
