"""Independent sources preserve token ownership, defaults and protected egress."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _isolated_dirs import isolated_data_dirs
_TMP = isolated_data_dirs('wb-capability-tests-')
import wb_accounts as A
import wb_proxy as P
import wb_settings as S
import wb_sources as C


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.account = A.Account({'uid': 'fixture', 'realm': 'cn', 'accessToken': 'account-only',
                                  'refreshToken': 'refresh-only'}, str(self.root / 'fixture.json'))

    def source(self, kind, realm='cn', **overrides):
        key = self.root / (kind + '-' + realm + '.key')
        key.write_text(kind + '-independent')
        key.chmod(0o600)
        value = {'enabled': True, 'base_url': 'https://' + kind + '.example.test/prefix',
                 'auth': 'bearer', 'key_file': str(key), **overrides}
        S.set_upstream_config(str(self.root), {'capabilities': {kind: {realm: value}}})
        return value

    def test_defaults_preserve_native_urls_headers_and_product(self):
        for kind, base in [('billing', A.get_realm_config('cn')['billing_upstream']),
                           ('models', A.get_realm_config('cn')['chat_upstream']),
                           ('chat', A.wb_identity.endpoint_for('cn', self.account.product)[0])]:
            url, headers, proxy = self.account.capability_snapshot(kind, '/fixture')
            self.assertEqual(url, base + '/fixture')
            self.assertEqual(headers['Authorization'], 'Bearer account-only')
            self.assertNotIsInstance(headers, C.SourceHeaders)
            self.assertEqual(proxy, self.account.proxy)
        self.account.set_product('vscode')
        self.assertEqual(self.account.capability_snapshot('chat')[0], self.account.chat_base_url())

    def test_sources_realms_and_credentials_are_independent(self):
        for kind in C.KINDS:
            self.source(kind)
        for kind in C.KINDS:
            url, headers, _ = self.account.capability_snapshot(kind, '/read')
            self.assertEqual(url, 'https://' + kind + '.example.test/prefix/read')
            self.assertEqual(headers['Authorization'], 'Bearer ' + kind + '-independent')
            self.assertNotIn('X-User-Id', headers)
            self.assertNotIn('X-Device-Token', headers)
            self.assertNotIn('X-Refresh-Token', headers)
            request = C.request(url, headers=headers)
            self.assertEqual(request.capability_kind, kind)
            self.assertNotIn('capability_source', dict(request.header_items()))
            with self.assertRaises(C.SourceError):
                C.request('https://other.example/read', headers=headers)
        self.account.realm = 'intl'
        self.assertEqual(self.account.capability_snapshot('billing')[0], A.get_realm_config('intl')['billing_upstream'])

    def test_partial_settings_keep_other_sources_and_never_echo_credentials(self):
        self.source('billing')
        self.source('models', auth='header', header_name='X-Reader-Key')
        S.set_upstream_config(str(self.root), {'capabilities': {'billing': {'cn': {'timeout_seconds': 9}}}})
        stored = S.upstream_config(str(self.root))
        self.assertTrue(stored['capabilities']['models']['cn']['enabled'])
        self.assertEqual(stored['capabilities']['billing']['cn']['timeout_seconds'], 9)
        self.assertNotIn('billing-independent', json.dumps(stored))
        headers = self.account.capability_snapshot('models')[1]
        self.assertEqual(headers['X-Reader-Key'], 'models-independent')
        self.assertNotIn('Authorization', headers)

    def test_no_auth_does_not_read_a_key_and_account_auth_stays_at_origin(self):
        self.source('billing', auth='none')
        with mock.patch.object(C, 'read_key', side_effect=AssertionError('unexpected key read')):
            self.assertNotIn('Authorization', self.account.capability_snapshot('billing')[1])
        self.source('billing', auth='account')
        with self.assertRaises(C.SourceError): self.account.capability_snapshot('billing')
        self.source('billing', auth='account', base_url=A.get_realm_config('cn')['billing_upstream'])
        self.assertEqual(self.account.capability_snapshot('billing')[1]['Authorization'], 'Bearer account-only')

    def test_refresh_remains_on_original_auth_origin(self):
        self.source('chat')
        self.source('billing')
        with mock.patch.object(A, 'http_json', side_effect=ValueError('stop after inspecting request')) as transport:
            self.assertFalse(self.account.refresh())
        self.assertEqual(transport.call_args.args[0], A.get_realm_config('cn')['chat_upstream'] + A.REFRESH_PATH)
        self.assertEqual(transport.call_args.kwargs['headers']['X-Refresh-Token'], 'refresh-only')

    def test_missing_unsafe_key_and_invalid_configs_fail_closed(self):
        value = self.source('billing')
        Path(value['key_file']).chmod(0o644)
        with self.assertRaises(C.SourceError): self.account.capability_snapshot('billing')
        Path(value['key_file']).unlink()
        with self.assertRaises(C.SourceError): self.account.capability_snapshot('billing')
        for invalid in [{'base_url': 'http://remote.example'}, {'header_name': 'Host'},
                        {'enabled': 'false'}, {'timeout_seconds': True}, {'key_file': 'relative'}]:
            with self.assertRaises(C.SourceError):
                S.set_upstream_config(str(self.root), {'capabilities': {'models': {'cn': invalid}}})

    def test_independent_writes_are_not_replayed_on_ambiguous_failures(self):
        self.source('billing')
        url, headers, _ = self.account.capability_snapshot('billing', '/write')
        with mock.patch.object(A, 'urlopen', side_effect=TimeoutError('ambiguous')) as transport:
            with self.assertRaises(TimeoutError): A.http_json(url, data=b'{}', headers=headers)
        self.assertEqual(transport.call_count, 1)

    def test_billing_call_sites_use_their_source(self):
        self.source('billing')
        for operation in (self.account._checkin_upstream, self.account._fetch_credits_cn_detailed,
                          self.account._fetch_credits_fallback, self.account._fetch_credits_enterprise):
            with mock.patch.object(A, 'http_json', return_value={'code': 0, 'data': {}}) as transport:
                operation()
                self.assertTrue(transport.called)
                for call in transport.call_args_list:
                    self.assertTrue(call.args[0].startswith('https://billing.example.test/prefix/'))
                    self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer billing-independent')

    def test_model_directory_uses_models_source_without_changing_chat(self):
        self.source('models')
        pool = A.AccountPool(str(self.root))
        pool.accounts = [self.account]
        payload = {'code': 0, 'data': {'agents': [{'name': 'cli', 'models': ['fixture-model']}]}}
        with mock.patch.object(P, 'POOL', pool), mock.patch.object(A, 'urlopen', return_value=io.BytesIO(json.dumps(payload).encode())) as transport:
            result = P.fetch_remote_product_config('cn')
        self.assertEqual(result[0], ['fixture-model'])
        req = transport.call_args.args[0]
        self.assertEqual(req.full_url, 'https://models.example.test/prefix' + P.REMOTE_CONFIG_PATH)
        self.assertEqual(req.get_header('Authorization'), 'Bearer models-independent')
        self.assertNotEqual(self.account.chat_base_url(), 'https://models.example.test/prefix')

    def test_redirect_and_pool_bypass_never_forward_credentials(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                seen.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/sink')
                self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = 'http://127.0.0.1:' + str(server.server_port)
            req = C.request(base + '/start', headers=C.SourceHeaders({'Authorization': 'Bearer fixture'}, base, 3, 'models'))
            with mock.patch.object(A.wb_upstream_pool, 'enabled', return_value=False):
                with self.assertRaises(C.SourceError): A.urlopen(req)
            self.assertEqual(seen, ['/start'])
            with mock.patch.object(A.wb_upstream_pool, 'enabled', return_value=True), mock.patch.object(A.wb_upstream_pool, 'urlopen', side_effect=A.wb_upstream_pool.PoolBypass('redirect')), mock.patch.object(A.urllib.request, 'urlopen') as fallback:
                with self.assertRaises(C.SourceError): A.urlopen(req)
                fallback.assert_not_called()
            with mock.patch.object(A.wb_bridge, 'strict_proxy', return_value=True), mock.patch.object(A.wb_upstream_pool, 'urlopen', side_effect=A.wb_upstream_pool.PoolBypass('redirect')), mock.patch.object(A.urllib.request, 'urlopen') as fallback:
                with self.assertRaises(RuntimeError): A.urlopen(req, proxy='http://protected.example')
                fallback.assert_not_called()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == '__main__': unittest.main()
