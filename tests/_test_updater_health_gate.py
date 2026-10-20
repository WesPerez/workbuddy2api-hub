"""Health fixtures are loopback-only, with no accounts, models or credentials."""
import contextlib
import io
import json
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy'))
import health_probe as hp
if os.name == 'nt':
    # Production updater targets Linux only. Probe fixtures still run on
    # Windows; Linux deployment-lock wiring tests below are explicitly skipped.
    with mock.patch.dict(sys.modules, {'fcntl': mock.MagicMock()}):
        import update
else:
    import update

COMMIT = '1' * 40
GOOD = {'ok': True, 'commit': COMMIT, 'activeJobs': 0}


@contextlib.contextmanager
def server(responses):
    calls = []
    workers = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            workers.append(threading.current_thread())
            index = len(calls)
            calls.append(self.path)
            row = responses[min(index, len(responses) - 1)]
            if row.get('disconnect'):
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if row.get('header_delay'):
                time.sleep(row['header_delay'])
            body = row.get('body', json.dumps(GOOD).encode())
            try:
                self.send_response(row.get('status', 200))
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                if row.get('body_delay'):
                    time.sleep(row['body_delay'])
                if row.get('trickle'):
                    for value in body:
                        self.wfile.write(bytes([value]))
                        self.wfile.flush()
                        time.sleep(row['trickle'])
                else:
                    self.wfile.write(body)
            except (OSError, BrokenPipeError):
                pass
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:%d/healthz' % httpd.server_port, calls
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        for worker in workers:
            worker.join(timeout=4)


