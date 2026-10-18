"""健康探测的访问日志静默：/healthz 与 /health 一致。

/healthz 是部署更新器每两分钟轮询一次的探针（见 deploy/update.py 的 health()）。
它原先不在 Handler.log_message 的静默前缀里，于是每次探测都要在发响应前同步写一行
访问日志，还会把看板「日志」页刷满无信息量的 200。这个套件钉住三件事：

  1. 正常的 GET /healthz 不落日志，行为与 /health 一致；
  2. 非 200 或非 GET 的 /healthz 照样记录——排障时不能把真正的失败吞掉；
  3. 静默是按路径边界匹配的，/healthz-other 这类相邻路径不受影响。

Run with: python tests/_test_health_probe_logging.py
No network, no credentials, nothing written to disk.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy


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


if __name__ == "__main__":
    unittest.main()
