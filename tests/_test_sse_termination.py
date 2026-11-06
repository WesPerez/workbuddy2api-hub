import io
import os
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wb_proxy as proxy


class DoneOnly:
    def __iter__(self):
        yield b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
        yield b'data: [DONE]\n\n'
        raise AssertionError('read after terminal event')


class Read1Stream:
    def __init__(self):
        self.reads = 0

    def read1(self, size):
        self.reads += 1
        if self.reads > 1:
            raise AssertionError('read after terminal event')
        return (b'data: {"id":"x","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
                b'data: {"id":"x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
                b'data: [DONE]\n\n')


@mock.patch.dict(os.environ, {"WB_STOP_AT_SSE_DONE": "1"})
class ProtocolRegression(unittest.TestCase):
    def test_default_keeps_reading_after_done(self):
        class Continued:
            reached_end = False
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
                yield b"data: [DONE]\n\n"
                self.reached_end = True
        with mock.patch.dict(os.environ, {"WB_STOP_AT_SSE_DONE": "0"}):
            # Raw converters retain the optional upstream behavior. The HTTP
            # gateway's completion validator always stops at a validated DONE.
            for consume in (lambda stream: proxy.aggregate_stream(stream, 'model', 'id'),
                            lambda stream: list(proxy.stream_responses_events(stream, 'model', {}))):
                stream = Continued()
                consume(stream)
                self.assertTrue(stream.reached_end)

    def test_aggregate_stops_at_done(self):
        proxy.aggregate_stream(DoneOnly(), 'model', 'id')

    def test_responses_stops_at_done(self):
        list(proxy.stream_responses_events(DoneOnly(), 'model', {}))

    def run_chat(self, stream):
        output = io.BytesIO()
        handler = types.SimpleNamespace(wfile=output, _key_id=lambda: None)
        account = types.SimpleNamespace(uid='test')
        with mock.patch.object(proxy.wb_settings, 'egress_backoff_enabled', return_value=False), \
             mock.patch.object(proxy, 'record_upstream_usage'), \
             mock.patch.object(proxy, 'record_error') as error:
            proxy.Handler._chat_stream_response(handler, stream, 'model', None, account,
                                                time.time(), headers_sent=True)
        error.assert_not_called()
        return output.getvalue()

    def test_chat_stops_at_done(self):
        self.assertIn(b'data: [DONE]\n\n', self.run_chat(DoneOnly()))
        with mock.patch.dict(os.environ, {"WB_STOP_AT_SSE_DONE": "0"}):
            self.assertIn(b'data: [DONE]\n\n', self.run_chat(DoneOnly()))

    def test_done_without_generated_output_remains_a_failure(self):
        handler = types.SimpleNamespace(wfile=io.BytesIO(), _key_id=lambda: None)
        with mock.patch.object(proxy, 'record_error') as error:
            proxy.Handler._chat_stream_response(
                handler, iter([b'data: [DONE]\n\n']), 'model', None,
                types.SimpleNamespace(uid='test'), time.time(), headers_sent=True)
        self.assertEqual(error.call_count, 1)
        self.assertIn('empty upstream response', error.call_args.args[2])

    def test_chat_read1_delivers_finish_usage_and_done_without_another_read(self):
        stream = Read1Stream()
        output = self.run_chat(stream)
        self.assertEqual(stream.reads, 1)
        self.assertIn(b'"finish_reason": "stop"', output)
        self.assertIn(b'"total_tokens": 2', output)
        self.assertIn(b'data: [DONE]\n\n', output)

    def test_reset_direction_is_not_inferred_from_exception_type(self):
        class Upstream:
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
                raise ConnectionResetError('upstream reset')
            def close(self):
                pass
        class Downstream(io.BytesIO):
            def write(self, data):
                raise ConnectionResetError('client reset')
        account = types.SimpleNamespace(uid='test')
        for protocol in ('chat', 'responses', 'messages'):
            for direction in ('upstream', 'client'):
                with self.subTest(protocol=protocol, direction=direction):
                    handler = types.SimpleNamespace(
                        path='/v1/responses', wfile=Downstream() if direction == 'client' else io.BytesIO(),
                        _key_id=lambda: None, send_response=lambda *a: None,
                        send_header=lambda *a: None, end_headers=lambda: None)
                    source = Upstream() if direction == 'upstream' else DoneOnly()
                    with mock.patch.object(proxy.wb_settings, 'egress_backoff_enabled', return_value=False), \
                         mock.patch.object(proxy, 'record_upstream_usage') as usage, \
                         mock.patch.object(proxy, 'record_error') as error:
                        if protocol == 'chat':
                            proxy.Handler._chat_stream_response(handler, source, 'model', None, account, time.time(), headers_sent=True)
                        elif protocol == 'responses':
                            proxy.Handler._responses_stream_response(handler, source, 'model', {}, {}, None, account, time.time())
                        else:
                            proxy.Handler._messages_stream_response(handler, source, 'model', None, account, time.time())
                    if direction == 'upstream':
                        self.assertEqual(error.call_count, 1)
                        self.assertEqual(error.call_args.kwargs['outcome'], 'upstream_aborted')
                        usage.assert_not_called()
                    else:
                        self.assertEqual(usage.call_count, 1)
                        self.assertEqual(usage.call_args.kwargs['outcome'], 'client_aborted')
                        error.assert_not_called()

    def test_downstream_os_errors_and_context_exit_keep_direction(self):
        for exc in (TimeoutError('write timed out'), OSError('write failed')):
            def write(data):
                raise exc
            with self.subTest(error=type(exc).__name__):
                heartbeat = proxy.SSEHeartbeat(write, interval=10)
                try:
                    with self.assertRaises(proxy.ClientStreamDisconnected):
                        heartbeat.write(b'data: next\n\n')
                    with self.assertRaises(proxy.ClientStreamDisconnected):
                        heartbeat.__exit__(None, None, None)
                finally:
                    heartbeat.close()

    def test_failed_background_heartbeat_is_observable(self):
        def write(data):
            raise ConnectionResetError('client reset')
        heartbeat = proxy.SSEHeartbeat(write, interval=0.05)
        try:
            heartbeat._thread.join(timeout=1)
            with self.assertRaises(proxy.ClientStreamDisconnected):
                heartbeat.write(b'data: next\n\n')
        finally:
            heartbeat.close()


if __name__ == '__main__':
    unittest.main()
