"""A failed account cannot freeze membership, prices or freshness metadata."""
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _isolated_dirs import isolated_data_dirs
_TMP = isolated_data_dirs("wb-catalog-recovery-")
import wb_accounts as A
import wb_proxy as P
P.ACCOUNTS_DIR = os.environ["ACCOUNTS_DIR"]


class CatalogRecoveryTests(unittest.TestCase):
    def test_metadata_selection_skips_disabled_cooling_and_expired_but_not_spend_caps(self):
        pool = A.AccountPool(os.environ["ACCOUNTS_DIR"])
        accounts = [A.Account({"uid": str(i), "accessToken": "fixture", "realm": "intl"}) for i in range(5)]
        accounts[0].enabled = False
        accounts[1].breaker_until = time.time() + 300
        accounts[2].expires_at = time.time() - 60
        accounts[3].daily_token_limit, accounts[3].daily_tokens_today = 1, 10
        pool.accounts = accounts
        self.assertEqual([a.uid for a in pool.representatives("intl")], ["3", "4"])

    def test_second_account_supplies_catalogue_after_first_transport_fails(self):
        pool = A.AccountPool(os.environ["ACCOUNTS_DIR"])
        pool.accounts = [A.Account({"uid": str(i), "accessToken": "fixture", "realm": "intl"}) for i in range(2)]
        body = {"code": 0, "data": {"agents": [{"name": "cli", "models": ["new-model"]}],
                                   "models": [{"id": "new-model", "credits": "x0.42"}]}}
        with patch.object(P, "POOL", pool), patch.object(A, "urlopen", side_effect=[TimeoutError(), io.BytesIO(json.dumps(body).encode())]) as network:
            ids, meta = P.fetch_remote_product_config("intl")
        self.assertEqual(ids, ["new-model"])
        self.assertEqual(meta["new-model"]["credits"], "x0.42")
        self.assertEqual(network.call_count, 2)
        self.assertEqual(network.call_args.args[0].get_header("X-user-id"), "1")

    def test_removed_models_do_not_return_on_outage_or_restart_and_missing_rate_is_unknown(self):
        original = P._models_cache
        P._models_cache = {}
        self.addCleanup(setattr, P, "_models_cache", original)
        source = (["hy3", "new-model"], {"hy3": {"id": "hy3"}, "new-model": {"credits": "x0.2"}})
        with patch.object(P, "fetch_remote_product_config", return_value=source):
            entries = dict(P.fetch_models("intl"))
        self.assertEqual(set(entries), {"hy3", "new-model"})
        self.assertNotIn("credits", entries["hy3"])
        self.assertFalse(P.catalog_status("intl")["stale"])
        P._models_cache.clear()  # simulate a process restart
        with patch.object(P, "fetch_remote_product_config", return_value=None):
            cached = dict(P.fetch_models("intl"))
        self.assertEqual(set(cached), set(entries))
        self.assertTrue(P.catalog_status("intl")["stale"])
        self.assertEqual(P.catalog_status("intl")["source"], "last_good")

    def test_rejected_envelope_does_not_publish_embedded_models(self):
        self.assertIsNone(P.parse_remote_catalog({"code": 11102, "data": {"agents": [{"name": "cli", "models": ["old"]}]}}))


if __name__ == "__main__": unittest.main()
