#!/usr/bin/python3
"""Temporary visible color/motion target; no input injection or display changes.
CPU-drawn fixture content is composed by GNOME like any app; this is independent
of the capture/encode path under test. Automatically closes after 45 seconds.
"""
import argparse
import ctypes
import json
import signal
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frame-clock', action='store_true', help='Animate every GTK frame-clock tick (screen-refresh test)')
    parser.add_argument('--interval-ms', type=int, default=33, help='Timer-driven animation interval, 1–1000 ms')
    parser.add_argument('--virtual-output', action='store_true', help='Move only this fixture to a newly added Meta virtual monitor')
    parser.add_argument('--gpu', action='store_true', help='Draw the synthetic target with OpenGL clears, not CPU Cairo')
    parser.add_argument('--cursor-cycle', action='store_true',
                        help='Fullscreen cursor fixture: freeze desktop after three seconds, cycle cursor shapes')
    options = parser.parse_args()
    if not 1 <= options.interval_ms <= 1000:
        parser.error('interval must be from 1 to 1000 ms')
    window = Gtk.Window(title='UU GPU capture validation (temporary)')
    if options.cursor_cycle:
        if options.virtual_output:
            parser.error('Cursor-cycle fixture is for one existing output only')
        window.set_decorated(False)
        window.fullscreen()
    window.set_default_size(640, 360)
    window.set_resizable(options.virtual_output)
    drawing = Gtk.GLArea() if options.gpu else Gtk.DrawingArea()
    if options.gpu:
        drawing.set_required_version(3, 2)
        drawing.set_auto_render(False)
        gl = ctypes.CDLL('libGL.so.1')
        for name, args, result in [
            ('glGetString', [ctypes.c_uint], ctypes.c_char_p),
            ('glEnable', [ctypes.c_uint], None), ('glDisable', [ctypes.c_uint], None),
            ('glScissor', [ctypes.c_int] * 4, None),
            ('glClearColor', [ctypes.c_float] * 4, None), ('glClear', [ctypes.c_uint], None)]:
            getattr(gl, name).argtypes = args
            getattr(gl, name).restype = result
    window.add(drawing)
    display = Gdk.Display.get_default()
    focus_pointer = None
    cursor_evidence = dict(cursor_owned_ticks=0, cursor_changes=0, fullscreen_observed=False)
    if options.cursor_cycle:
        if display.get_n_monitors() != 1:
            raise RuntimeError('Cursor-cycle fixture requires one output')
        cycle_start = GLib.get_monotonic_time()
        cycle = [0]
        from pathlib import Path
        from native_text_wayland import OwnedFocusPointer
        focus_pointer = OwnedFocusPointer(Path(__file__).resolve().parents[2])

        def nudge_pointer():
            # No clicks or text. One tiny relative movement refreshes native
            # pointer focus after our fullscreen surface has mapped.
            focus_pointer.send([(1, 1, 0)])
            return False
        GLib.timeout_add(1000, nudge_pointer)

        def change_cursor():
            surface = drawing.get_window()
            if surface is not None:
                pointed, _, _ = display.get_default_seat().get_pointer().get_window_at_position()
                owned = pointed is not None and pointed.get_toplevel() == window.get_window()
                cursor_evidence['cursor_owned_ticks'] += int(owned)
                cursor_evidence['fullscreen_observed'] |= bool(window.get_window().get_state() & Gdk.WindowState.FULLSCREEN)
                shape = ('default', 'text', 'pointer', 'none')[cycle[0] % 4]
                surface.set_cursor(Gdk.Cursor.new_from_name(display, shape))
                cycle[0] += 1
                cursor_evidence['cursor_changes'] += 1
            return True
        GLib.timeout_add(400, change_cursor)
    if options.virtual_output:
        existing = {display.get_monitor(i) for i in range(display.get_n_monitors())}

        def select_virtual_monitor():
            # wl_output metadata may arrive after GDK's monitor-added signal.
            # Retry only newly added monitors; never move onto a user's old one.
            for index in range(display.get_n_monitors()):
                monitor = display.get_monitor(index)
                if (monitor not in existing and monitor.get_manufacturer() == 'MetaVendor'
                        and monitor.get_model() in ('MetaVirtualMonitor', 'Virtual remote monitor')):
                    window.fullscreen_on_monitor(window.get_screen(), index)
                    print(json.dumps(dict(fixture_virtual_monitor_selected=True,
                        fixture_monitor_geometry=[monitor.get_geometry().width,
                                                  monitor.get_geometry().height])), flush=True)
                    return False
            return True
        GLib.timeout_add(100, select_virtual_monitor)
    frame = 0
    drawn = 0
    report_start = GLib.get_monotonic_time()
    reported = 0

    def draw(widget, context):
        nonlocal drawn
        drawn += 1
        width, height = widget.get_allocated_width(), widget.get_allocated_height()
        for x, y, color in [(0, 0, (1, 0, 0)), (1, 0, (0, 1, 0)),
                            (0, 1, (0, 0, 1)), (1, 1, (1, 1, 1))]:
            context.set_source_rgb(*color)
            context.rectangle(x * width / 2, y * height / 2, width / 2, height / 2)
            context.fill()
        context.set_source_rgb(0, 0, 0)
        context.rectangle((frame * 7) % width, 0, 8, height)
        context.fill()
        if drawn == 1:
            print(json.dumps(dict(fixture_ready=True)), flush=True)
        return False

    def render(widget, context):
        nonlocal drawn
        if widget.get_error():
            raise RuntimeError(str(widget.get_error()))
        if not drawn:
            renderer = (gl.glGetString(0x1f01) or b'').decode('ascii', errors='replace')
            if not renderer or any(name in renderer.lower() for name in ('llvmpipe', 'softpipe', 'software')):
                raise RuntimeError('Hardware OpenGL fixture unavailable')
        drawn += 1
        scale = widget.get_scale_factor()
        width, height = widget.get_allocated_width() * scale, widget.get_allocated_height() * scale
        gl.glEnable(0x0c11)  # GL_SCISSOR_TEST; GL's origin is bottom-left.
        for x, y, color in [(0, 1, (1, 0, 0)), (1, 1, (0, 1, 0)),
                            (0, 0, (0, 0, 1)), (1, 0, (1, 1, 1))]:
            gl.glScissor(x * width // 2, y * height // 2, width // 2, height // 2)
            gl.glClearColor(*color, 1)
            gl.glClear(0x4000)
        gl.glScissor((frame * 7) % width, 0, 8, height)
        gl.glClearColor(0, 0, 0, 1)
        gl.glClear(0x4000)
        gl.glDisable(0x0c11)
        if drawn == 1:
            print(json.dumps(dict(fixture_ready=True, fixture_gpu_renderer=renderer)), flush=True)
        return True

    def tick(*unused):
        nonlocal frame
        if options.cursor_cycle and GLib.get_monotonic_time() - cycle_start > 3000000:
            return True
        frame += 1
        if options.gpu:
            drawing.queue_render()
        else:
            drawing.queue_draw()
        return True

    def report():
        nonlocal report_start, reported
        now = GLib.get_monotonic_time()
        print(json.dumps(dict(fixture_draws=drawn - reported,
            fixture_draw_fps=(drawn - reported) * 1000000 / (now - report_start),
            fixture_width=drawing.get_allocated_width(), fixture_height=drawing.get_allocated_height())), flush=True)
        report_start, reported = now, drawn
        return True

    drawing.connect('render', render) if options.gpu else drawing.connect('draw', draw)
    window.connect('destroy', Gtk.main_quit)
    if options.frame_clock:
        drawing.add_tick_callback(tick)
    else:
        GLib.timeout_add(options.interval_ms, tick)
    GLib.timeout_add_seconds(45, lambda: (window.destroy(), False)[1])
    window.show_all()
    signal.signal(signal.SIGTERM, lambda *_: GLib.idle_add(window.destroy))
    GLib.timeout_add(8000, report)
    try:
        Gtk.main()
    finally:
        if focus_pointer:
            focus_pointer.close()
        if options.cursor_cycle:
            print(json.dumps(dict(cursor_fixture=cursor_evidence)), flush=True)


if __name__ == '__main__':
    main()
