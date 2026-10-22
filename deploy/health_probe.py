"""Bounded, fail-closed health diagnostics. No response body is ever logged.

An isolated reader process lets the owner enforce one monotonic total budget,
including slow/trickling response reads. Socket timeout alone is not a deadline.
This probe performs GET only and never accesses application account material.
"""
# Emit before the heavier standard-library imports. Absence of this marker
# includes interpreter startup; boot -> ready includes imports and stdin read.
import sys
import time
if __name__ == '__main__' and sys.argv[1:] == ['--worker']:
    try:
        print('{"phase":"worker_boot","at_ns":%d}' % time.monotonic_ns(), flush=True)
    except OSError:
        pass  # Broken telemetry pipe must not change transport classification.

from dataclasses import dataclass
import errno
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import urllib.error
import urllib.request

SHA = re.compile(r'^[a-f0-9]{40}$')
MAX_RESPONSE = 65536
REASONS = frozenset({'ok', 'connection_timeout', 'read_timeout', 'timeout_unknown', 'transport_failure',
                     'http_status', 'response_too_large', 'json_invalid', 'schema_invalid',
                     'ok_false', 'commit_mismatch', 'busy', 'deadline_exhausted', 'probe_error'})
TRANSIENT_ERRNOS = frozenset({errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED,
                            errno.EPIPE, errno.ETIMEDOUT})
TRANSIENT_WINERRORS = frozenset({10053, 10054, 10060, 10061})
PHASES = ('attempt_start', 'worker_boot', 'worker_ready', 'connect_start',
          'tcp_connected', 'connection_ready', 'request_sent',
          'headers_received', 'body_complete')
TIMEOUT_SOURCES = frozenset({'parent_deadline', 'socket_timeout'})
MAX_WORKER_OUTPUT = 4096
PROBE_ID = re.compile(r'^[a-f0-9]{32}$')


def safe_commit(value):
    return value if isinstance(value, str) and (SHA.fullmatch(value) or value == 'development') else None


@dataclass(frozen=True)
class HealthResult:
    accepted: bool
    reason: str
    http_status: object = None
    observed_commit: object = None
    elapsed_ms: int = 0
    attempts: int = 0
    attempt_reasons: tuple = ()
    last_phase: object = None
    phase_offsets_ms: object = None
    timeout_source: object = None
    probe_id: object = None

    def __bool__(self):
        return self.accepted

    def safe_fields(self):
        # Explicit allowlist: never serialize exceptions, URLs, headers or bodies.
        result = {'reason': self.reason, 'elapsed_ms': self.elapsed_ms,
                  'attempts': self.attempts, 'attempt_reasons': list(self.attempt_reasons)}
        if type(self.http_status) is int and 100 <= self.http_status <= 599:
            result['http_status'] = self.http_status
        commit = safe_commit(self.observed_commit)
        if commit is not None:
            result['observed_commit'] = commit
        if isinstance(self.last_phase, str) and self.last_phase in PHASES:
            result['last_phase'] = self.last_phase
        if isinstance(self.phase_offsets_ms, dict):
            offsets = {phase: self.phase_offsets_ms[phase] for phase in PHASES
                       if type(self.phase_offsets_ms.get(phase)) is int
                       and 0 <= self.phase_offsets_ms[phase] <= 86400000}
            if offsets:
                result['phase_offsets_ms'] = offsets
        if isinstance(self.timeout_source, str) and self.timeout_source in TIMEOUT_SOURCES:
            result['timeout_source'] = self.timeout_source
        if isinstance(self.probe_id, str) and PROBE_ID.fullmatch(self.probe_id):
            result['probe_id'] = self.probe_id
        return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class PhaseTrace:
    """A fixed, body/header/URL-free set; telemetry errors cannot alter I/O."""
    def __init__(self):
        self.seen = set()

    def mark(self, phase, status=None):
        if phase not in PHASES or phase in self.seen:
            return
        self.seen.add(phase)
        row = {'phase': phase, 'at_ns': time.monotonic_ns()}
        if type(status) is int and 100 <= status <= 599:
            row['http_status'] = status
        try:
            print(json.dumps(row), flush=True)
        except OSError:
            pass


class TracedConnection:
    def __init__(self, *args, trace, **kwargs):
        self.trace = trace
        super().__init__(*args, **kwargs)

    def connect(self):
        self.trace.mark('connect_start')
        # Wrap only this connection's existing connector. DNS and TCP remain
        # stdlib behavior. HTTPSConnection also uses this inherited connector;
        # TCP completion is before its TLS handshake, not inferred from it.
        connector = self._create_connection
        def connected(*args, **kwargs):
            sock = connector(*args, **kwargs)
            self.trace.mark('tcp_connected')
            return sock
        self._create_connection = connected
        try:
            super().connect()
        finally:
            self._create_connection = connector
        self.trace.mark('connection_ready')

    def request(self, *args, **kwargs):
        super().request(*args, **kwargs)
        # Sent to the local socket, not proof the server has accepted/read it.
        self.trace.mark('request_sent')

    def getresponse(self):
        response = super().getresponse()
        self.trace.mark('headers_received', response.status)
        return response