class HealthProbeTests(unittest.TestCase):
    def probe(self, rows, **kwargs):
        with server(rows) as (url, calls):
            result = hp.probe_health(url, COMMIT, **kwargs)
            return result, len(calls)

    def test_normal_current_and_idle(self):
        for idle in (False, True):
            result, calls = self.probe([{}], idle=idle)
            self.assertTrue(result)
            self.assertEqual((result.reason, result.http_status, result.attempts, calls), ('ok', 200, 1, 1))
            self.assertEqual(result.observed_commit, COMMIT)

    def test_semantic_failures_are_never_retried(self):
        rows = [(dict(GOOD, commit='2' * 40), 'commit_mismatch'),
                (dict(GOOD, ok=False), 'ok_false'), (dict(GOOD, activeJobs=1), 'busy'),
                ([], 'schema_invalid'), (dict(GOOD, ok=1), 'schema_invalid'),
                (dict(GOOD, activeJobs=False), 'schema_invalid'),
                (dict(GOOD, activeJobs=-1), 'schema_invalid'),
                (dict(GOOD, activeJobs='0'), 'schema_invalid'),
                (dict(GOOD, commit=123), 'schema_invalid')]
        for field in ('ok', 'commit', 'activeJobs'):
            row = dict(GOOD)
            del row[field]
            rows.append((row, 'schema_invalid'))
        for data, reason in rows:
            with self.subTest(reason=reason, data=data):
                result, calls = self.probe([{'body': json.dumps(data).encode()}, {}], idle=True)
                self.assertFalse(result)
                self.assertEqual((result.reason, result.attempts, calls), (reason, 1, 1))

    def test_busy_current_is_not_an_idle_gate(self):
        result, calls = self.probe([{'body': json.dumps(dict(GOOD, activeJobs=2)).encode()}])
        self.assertTrue(result)
        self.assertEqual(calls, 1)

    def test_json_http_and_size_failures_are_not_retried(self):
        cases = [({'body': b'not-json'}, 'json_invalid'),
                 ({'body': b'x' * 65537}, 'response_too_large'),
                 ({'status': 503}, 'http_status'), ({'status': 302}, 'http_status')]
        for row, reason in cases:
            result, calls = self.probe([row, {}])
            self.assertFalse(result)
            self.assertEqual((result.reason, result.attempts, calls), (reason, 1, 1))

    def test_transient_disconnect_can_recover_once(self):
        result, calls = self.probe([{'disconnect': True}, {}])
        self.assertTrue(result)
        self.assertEqual((result.attempts, calls), (2, 2))
        self.assertEqual(result.attempt_reasons, ('transport_failure', 'ok'))

    def test_connection_failure_exhaustion_is_rejected(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        result = hp.probe_health('http://127.0.0.1:%d/healthz' % port, COMMIT)
        self.assertFalse(result)
        self.assertEqual((result.reason, result.attempts), ('transport_failure', 2))

    def test_delayed_headers_and_reads_are_classified_and_bounded(self):
        for row, reason in [({'header_delay': 1.2}, 'connection_timeout'),
                            ({'body_delay': 1.2}, 'read_timeout'),
                            ({'trickle': .06}, 'read_timeout')]:
            with self.subTest(reason=reason, row=row):
                start = time.monotonic()
                with server([row]) as (url, calls):
                    result = hp.probe_health(url, COMMIT, total_timeout=.8)
                    elapsed = time.monotonic() - start
                    self.assertFalse(result)
                    self.assertEqual(result.reason, reason)
                    self.assertEqual(result.attempts, 1)
                    # Small process creation/reaping/scheduler overhead is not
                    # treated as permission for another full timeout budget.
                    self.assertLess(elapsed, 1.5)

    def test_slow_healthy_response_keeps_original_four_second_allowance(self):
        result, calls = self.probe([{'body_delay': 2.5}])
        self.assertTrue(result)
        self.assertEqual((result.attempts, calls), (1, 1))
        self.assertEqual(result.attempt_reasons, ('ok',))
        self.assertLess(result.elapsed_ms, 4500)

    def test_safe_log_never_contains_url_or_response_secret(self):
        secret = 'secret-cookie-do-not-log'
        with server([{'body': json.dumps(dict(GOOD, commit=secret)).encode()}]) as (url, _):
            result = hp.probe_health(url + '?token=' + secret, COMMIT)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            update.health_event('unhealthy_current', result, COMMIT)
        text = stream.getvalue()
        self.assertNotIn(secret, text)
        self.assertNotIn('http://', text)
        self.assertNotIn('token', text)
        row = json.loads(text)
        self.assertLessEqual(set(row), {'state', 'reason', 'elapsed_ms', 'attempts',
                                       'attempt_reasons', 'http_status', 'observed_commit', 'commit'})

    def test_reader_timeout_kills_and_reaps_without_leaking_url_in_argv(self):
        process = mock.Mock()
        process.poll.side_effect = [None, -9]
        process.communicate.side_effect = [subprocess.TimeoutExpired('reader', 1),
                                           (b'{"phase": "read"}\n', b'')]
        url = 'http://127.0.0.1/healthz?token=private-value'
        with mock.patch.dict(hp.os.environ, {'SYSTEMROOT': 'fixture-system-root', 'GH_TOKEN': 'private-value'}), \
             mock.patch.object(hp.subprocess, 'Popen', return_value=process) as spawn:
            result = hp.run_attempt(url, COMMIT, True, time.monotonic() + 1)
        self.assertEqual(result['reason'], 'read_timeout')
        process.kill.assert_called_once()
        process.wait.assert_called_once()
        args, kwargs = spawn.call_args
        self.assertNotIn(url, ' '.join(args[0]))
        self.assertIn('-I', args[0])
        self.assertIs(kwargs['stderr'], hp.subprocess.DEVNULL)
        self.assertNotIn('PYTHONPATH', kwargs['env'])
        self.assertNotIn('GH_TOKEN', kwargs['env'])
        self.assertEqual(kwargs['env']['SYSTEMROOT'], 'fixture-system-root')
        self.assertIn(url.encode(), process.communicate.call_args_list[0].args[0])

    def test_worker_failures_remain_fail_closed(self):
        with mock.patch.object(hp.subprocess, 'Popen', side_effect=OSError('private-error')):
            self.assertEqual(hp.run_attempt('http://fixture', COMMIT, False,
                                            time.monotonic() + 1)['reason'], 'probe_error')
        for output in (b'[]', b'{"reason": []}', b'{"reason": "secret"}', b'bad-json'):
            self.assertIsNone(hp.decode_worker(output))

    def test_late_success_is_rejected(self):
        def late(*args):
            time.sleep(.03)
            return {'reason': 'ok'}
        with mock.patch.object(hp, 'run_attempt', side_effect=late):
            result = hp.probe_health('http://fixture', COMMIT, total_timeout=.01)
        self.assertFalse(result)
        self.assertEqual(result.reason, 'deadline_exhausted')


@unittest.skipIf(os.name == 'nt', 'Production updater requires Linux flock/symlinks')
class UpdaterGateTests(unittest.TestCase):
    def setup_runtime(self, directory, current=COMMIT):
        root = Path(directory) / 'root'
        release = root / 'releases' / current
        release.mkdir(parents=True)
        (root / 'current').symlink_to(release, target_is_directory=True)
        data = Path(directory) / 'data'
        data.mkdir()
        config = Path(directory) / 'config.json'
        config.write_text(json.dumps({'root': str(root), 'data': str(data),
                                      'unit': 'fixture.service', 'health': 'http://fixture/healthz'}))
        return root, data, config

    def run_main(self, directory, current, selected, result, marker=None):
        root, data, config = self.setup_runtime(directory, current)
        if marker is not None:
            (data / '.draining').write_text(marker)
        project = dict(update.PROJECT, id='fixture-health-' + Path(directory).name)
        stream = io.StringIO()
        # Map only persistent state into the fixture. No production paths or
        # credentials are read and no install/systemctl command can execute.
        real_path = Path
        def path(value):
            if str(value) == '/var/lib/' + project['id'] + '-auto-update':
                return real_path(directory) / 'state'
            return real_path(value)
        with mock.patch.object(update, 'Path', side_effect=path), mock.patch.object(update, 'PROJECT', project), \
             mock.patch.object(update, 'GitHub'), mock.patch.object(update, 'select', return_value=(selected, {})), \
             mock.patch.object(update, 'health', return_value=result) as health, mock.patch.object(update, 'command') as command, \
             mock.patch.object(sys, 'argv', ['update.py', '--config', str(config), '--apply']), \
             contextlib.redirect_stdout(stream), mock.patch.dict(update.os.environ, {}, clear=False):
            update.main()
            command.assert_not_called()
            if current == selected:
                health.assert_called_once_with('http://fixture/healthz', COMMIT)
            else:
                health.assert_called_once_with('http://fixture/healthz', COMMIT, idle=True)
        return json.loads(stream.getvalue()), root, data

    def test_unhealthy_current_never_clears_owned_drain_or_deploys(self):
        with tempfile.TemporaryDirectory() as directory:
            row, root, data = self.run_main(directory, COMMIT, COMMIT,
                                            hp.HealthResult(False, 'commit_mismatch'), marker='update:' + COMMIT)
            self.assertEqual(row['state'], 'unhealthy_current')
            self.assertEqual(root.joinpath('current').resolve().name, COMMIT)
            self.assertEqual(data.joinpath('.draining').read_text(), 'update:' + COMMIT)

    def test_idle_failure_never_installs_or_stops_service(self):
        for reason in ('busy', 'ok_false', 'read_timeout', 'schema_invalid'):
            with tempfile.TemporaryDirectory() as directory:
                row, root, _ = self.run_main(directory, COMMIT, '2' * 40,
                                             hp.HealthResult(False, reason))
                self.assertEqual(row['state'], 'waiting_for_idle')
                self.assertEqual(root.joinpath('current').resolve().name, COMMIT)

    def test_same_sha_success_keeps_current_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            row, root, data = self.run_main(directory, COMMIT, COMMIT, hp.HealthResult(True, 'ok'),
                                            marker='update:' + COMMIT)
            self.assertEqual(row['state'], 'current')
            self.assertEqual(root.joinpath('current').resolve().name, COMMIT)
            self.assertFalse(data.joinpath('.draining').exists())
        with tempfile.TemporaryDirectory() as directory:
            _, _, data = self.run_main(directory, COMMIT, COMMIT, hp.HealthResult(True, 'ok'),
                                        marker='external-owner')
            self.assertEqual(data.joinpath('.draining').read_text(), 'external-owner')

    def transaction(self, directory, after_drain):
        selected = '2' * 40
        root, data, config = self.setup_runtime(directory)
        project = dict(update.PROJECT, id='fixture-health-' + Path(directory).name)
        real_path = Path
        def path(value):
            if str(value) == '/var/lib/' + project['id'] + '-auto-update':
                return real_path(directory) / 'state'
            return real_path(value)
        archive_name = project['archive_prefix'] + '-' + selected + '.tar.gz'
        fake_archive = b'fixture artifact; installer is mocked'
        checksum = hashlib.sha256(fake_archive).hexdigest()
        commands = []
        def artifact(item, destination):
            with zipfile.ZipFile(destination, 'w') as archive:
                archive.writestr(archive_name, fake_archive)
                archive.writestr('SHA256SUMS', checksum + '  ' + archive_name + '\n')
        def command(args, **kwargs):
            args = [str(a) for a in args]
            commands.append(args)
            if '--activate' in args:
                target = root / 'releases' / selected
                target.mkdir()
                root.joinpath('current').unlink()
                root.joinpath('current').symlink_to(target)
            return mock.Mock(stdout=b'{}')
        results = ([hp.HealthResult(True, 'ok')] + [hp.HealthResult(False, 'busy')] * 30
                   if not after_drain else [hp.HealthResult(True, 'ok')] * 2
                   + [hp.HealthResult(False, 'commit_mismatch')] * 30)
        gh = mock.Mock()
        gh.file.side_effect = artifact
        stream = io.StringIO()
        error = None
        with mock.patch.object(update, 'Path', side_effect=path), mock.patch.object(update, 'PROJECT', project), \
             mock.patch.object(update, 'GitHub', return_value=gh), \
             mock.patch.object(update, 'select', return_value=(selected, {})), \
             mock.patch.object(update.evidence, 'locate_artifact', return_value={'digest': 'sha256:' + checksum}), \
             mock.patch.object(update, 'health', side_effect=results) as health, \
             mock.patch.object(update, 'command', side_effect=command), \
             mock.patch.object(update, 'backup_data') as backup, \
             mock.patch.object(update.shutil, 'disk_usage', return_value=mock.Mock(free=4 * 1024**3)), \
             mock.patch.object(update.time, 'sleep'), \
             mock.patch.object(sys, 'argv', ['update.py', '--config', str(config), '--apply']), \
             mock.patch.dict(update.os.environ, {}, clear=False), contextlib.redirect_stdout(stream):
            try:
                update.main()
            except ValueError as caught:
                error = caught
            # The initial and drain probes must target the actual old identity.
            self.assertEqual(health.call_args_list[0], mock.call('http://fixture/healthz', COMMIT, idle=True))
            self.assertEqual(health.call_args_list[1], mock.call('http://fixture/healthz', COMMIT, idle=True))
            if after_drain:
                self.assertEqual(health.call_args_list[2], mock.call('http://fixture/healthz', selected))
                backup.assert_called_once()
            else:
                backup.assert_not_called()
        return root, data, commands, stream.getvalue(), error

    def test_draining_retries_exhaustion_never_stops_or_activates(self):
        with tempfile.TemporaryDirectory() as directory:
            root, data, commands, logs, error = self.transaction(directory, after_drain=False)
            self.assertIsNone(error)
            self.assertFalse(any(args[0] == 'systemctl' or '--activate' in args for args in commands))
            self.assertEqual(root.joinpath('current').resolve().name, COMMIT)
            self.assertFalse(data.joinpath('.draining').exists())
            events = [json.loads(line) for line in logs.splitlines()]
            self.assertEqual(events[-1]['state'], 'waiting_for_idle')
            self.assertEqual(sum(row['state'] == 'draining_health' for row in events), 30)

    def test_new_identity_failure_restores_old_runtime_and_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            root, data, commands, logs, error = self.transaction(directory, after_drain=True)
            self.assertIsInstance(error, ValueError)
            self.assertEqual(root.joinpath('current').resolve().name, COMMIT)
            self.assertFalse(data.joinpath('.draining').exists())
            self.assertGreaterEqual(sum(args[:2] == ['systemctl', 'stop'] for args in commands), 2)
            self.assertEqual([args for args in commands if args[0] == 'systemctl'],
                             [['systemctl', 'stop', 'fixture.service'], ['systemctl', 'start', 'fixture.service'],
                              ['systemctl', 'stop', 'fixture.service'], ['systemctl', 'start', 'fixture.service']])
            self.assertFalse(Path(directory).joinpath('state', 'last-success.json').exists())
            self.assertEqual(sum(json.loads(line)['state'] == 'new_service_health' for line in logs.splitlines()), 30)


if __name__ == '__main__':
    unittest.main()
