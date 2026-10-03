"""Durable, independently timed rollback for native display transactions."""
import secrets
import time
from native_display_config import DisplayTransaction
from native_runtime_state import read_private, write_private


class DisplayDefaultWriteError(RuntimeError):
    pass


class DisplayGuardian:
    def __init__(self, backend, journal, timeout=30):
        if not 5 <= timeout <= 60:
            raise ValueError('Invalid display confirmation window')
        self.backend, self.journal, self.timeout = backend, journal, timeout
        self.identity = backend.identity()
        self.identifier = None
        self.confirmation_policy = 'manual'
        self.deadline = 0
        # Windows display defaults belong to this owned UU virtual machine,
        # not to other Linux users or persistent Mutter configuration.
        self.registry_default = None
        self.registry_candidate = None
        self.commit_default = False
        self.transaction = DisplayTransaction(backend, self.save)
        if journal.exists() or journal.is_symlink():
            record = read_private(journal)
            if record.get('version') != 1:
                raise ValueError('Unreviewed display recovery record')
            self.registry_default = self.valid_default(record.get('registry_default'))
            if record.get('identity') != self.identity:
                # Never restore a pre-reboot or another compositor's layout.
                self.save(None)
            elif record.get('pending') is not None:
                self.transaction.pending = record['pending']
                self.identifier = record['transaction']
                self.confirmation_policy = record.get('confirmation_policy', 'manual')
                self.registry_candidate = self.valid_default(record.get('registry_candidate'))
                self.deadline = time.monotonic()  # Recover immediately after daemon restart.

    @staticmethod
    def valid_default(value):
        if value is None:
            return None
        import math
        if (not isinstance(value, dict) or set(value) != {'width', 'height', 'refresh', 'scale', 'scope'} or
            any(type(value[k]) is not int or not 2 <= value[k] <= 4096 or value[k] % 2 for k in ('width', 'height')) or
            any(type(value[k]) not in (int, float) or not math.isfinite(value[k]) for k in ('refresh', 'scale')) or
            not 1 <= value['refresh'] <= 120 or not 0.5 <= value['scale'] <= 4 or value['scope'] not in ('user', 'global')):
            raise ValueError('Unreviewed UU display default')
        return dict(value)

    @staticmethod
    def target_matches_request(target, request):
        """Recognize a repeated client request for the already-pending mode.

        UU asks for a read-only verification around an apply and can repeat the
        apply after it receives WM_DISPLAYCHANGE. Treating that exact request
        as idempotent avoids a second click being required while still
        rejecting a different mode during the protected transaction.
        """
        if not isinstance(target, dict) or not isinstance(request, dict):
            return False
        if target.get('width') != request.get('width') or target.get('height') != request.get('height'):
            return False
        scale = request.get('scale')
        if scale is not None and abs(float(target.get('scale', -1)) - float(scale)) > 0.00001:
            return False
        refresh = request.get('refresh', 0)
        return refresh in (0, 1) or abs(float(target.get('refresh', -1)) - float(refresh)) < 0.75

    def save(self, pending):
        # Mode lists are only needed for fresh selection, not recovery. Keep
        # the durable record small and omit monitor names/EDID/serials.
        if pending is not None:
            pending = dict(pending)
            for key in ('before', 'observed'):
                if pending[key] is not None:
                    pending[key] = {k: v for k, v in pending[key].items() if k != 'modes'}
        default = self.registry_candidate if pending is None and self.commit_default else self.registry_default
        try:
            write_private(self.journal, dict(version=1, identity=self.identity, transaction=self.identifier,
                                            confirmation_policy=self.confirmation_policy, pending=pending,
                                            registry_default=default, registry_candidate=self.registry_candidate if pending is not None else None))
        except Exception as error:
            if self.registry_candidate is not None:
                raise DisplayDefaultWriteError('UU display-default checkpoint failed') from error
            raise
        self.registry_default = default
        if pending is None:
            self.registry_candidate = None

    def request(self, request):
        if not isinstance(request, dict) or type(request.get('version')) is not int or request['version'] != 1:
            raise ValueError('Invalid display request')
        op = request.get('op')
        if op == 'inspect' and set(request) == {'version', 'op'}:
            state = self.backend.current()
            current = next(m for m in state['modes'] if m['id'] == state['mode'])
            return dict(serial=state['serial'], width=current['width'], height=current['height'], refresh=current['refresh'],
                        scale=state['scale'], pending=self.transaction.pending is not None,
                        registry=self.registry_candidate or self.registry_default or
                            dict(width=current['width'], height=current['height'], refresh=current['refresh'], scale=state['scale']),
                        modes=[{k: m[k] for k in ('width', 'height', 'refresh', 'scales')} for m in state['modes']])
        if op in ('verify', 'apply'):
            if set(request) - {'version', 'op', 'serial', 'width', 'height', 'refresh', 'scale', 'confirmation', 'default_scope', 'force_reset'}:
                raise ValueError('Unexpected display request fields')
            if 'default_scope' in request and (op != 'apply' or request['default_scope'] not in ('user', 'global')):
                raise ValueError('Unsupported display-default scope')
            if 'force_reset' in request and (op != 'apply' or request['force_reset'] is not True):
                raise ValueError('Invalid forced display reset')
            if 'confirmation' in request and (op != 'apply' or request['confirmation'] != 'gpu_frame'):
                raise ValueError('Unsupported display confirmation policy')
            if self.transaction.pending is not None:
                pending_target = self.transaction.pending.get('target')
                if self.target_matches_request(pending_target, request):
                    current = self.backend.current()
                    if op == 'verify':
                        return dict(verified=True, desktop_changed=True)
                    remaining = max(0, int(self.deadline - time.monotonic()))
                    observed = self.transaction.pending.get('observed') or current
                    return dict(changed=True, serial=observed.get('serial', current.get('serial')),
                                transaction=self.identifier, confirmation_seconds=remaining)
            if self.backend.identity() != self.identity:
                raise RuntimeError('Compositor identity changed')
            plan = self.transaction.prepare(request['serial'], request['width'], request['height'],
                                            request.get('refresh', 0), request.get('scale'))
            if op == 'verify':
                return dict(verified=True, desktop_changed=False)
            self.identifier = secrets.token_hex(16)
            self.confirmation_policy = request.get('confirmation', 'manual')
            self.deadline = time.monotonic() + self.timeout
            plan['force_reset'] = request.get('force_reset', False)
            # A normal UU resolution change must survive the client's later
            # ChangeDisplaySettings(NULL, NULL, 0) refresh.  That call reads
            # the owned registry default, so leaving it at the installation
            # mode causes an apparently random jump back after WM_DISPLAYCHANGE.
            # Explicit user/global requests keep their scope; ordinary mode
            # changes inherit the existing scope and use ``user`` on a fresh
            # journal.  The candidate is still staged and is committed only
            # by confirmation, so rollback never rewrites the old default.
            default_scope = request.get('default_scope')
            if default_scope is None:
                default_scope = ((self.registry_default or {}).get('scope') or 'user')
            self.registry_candidate = ({k: plan['target'][k] for k in ('width', 'height', 'refresh', 'scale')} |
                                      {'scope': default_scope})
            try:
                result = self.transaction.apply(plan)
                if not result['changed'] and self.registry_candidate is not None:
                    self.commit_default = True
                    try:
                        self.save(None)
                    finally:
                        self.commit_default = False
            except Exception:
                if self.transaction.pending is None:
                    self.registry_candidate = None
                raise
            return dict(result, transaction=self.identifier if result['changed'] else None,
                        confirmation_seconds=self.timeout if result['changed'] else 0)
        if op in ('confirm', 'rollback'):
            expected = {'version', 'op', 'transaction'} | ({'serial'} if op == 'confirm' else set())
            if set(request) != expected or not self.identifier or request['transaction'] != self.identifier:
                raise ValueError('Stale display confirmation')
            if self.backend.identity() != self.identity:
                raise RuntimeError('Compositor identity changed')
            if op == 'confirm':
                if time.monotonic() >= self.deadline:
                    raise RuntimeError('Display confirmation expired')
                self.commit_default = self.registry_candidate is not None
                try:
                    self.transaction.confirm(request['serial'])
                finally:
                    self.commit_default = False
                return dict(confirmed=True)
            return dict(rolled_back=self.transaction.rollback())
        raise ValueError('Unsupported display operation')

    def expire(self):
        if self.transaction.pending is None or time.monotonic() < self.deadline:
            return None
        if self.backend.identity() != self.identity:
            self.save(None)
            self.transaction.pending = None
            return 'compositor_changed_preserved'
        # An apply whose observation never completed is retained for explicit
        # review. Guessing a new serial could undo an intervening user change.
        if self.transaction.pending['observed'] is None:
            self.deadline = time.monotonic() + 5
            return 'ambiguous_apply_requires_review'
        try:
            restored = self.transaction.rollback()
        finally:
            self.deadline = time.monotonic() + 5
        return 'rolled_back' if restored else 'later_display_change_preserved'
