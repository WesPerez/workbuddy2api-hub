"""Scoped login delivery, ownership, egress and secret export regressions."""
import base64
import copy
import io
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _isolated_dirs import isolated_data_dirs
_TMP = isolated_data_dirs("wb-bridge-")
os.environ["WB_DATA_DIR"] = _TMP.name
import wb_accounts as accounts
import wb_bridge as bridge
import wb_upstream_pool as upstream
import wb_resin

def route(ip="203.0.113.5"):
    return {"key": "ldc-9", "proxy": {"server": "http://127.0.0.1:10834", "username": "AppsGlobal.ldc-9", "password": "synthetic-proxy-secret"},
            "binding": {"platform": "AppsGlobal", "leaseGuardVersion": 1, "nodeHash": "a" * 32, "leaseCreatedAt": "123456789", "ip": ip}}

def token(uid="official-uid"):
    enc = lambda v: base64.urlsafe_b64encode(json.dumps(v).encode()).decode().rstrip("=")
    return enc({"alg": "none"}) + "." + enc({"sub": uid, "exp": int(time.time()) + 3600}) + ".signature"

class BridgeTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory(dir=_TMP.name)
        self.env = patch.dict(os.environ, {"WB_DATA_DIR": self.temp.name})
        self.env.start()
        self.pool = accounts.AccountPool(str(Path(self.temp.name) / "accounts"))
        self.bridge = bridge.Bridge(self.pool)

    def tearDown(self):
        self.bridge.db.close(); self.env.stop(); self.temp.cleanup()

    def pending(self, uid=None):
        payload = {"accountRef": "account-hub:instance:9", "idempotencyKey": "synthetic-login-001",
                   "githubLogin": "WesPerez", "route": route(), "expectedUid": uid}
        with patch.object(accounts, "http_json", return_value={"data": {"state": "synthetic-state", "authUrl": "https://www.workbuddy.ai/login?state=synthetic-state"}}):
            return self.bridge.start(payload)

    def test_guard_cannot_fall_back_to_direct_even_when_pool_is_disabled(self):
        proxy = bridge.guarded_proxy(route())
        with patch.object(upstream, "enabled", return_value=False), patch.object(upstream, "urlopen", side_effect=upstream.PoolBypass("unsupported")), patch.object(accounts.urllib.request, "urlopen") as direct:
            with self.assertRaises(RuntimeError): accounts.urlopen("https://www.workbuddy.ai/", proxy=proxy)
            direct.assert_not_called()
        with patch.object(upstream, "_proxy_bypassed", return_value=True):
            self.assertEqual(upstream._fold_bypassed("www.workbuddy.ai", proxy)[1], proxy)

    def test_native_import_and_public_export_preserve_the_protection_boundary(self):
        acc = accounts.Account({"uid": "u", "realm": "intl", "accessToken": token(), "refreshToken": "synthetic-refresh",
                                "source": "account-hub", "bridgeRef": "account-hub:instance:9", "bridgeRoute": route()})
        row = accounts.account_to_export(acc)
        for value in [row, {"auth": row}, {"account": row}, {"access_token": token(), "bridge_route": route()}]:
            with self.assertRaises(ValueError): accounts.normalise_import_row(value)
        encoded = json.dumps(accounts.build_export_document([acc], include_secrets=False))
        for secret in [token(), "synthetic-refresh", "synthetic-proxy-secret"]: self.assertNotIn(secret, encoded)
        orphan = accounts.Account({"uid": "orphan", "source": "account-hub", "accessToken": token()})
        with self.assertRaises(ValueError): orphan.request_snapshot()

    def test_snapshot_cannot_mix_token_and_route_during_reauthorization(self):
        acc = accounts.Account({"uid": "u", "realm": "intl", "accessToken": "old", "bridgeRef": "account-hub:instance:9", "bridgeRoute": route()})
        entered, release, committed = threading.Event(), threading.Event(), threading.Event()
        original = acc.headers
        def headers(purpose="chat"):
            result = original(purpose); entered.set(); self.assertTrue(release.wait(3)); return result
        acc.headers = headers
        captured = []
        reader = threading.Thread(target=lambda: captured.append(acc.request_snapshot()))
        def write():
            with acc._refresh_lock:
                acc.access_token = "new"; acc.bridge_route = route("203.0.113.6"); committed.set()
        writer = threading.Thread(target=write)
        reader.start(); self.assertTrue(entered.wait(3)); writer.start()
        self.assertFalse(committed.wait(0.05)); release.set(); reader.join(3); writer.join(3)
        self.assertEqual(captured[0][0]["Authorization"], "Bearer old")
        self.assertEqual(captured[0][1], bridge.guarded_proxy(route()))
        self.assertTrue(committed.is_set())

    def test_first_link_can_verify_a_known_official_uid_and_recover_account_commit(self):
        pending = self.pending("official-uid")
        response = {"code": 0, "data": {"accessToken": token(), "refreshToken": "synthetic-refresh"}}
        persist = self.bridge.persist
        def interrupt(row):
            if row["state"] == "ready": raise OSError("simulated acknowledgement loss")
            return persist(row)
        with patch.object(accounts, "http_json", return_value=response), patch.object(accounts, "fetch_account_profile", return_value=("official-uid", "Name")), patch.object(self.bridge, "persist", side_effect=interrupt):
            with self.assertRaises(OSError): self.bridge.poll(pending["id"])
        self.assertEqual(len(self.pool.accounts), 1)
        self.bridge.db.close(); self.pool = accounts.AccountPool(self.pool.dir); self.pool.load(); self.bridge = bridge.Bridge(self.pool)
        with patch.object(accounts, "http_json") as network:
            result = self.bridge.poll(pending["id"]); network.assert_not_called()
        self.assertEqual(result["status"], "ready")
        self.assertNotIn("accessToken", json.dumps(result))
        self.assertNotIn("candidate", json.loads(self.bridge.stored(pending["id"])["payload"]))

    def test_failed_file_save_never_commits_ready(self):
        pending = self.pending()
        response = {"code": 0, "data": {"accessToken": token(), "refreshToken": "synthetic-refresh"}}
        with patch.object(accounts, "http_json", return_value=response), patch.object(accounts, "fetch_account_profile", return_value=("official-uid", "Name")), patch.object(accounts.Account, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): self.bridge.poll(pending["id"])
        self.assertEqual(self.bridge.stored(pending["id"])["state"], "candidate")
        self.assertEqual(self.pool.accounts, [])

    def test_cancel_revokes_pending_authorization_and_releases_identity_lock(self):
        pending = self.pending("official-uid")
        result = self.bridge.cancel(pending["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertNotIn("authUrl", result)
        self.assertEqual(self.bridge.cancel(pending["id"]), result)
        with patch.object(accounts, "http_json") as network:
            self.assertEqual(self.bridge.poll(pending["id"])["status"], "cancelled")
            network.assert_not_called()
        saved = json.loads(self.bridge.stored(pending["id"])["payload"])
        for field in ("candidate", "oauthState", "authUrl"):
            self.assertNotIn(field, saved)
        with patch.object(accounts, "http_json", return_value={"data": {"state": "next-state", "authUrl": "https://www.workbuddy.ai/login?state=next-state"}}):
            next_task = self.bridge.start({"accountRef": "account-hub:instance:9", "idempotencyKey": "synthetic-login-002",
                "githubLogin": "WesPerez", "route": route(), "expectedUid": "official-uid"})
        self.assertNotEqual(next_task["id"], pending["id"])
        self.assertEqual(next_task["status"], "pending")

    def test_cancel_reconciles_a_completed_account_file_before_reporting_cancelled(self):
        pending = self.pending("official-uid")
        response = {"code": 0, "data": {"accessToken": token(), "refreshToken": "synthetic-refresh"}}
        persist = self.bridge.persist
        def interrupt(row):
            if row["state"] == "ready":
                raise OSError("simulated receipt interruption")
            return persist(row)
        with patch.object(accounts, "http_json", return_value=response), patch.object(accounts, "fetch_account_profile", return_value=("official-uid", "Name")), patch.object(self.bridge, "persist", side_effect=interrupt):
            with self.assertRaises(OSError):
                self.bridge.poll(pending["id"])
        with patch.object(accounts, "http_json") as network:
            result = self.bridge.cancel(pending["id"])
            network.assert_not_called()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(self.pool.accounts), 1)
        self.assertNotIn("synthetic-refresh", json.dumps(result))
        self.assertEqual(self.bridge.cancel(pending["id"]), result)

    def test_international_automated_chat_is_disabled_by_default(self):
        with patch.dict(os.environ, {"WB_INTERNATIONAL_DAILY_CHAT": "0"}):
            acc = accounts.Account({"uid": "u", "realm": "intl", "accessToken": token()})
            with patch.object(accounts, "http_json") as network:
                self.assertFalse(acc.can_daily_chat()); self.assertFalse(acc.daily_chat()["ok"])
                self.assertFalse(acc.daily_chat_web()["ok"]); network.assert_not_called()


    def delegated(self, ref="account-hub:instance:9", rt=None, revision="rev-1", route_revision="rr-1"):
        acc = accounts.Account({"uid": "official-uid", "realm": "intl", "accessToken": token(),
                                "refreshToken": "synthetic-refresh", "source": "account-hub",
                                "bridgeRef": ref, "bridgeRoute": rt or route(), "bridgeRevision": revision,
                                "routeRevision": route_revision})
        acc.save(self.pool.dir)
        self.pool.accounts.append(acc)
        return acc

    def test_login_route_refresh_replaces_a_stale_snapshot_and_forgets_the_lease_cache(self):
        acc = self.delegated()
        newer = route(ip="203.0.113.77")
        newer["key"] = "hub-9-github-2"
        newer["proxy"]["username"] = "AppsGlobal.hub-9-github-2"
        newer["binding"]["leaseCreatedAt"] = "987654321"
        with patch.dict(os.environ, {"WB_RESIN_PLATFORM": "AppsGlobal"}), patch.object(wb_resin, "forget_route") as forgotten:
            result = self.bridge.update_route({"accountRef": "account-hub:instance:9", "route": newer,
                                               "expectedRouteRevision": "rr-1", "uid": "official-uid"})
        self.assertTrue(result["changed"])
        self.assertTrue(result["committed"])
        self.assertEqual(result["key"], "hub-9-github-2")
        self.assertEqual(result["previousKey"], "ldc-9")
        self.assertNotEqual(result["routeRevision"], "rr-1")
        # bridgeRevision is the token-refresh generation and must NOT move here.
        self.assertEqual(result["revision"], "rev-1")
        saved = json.loads(Path(acc.path).read_text())
        self.assertEqual(saved["bridgeRoute"]["key"], "hub-9-github-2")
        self.assertEqual(saved["bridgeRoute"]["binding"]["leaseCreatedAt"], "987654321")
        self.assertEqual(saved["routeRevision"], result["routeRevision"])
        forgotten.assert_called_once()

    def test_route_revision_survives_a_token_refresh_that_rotates_bridge_revision(self):
        acc = self.delegated(route_revision="rr-1", revision="rev-1")
        # A normal refresh rotates bridgeRevision only.
        acc.bridge_revision = "rev-2"
        newer = route(ip="203.0.113.5")
        newer["key"] = "hub-9-github-3"
        newer["proxy"]["username"] = "AppsGlobal.hub-9-github-3"
        with patch.dict(os.environ, {"WB_RESIN_PLATFORM": "AppsGlobal"}), patch.object(wb_resin, "forget_route"):
            result = self.bridge.update_route({"accountRef": "account-hub:instance:9", "route": newer,
                                               "expectedRouteRevision": "rr-1"})
        self.assertTrue(result["changed"], "a token refresh must not block a route update")
        self.assertEqual(result["revision"], "rev-2", "the route write keeps the refreshed token revision")

    def test_login_route_refresh_is_idempotent_even_with_a_stale_revision(self):
        self.delegated()
        with patch.dict(os.environ, {"WB_RESIN_PLATFORM": "AppsGlobal"}), patch.object(wb_resin, "forget_route"):
            result = self.bridge.update_route({"accountRef": "account-hub:instance:9", "route": route(),
                                               "expectedRouteRevision": "rr-stale"})
        self.assertFalse(result["changed"])
        self.assertTrue(result["committed"])
        self.assertEqual(result["routeRevision"], "rr-1")

    def test_proxy_only_route_changes_require_cas_and_commit_the_complete_configuration(self):
        acc = self.delegated()
        changes = (
            {"server": "http://127.0.0.1:10835"},
            {"password": "synthetic-replacement-secret"},
            {"username": "ReplacementPool.ldc-9"},
        )
        for change in changes:
            with self.subTest(field=next(iter(change))):
                newer = copy.deepcopy(acc.bridge_route)
                newer["proxy"].update(change)
                if "username" in change:
                    newer["binding"]["platform"] = "ReplacementPool"
                revision = acc.route_revision
                before = copy.deepcopy(acc.bridge_route)
                with patch.object(wb_resin, "forget_route") as forgotten:
                    with self.assertRaises(ValueError):
                        self.bridge.update_route({"accountRef": acc.bridge_ref, "route": newer,
                                                  "expectedRouteRevision": "stale-revision"})
                    self.assertEqual(acc.bridge_route, before)
                    forgotten.assert_not_called()
                    result = self.bridge.update_route({"accountRef": acc.bridge_ref, "route": newer,
                                                       "expectedRouteRevision": revision})
                    forgotten.assert_called_once_with(acc)
                self.assertTrue(result["changed"])
                self.assertTrue(result["committed"])
                self.assertNotEqual(acc.route_revision, revision)
                self.assertEqual(acc.bridge_revision, "rev-1")
                self.assertEqual(json.loads(Path(acc.path).read_text())["bridgeRoute"], newer)
                with patch.object(wb_resin, "forget_route") as forgotten:
                    duplicate = self.bridge.update_route({"accountRef": acc.bridge_ref, "route": newer,
                                                           "expectedRouteRevision": revision})
                    forgotten.assert_not_called()
                self.assertFalse(duplicate["changed"])
                self.assertEqual(duplicate["routeRevision"], acc.route_revision)

    def test_login_route_refresh_refuses_unknown_account_stale_revision_and_identity_mismatch(self):
        self.delegated()
        with self.assertRaises(ValueError):
            self.bridge.update_route({"accountRef": "account-hub:instance:404", "route": route()})
        with self.assertRaises(ValueError):
            self.bridge.update_route({"accountRef": "account-hub:instance:9", "route": route(),
                                      "uid": "a-different-uid"})
        with patch.dict(os.environ, {"WB_RESIN_PLATFORM": "AppsGlobal"}), patch.object(wb_resin, "forget_route") as forgotten:
            with self.assertRaises(ValueError):
                self.bridge.update_route({"accountRef": "account-hub:instance:9",
                                          "route": route(ip="203.0.113.88"), "expectedRouteRevision": "rr-stale"})
            forgotten.assert_not_called()
        acc = self.pool.accounts[0]
        self.assertEqual(acc.bridge_route["key"], "ldc-9")
        self.assertEqual(acc.route_revision, "rr-1")

    def test_login_route_refresh_refuses_an_unusable_route_and_rolls_back_on_save_failure(self):
        acc = self.delegated()
        broken = route()
        broken["proxy"]["username"] = "wrong.prefix"
        with self.assertRaises(ValueError):
            self.bridge.update_route({"accountRef": "account-hub:instance:9", "route": broken})
        newer = route(ip="203.0.113.99")
        newer["binding"]["leaseCreatedAt"] = "555555555"
        with patch.dict(os.environ, {"WB_RESIN_PLATFORM": "AppsGlobal"}), patch.object(accounts.Account, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.bridge.update_route({"accountRef": "account-hub:instance:9", "route": newer,
                                          "expectedRouteRevision": "rr-1"})
        self.assertEqual(acc.bridge_route["key"], "ldc-9")
        self.assertEqual(acc.route_revision, "rr-1")

    def test_a_cache_invalidation_failure_is_reported_as_committed_with_a_warning(self):
        self.delegated()
        newer = route(ip="203.0.113.111")
        newer["key"] = "hub-9-github-4"
        newer["proxy"]["username"] = "AppsGlobal.hub-9-github-4"
        with patch.dict(os.environ, {"WB_RESIN_PLATFORM": "AppsGlobal"}),                 patch.object(wb_resin, "forget_route", side_effect=OSError("receipt write failed")):
            result = self.bridge.update_route({"accountRef": "account-hub:instance:9", "route": newer,
                                               "expectedRouteRevision": "rr-1"})
        self.assertTrue(result["changed"])
        self.assertTrue(result["committed"], "a committed write must be reported even when cache invalidation failed")
        self.assertEqual(result["warnings"][0]["step"], "forget_route")
        self.assertEqual(self.pool.accounts[0].bridge_route["key"], "hub-9-github-4")

    def test_read_routes_reports_the_committed_state_without_secrets(self):
        self.delegated()
        result = self.bridge.read_routes({"accountRef": "account-hub:instance:9"})
        row = result["accounts"][0]
        self.assertEqual(row["uid"], "official-uid")
        self.assertEqual(row["key"], "ldc-9")
        self.assertEqual(row["routeRevision"], "rr-1")
        serialized = json.dumps(result)
        self.assertNotIn("synthetic-proxy-secret", serialized)
        self.assertNotIn(token(), serialized)

    def test_forget_route_drops_the_cached_generation(self):
        acc = self.delegated()
        with patch.dict(os.environ, {"WB_RESIN_PLATFORM": "AppsGlobal"}):
            with patch.object(wb_resin, "_control") as control:
                control.return_value = {"recovery_version": 1, "status": "available", "lease": {
                    "account": wb_resin.identity(acc), "platform_id": "p-1", "node_hash": "b" * 32,
                    "egress_ip": "203.0.113.5", "created_at_ns": "123456789", "lease_guard_version": 1,
                    "expiry": "later"}}
                wb_resin.proxy_for(acc)
                self.assertEqual(wb_resin._states[acc.uid]["status"], "available")
                control.reset_mock()
                wb_resin.forget_route(acc)
                self.assertEqual(wb_resin._states[acc.uid]["status"], "route_updated")
                self.assertEqual(wb_resin._states[acc.uid]["retryAt"], 0)
                wb_resin.proxy_for(acc)
                self.assertTrue(control.called)

    def test_route_update_requires_a_real_route_revision_instead_of_unconditional_write(self):
        # Hub's pre-migration metadata has no routeRevision. Passing None must not
        # become an unconditional write: the caller has to read the binding and
        # retry with what it observed.
        acc = self.delegated()
        newer = route(ip="203.0.113.123")
        newer["key"] = "hub-9-github-9"
        newer["proxy"]["username"] = "AppsGlobal.hub-9-github-9"
        with self.assertRaises(ValueError):
            self.bridge.update_route({"accountRef": "account-hub:instance:9", "route": newer,
                                      "expectedRouteRevision": None})
        self.assertEqual(acc.bridge_route["key"], "ldc-9")
        with self.assertRaises(ValueError):
            self.bridge.update_route({"accountRef": "account-hub:instance:9", "route": newer})
        self.assertEqual(acc.bridge_route["key"], "ldc-9")
    def test_a_login_that_changes_the_route_advances_the_route_revision(self):
        acc = self.delegated(route_revision="rr-1", revision="rev-1")
        # A login delivering a DIFFERENT route must move routeRevision, otherwise
        # Hub's CAS can never observe the change.
        with patch("wb_bridge.guarded_proxy", side_effect=lambda r: "/dev/null"), \
                patch.object(accounts, "http_json", side_effect=[
                    {"data": {"state": "s", "authUrl": "https://www.workbuddy.ai/login?state=s"}},  # start
                    {"code": 0, "data": {"accessToken": token(), "refreshToken": "r", "expiresAt": 1}},  # poll
                ]), \
                patch.object(accounts, "fetch_account_profile", return_value=("official-uid", "N")):
            moved = route(ip="203.0.113.200")
            moved["key"] = "hub-9-github-7"
            moved["proxy"]["username"] = "AppsGlobal.hub-9-github-7"
            started = self.bridge.start({"accountRef": "account-hub:instance:9",
                                         "idempotencyKey": "synthetic-login-route-move",
                                         "githubLogin": "WesPerez", "route": moved,
                                         "expectedUid": "official-uid"})
            self.bridge.poll(started["id"])
        self.assertNotEqual(acc.route_revision, "rr-1", "the route generation must advance with the route")
        self.assertEqual(acc.bridge_route["key"], "hub-9-github-7")
    def test_a_route_update_during_a_login_is_detected_by_the_frozen_expectation(self):
        acc = self.delegated(route_revision="rr-1", revision="rev-1")
        responses = [
            {"data": {"state": "s", "authUrl": "https://www.workbuddy.ai/login?state=s"}},
            {"code": 0, "data": {"accessToken": token(), "refreshToken": "r", "expiresAt": 1}},
        ]
        def http_json(*args, **kwargs):
            return responses.pop(0)
        with patch("wb_bridge.guarded_proxy", side_effect=lambda r: "/dev/null"), \
                patch.object(accounts, "http_json", side_effect=http_json), \
                patch.object(accounts, "fetch_account_profile", return_value=("official-uid", "N")):
            started = self.bridge.start({"accountRef": "account-hub:instance:9",
                                         "idempotencyKey": "synthetic-login-frozen",
                                         "githubLogin": "WesPerez", "route": route(),
                                         "expectedUid": "official-uid"})
            # Another writer moves the route generation while the login is pending.
            acc.route_revision = "rr-moved"
            with self.assertRaises(ValueError):
                self.bridge.poll(started["id"])
        self.assertEqual(acc.route_revision, "rr-moved", "the concurrent route writer wins")

if __name__ == "__main__": unittest.main()
