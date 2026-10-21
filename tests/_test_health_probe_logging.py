"""健康探测的访问日志静默：/healthz 与 /health 一致。

/healthz 是部署更新器每两分钟轮询一次的探针（见 deploy/update.py 的 health()）。
它原先不在 Handler.log_message 的静默前缀里，于是每次探测都要在发响应前同步写一行
访问日志，还会把看板「日志」页刷满无信息量的 200。这个套件钉住三件事：

  1. 正常的 GET /healthz 不落日志，行为与 /health 一致；
  2. 非 200 或非 GET 的 /healthz 照样记录——排障时不能把真正的失败吞掉；
  3. 静默是按路径边界匹配的，/healthz-other 这类相邻路径不受影响。

Run with: python tests/_test_health_probe_logging.py
Only synthetic loopback fixtures; no credentials or upstream requests.
"""
import os
import sys
import unittest
import json
import io
import socket
import threading
import time
from email.message import Message
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy
import wb_health_diagnostics as hd
import wb_bridge


class Probe:
    """Stand-in for the request handler's logging surface."""

    command = "GET"
    log_message = wb_proxy.Handler.log_message

    def __init__(self, path, command="GET"):
        self.path = path
        self.command = command


class HealthProbeLoggingTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        self._saved = wb_proxy.log
        wb_proxy.log = lambda *a, **k: self.records.append(a)

    def tearDown(self):
        wb_proxy.log = self._saved

    def emit(self, path, status="200", command="GET"):
        self.records.clear()
        probe = Probe(path, command)
        probe.log_message("%s %s", path, str(status))
        return list(self.records)

    def test_successful_healthz_get_is_quiet(self):
        self.assertEqual(self.emit("/healthz"), [])

    def test_successful_health_get_stays_quiet(self):
        self.assertEqual(self.emit("/health"), [])

    def test_healthz_with_query_string_is_still_quiet(self):
        self.assertEqual(self.emit("/healthz?probe=1"), [])

    def test_failing_healthz_is_logged(self):
        for status in ("400", "500", "503"):
            self.assertEqual(len(self.emit("/healthz", status)), 1, status)

    def test_non_get_healthz_is_logged(self):
        self.assertEqual(len(self.emit("/healthz", command="POST")), 1)

    def test_lookalike_path_is_not_silenced(self):
        self.assertEqual(len(self.emit("/healthz-other")), 1)
        self.assertEqual(len(self.emit("/healthzcheck")), 1)


class HealthServerDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.now = 10**9
        self.diag = hd.Diagnostics(emit=lambda row: None, clock=lambda: self.now)

    def handler(self, probe_id='a' * 32, path='/healthz', command='GET', host='127.0.0.1'):
        headers = Message()
        headers[hd.HEADER] = probe_id
        return SimpleNamespace(command=command, path=path, client_address=(host, 123), headers=headers,
                               _health_early={'accepted': self.now, 'handler_start': self.now})

    def test_only_exact_local_unique_opaque_header_is_traced(self):
        for kwargs in ({'path': '/healthz?token=secret'}, {'path': '/healthz-other'},
                       {'command': 'POST'}, {'host': '192.0.2.1'}, {'probe_id': 'secret'}):
            self.assertIsNone(self.diag.begin(self.handler(**kwargs)))
        h = self.handler()
        h.headers[hd.HEADER] = 'b' * 32
        self.assertIsNone(self.diag.begin(h))
        self.assertEqual(self.diag.begin(self.handler()), 'a' * 32)

    def test_pending_is_once_then_terminal_and_no_sensitive_fields(self):
        key = self.diag.begin(self.handler())
        self.now += 5 * 10**9
        self.diag.mark(key, 'daily_read')
        rows = self.diag.collect()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['outcome'], rows[0]['last_stage']), ('pending', 'daily_read'))
        self.assertEqual(self.diag.collect(), [])
        self.diag.mark(key, 'daily_done')
        self.diag.finish(key)
        rows = self.diag.collect()
        self.assertEqual([r['outcome'] for r in rows], ['complete'])
        self.assertLessEqual(set(rows[0]), {'state', 'probe_id', 'outcome', 'last_stage',
                                         'server_elapsed_ms', 'stage_offsets_ms', 'response_status'})
        self.assertLessEqual(set(rows[0]['stage_offsets_ms']), set(hd.STAGES))
        self.assertEqual(self.diag.collect(), [])

    def test_missing_completion_expires_and_rate_budget_recovers(self):
        for i in range(32):
            self.assertIsNotNone(self.diag.begin(self.handler('%032x' % i)))
        self.assertIsNone(self.diag.begin(self.handler('f' * 32)))
        self.now += 121 * 10**9
        rows = self.diag.collect()
        self.assertEqual(len(rows), 32)
        self.assertEqual({r['outcome'] for r in rows}, {'expired'})
        self.assertFalse(self.diag.active)
        self.assertIsNotNone(self.diag.begin(self.handler('f' * 32)))

    def test_lock_busy_is_lossy_and_never_waits(self):
        self.diag.lock.acquire()
        try:
            started = time.monotonic()
            self.assertIsNone(self.diag.begin(self.handler()))
            self.diag.mark('a' * 32, 'health_enter')
            self.diag.finish('a' * 32)
            self.assertEqual(self.diag.collect(), [])
            self.assertLess(time.monotonic() - started, .1)
        finally:
            self.diag.lock.release()

    def test_preparse_ring_and_completed_queue_have_fixed_bounds(self):
        with socket.socket() as sock:
            self.diag.accepted(sock, ('127.0.0.1', 1))
            self.assertEqual(self.diag.take_accepted(sock), self.now)
            self.assertIsNone(self.diag.take_accepted(sock))
        socks = [socket.socket() for _ in range(300)]
        try:
            for sock in socks:
                self.diag.accepted(sock, ('127.0.0.1', 1))
            self.assertEqual(len(self.diag.accepts), 256)
        finally:
            for sock in socks:
                sock.close()
        for i in range(160):
            self.now += 61 * 10**9
            key = self.diag.begin(self.handler('%032x' % i))
            self.diag.finish(key)
        self.assertEqual(len(self.diag.finished), 128)

    def test_accept_object_reuse_cannot_supply_old_timestamp(self):
        with socket.socket() as old, socket.socket() as new:
            self.diag.accepted(old, ('127.0.0.1', 1))
            self.diag.accepts[id(new)] = self.diag.accepts.pop(id(old))
            self.assertIsNone(self.diag.take_accepted(new))

    def test_observer_emits_outside_lock_and_io_failure_is_lossy(self):
        key = self.diag.begin(self.handler())
        self.diag.finish(key)
        seen = []
        def emit(row):
            self.assertTrue(self.diag.lock.acquire(False))
            self.diag.lock.release()
            seen.append(row)
            raise BrokenPipeError('private telemetry error')
        self.diag.emit = emit
        with mock.patch.object(hd.time, 'sleep', side_effect=[None, KeyboardInterrupt()]):
            with self.assertRaises(KeyboardInterrupt):
                self.diag._observe()
        self.assertEqual(len(seen), 1)
        self.assertFalse(self.diag.active)
        self.assertEqual(self.diag.collect(), [])

    def test_diagnostic_internal_exception_does_not_escape_handler_hook(self):
        with mock.patch.object(self.diag, 'clock', side_effect=OSError('private error')):
            self.assertIsNone(self.diag.begin(self.handler()))
        self.assertTrue(self.diag.lock.acquire(False))
        self.diag.lock.release()

    def test_stage_order_and_status_are_allowlisted(self):
        key = self.diag.begin(self.handler())
        self.diag.mark(key, 'health_enter')
        self.diag.mark(key, 'headers_write', 200)
        self.diag.mark(key, 'secret', 999)
        self.diag.mark(key, 'health_enter', True)
        self.diag.finish(key, 'write_error')
        row, = self.diag.collect()
        self.assertEqual(row['response_status'], 200)
        self.assertEqual(row['last_stage'], 'headers_write')
        self.assertNotIn('secret', json.dumps(row))

    def test_health_callback_keeps_live_busy_without_release_io(self):
        proxy = SimpleNamespace(MAX_CONCURRENT_CHAT=4, _chat_slots=SimpleNamespace(_value=2),
                                POOL=SimpleNamespace(accounts=[SimpleNamespace(
                                    _refresh_lock=SimpleNamespace(locked=lambda: True),
                                    _credits_lock=SimpleNamespace(locked=lambda: False))]),
                                SCHEDULER=SimpleNamespace(_run_lock=SimpleNamespace(locked=lambda: True)))
        stages = []
        with mock.patch.object(wb_bridge, '_bridge', SimpleNamespace(active=3)), \
             mock.patch.object(wb_bridge, 'daily_enabled', return_value=False) as daily, \
             mock.patch.object(wb_bridge, '_HEALTH_COMMIT', 'a' * 40), \
             mock.patch.object(wb_bridge.Path, 'open', side_effect=AssertionError('health must not read disk')) as release:
            value = wb_bridge.health(proxy, stages.append)
        self.assertEqual(value['activeJobs'], 7)
        self.assertFalse(value['internationalDailyChat'])
        daily.assert_called_once()
        self.assertEqual(value['commit'], 'a' * 40)
        release.assert_not_called()
        self.assertEqual(stages, ['release_read', 'release_done', 'busy_read', 'busy_done', 'daily_read', 'daily_done'])

    def test_release_identity_is_bounded_and_invalid_metadata_fails_closed(self):
        values = [b'[]', b'null', b'1', b'{}', b'{"commit":true}', b'{"commit":1}',
                  b'{"commit":null}', b'{"commit":[]}', b'{"commit":"short"}',
                  json.dumps({'commit': 'A' * 40}).encode(), b'broken', b'\xff',
                  json.dumps({'commit': 'a' * 40}).encode() + b' ' * 4096]
        for raw in values:
            with self.subTest(raw=raw[:60]), mock.patch.object(wb_bridge.Path, 'open', return_value=io.BytesIO(raw)):
                self.assertEqual(wb_bridge._read_release_commit(), 'development')
        with mock.patch.object(wb_bridge.Path, 'open', side_effect=OSError('unavailable')):
            self.assertEqual(wb_bridge._read_release_commit(), 'development')
        stream = mock.MagicMock()
        stream.__enter__.return_value = stream
        stream.read.return_value = json.dumps({'commit': 'b' * 40}).encode()
        with mock.patch.object(wb_bridge.Path, 'open', return_value=stream):
            self.assertEqual(wb_bridge._read_release_commit(), 'b' * 40)
        stream.read.assert_called_once_with(4097)

    def test_import_pins_release_identity_but_busy_stays_live(self):
        source = wb_bridge.Path(wb_bridge.__file__).read_text()
        code = compile(source, wb_bridge.__file__, 'exec')
        proxy = SimpleNamespace(MAX_CONCURRENT_CHAT=4, _chat_slots=SimpleNamespace(_value=4),
                                POOL=None, SCHEDULER=None)
        for raw, expected in ((json.dumps({'commit': 'c' * 40}).encode(), 'c' * 40),
                              (b'[]', 'development')):
            module = {'__name__': 'synthetic_health_identity', '__file__': wb_bridge.__file__}
            with mock.patch.object(wb_bridge.Path, 'open', return_value=io.BytesIO(raw)) as release:
                exec(code, module)
            release.assert_called_once()
            with mock.patch.object(wb_bridge.Path, 'open', side_effect=AssertionError('no late identity read')):
                proxy._chat_slots._value = 4
                self.assertEqual(module['health'](proxy)['commit'], expected)
                self.assertEqual(module['health'](proxy)['activeJobs'], 0)
                proxy._chat_slots._value = 1
                self.assertEqual(module['health'](proxy)['activeJobs'], 3)
                self.assertEqual(module['health'](proxy)['commit'], expected)

    def test_real_gateway_pending_health_read_then_complete_without_access_log(self):
        self.diag.clock = time.monotonic_ns
        entered, release = threading.Event(), threading.Event()
        def health(proxy, trace=None):
            if trace:
                trace('release_read')
            entered.set()
            if not release.wait(3):
                raise RuntimeError('fixture join timed out')
            if trace:
                for stage in ('release_done', 'busy_read', 'busy_done', 'daily_read', 'daily_done'):
                    trace(stage)
            return {'ok': True, 'commit': 'development', 'activeJobs': 0}
        with mock.patch.object(hd, 'DIAGNOSTICS', self.diag), \
             mock.patch.object(self.diag, 'start'), \
             mock.patch.object(wb_bridge, 'health', side_effect=health), \
             mock.patch.object(wb_proxy, 'cors_origin_allowed', return_value=False), \
             mock.patch.object(wb_proxy, 'log') as access_log:
            httpd = wb_proxy.GatewayServer(('127.0.0.1', 0), wb_proxy.Handler)
            httpd.daemon_threads = True
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with socket.create_connection(httpd.server_address, timeout=3) as client:
                    client.sendall(('GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n'
                                    + hd.HEADER + ': ' + 'a' * 32 + '\r\n\r\n').encode())
                    self.assertTrue(entered.wait(2))
                    self.diag.clock = lambda: time.monotonic_ns() + 5 * 10**9
                    row, = self.diag.collect()
                    self.assertEqual((row['probe_id'], row['outcome'], row['last_stage']),
                                     ('a' * 32, 'pending', 'release_read'))
                    self.assertIn('accepted', row['stage_offsets_ms'])
                    self.assertIn('handler_start', row['stage_offsets_ms'])
                    release.set()
                    response = b''
                    while True:
                        data = client.recv(65536)
                        if not data:
                            break
                        response += data
                    self.assertIn(b'200 OK', response)
                    self.assertEqual(json.loads(response.split(b'\r\n\r\n', 1)[1])['activeJobs'], 0)
                row, = self.diag.collect()
                self.assertEqual((row['outcome'], row['last_stage'], row['response_status']),
                                 ('complete', 'body_flushed', 200))
                access_log.assert_not_called()
            finally:
                release.set()
                httpd.shutdown()
                httpd.server_close()
                thread.join(2)
                self.assertFalse(thread.is_alive())

    def test_handler_exception_has_one_latched_outcome(self):
        headers = Message()
        headers[hd.HEADER] = 'a' * 32
        class FailingHandler:
            key_entry = None
            max_request_line = 1024
            rfile = SimpleNamespace(readline=lambda limit: b'GET /healthz HTTP/1.1\r\n')
            wfile = SimpleNamespace(flush=lambda: None)
            client_address = ('127.0.0.1', 1)
            path = '/healthz'
            command = 'GET'
            def parse_request(self):
                self.headers = headers
                return True
            def do_GET(self):
                raise ValueError('fixture handler failure')
        handler = FailingHandler()
        with mock.patch.object(hd, 'DIAGNOSTICS', self.diag):
            with self.assertRaises(ValueError):
                wb_proxy.Handler.handle_one_request(handler)
        row, = self.diag.collect()
        self.assertEqual(row['outcome'], 'handler_error')
        self.assertFalse(self.diag.active)

    def test_keepalive_second_request_resets_early_state_and_write_error(self):
        headers = Message()
        headers[hd.HEADER] = 'a' * 32
        class TwoRequests:
            key_entry = None
            max_request_line = 1024
            rfile = SimpleNamespace(readline=lambda limit: b'GET /healthz HTTP/1.1\r\n')
            wfile = SimpleNamespace(flush=lambda: None)
            client_address = ('127.0.0.1', 1)
            path = '/healthz'
            command = 'GET'
            _health_early = {'accepted': 10**9, 'handler_start': 10**9}
            def parse_request(self):
                self.headers = headers
                return True
            def do_GET(self):
                self.wfile.write(b'fixture response')
        handler = TwoRequests()
        handler.wfile = SimpleNamespace(flush=lambda: None,
                                        write=mock.Mock(side_effect=BrokenPipeError('fixture write failure')))
        with mock.patch.object(hd, 'DIAGNOSTICS', self.diag):
            with self.assertRaises(BrokenPipeError):
                wb_proxy.Handler.handle_one_request(handler)
            first, = self.diag.collect()
            self.assertEqual(first['outcome'], 'write_error')
            self.assertEqual(handler._health_early, {})
            headers.replace_header(hd.HEADER, 'b' * 32)
            handler.do_GET = lambda: None
            wb_proxy.Handler.handle_one_request(handler)
            second, = self.diag.collect()
        self.assertEqual(second['outcome'], 'complete')
        self.assertEqual(second['probe_id'], 'b' * 32)
        self.assertNotIn('accepted', second['stage_offsets_ms'])
        self.assertNotIn('handler_start', second['stage_offsets_ms'])


if __name__ == "__main__":
    unittest.main()
