import copy
import json
import os
import tempfile
import unittest
from unittest import mock

import chart
import fxcore as fc


def bars(n, start=1000000, step=900, base=150.0):
    out = []
    for i in range(n):
        c = base + (i % 7) * 0.1 + i * 0.01
        out.append((start + i * step, c - 0.05, c + 0.2, c - 0.2, c + 0.05))
    return out


class TestFetchAndDrop(unittest.TestCase):
    def test_fetch_ohlc_parses_and_skips_nulls(self):
        payload = {"chart": {"result": [{
            "timestamp": [1, 2, 3],
            "indicators": {"quote": [{
                "open": [1.0, None, 3.0], "high": [2.0, 2.5, 3.5],
                "low": [0.5, 0.6, 2.5], "close": [1.5, 1.6, 3.2]}]},
        }]}}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b""

        with mock.patch("chart.urllib.request.urlopen", return_value=Resp()), \
             mock.patch("chart.json.load", return_value=payload):
            out = chart.fetch_ohlc("USDJPY", "15m", "1d")
        self.assertEqual(out, [(1, 1.0, 2.0, 0.5, 1.5), (3, 3.0, 3.5, 2.5, 3.2)])

    def test_drop_forming(self):
        c = bars(3, start=0, step=900)  # ts 0, 900, 1800
        self.assertEqual(len(chart.drop_forming(c, "15m", now=1800 + 901)), 3)
        self.assertEqual(len(chart.drop_forming(c, "15m", now=1800 + 100)), 2)

    def test_periods_are_defined(self):
        labels = [p[0] for p in chart.PERIODS]
        self.assertEqual(len(labels), len(set(labels)))
        for _, itv, rng, span in chart.PERIODS:
            self.assertIn(itv, fc.INTERVAL_SEC)
            self.assertGreater(span, 0)


class TestBuildPayload(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(fc.DEFAULT_CONFIG)

    def test_series_match_displayed_bars(self):
        hist = bars(300, step=86400)  # 日足300本
        with mock.patch.object(chart, "fetch_ohlc", return_value=hist):
            p = chart.build_payload(self.cfg, "1ヶ月")  # 日足 → 表示は直近30日
        n = len(p["bars"])
        self.assertLessEqual(n, 31)
        self.assertGreater(n, 25)
        self.assertEqual(len(p["mas"]), 3)
        for ma in p["mas"]:
            self.assertEqual(len(ma["values"]), n)
        for k in ("line", "signal", "hist"):
            self.assertEqual(len(p["macd"][k]), n)

    def test_long_ma_uses_history_before_window(self):
        hist = bars(300, step=86400)
        with mock.patch.object(chart, "fetch_ohlc", return_value=hist):
            p = chart.build_payload(self.cfg, "1ヶ月")
        self.assertIsNotNone(p["mas"][2]["values"][0])  # 表示の左端でも長期(75本)が出ている

    def test_payload_is_json_serializable(self):
        with mock.patch.object(chart, "fetch_ohlc", return_value=bars(300, step=86400)):
            p = chart.build_payload(self.cfg, "3ヶ月")
        json.dumps(p)

    def test_empty_data_raises(self):
        with mock.patch.object(chart, "fetch_ohlc", return_value=[]):
            with self.assertRaises(ValueError):
                chart.build_payload(self.cfg, "1日")


class TestOpenWindow(unittest.TestCase):
    def test_launches_window_process_with_payload_on_stdin(self):
        cfg = copy.deepcopy(fc.DEFAULT_CONFIG)
        proc = mock.Mock()
        proc.stdin = mock.Mock()
        with mock.patch.object(chart, "fetch_ohlc", return_value=bars(50, step=86400)), \
             mock.patch("chart.subprocess.Popen", return_value=proc) as popen:
            chart.open_window(cfg, "1ヶ月")
        args = popen.call_args[0][0]
        self.assertEqual(args[1], chart.WINDOW_SCRIPT)
        self.assertTrue(os.path.exists(chart.WINDOW_SCRIPT))
        written = proc.stdin.write.call_args[0][0]
        self.assertEqual(json.loads(written.decode("utf-8"))["period"], "1ヶ月")
        proc.stdin.close.assert_called_once()

    def test_no_browser_and_no_files(self):
        cfg = copy.deepcopy(fc.DEFAULT_CONFIG)
        proc = mock.Mock()
        proc.stdin = mock.Mock()
        tmp = tempfile.mkdtemp()
        with mock.patch.object(chart, "fetch_ohlc", return_value=bars(50, step=86400)), \
             mock.patch.object(fc, "DATA_DIR", tmp), \
             mock.patch("chart.subprocess.Popen", return_value=proc), \
             mock.patch("chart.subprocess.run") as run:
            chart.open_window(cfg, "1ヶ月")
        run.assert_not_called()  # ブラウザ（open）は使わない
        self.assertEqual(os.listdir(tmp), [])


if __name__ == "__main__":
    unittest.main()
