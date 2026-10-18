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
        snapshot_patch = patch.object(console, "_collect_snapshot", side_effect=lambda connector=None: copy.deepcopy(self.snapshot))
        snapshot_patch.start(); self.addCleanup(snapshot_patch.stop)
        update_patch = patch.object(console, '_update_command', return_value=dict(
            installed=None, installable=False, busy=False, progress=[]))
        update_patch.start(); self.addCleanup(update_patch.stop)
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

    def test_reset_image_keeps_explicit_default_for_service_restart(self):
        custom = self.config / "custom.png"
        custom.write_bytes(b"fixture image")
        console._write_json(self.config / "desktop-image.json", {"version": 1, "path": str(custom)})
        self.ui.reset_image()
        saved = console._read_json(self.config / "desktop-image.json")
        self.assertEqual(Path(saved["path"]).resolve(), console.DEFAULT_IMAGE.resolve())
        self.assertTrue(self.ui.restart_revealer.get_reveal_child())
        self.assertIn("设备封面", self.ui.restart_label.get_text())

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

    def test_secondary_output_is_pinned_through_apply_and_confirmation(self):
        monitors = [dict(index=0, connector='screen-a', friendly='A'),
                    dict(index=1, connector='screen-b', friendly='B')]
        self.snapshot['display'] = dict(self.display, monitors=monitors)
        requests = []
        selected = dict(self.display, monitors=monitors, output=1)
        def snapshot(connector=None):
            value = copy.deepcopy(self.snapshot)
            if connector == 'screen-b':
                value['display'] = copy.deepcopy(selected)
            return value
        def request(runtime, payload):
            requests.append(payload)
            if payload['op'] == 'apply':
                selected.update(serial=2, pending=True, width=2560, height=1440)
                return dict(transaction='secondary-trial', confirmation_seconds=30)
            selected['pending'] = False
            return dict(ok=True)
        with patch.object(console, '_collect_snapshot', side_effect=snapshot), \
                patch.object(console, '_display_request', side_effect=request), \
                patch.object(self.ui, '_confirm', return_value=True):
            self.ui.refresh()
            self.ui.display_outputs.set_active_id('screen-b')
            self.assertEqual(self.ui.display_output, 1)
            self.ui.display_modes.set_active(1)
            self.ui.display_apply()
            self.assertEqual(requests[0]['output'], 1)
            self.assertFalse(self.ui.display_outputs.get_sensitive())
            self.ui.keep_button.clicked()
            self.assertEqual(requests[1], dict(version=1, op='confirm', output=1,
                                              transaction='secondary-trial', serial=2))

    def test_super_screen_controls_use_service_and_do_not_restart_bridge(self):
        self.snapshot['display'] = dict(self.display, virtual_displays=dict(supported=True, screens=[]))
        self.ui.refresh()
        self.ui.virtual_resolution.set_active(1)
        requests = []
        def request(runtime, payload):
            requests.append(payload)
            return dict(ok=True, created=True, slot=0, connector='Meta-fixture')
        with patch.object(console, '_display_request', side_effect=request), \
                patch.object(console.subprocess, 'run') as run:
            self.ui.virtual_create_button.clicked()
            run.assert_not_called()
        self.assertEqual(requests[0]['op'], 'virtual-create')
        self.assertEqual(requests[0]['modes'][0], [2560, 1440, 60])
        self.assertEqual(requests[0]['slot'], 0)
        self.ui.display_transaction = 'pending-trial'
        self.ui.virtual_action()
        self.assertEqual(len(requests), 1)

    def test_update_manual_check_and_auto_switch_never_install(self):
        latest = dict(installed='1.2.0',latest='1.3.0',available=True,installable=True,checked_at=100,
                      url='https://github.com/GaryOAO/UUWay/releases/tag/v1.3.0')
        with patch.object(console, '_update_command', return_value=latest) as command:
            self.ui.update_check_button.clicked()
            command.assert_called_once_with('check')
            self.assertTrue(self.ui.update_install_button.get_sensitive())
            self.assertIn('1.3.0',self.ui.update_version.get_text())
            self.ui.update_auto.set_active(False)
            self.assertFalse(console._read_json(self.config/'console-updates.json')['auto_check'])
            self.ui.update_auto.set_active(True)
            self.assertTrue(console._read_json(self.config/'console-updates.json')['auto_check'])
            self.assertEqual(command.call_count,1)

    def test_update_install_requires_the_gui_confirmation(self):
        self.ui.latest_update=dict(available=True,installable=True)
        with patch.object(self.ui,'_confirm',return_value=False), patch.object(console,'_update_command') as command:
            self.ui.install_update()
            command.assert_not_called()
        with patch.object(self.ui,'_confirm',return_value=True), patch.object(console,'_update_command',return_value='done') as command, \
                patch.object(self.ui,'refresh_update_status'):
            self.ui.install_update()
            command.assert_called_once_with('install',machine=False,install=True)

    def test_automatic_new_version_prompts_once_and_cancel_never_installs(self):
        latest = dict(installed='1.2.0',latest='1.3.0',tag='v1.3.0',available=True,installable=True)
        with patch.object(console,'_update_command',return_value=latest) as command, \
                patch.object(self.ui,'_confirm',return_value=False) as confirm:
            self.ui.check_update(automatic=True)
            self.ui.check_update(automatic=True)
            self.assertEqual([c.args[0] for c in command.call_args_list],['check','check'])
            confirm.assert_called_once()
            self.assertIn('1.3.0',confirm.call_args.args[0])
        self.assertEqual(console._read_json(self.config/'console-updates.json')['notified_tag'],'v1.3.0')

    def test_automatic_update_starts_only_after_accepting_the_notice(self):
        latest = dict(installed='1.2.0',latest='1.3.0',tag='v1.3.0',available=True,installable=True)
        with patch.object(console,'_update_command',side_effect=[latest,'started']) as command, \
                patch.object(self.ui,'_confirm',return_value=True) as confirm, \
                patch.object(self.ui,'refresh_update_status'):
            self.ui.check_update(automatic=True)
            self.assertEqual([c.args[0] for c in command.call_args_list],['check','install'])
            confirm.assert_called_once()

    def test_external_update_disables_actions_until_it_finishes(self):
        self.ui.latest_update = dict(installed='1.2.0', latest='1.3.0', available=True, installable=True)
        with patch.object(console, '_update_command', return_value=dict(
                busy=True, installed='1.2.0', installable=True, progress=['正在安装'])):
            self.ui.refresh_update_status()
        self.assertFalse(self.ui.update_install_button.get_sensitive())
        self.assertFalse(self.ui.update_check_button.get_sensitive())
        with patch.object(console, '_update_command') as command:
            self.ui.install_update()
            self.ui.check_update()
            command.assert_not_called()
        with patch.object(console, '_update_command', return_value=dict(
                busy=False, installed='1.3.0', installable=True, progress=[], result=dict(outcome='success'))):
            self.ui.refresh_update_status()
        self.assertIn('1.3.0', self.ui.update_version.get_text())
        self.assertTrue(self.ui.update_check_button.get_sensitive())
        self.assertFalse(self.ui.update_install_button.get_sensitive())

    def test_auto_check_obeys_preference_interval_and_failed_attempt_backoff(self):
        now = time.time()
        latest = dict(installed='1.2.0', latest='1.3.0', available=True, installable=True, checked_at=now)
        self.ui.latest_update = dict(latest)
        with patch.object(self.ui, 'refresh_update_status'), patch.object(console, '_update_command', return_value=latest) as command:
            self.ui._poll_updates()
            command.assert_not_called()
            self.ui.latest_update['checked_at'] = now - console.UPDATE_INTERVAL - 10
            self.ui.update_auto.set_active(False)
            self.ui._poll_updates()
            command.assert_not_called()
            self.ui.update_auto.set_active(True)
            command.side_effect = ValueError('offline')
            self.ui._poll_updates()
            command.assert_called_once_with('check')
            self.ui._poll_updates()
            self.assertEqual(command.call_count, 1)
        self.assertIn('检查失败', self.ui.update_result.get_text())

    def test_rollback_uses_existing_updater_after_confirmation(self):
        self.ui.update_rollback_available = True
        with patch.object(self.ui, '_confirm', return_value=True), \
                patch.object(console, '_update_command', return_value='done') as command, \
                patch.object(self.ui, 'refresh_update_status'):
            self.ui.install_update(rollback=True)
            command.assert_called_once_with('rollback', machine=False, install=True)

    def test_auto_check_switch_reverts_if_preference_cannot_be_saved(self):
        self.assertTrue(self.ui.update_auto.get_active())
        with patch.object(console, '_write_json', side_effect=OSError('full')):
            self.ui.update_auto.set_active(False)
        self.assertTrue(self.ui.update_auto.get_active())

    def test_corrupt_update_preferences_and_cache_do_not_prevent_startup(self):
        console._write_json(self.config / 'console-update-cache.json', ['broken'])
        console._write_json(self.config / 'console-updates.json', ['broken'])
        other = console.Console(Gtk, GLib, Gio)
        other._async = self.inline
        try:
            other.build(self.app)
            self.assertTrue(other.update_auto.get_active())
            self.assertFalse(other.update_install_button.get_sensitive())
        finally:
            other.window.destroy()

    def test_output_reset_releases_super_screens_and_disables_old_test_outputs(self):
        outputs=[dict(connector='DP-1',enabled=True,kind='physical',managed=False),
                 dict(connector='Virtual-1',enabled=True,kind='vkms',managed=False),
                 dict(connector='Meta-0',enabled=True,kind='super_screen',managed=True,slot=0)]
        self.snapshot['display']=dict(self.display, virtual_displays=dict(supported=True,outputs=outputs,
                                        screens=[dict(slot=0,connector='Meta-0')]))
        self.ui.refresh()
        self.assertEqual(len(self.ui.output_list.get_children()),3)
        requests=[]
        with patch.object(console,'_display_request',side_effect=lambda runtime,request: requests.append(request) or dict(ok=True)):
            self.ui.virtual_reset_button.clicked()
        self.assertEqual([r['op'] for r in requests],['virtual-release','virtual-output'])
        self.assertEqual(requests[1]['connector'],'Virtual-1')
        self.assertFalse(requests[1]['enabled'])

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
            # GTK's CSD shadows can add 102 px on each side under Xvfb.
            # Child coordinates include that shadow; compare to the actual
            # content edge, rather than treating it as usable window space.
            content = self.ui.window.get_child()
            origin, _ = content.translate_coordinates(self.ui.window, 0, 0)
            self.assertLessEqual(x + widget.get_allocated_width(), origin + content.get_allocated_width(), name)
            self.assertEqual(self.ui.window.get_size().width, 780)
        self.assertLess(self.ui.sidebar.get_allocated_width(), 100)
        self.assertFalse(self.ui.nav_labels[0].get_visible())
        self.ui.window.resize(1120, 800)
        settle(.25)
        self.assertTrue(self.ui.nav_labels[0].get_visible())


if __name__ == "__main__":
    unittest.main()
