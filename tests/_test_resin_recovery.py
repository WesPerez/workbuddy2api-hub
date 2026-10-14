"""Gateway leases: exact identity, guarded recovery, bounded failure and no replay."""
import copy
import base64
import http.client
import io
import json
import os
from pathlib import Path
import ssl
import sys
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _isolated_dirs import isolated_data_dirs
_TMP = isolated_data_dirs("wb-resin-")
os.environ["WB_DATA_DIR"] = _TMP.name
import wb_accounts as A
import wb_resin as R
import wb_bridge as B


def account(uid="u", ref="account-hub:1"):
    return A.Account({"uid": uid, "realm": "intl", "accessToken": "synthetic-token",
        "bridgeRef": ref, "source": "account-hub", "bridgeRoute": {
            "key": "github-1", "proxy": {"server": "http://127.0.0.1:10834",
                "username": "LoginPool.github-1", "password": "synthetic-secret"},
            "binding": {"platform": "LoginPool", "nodeHash": "a" * 32,
                "ip": "203.0.113.1", "leaseGuardVersion": 1, "leaseCreatedAt": "101"}}})


def result(route, generation="201", status="available", node="b", ip="203.0.113.2"):
    return {"recovery_version": 1, "status": status, "lease": {
        "account": route["key"], "platform_id": "fixture", "node_hash": node * 32,
        "egress_ip": ip, "created_at_ns": generation, "lease_guard_version": 1}}


class ResinTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"WB_RESIN_PLATFORM": "AppsGlobal"})
        self.env.start()
        R._states.clear()
        self.acc = account()
        self.calls = []
        self.generation = "201"
        self.status = "available"
        def control(route, action, body):
            self.calls.append((copy.deepcopy(route), action, body))
            return result(route, self.generation, self.status)
        self.control = patch.object(R, "_control", side_effect=control)
        self.control.start()

    def tearDown(self):
        self.control.stop()
        self.env.stop()
        R._states.clear()

    def test_one_account_one_apps_lease_preserves_login_route_and_redacts_receipt(self):
        original = copy.deepcopy(self.acc.bridge_route)
        proxy = self.acc.proxy
        self.assertIsInstance(proxy, R.ManagedProxy)
        self.assertIn("AppsGlobal.", proxy)
        self.assertIn("~r1~", proxy)
        self.assertEqual(proxy.route["key"], R.identity(self.acc))
        self.assertNotEqual(R.identity(self.acc), R.identity(account("v")))
        self.assertEqual(self.acc.bridge_route, original)
        receipt = (Path(_TMP.name) / "resin-leases.json").read_text()
        self.assertNotIn("synthetic-secret", receipt)
        self.assertNotIn("synthetic-token", receipt)
        self.assertEqual(self.acc.public()["egress"]["platform"], "AppsGlobal")

    def test_parallel_acquire_is_single_flight_and_does_not_write_credentials(self):
        values = []
        with patch.object(self.acc, "save") as save:
            threads = [threading.Thread(target=lambda: values.append(self.acc.proxy)) for _ in range(8)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(3)
            save.assert_not_called()
        self.assertEqual(len(values), 8)
        self.assertEqual(len(self.calls), 1)

    def test_ipv6_api_lease_keeps_exact_guard_without_relaxing_browser_login(self):
        route = R.source_route(self.acc)
        with patch.object(R, "_control", return_value=result(route, ip="2001:db8::7")):
            proxy = self.acc.proxy
        username = urllib.parse.unquote(urllib.parse.urlsplit(proxy).username)
        encoded_ip = username.rsplit("~", 1)[1]
        self.assertEqual(base64.urlsafe_b64decode(encoded_ip + "=" * (-len(encoded_ip) % 4)).decode(),
                         "2001:db8::7")
        self.assertEqual(self.acc.public()["egress"]["ip"], "2001:db8::7")
        with self.assertRaises(ValueError):
            B.guarded_proxy(proxy.route)
        for ip in ("fe80::1%eth0", "::", "::1", "ff02::1", "not-an-ip"):
            R._states.clear()
            with patch.object(R, "_control", return_value=result(route, ip=ip)), self.assertRaises(R.RouteUnavailable):
                self.acc.proxy

    def test_connect_409_reacquires_once_and_uses_new_generation(self):
        proxy = self.acc.proxy
        self.generation = "202"
        rejection = urllib.error.URLError(OSError("Tunnel connection failed: 409 Conflict"))
        response = io.BytesIO(b"OK")
        with patch.object(A.wb_upstream_pool, "urlopen", side_effect=[rejection, response]) as upstream:
            with A.urlopen(urllib.request.Request("https://www.workbuddy.ai/v3/config"), proxy=proxy) as reply:
                self.assertEqual(reply.read(), b"OK")
        self.assertEqual(upstream.call_count, 2)
        self.assertIn("~202~", str(upstream.call_args.kwargs["proxy"]))
        self.assertEqual([row[1] for row in self.calls], ["acquire", "acquire"])

    def test_origin_409_quota_and_tls_certificate_error_never_rotate_or_replay(self):
        proxy = self.acc.proxy
        errors = [urllib.error.HTTPError("https://www.workbuddy.ai", n, "fixture", {}, io.BytesIO()) for n in (409, 429, 402)]
        errors.append(urllib.error.URLError(ssl.SSLCertVerificationError("fixture")))
        for error in errors:
            with self.subTest(type=type(error).__name__), patch.object(A.wb_upstream_pool, "urlopen", side_effect=error) as upstream:
                with self.assertRaises(type(error)):
                    A.urlopen(urllib.request.Request("https://www.workbuddy.ai/chat/completions", data=b"{}"), proxy=proxy)
                self.assertEqual(upstream.call_count, 1)
        self.assertEqual(len(self.calls), 1)

    def test_transport_threshold_reports_observed_generation_and_never_replays_post(self):
        proxy = self.acc.proxy
        self.generation, self.status = "202", "rotated"
        for _ in range(2):
            with patch.object(A.wb_upstream_pool, "urlopen", side_effect=ConnectionResetError()) as upstream:
                with self.assertRaises(ConnectionResetError):
                    A.urlopen(urllib.request.Request("https://www.workbuddy.ai/chat/completions", data=b"{}"), proxy=proxy)
                self.assertEqual(upstream.call_count, 1)
        report = self.calls[-1]
        self.assertEqual(report[1], "report-failure")
        self.assertEqual(report[2]["expected_created_at_ns"], "201")
        self.assertEqual(report[2]["expected_node_hash"], "b" * 32)
        count = len(self.calls)
        R.note_failure(proxy, ConnectionResetError())
        self.assertEqual(len(self.calls), count, "late failure must not rotate a new generation")

    def test_control_outage_no_alternative_and_limited_recovery_fail_closed_with_backoff(self):
        for status in ("no_alternative", "recovery_limited"):
            R._states.clear()
            self.calls.clear()
            self.status = status
            for _ in range(2):
                with self.assertRaises(R.RouteUnavailable): self.acc.proxy
            self.assertEqual(len(self.calls), 1)
        R._states.clear()
        with patch.object(R, "_control", side_effect=R.RouteUnavailable("fixture")) as control:
            for _ in range(2):
                with self.assertRaises(R.RouteUnavailable): self.acc.proxy
            self.assertEqual(control.call_count, 1)

    def test_invalid_capability_identity_and_numeric_generation_fail_closed(self):
        route = R.source_route(self.acc)
        for field, value in (("account", "other"), ("created_at_ns", 201), ("lease_guard_version", 0)):
            R._states.clear()
            body = result(route)
            body["lease"][field] = value
            with patch.object(R, "_control", return_value=body), self.assertRaises(R.RouteUnavailable): self.acc.proxy

    def test_stream_failures_count_but_client_cancellation_does_not(self):
        proxy = self.acc.proxy
        first = R.ManagedResponse(io.BytesIO(b"data"), proxy, True)
        first.read(1)
        first.close()
        self.assertEqual(R._states[self.acc.uid].get("failures"), 0)
        empty = R.ManagedResponse(io.BytesIO(), proxy, True)
        empty.read()
        self.assertEqual(R._states[self.acc.uid]["failures"], 1)
        self.assertEqual(len(self.calls), 1)

    def test_unbounded_http_read_obeys_body_framing_and_never_waits_for_socket_eof(self):
        proxy = self.acc.proxy
        class Socket:
            def __init__(self, wire): self.wire = wire
            def makefile(self, *_): return io.BytesIO(self.wire)
        wires = (
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOKnext-response",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nOK\r\n0\r\n\r\n",
        )
        for wire in wires:
            with self.subTest(chunked=b"chunked" in wire):
                response = http.client.HTTPResponse(Socket(wire))
                response.begin()
                managed = R.ManagedResponse(response, proxy)
                self.assertEqual(managed.read(), b"OK")
                self.assertTrue(response.isclosed())
                self.assertTrue(managed.finished)
        self.assertEqual(len(self.calls), 1, "reading a framed body must not rotate its route")


if __name__ == "__main__": unittest.main()
