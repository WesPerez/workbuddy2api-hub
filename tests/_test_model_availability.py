"""Advertised availability follows semantic evidence, including empty SSE failures."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _isolated_dirs import isolated_data_dirs
_TMP = isolated_data_dirs("wb-model-availability-")
import wb_accounts as A
import wb_proxy as P


def sse(delta=None, usage=None):
    chunk = {"choices": [{"delta": delta or {}, "finish_reason": "stop"}]}
    if usage is not None:
        chunk["usage"] = usage
    return io.BytesIO(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.usage = Path(root.name) / "usage.jsonl"
        self.accounts = [A.Account({"uid": uid, "accessToken": "fixture", "realm": "intl"})
                         for uid in ("one", "two")]
        self.pool = SimpleNamespace(accounts=self.accounts,
            get=lambda uid: next((a for a in self.accounts if a.uid == uid), None))
        for target, name, value in ((P, "USAGE_DIR", root.name), (P, "USAGE_LOG", str(self.usage)),
                (P, "POOL", self.pool), (P, "_model_observation_cache", {"path": None, "rows": {}}),
                (P, "_models_cache", {})):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.now = time.time()

    def observe(self, status, account="one", model="m", realm="intl", at=None):
        row = {"at": self.now if at is None else at, "model": model, "realm": realm, "account": account}
        if status == "available":
            row.update(outcome="completed", response_validated=True)
        elif status == "unavailable":
            row.update(status=400, message=json.dumps({"code": 11102,
                "msg": "model [%s] service info not found" % model}))
        else:
            row.update(status=502, message=P.EMPTY_UPSTREAM_RESPONSE)
        P.remember_model_observation(row)

    def listing(self, now=None):
        return P.model_listing("intl", [("m", {"credits": "x0.42"})], now or self.now)

    def test_catalog_membership_is_not_a_successful_probe(self):
        data, excluded = self.listing()
        self.assertEqual(data[0]["availability"]["status"], "unverified")
        self.assertEqual(excluded, [])

    def test_transport_and_account_failures_do_not_claim_model_unavailable(self):
        for code, message in ((502, "read timed out"), (401, "expired"), (429, "rate limited"),
                              (400, '{"code":11102,"msg":"other service info not found"}')):
            self.assertIsNone(P.model_observation({"at": self.now, "model": "m", "status": code,
                                                  "message": message}))

    def test_rejection_and_empty_response_stay_excluded_until_success(self):
        for state in ("unavailable", "degraded"):
            self.observe(state)
            data, excluded = self.listing(self.now + 172800)
            self.assertEqual(data, [])
            self.assertEqual(excluded[0]["availability"]["status"], state)
            self.assertTrue(excluded[0]["availability"]["recheckDue"])
            self.assertEqual(excluded[0]["credits"], "x0.42")
            self.observe("available", at=self.now + 172801)
            data, excluded = self.listing(self.now + 172802)
            self.assertEqual(excluded, [])
            self.assertEqual(data[0]["availability"]["status"], "available")
            self.now += 172810

    def test_one_account_failure_does_not_hide_another_accounts_success(self):
        self.observe("available", "one")
        self.observe("unavailable", "two", at=self.now + 1)
        data, excluded = self.listing(self.now + 2)
        self.assertEqual(excluded, [])
        self.assertEqual(data[0]["availability"]["successfulAccounts"], 1)
        self.assertEqual(data[0]["availability"]["failedAccounts"], 1)
        self.accounts[0].enabled = False
        self.assertEqual(self.listing(self.now + 2)[0], [])

    def test_success_expires_to_unverified_and_old_events_do_not_win(self):
        self.observe("available")
        self.observe("unavailable", at=self.now - 1)
        self.assertEqual(self.listing()[0][0]["availability"]["status"], "available")
        self.assertEqual(self.listing(self.now + 86401)[0][0]["availability"]["status"], "unverified")

    def test_history_zero_usage_without_output_evidence_is_not_an_exclusion(self):
        P.remember_model_observation({"at": self.now, "model": "m", "account": "one", "realm": "intl",
            "outcome": "completed", "total_tokens": 0, "completion_tokens": 0})
        data, excluded = self.listing()
        self.assertEqual(excluded, [])
        self.assertEqual(data[0]["availability"]["reason"], "zero_usage_unverified")

    def test_persistence_survives_restart_without_usage_tail_or_private_fields(self):
        self.observe("unavailable")
        saved = (Path(P.USAGE_DIR) / "model-availability.json").read_text()
        self.assertNotIn("service info not found", saved)
        self.assertNotIn("fixture", saved)
        P._model_observation_cache = {"path": None, "rows": {}}
        self.assertEqual(self.listing()[1][0]["availability"]["status"], "unavailable")

    def test_migration_reads_completed_requests_and_ignores_broken_lines(self):
        self.usage.write_text('not-json\n' + json.dumps({"at": self.now, "model": "m", "realm": "intl",
            "account": "one", "outcome": "completed", "completion_tokens": 12, "total_tokens": 24}) + '\n')
        self.assertEqual(self.listing()[0][0]["availability"]["status"], "available")

    def test_all_realms_keeps_a_model_available_elsewhere(self):
        self.accounts.append(A.Account({"uid": "cn-one", "accessToken": "fixture", "realm": "cn"}))
        self.observe("unavailable")
        self.observe("available", account="cn-one", realm="cn")
        P._models_cache = {r: {"data": [("m", {})]} for r in ("intl", "cn")}
        data, excluded = P.model_listing("all", [("m", {})], self.now)
        self.assertEqual(excluded, [])
        self.assertEqual(data[0]["availability"]["realm"], "cn")

    def handler(self):
        handler = object.__new__(P.Handler)
        handler.wfile = io.BytesIO()
        handler.path = "/v1/chat/completions"
        handler.send_response = lambda *args: None
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler._key_id = lambda: ""
        handler._json = lambda status, body: (status, body)
        handler._error = lambda status, message: (status, message)
        handler._anthropic_error = handler._error
        return handler

    def test_empty_and_prompt_only_replies_are_rejected_in_all_six_paths(self):
        for streaming in (False, True):
            for protocol in ("chat", "responses", "messages"):
                for usage in (None, {"prompt_tokens": 15, "completion_tokens": 0, "total_tokens": 15}):
                    with self.subTest(streaming=streaming, protocol=protocol, usage=usage):
                        self.usage.unlink(missing_ok=True)
                        handler = self.handler()
                        args = [sse({"role": "assistant"}, usage), "m"]
                        if protocol == "responses":
                            args += [set(), {}]
                        args += [None, self.accounts[0], time.time()]
                        result = getattr(handler, "_" + protocol + ("_stream_response" if streaming else "_nonstream_response"))(*args)
                        rows = [json.loads(line) for line in self.usage.read_text().splitlines()]
                        self.assertEqual(len(rows), 1)
                        self.assertEqual(rows[0]["status"], 502)
                        self.assertNotEqual(rows[0]["outcome"], "completed")
                        self.assertIn(P.EMPTY_UPSTREAM_RESPONSE, rows[0]["message"])
                        if streaming:
                            output = handler.wfile.getvalue().decode()
                            self.assertIn("response.failed" if protocol == "responses" else "error", output)
                            self.assertNotIn("response.completed", output)
                        else:
                            self.assertEqual(result[0], 502)

    def test_tool_only_zero_usage_is_valid_for_each_streaming_protocol(self):
        delta = {"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"}}]}
        for protocol in ("chat", "responses", "messages"):
            with self.subTest(protocol=protocol):
                self.usage.unlink(missing_ok=True)
                handler = self.handler()
                args = [sse(delta, {"total_tokens": 0, "completion_tokens": 0}), "m"]
                if protocol == "responses":
                    args += [set(), {}]
                args += [None, self.accounts[0], time.time()]
                getattr(handler, "_" + protocol + "_stream_response")(*args)
                row = json.loads(self.usage.read_text().splitlines()[-1])
                self.assertEqual(row["outcome"], "completed")
                self.assertTrue(row["response_validated"])
                self.assertEqual(self.listing(time.time())[0][0]["availability"]["status"], "available")

    def test_sse_error_cannot_be_recorded_as_success(self):
        raw = io.BytesIO(b'data: {"error":{"message":"fixture error"}}\n\ndata: [DONE]\n\n')
        with self.assertRaisesRegex(RuntimeError, "upstream stream returned an error"):
            list(P.require_nonempty_stream(raw))


if __name__ == "__main__":
    unittest.main()