class TracedHTTPConnection(TracedConnection, http.client.HTTPConnection):
    pass


class TracedHTTPSConnection(TracedConnection, http.client.HTTPSConnection):
    pass


class TracedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace

    def http_open(self, request):
        return self.do_open(lambda *a, **kw: TracedHTTPConnection(*a, trace=self.trace, **kw), request)


class TracedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, trace):
        super().__init__()
        self.trace = trace

    def https_open(self, request):
        # 3.9 exposes _check_hostname; 3.12 removed that argument. Preserve
        # the default handler's certificate/hostname policy in both versions.
        kwargs = {'context': self._context}
        if hasattr(self, '_check_hostname'):
            kwargs['check_hostname'] = self._check_hostname
        return self.do_open(lambda *a, **kw: TracedHTTPSConnection(*a, trace=self.trace, **kw),
                            request, **kwargs)


def decode_trace(output, started_ns, ended_ns):
    """Offsets use Linux/Windows system-wide monotonic clocks, LAST attempt only.

    Missing milestones are unknown, not evidence the corresponding operation
    never happened. Events after the deadline observation are not backdated.
    attempt_start is the parent's baseline, NOT proof the worker has started.
    Only Linux 3.9+ and Windows 3.12 are deployment/fixture targets here.
    """
    result = {'last_phase': 'attempt_start', 'phase_offsets_ms': {'attempt_start': 0}}
    if not output or len(output) > MAX_WORKER_OUTPUT:
        return result
    previous = started_ns
    index = 0
    for line in output.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(row, dict):
            continue
        phase, at = row.get('phase'), row.get('at_ns')
        if (not isinstance(phase, str) or phase not in PHASES or type(at) is not int
                or not previous <= at <= ended_ns or PHASES.index(phase) <= index):
            continue
        previous, index = at, PHASES.index(phase)
        result['last_phase'] = phase
        result['phase_offsets_ms'][phase] = round((at - started_ns) / 1000000)
        if phase == 'headers_received' and type(row.get('http_status')) is int and 100 <= row['http_status'] <= 599:
            result['http_status'] = row['http_status']
    return result


def classify_payload(body, sha, idle):
    if len(body) > MAX_RESPONSE:
        return {'reason': 'response_too_large'}
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        return {'reason': 'json_invalid'}
    if (not isinstance(data, dict) or type(data.get('ok')) is not bool
            or safe_commit(data.get('commit')) is None
            or type(data.get('activeJobs')) is not int or data['activeJobs'] < 0):
        return {'reason': 'schema_invalid'}
    result = {'observed_commit': data['commit']}
    if data['ok'] is not True:
        return dict(result, reason='ok_false')
    if sha is not None and data['commit'] != sha:
        return dict(result, reason='commit_mismatch')
    if idle and data['activeJobs'] != 0:
        return dict(result, reason='busy')
    return dict(result, reason='ok')


def transient(error):
    if isinstance(error, http.client.RemoteDisconnected):
        return True
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    return isinstance(error, OSError) and (error.errno in TRANSIENT_ERRNOS
                                          or getattr(error, 'winerror', None) in TRANSIENT_WINERRORS)


def timeout_reason(phases):
    # request() returned only after the request was sent to the local socket.
    # Waiting for response headers is already a read, not a TCP connection.
    # Missing telemetry is unknown; connection_ready alone could be a blocked
    # send, and startup/pipe/scheduler time is not evidence of connection I/O.
    if 'body_complete' in phases:
        return 'timeout_unknown'  # Payload parsing/worker exit is not a socket read.
    if any(phase in phases for phase in ('request_sent', 'headers_received')):
        return 'read_timeout'
    if 'connection_ready' not in phases and any(phase in phases for phase in ('connect_start', 'tcp_connected')):
        return 'connection_timeout'
    return 'timeout_unknown'


def worker_probe(url, sha, idle, timeout, probe_id=None):
    status = None
    trace = PhaseTrace()
    trace.mark('worker_ready')
    try:
        # Health is configured as the local app endpoint, never an account
        # upstream. Do not inherit credential-bearing proxy environment.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                            TracedHTTPHandler(trace), TracedHTTPSHandler(trace))
        request = urllib.request.Request(url, headers={'X-WB-Health-Probe': probe_id}
                                         if isinstance(probe_id, str) and PROBE_ID.fullmatch(probe_id) else {})
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            trace.mark('headers_received', status)
            if status != 200:
                return {'reason': 'http_status', 'http_status': status}
            body = response.read(MAX_RESPONSE + 1)
            trace.mark('body_complete')
            result = classify_payload(body, sha, idle)
            return dict(result, http_status=status)
    except urllib.error.HTTPError as error:
        error.close()
        return {'reason': 'http_status', 'http_status': error.code}
    except urllib.error.URLError as error:
        cause = error.reason
        if isinstance(cause, (TimeoutError, socket.timeout)):
            return {'reason': timeout_reason(trace.seen), 'retryable': True, 'timeout_source': 'socket_timeout'}
        return {'reason': 'transport_failure', 'retryable': transient(cause)}
    except (TimeoutError, socket.timeout):
        return {'reason': timeout_reason(trace.seen),
                'http_status': status, 'retryable': True, 'timeout_source': 'socket_timeout'}
    except (OSError, http.client.HTTPException) as error:
        return {'reason': 'transport_failure', 'http_status': status, 'retryable': transient(error)}
    except Exception:
        # Worker bugs are fail-closed and sanitized, not transport retries.
        return {'reason': 'probe_error'}


