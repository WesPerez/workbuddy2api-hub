"""Health-only, lossy bounded telemetry; never an admission/health authority.

Only the loopback updater's opaque ID is retained. Missing/dropped telemetry is
unknown. HTTP workers never wait for the observer, its lock or a journal write.
"""
from collections import OrderedDict, deque
import ipaddress
import json
import os
import re
import threading
import time
import weakref
import functools

HEADER = 'X-WB-Health-Probe'
ID = re.compile(r'^[a-f0-9]{32}$')
STAGES = ('accepted', 'handler_start', 'request_line_done', 'headers_parsed',
          'health_enter', 'release_read', 'release_done', 'busy_read', 'busy_done',
          'daily_read', 'daily_done', 'health_exit', 'headers_write',
          'headers_written', 'body_write', 'body_flushed')
OUTCOMES = frozenset({'pending', 'complete', 'handler_error', 'write_error', 'expired'})


def lossy(method):
    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception:
            return None
    return wrapped


def loopback(address):
    try:
        return ipaddress.ip_address(address[0]).is_loopback
    except (ValueError, TypeError, IndexError):
        return False


class Diagnostics:
    def __init__(self, emit=None, clock=time.monotonic_ns):
        self.clock = clock
        # One bounded write, without holding TextIOWrapper's lock: a blocked
        # observer must not lock out unrelated threads using print/log.
        self.emit = emit or (lambda row: os.write(1, (json.dumps(row) + '\n').encode('utf-8')))
        self.lock = threading.Lock()
        self.accepts = OrderedDict()
        self.active = {}
        self.finished = deque(maxlen=128)
        self.starts = deque(maxlen=32)
        self.observer = None

    @lossy
    def accepted(self, sock, address):
        if not loopback(address) or not self.lock.acquire(False):
            return
        try:
            self.accepts[id(sock)] = (weakref.ref(sock), self.clock())
            while len(self.accepts) > 256:
                self.accepts.popitem(last=False)
        finally:
            self.lock.release()

    @lossy
    def take_accepted(self, sock):
        if not self.lock.acquire(False):
            return None
        try:
            row = self.accepts.pop(id(sock), None)
            return row[1] if row is not None and row[0]() is sock else None
        finally:
            self.lock.release()

    @lossy
    def begin(self, handler):
        # Do not store raw paths, address, headers, handler/socket or exceptions.
        if (getattr(handler, 'command', None) != 'GET' or getattr(handler, 'path', None) != '/healthz'
                or not loopback(getattr(handler, 'client_address', ()))):
            return None
        values = handler.headers.get_all(HEADER, [])
        if len(values) != 1 or not isinstance(values[0], str) or not ID.fullmatch(values[0]):
            return None
        if not self.lock.acquire(False):
            return None
        try:
            now = self.clock()
            while self.starts and now - self.starts[0] >= 60 * 10**9:
                self.starts.popleft()
            if len(self.active) >= 64 or len(self.starts) >= 32 or values[0] in self.active:
                return None
            times = {stage: at for stage, at in getattr(handler, '_health_early', {}).items()
                     if stage in STAGES[:3] and type(at) is int and 0 < at <= now}
            times['headers_parsed'] = now
            row = {'probe_id': values[0], 'start': min(times.values()), 'times': times,
                   'last_stage': 'headers_parsed', 'pending_sent': False}
            self.active[values[0]] = row
            self.starts.append(now)
            return values[0]
        finally:
            self.lock.release()

    @lossy
    def mark(self, probe_id, stage, status=None):
        if not probe_id or stage not in STAGES or not self.lock.acquire(False):
            return
        try:
            row = self.active.get(probe_id)
            if row is None or stage in row['times']:
                return
            at = self.clock()
            # Never manufacture an earlier stage after a later event.
            if STAGES.index(stage) <= STAGES.index(row['last_stage']):
                return
            row['times'][stage] = at
            row['last_stage'] = stage
            if type(status) is int and 100 <= status <= 599:
                row['response_status'] = status  # intended status, not client receipt
        finally:
            self.lock.release()

    @lossy
    def finish(self, probe_id, outcome='complete'):
        if not probe_id or outcome not in OUTCOMES or outcome == 'pending' or not self.lock.acquire(False):
            return
        try:
            row = self.active.pop(probe_id, None)
            if row is not None:
                self.finished.append(self._safe(row, self.clock(), outcome))
        finally:
            self.lock.release()

    @staticmethod
    def _safe(row, now, outcome):
        fields = {'state': 'health_server_probe', 'probe_id': row['probe_id'], 'outcome': outcome,
                  'last_stage': row['last_stage'], 'server_elapsed_ms': max(0, (now - row['start']) // 10**6),
                  'stage_offsets_ms': {stage: max(0, (row['times'][stage] - row['start']) // 10**6)
                                       for stage in STAGES if stage in row['times']}}
        if 'response_status' in row:
            fields['response_status'] = row['response_status']
        return fields

    def collect(self):
        if not self.lock.acquire(False):
            return []
        try:
            now = self.clock()
            rows = list(self.finished)
            self.finished.clear()
            for probe_id, row in list(self.active.items()):
                if now - row['start'] >= 120 * 10**9:
                    rows.append(self._safe(row, now, 'expired'))
                    del self.active[probe_id]
                elif now - row['start'] >= 4 * 10**9 and not row['pending_sent']:
                    rows.append(self._safe(row, now, 'pending'))
                    row['pending_sent'] = True
            return rows
        finally:
            self.lock.release()

    def _observe(self):
        while True:
            time.sleep(.25)
            for row in self.collect():
                try:
                    self.emit(row)
                except Exception:
                    pass  # Telemetry failure is not a health result.

    @lossy
    def start(self):
        # Called before serve_forever, not in an HTTP worker. Thread startup
        # can wait for scheduling; it is not added to a health request's path.
        if self.observer is None:
            observer = threading.Thread(target=self._observe, name='health-diagnostics', daemon=True)
            self.observer = observer
            try:
                observer.start()
            except RuntimeError:
                self.observer = None


DIAGNOSTICS = Diagnostics()


def mark(handler, stage, status=None):
    DIAGNOSTICS.mark(getattr(handler, '_health_probe_id', None), stage, status)
