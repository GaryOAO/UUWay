"""Bounded AT-SPI witness for replacing only text this daemon just inserted.

Never reads a whole field or logs its contents. Unsupported/inaccessible or
password fields do not acquire deletion credit. Selection is not deletion:
the application replaces the validated range only when it accepts the paste.
"""
import time
import os
import threading
import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi, Gio, GLib


def accessibility_available():
    """Probe the existing accessibility bus before libatspi can abort on it.

    Never start/restart a bus or log its address. A successful probe is not a
    focus/text witness and cannot guarantee the bus will remain available.
    """
    cancelled = Gio.Cancellable()
    deadline = threading.Timer(0.5, cancelled.cancel)
    deadline.daemon = True
    connection = None
    deadline.start()
    try:
        address = os.environ.get('AT_SPI_BUS_ADDRESS')
        if not address:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, cancelled)
            # NO_AUTO_START deliberately leaves a stopped helper alone.
            address = bus.call_sync('org.a11y.Bus', '/org/a11y/bus', 'org.a11y.Bus',
                'GetAddress', None, GLib.VariantType.new('(s)'),
                Gio.DBusCallFlags.NO_AUTO_START, 400, cancelled).unpack()[0]
        if not isinstance(address, str) or not 0 < len(address) <= 4096 or not address.startswith('unix:'):
            return False
        connection = Gio.DBusConnection.new_for_address_sync(address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, cancelled)
        return not connection.is_closed()
    except GLib.Error:
        return False
    finally:
        if connection is not None:
            connection.close(None, None, None)
        deadline.cancel()
        deadline.join(timeout=0.6)


class RevisionWitness:
    MAX_CHARS = 2048
    WINDOW_SECONDS = 2

    def __init__(self):
        if not accessibility_available():
            raise RuntimeError('Existing accessibility bus unavailable')
        Atspi.set_timeout(100, 100)
        self.focus = None
        self.focus_epoch = 0
        self.record = None
        self.listener = Atspi.EventListener.new(self._focus_changed)
        if not self.listener.register('object:state-changed:focused'):
            raise RuntimeError('Focused-field notifications unavailable')

    def _focus_changed(self, event, *unused):
        if (event.detail1 and self.focus == event.source) or (not event.detail1 and self.focus != event.source):
            return
        self.focus_epoch += 1
        self.record = None
        self.focus = event.source if event.detail1 else None

    @staticmethod
    def read_text(target, start, end):
        # Accessible.get_text/get_selection are old interface accessors which
        # shadow these Text methods in PyGObject. Invoke the Text interface.
        return Atspi.Text.get_text(target, start, end)

    @staticmethod
    def selected_range(target):
        return Atspi.Text.get_selection(target, 0)

    @staticmethod
    def eligible(accessible):
        if accessible is None or accessible.get_role() == Atspi.Role.PASSWORD_TEXT:
            return False
        state = accessible.get_state_set()
        # GTK 3 TextView supports selection without advertising SELECTABLE_TEXT.
        # Verify add_selection and its exact returned range at execution time.
        return all(state.contains(value) for value in (Atspi.StateType.FOCUSED, Atspi.StateType.EDITABLE))

    def current(self):
        if self.eligible(self.focus):
            return self.focus
        # Initial focus can predate listener registration. Discover only an
        # active window, bounded by count/depth/time, without reading any text.
        desktop = Atspi.get_desktop(0)
        deadline = time.monotonic() + 0.15
        visited = 0
        pending = [(desktop, 0, False)]
        while pending and visited < 512 and time.monotonic() < deadline:
            node, depth, active = pending.pop()
            visited += 1
            if node is None or depth > 16:
                continue
            if depth >= 2:
                active = active or node.get_state_set().contains(Atspi.StateType.ACTIVE)
                if not active:
                    continue
                if self.eligible(node):
                    self.focus = node
                    return node
            for i in range(min(node.get_child_count(), 64) - 1, -1, -1):
                pending.append((node.get_child_at_index(i), depth + 1, active))
        return None

    def before(self):
        try:
            target = self.current()
            if target is None:
                return None
            caret = target.get_caret_offset()
            count = target.get_n_selections()
            if caret < 0 or count not in (0, 1):
                return None
            start = end = caret
            if count:
                selection = self.selected_range(target)
                start, end = selection.start_offset, selection.end_offset
                if not 0 <= start <= end or caret not in (start, end):
                    return None
            return target, self.focus_epoch, start, end
        except (GLib.Error, AttributeError):
            return None

    def after(self, before, inserted):
        previous = self.record
        if before is None or not inserted or len(inserted) > self.MAX_CHARS:
            return False
        target, epoch, start, old_end = before
        try:
            if epoch != self.focus_epoch or self.current() != target or not self.eligible(target):
                return False
            end = start + len(inserted)
            if target.get_caret_offset() != end or target.get_n_selections() or self.read_text(target, start, end) != inserted:
                return False
            text = inserted
            if (previous and previous['target'] == target and previous['epoch'] == epoch and
                    previous['end'] == start == old_end and time.monotonic() - previous['time'] <= self.WINDOW_SECONDS):
                text = (previous['text'] + inserted)[-self.MAX_CHARS:]
                # Validate only our own retained suffix, not surrounding data.
                if self.read_text(target, end - len(text), end) != text:
                    text = inserted
            self.record = dict(target=target, epoch=epoch, end=end, text=text, time=time.monotonic())
            return True
        except (GLib.Error, AttributeError):
            return False

    def select(self, delete_before):
        record = self.record
        self.record = None  # Never reuse deletion credit after any attempt.
        if type(delete_before) is not int or not 0 < delete_before <= self.MAX_CHARS or not record:
            raise RuntimeError('No witnessed text revision credit')
        if time.monotonic() - record['time'] > self.WINDOW_SECONDS or record['epoch'] != self.focus_epoch:
            raise RuntimeError('Text revision credit expired or focus changed')
        target = record['target']
        if self.current() != target or not self.eligible(target) or target.get_n_selections():
            raise RuntimeError('Text revision target changed')
        if delete_before > len(record['text']):
            raise RuntimeError('Revision exceeds text inserted by this bridge')
        end = record['end']; start = end - delete_before
        if target.get_caret_offset() != end or self.read_text(target, start, end) != record['text'][-delete_before:]:
            raise RuntimeError('Old candidate no longer matches the focused field')
        if not target.add_selection(start, end):
            raise RuntimeError('Focused field refused revision selection')
        try:
            selected = self.selected_range(target)
            if (self.current() != target or record['epoch'] != self.focus_epoch or target.get_n_selections() != 1 or
                    selected.start_offset != start or selected.end_offset != end or
                    self.read_text(target, start, end) != record['text'][-delete_before:]):
                raise RuntimeError('Revision selection changed before paste')
        except BaseException:
            self.cancel((target, record['epoch'], start, end))
            raise
        return target, record['epoch'], start, end

    def cancel(self, selection):
        target, epoch, start, end = selection
        try:
            if self.current() != target or epoch != self.focus_epoch or target.get_n_selections() != 1:
                return
            actual = self.selected_range(target)
            if actual.start_offset == start and actual.end_offset == end:
                target.remove_selection(0)
                target.set_caret_offset(end)
        except (GLib.Error, AttributeError):
            pass

    def close(self):
        self.listener.deregister('object:state-changed:focused')
        self.record = None
        self.focus = None
