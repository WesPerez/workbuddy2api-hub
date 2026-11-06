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
try:
    import resource
except ImportError:  # Windows: native ID and thread CPU remain available.
    resource = None

HEADER = 'X-WB-Health-Probe'
ID = re.compile(r'^[a-f0-9]{32}$')
STAGES = ('accepted', 'handler_start', 'request_line_done', 'headers_parsed',
          'health_enter', 'release_read', 'release_done', 'busy_read', 'busy_done',
          'daily_read', 'daily_done', 'health_exit', 'headers_write',
          'headers_written', 'body_write', 'body_flushed')
OUTCOMES = frozenset({'pending', 'complete', 'handler_error', 'write_error', 'expired'})
COUNTERS = ('cpu_ns', 'minor_faults', 'major_faults', 'voluntary_switches', 'involuntary_switches')
USAGE_STAGES = frozenset({'headers_parsed', 'health_enter', 'release_done',
                          'busy_done', 'daily_done', 'health_exit', 'body_flushed'})


def thread_sample():
    """Current thread only; no /proc, files, locks, stacks or exception text.

    This is diagnostic evidence, not time attribution. Major faults do not
    identify a backing file or prove that all elapsed time was page-in time.
    """
    result = {'counters': {}}
    try:
        result['native_id'] = threading.get_native_id()
    except Exception:
        return None
    try:
        result['counters']['cpu_ns'] = time.thread_time_ns()
    except Exception:
        pass
    try:
        if resource is not None and hasattr(resource, 'RUSAGE_THREAD'):
            usage = resource.getrusage(resource.RUSAGE_THREAD)
            for name, field in (('minor_faults', 'ru_minflt'), ('major_faults', 'ru_majflt'),
                                ('voluntary_switches', 'ru_nvcsw'), ('involuntary_switches', 'ru_nivcsw')):
                result['counters'][name] = getattr(usage, field)
    except Exception:
        pass
    return result


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
    def __init__(self, emit=None, clock=time.monotonic_ns, sample=thread_sample):
        self.clock = clock
        self.sample = sample
        # One bounded write, without holding TextIOWrapper's lock: a blocked
        # observer must not lock out unrelated threads using print/log.
        self.emit = emit or (lambda row: os.write(1, (json.dumps(row) + '\n').encode('utf-8')))
        self.lock = threading.Lock()
        self.accepts = OrderedDict()
        self.active = {}
        self.finished = deque(maxlen=128)
        self.starts = deque(maxlen=32)
        self.observer = None

    def _sample(self):
        # A broken/unsupported counter source must not lose the existing
        # wall-clock diagnostics or become a health decision.
        try:
            value = self.sample()
            if not isinstance(value, dict):
                return None
            tid = value.get('native_id')
            counters = value.get('counters')
            if type(tid) is not int or not 0 < tid < 2**63 or not isinstance(counters, dict):
                return None
            return {'native_id': tid, 'counters': {key: counters[key] for key in COUNTERS
                    if type(counters.get(key)) is int and 0 <= counters[key] < 2**63}}
        except Exception:
            return None

    def _usage(self, row, stage):
        if stage not in USAGE_STAGES:
            return
        sample = self._sample()
        if sample is None:
            return
        if stage == 'headers_parsed':
            row['thread_baseline'] = sample
            row['thread_previous'] = dict(sample['counters'])
            row['thread_invalid'] = set()
            row['thread_deltas'] = {stage: {key: 0 for key in sample['counters']}}
            return
        base = row.get('thread_baseline')
        if base is None or sample['native_id'] != base['native_id']:
            return  # Never subtract another thread's counters.
        delta = {}
        for key, value in base['counters'].items():
            current = sample['counters'].get(key)
            if current is None or key in row['thread_invalid']:
                continue
            if current < row['thread_previous'][key]:
                row['thread_invalid'].add(key)
                continue  # A reset stays unknown even if a later value rises.
            row['thread_previous'][key] = current
            delta[key] = current - value
        row['thread_deltas'][stage] = delta  # Missing/reset counters stay unknown, not zero.

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
            self._usage(row, 'headers_parsed')
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
            self._usage(row, stage)
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
        if 'thread_baseline' in row:
            fields['handler_native_id'] = row['thread_baseline']['native_id']
            # Pending/expired snapshots only serialize the handler's last
            # captured samples, never sample the observer thread as handler.
            fields['thread_usage_delta'] = {stage: dict(row['thread_deltas'][stage])
                                           for stage in STAGES if stage in row['thread_deltas']}
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
