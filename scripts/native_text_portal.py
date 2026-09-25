"""Authorized public Portal clipboard/text backend; no RDP or screen capture.

Text and preserved clipboard representations stay in process memory. The
backend owns a separate keyboard+clipboard Portal session and restore token,
never RustDesk's or the video producer's session. Calling close removes only
this session; as with every clipboard owner, a process exit may invalidate
content still owned by it. Keep this backend alive across UU reconnections.
"""
import importlib.util
import os
from pathlib import Path
import secrets
import time
from gi.repository import Gio, GLib

SERVICE = 'org.freedesktop.portal.Desktop'
PATH = '/org/freedesktop/portal/desktop'
REMOTE = 'org.freedesktop.portal.RemoteDesktop'
CLIPBOARD = 'org.freedesktop.portal.Clipboard'
spec = importlib.util.spec_from_file_location('text_restore', Path(__file__).with_name('probe-wayland-portal.py'))
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


class TextPortal:
    MAX_BYTES = 16 * 1024 * 1024
    MAX_TYPES = 32
    CONTROL_TYPES = {'TARGETS', 'MULTIPLE', 'TIMESTAMP', 'SAVE_TARGETS'}

    def __init__(self, state_path, authorization_timeout=120):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.session = None
        self.state_path = state_path
        self.authorization_timeout = authorization_timeout
        self.types = []
        self.types_known = True
        self.owner = False
        self.owner_epoch = 0
        self.payloads = {}
        self.transfers = {}
        self.completed = 0
        self.subscriptions = []
        self.closed = False
        self.busy = False
        self.phase = 'idle'
        self.revisions = None
        self.witnessed = False

    def clipboard_call(self, method, signature, values, with_fd=False):
        service, path, interface = SERVICE, PATH, CLIPBOARD
        signature = 'o' + signature
        values = (self.session, *values)
        arguments = GLib.Variant('(' + signature + ')', values)
        if with_fd:
            return self.bus.call_with_unix_fd_list_sync(service, path, interface, method, arguments,
                GLib.VariantType.new('(h)'), Gio.DBusCallFlags.NONE, 1000, None, None)
        return self.bus.call_sync(service, path, interface, method, arguments, None,
            Gio.DBusCallFlags.NONE, 1000, None)

    def call(self, interface, method, signature=None, values=(), path=PATH):
        return self.bus.call_sync(SERVICE, path, interface, method,
            GLib.Variant(signature, values) if signature else None, None,
            Gio.DBusCallFlags.NONE, 3000, None)

    def pump_until(self, predicate, seconds):
        deadline = time.monotonic() + seconds
        context = GLib.MainContext.default()
        while not predicate() and not self.closed and time.monotonic() < deadline:
            # A bounded dispatch batch keeps timeouts effective under a flood.
            for _ in range(32):
                if not context.pending():
                    break
                context.iteration(False)
            if not predicate():
                time.sleep(0.002)
        return bool(predicate())

    def request(self, method, signature, values, options):
        token = 'uurb_text_' + secrets.token_hex(12)
        sender = self.bus.get_unique_name()[1:].replace('.', '_')
        path = f'{PATH}/request/{sender}/{token}'
        response = []
        subscription = self.bus.signal_subscribe(SERVICE, 'org.freedesktop.portal.Request', 'Response',
            path, None, Gio.DBusSignalFlags.NONE,
            lambda *args: response.append(args[5].unpack()), None)
        try:
            returned = self.call(REMOTE, method, signature,
                (*values, dict(options, handle_token=GLib.Variant('s', token)))).unpack()[0]
            if returned != path:
                raise RuntimeError('Unexpected Portal request handle')
            if not self.pump_until(lambda: bool(response), self.authorization_timeout):
                raise RuntimeError('Text Portal authorization timed out')
            status, result = response[0]
            if status:
                raise RuntimeError('Text Portal authorization cancelled or denied')
            return result
        finally:
            self.bus.signal_unsubscribe(subscription)
            if not response:
                try:
                    self.call('org.freedesktop.portal.Request', 'Close', path=path)
                except GLib.Error:
                    pass

    def start(self):
        if self.session:
            raise RuntimeError('Text Portal session already exists')
        remote = self.call('org.freedesktop.DBus.Properties', 'GetAll', '(s)', (REMOTE,)).unpack()[0]
        clipboard = self.call('org.freedesktop.DBus.Properties', 'GetAll', '(s)', (CLIPBOARD,)).unpack()[0]
        if remote.get('version', 0) < 2 or clipboard.get('version', 0) < 1 or not remote.get('AvailableDeviceTypes', 0) & 1:
            raise RuntimeError('Public keyboard/clipboard Portal interfaces unavailable')
        try:
            result = self.request('CreateSession', '(a{sv})', (),
                {'session_handle_token': GLib.Variant('s', 'uurb_text_' + secrets.token_hex(12))})
            self.session = result['session_handle']
            self.subscriptions.append(self.bus.signal_subscribe(SERVICE, 'org.freedesktop.portal.Session', 'Closed',
                self.session, None, Gio.DBusSignalFlags.NONE, self._closed, None))
            for name, callback in [('SelectionOwnerChanged', self._owner_changed), ('SelectionTransfer', self._transfer)]:
                # D-Bus arg0 string filtering does not match the Portal's
                # object-path argument. Filter the session inside callbacks.
                self.subscriptions.append(self.bus.signal_subscribe(SERVICE, CLIPBOARD, name, PATH, None,
                    Gio.DBusSignalFlags.NONE, callback, None))
            options = {'types': GLib.Variant('u', 1), 'persist_mode': GLib.Variant('u', 2)}
            token = restore.load_restore_token(self.state_path)
            if token:
                options['restore_token'] = GLib.Variant('s', token)
            self.request('SelectDevices', '(oa{sv})', (self.session,), options)
            self.call(CLIPBOARD, 'RequestClipboard', '(oa{sv})', (self.session, {}))
            result = self.request('Start', '(osa{sv})', (self.session, ''), {})
            if result.get('devices') != 1 or result.get('clipboard_enabled') is not True or result.get('streams'):
                raise RuntimeError('Expected only authorized keyboard and clipboard, no capture')
            if result.get('restore_token'):
                restore.save_restore_token(self.state_path, result['restore_token'])
            # Drain the initial owner notification; an empty clipboard need not
            # emit one. Never claim/read its content during authorization alone.
            self.pump_until(lambda: self.owner_epoch > 0, 0.1)
            try:
                from native_text_revision import RevisionWitness
                self.revisions = RevisionWitness()
            except (ImportError, GLib.Error, RuntimeError):
                self.revisions = None  # Pure text still works; revisions fail closed.
        except BaseException:
            self.close()
            raise

    def _closed(self, *args):
        self.closed = True

    def _owner_changed(self, *args):
        session, options = args[5].unpack()
        if session != self.session:
            return
        self.owner_epoch += 1
        self.owner = bool(options.get('session_is_owner', False))
        self.types = options.get('mime_types', [])
        # GNOME 46 wraps the list in '(as)', including through the public
        # Portal. Newer backends use the documented 'as'. Normalize only those
        # two shapes, never treat omitted formats on an existing owner as empty.
        if isinstance(self.types, tuple) and len(self.types) == 1 and isinstance(self.types[0], list):
            self.types = self.types[0]
        self.types_known = 'mime_types' in options or not options
        if not self.owner:
            self.payloads = {}

    def _transfer(self, *args):
        session, mime, serial = args[5].unpack()
        if session != self.session:
            return
        data = self.payloads.get(mime)
        if data is None or len(self.transfers) >= self.MAX_TYPES:
            self.clipboard_call('SelectionWriteDone', 'ub', (serial, False))
            return
        try:
            reply, fds = self.clipboard_call('SelectionWrite', 'u', (serial,), with_fd=True)
            fd = fds.get(reply.unpack()[0])
            os.set_blocking(fd, False)
            self.transfers[serial] = [fd, data, 0, time.monotonic() + 2]
            self._write(serial)
        except (GLib.Error, OSError):
            self._finish_transfer(serial, False)

    def _write(self, serial):
        transfer = self.transfers.get(serial)
        if transfer is None:
            return False
        fd, data, offset, deadline = transfer
        try:
            if time.monotonic() >= deadline:
                self._finish_transfer(serial, False)
                return False
            if offset < len(data):
                written = os.write(fd, data[offset:offset + 65536])
                if not written:
                    raise OSError('Empty clipboard transfer write')
                transfer[2] += written
            if transfer[2] == len(data):
                self._finish_transfer(serial, True)
                return False
        except BlockingIOError:
            pass
        except OSError:
            self._finish_transfer(serial, False)
            return False
        GLib.timeout_add(2, self._write, serial)
        return False

    def _finish_transfer(self, serial, success):
        transfer = self.transfers.pop(serial, None)
        if transfer:
            os.close(transfer[0])
        try:
            self.clipboard_call('SelectionWriteDone', 'ub', (serial, success))
            if success:
                self.completed += 1
        except GLib.Error:
            pass

    def read(self, mime, limit, deadline):
        reply, fds = self.clipboard_call('SelectionRead', 's', (mime,), with_fd=True)
        fd = fds.get(reply.unpack()[0])
        os.set_blocking(fd, False)
        result = bytearray()
        try:
            while time.monotonic() < deadline and not self.closed:
                try:
                    block = os.read(fd, min(65536, limit - len(result) + 1))
                    if not block:
                        return bytes(result)
                    result.extend(block)
                    if len(result) > limit:
                        raise RuntimeError('Clipboard preservation bound exceeded')
                except BlockingIOError:
                    self.pump_until(lambda: False, 0.002)
            raise RuntimeError('Clipboard preservation timed out')
        finally:
            os.close(fd)

    def preserve(self):
        self.pump_until(lambda: False, 0.005)
        epoch = self.owner_epoch
        if self.owner:
            return dict(self.payloads), epoch
        if not self.types_known:
            raise RuntimeError('Clipboard owner omitted format metadata; refusing to overwrite')
        if not isinstance(self.types, list) or len(self.types) > self.MAX_TYPES:
            raise RuntimeError('Clipboard format count outside preservation bound')
        payloads = {}
        remaining = self.MAX_BYTES
        deadline = time.monotonic() + 1
        for mime in self.types:
            if not isinstance(mime, str) or not mime or len(mime) > 256:
                raise RuntimeError('Invalid clipboard format')
            if mime in self.CONTROL_TYPES:
                continue
            payloads[mime] = self.read(mime, remaining, deadline)
            remaining -= len(payloads[mime])
        self.pump_until(lambda: False, 0.005)
        if epoch != self.owner_epoch:
            raise RuntimeError('Clipboard owner changed during preservation')
        return payloads, epoch

    def offer(self, payloads):
        if not payloads:
            # Mutter's clipboard manager resurrects the last cached text when
            # an owner simply disappears. Replace it with a content-free,
            # non-cacheable selection before clearing an originally empty
            # clipboard; never leave committed text in its previous cache.
            self.offer({'application/x-uurb-empty': b''})
        self.payloads = dict(payloads)
        before = self.owner_epoch
        options = {'mime_types': GLib.Variant('as', list(payloads))} if payloads else {}
        self.clipboard_call('SetSelection', 'a{sv}', (options,))
        if not self.pump_until(lambda: self.owner_epoch != before, 0.3) or self.owner != bool(payloads):
            raise RuntimeError('Clipboard ownership was not confirmed')

    def key(self, code, pressed):
        self.call(REMOTE, 'NotifyKeyboardKeycode', '(oa{sv}iu)', (self.session, {}, code, int(pressed)))

    def commit(self, text, delete_before=0):
        self.witnessed = False
        if self.busy or self.closed or not self.session:
            raise RuntimeError('Text Portal not available')
        if not isinstance(text, str) or not text or len(text) > 2048 or '\0' in text:
            raise ValueError('Invalid bounded text commit')
        if type(delete_before) is not int or not 0 <= delete_before <= 2048:
            raise ValueError('Invalid bounded revision')
        if delete_before and self.revisions is None:
            raise RuntimeError('Focused-text revision validation unavailable')
        encoded = text.encode('utf-8', errors='strict')
        # Never synthesize Enter/Tab from text: they remain literal clipboard
        # bytes. This is a paste path, not a shell/terminal command executor.
        self.busy = True
        previous = None
        owned_epoch = None
        selection = None
        before_field = self.revisions.before() if self.revisions else None
        succeeded = False
        try:
            self.phase = 'preserve'
            previous, _ = self.preserve()
            self.phase = 'offer'
            self.offer({'text/plain;charset=utf-8': encoded, 'text/plain': encoded,
                        'UTF8_STRING': encoded})
            owned_epoch = self.owner_epoch
            if delete_before:
                self.phase = 'select_verified_candidate'
                selection = self.revisions.select(delete_before)
                before_field = selection
            if before_field is not None and (self.revisions.current() != before_field[0] or
                                             self.revisions.focus_epoch != before_field[1]):
                raise RuntimeError('Focused field changed while preparing paste')
            before = self.completed
            try:
                self.phase = 'paste'
                self.key(29, True)  # evdev LeftCtrl
                self.key(47, True)  # evdev V
            finally:
                # Release both even if one D-Bus response is lost. Never replay
                # a possibly delivered paste request.
                try:
                    self.key(47, False)
                finally:
                    self.key(29, False)
            self.phase = 'transfer'
            served = self.pump_until(lambda: self.completed > before or self.owner_epoch != owned_epoch, 1)
            if not served or self.owner_epoch != owned_epoch or self.completed <= before:
                raise RuntimeError('No clipboard consumer completed the text transfer')
            self.pump_until(lambda: False, 0.05)
            if self.revisions:
                witnessed = self.pump_until(lambda: self.revisions.after(before_field, text), 0.15)
                if not witnessed:
                    self.revisions.record = None
                self.witnessed = witnessed
            succeeded = True
            return True  # dispatch/transfer only; caller must not claim UI insertion
        finally:
            try:
                if not succeeded and self.revisions:
                    self.revisions.record = None
                    if selection:
                        self.revisions.cancel(selection)
                # Do not overwrite a copy made by the user/application while
                # the paste was pending. Restore MIME data, not stale ownership.
                if previous is not None and self.owner and (owned_epoch is None or owned_epoch == self.owner_epoch):
                    self.offer(previous)
            finally:
                self.busy = False

    def close(self):
        if self.revisions:
            self.revisions.close()
            self.revisions = None
        for serial in list(self.transfers):
            self._finish_transfer(serial, False)
        if self.session:
            try:
                self.call('org.freedesktop.portal.Session', 'Close', path=self.session)
            except GLib.Error:
                pass
        self.closed = True
        self.session = None
        self.payloads = {}
        for subscription in self.subscriptions:
            self.bus.signal_unsubscribe(subscription)
        self.subscriptions.clear()
