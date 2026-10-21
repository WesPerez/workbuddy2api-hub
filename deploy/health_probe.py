"""Bounded, fail-closed health diagnostics. No response body is ever logged.

An isolated reader process lets the owner enforce one monotonic total budget,
including slow/trickling response reads. Socket timeout alone is not a deadline.
This probe performs GET only and never accesses application account material.
"""
from dataclasses import dataclass
import errno
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

SHA = re.compile(r'^[a-f0-9]{40}$')
MAX_RESPONSE = 65536
REASONS = frozenset({'ok', 'connection_timeout', 'read_timeout', 'transport_failure',
                     'http_status', 'response_too_large', 'json_invalid', 'schema_invalid',
                     'ok_false', 'commit_mismatch', 'busy', 'deadline_exhausted', 'probe_error'})
TRANSIENT_ERRNOS = frozenset({errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED,
                            errno.EPIPE, errno.ETIMEDOUT})
TRANSIENT_WINERRORS = frozenset({10053, 10054, 10060, 10061})


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
        return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


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


def worker_probe(url, sha, idle, timeout):
    phase = 'connect'
    status = None
    print(json.dumps({'phase': phase}), flush=True)
    try:
        # Health is configured as the local app endpoint, never an account
        # upstream. Do not inherit credential-bearing proxy environment.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(url, timeout=timeout) as response:
            status = response.status
            if status != 200:
                return {'reason': 'http_status', 'http_status': status}
            phase = 'read'
            print(json.dumps({'phase': phase}), flush=True)
            result = classify_payload(response.read(MAX_RESPONSE + 1), sha, idle)
            return dict(result, http_status=status)
    except urllib.error.HTTPError as error:
        error.close()
        return {'reason': 'http_status', 'http_status': error.code}
    except urllib.error.URLError as error:
        cause = error.reason
        if isinstance(cause, (TimeoutError, socket.timeout)):
            return {'reason': 'connection_timeout', 'retryable': True}
        return {'reason': 'transport_failure', 'retryable': transient(cause)}
    except (TimeoutError, socket.timeout):
        return {'reason': 'read_timeout' if phase == 'read' else 'connection_timeout',
                'http_status': status, 'retryable': True}
    except (OSError, http.client.HTTPException) as error:
        return {'reason': 'transport_failure', 'http_status': status, 'retryable': transient(error)}
    except Exception:
        # Worker bugs are fail-closed and sanitized, not transport retries.
        return {'reason': 'probe_error'}


def decode_worker(output):
    if not output or len(output) > 4096:
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
    budget = max(.001, deadline - time.monotonic())
    request = json.dumps({'url': url, 'sha': sha, 'idle': idle, 'timeout': budget}).encode()
    try:
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
        return decode_worker(output) if process.returncode == 0 else {'reason': 'probe_error'}
    except subprocess.TimeoutExpired as error:
        # Close the actual reader, not a detached timeout thread. Reap before
        # another attempt; no timed-out health request is left running.
        if process.poll() is None:
            process.kill()
        output, _ = process.communicate()
        partial = output or error.output or b''
        reading = b'"phase": "read"' in partial
        return {'reason': 'read_timeout' if reading else 'connection_timeout',
                'http_status': 200 if reading else None, 'retryable': True}
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
                        round(elapsed * 1000), len(reasons), tuple(reasons))


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit(2)
    try:
        request = json.loads(sys.stdin.buffer.read(16385))
        result = worker_probe(request['url'], request['sha'], request['idle'], request['timeout'])
    except Exception:
        result = {'reason': 'probe_error'}
    print(json.dumps(result), flush=True)
