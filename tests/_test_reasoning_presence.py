"""Missing, explicit zero, estimates and reasoning effort are separate facts."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _isolated_dirs import isolated_data_dirs
_TMP = isolated_data_dirs("wb-reasoning-presence-")
import wb_proxy as P
import wb_accounts as A
import wb_bridge as B


class ReasoningPresenceTests(unittest.TestCase):
    def test_missing_zero_and_nested_aliases_survive_recent_rows(self):
        for usage, expected in [
            ({"prompt_tokens": 2, "completion_tokens": 1}, None),
            ({"completion_tokens_details": {"reasoning_tokens": 0}}, 0),
            ({"output_tokens_details": {"reasoning_tokens": 7}}, 7),
            ({"reasoning_tokens": 3}, 3),
            ({"completion_tokens_details": {"reasoning_tokens": None}}, None),
        ]:
            row = P.record_usage("hy3", usage, effort="high")
            self.assertEqual(row["reasoning_tokens"], expected)
            self.assertEqual(row["reasoning_effort"], "high")
            self.assertEqual(row["reasoning_tokens_source"], "not_reported" if expected is None else "upstream")
        recent = P.recent_usage(limit=10)["rows"]
        self.assertTrue(any(row.get("reasoning_tokens_source") == "not_reported" for row in recent))

    def test_responses_does_not_invent_reported_zero(self):
        self.assertEqual(P._responses_usage({"completion_tokens": 1})["output_tokens_details"], {})
        self.assertEqual(P._responses_usage({"completion_tokens_details": {"reasoning_tokens": 0}})["output_tokens_details"], {"reasoning_tokens": 0})

    def test_international_rewards_cannot_be_enabled_with_legacy_setting(self):
        with patch.dict(os.environ, {"WB_INTERNATIONAL_DAILY_CHAT": "1"}), patch.object(A, "urlopen") as network:
            account = A.Account({"uid": "fixture", "accessToken": "fixture", "realm": "intl"})
            self.assertFalse(B.daily_enabled())
            self.assertFalse(account.can_daily_chat())
            self.assertFalse(account.daily_chat()["ok"])
            self.assertFalse(account.daily_chat_web()["ok"])
            network.assert_not_called()


if __name__ == "__main__": unittest.main()