def decode_worker(output):
    if not output or len(output) > MAX_WORKER_OUTPUT:
        return None
    try:
        row = json.loads(output.splitlines()[-1])
        if (not isinstance(row, dict) or not isinstance(row.get('reason'), str)
                or row['reason'] not in REASONS):
            return None
        return row
    except (ValueError, IndexError):
        return None


def run_attempt(url, sha, idle, deadline):
    started_ns = time.monotonic_ns()
    budget = max(.001, deadline - time.monotonic())
    try:
        probe_id = os.urandom(16).hex()  # opaque, per attempt; never account identity
    except OSError:
        probe_id = None  # Missing correlation must not change a health decision.
    request = json.dumps({'url': url, 'sha': sha, 'idle': idle, 'timeout': budget, 'probe_id': probe_id}).encode()
    try:
        # The worker is this exact immutable release file, not an independently
        # upgraded protocol peer. Legacy {phase: read} has no timestamp/identity
        # and is deliberately not interpreted as new milestone evidence.
        # Windows os.environ normalizes keys to uppercase. Dropping SYSTEMROOT
        # prevents Winsock initialization even for a loopback fixture.
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL'}}
        process = subprocess.Popen([sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--worker'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=env)
    except OSError:
        return {'reason': 'probe_error'}
    try:
        output, _ = process.communicate(request, timeout=max(.001, deadline - time.monotonic()))
        row = decode_worker(output) if process.returncode == 0 else None
        return dict(decode_trace(output, started_ns, time.monotonic_ns()), probe_id=probe_id,
                    **(row or {'reason': 'probe_error'}))
    except subprocess.TimeoutExpired as error:
        timed_out_ns = time.monotonic_ns()
        # Close the actual reader, not a detached timeout thread. Reap before
        # another attempt; no timed-out health request is left running.
        if process.poll() is None:
            process.kill()
        output, _ = process.communicate()
        partial = output or error.output or b''
        trace = decode_trace(partial, started_ns, timed_out_ns)
        status = trace.get('http_status')
        # Already observed HTTP errors remain non-retryable, even if the
        # worker was killed before emitting its final classification.
        if status is not None and status != 200:
            return dict(trace, reason='http_status', retryable=False, timeout_source='parent_deadline', probe_id=probe_id)
        return dict(trace, reason=timeout_reason(trace['phase_offsets_ms']),
                    retryable=True, timeout_source='parent_deadline', probe_id=probe_id)
    except OSError:
        return {'reason': 'probe_error'}
    finally:
        # The worker owns no child processes. Reap even on an unexpected pipe
        # failure; cleanup is never permission to accept a late health result.
        if process.poll() is None:
            process.kill()
        process.wait()


def probe_health(url, sha=None, idle=False, total_timeout=4.0, max_attempts=2):
    if total_timeout <= 0 or max_attempts not in (1, 2):
        raise ValueError('Invalid health probe budget')
    started = time.monotonic()
    deadline = started + total_timeout
    reasons = []
    row = {'reason': 'deadline_exhausted'}
    for attempt in range(max_attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Preserve the original four-second first-response allowance. Retry
        # only if a transient failure leaves time in this same deadline.
        row = run_attempt(url, sha, idle, deadline) or {'reason': 'probe_error'}
        reasons.append(row['reason'])
        if row['reason'] == 'ok' or not row.get('retryable') or attempt + 1 == max_attempts:
            break
        delay = min(0.2, max(0, deadline - time.monotonic()))
        if delay:
            time.sleep(delay)
    elapsed = time.monotonic() - started
    reason = row['reason']
    accepted = reason == 'ok' and time.monotonic() <= deadline
    if reason == 'ok' and not accepted:
        reason = 'deadline_exhausted'
    return HealthResult(accepted, reason, row.get('http_status'), safe_commit(row.get('observed_commit')),
                        round(elapsed * 1000), len(reasons), tuple(reasons),
                        row.get('last_phase'), row.get('phase_offsets_ms'), row.get('timeout_source'), row.get('probe_id'))


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit(2)
    try:
        request = json.loads(sys.stdin.buffer.read(16385))
        result = worker_probe(request['url'], request['sha'], request['idle'], request['timeout'], request.get('probe_id'))
    except Exception:
        result = {'reason': 'probe_error'}
    print(json.dumps(result), flush=True)
