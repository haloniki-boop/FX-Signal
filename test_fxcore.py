import copy
import csv
import json
import os
import tempfile
import time
import math
import unittest
from unittest import mock

import fxcore as fc


def series(closes, spread=0.1, step=900):
    return [(i * step, c + spread, c - spread, c) for i, c in enumerate(closes)]


def cfg_with(**kw):
    c = copy.deepcopy(fc.DEFAULT_CONFIG)
    c.update(kw)
    return c


class TestIndicators(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(fc.sma([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])

    def test_ema_seed_and_length(self):
        e = fc.ema([1.0, 2.0, 3.0, 4.0], 2)
        self.assertIsNone(e[0])
        self.assertEqual(e[1], 1.5)
        self.assertEqual(len(e), 4)

    def test_rsi_extremes(self):
        self.assertEqual(fc.rsi([float(i) for i in range(1, 30)], 14)[-1], 100.0)
        self.assertAlmostEqual(fc.rsi([float(100 - i) for i in range(30)], 14)[-1], 0.0)

    def test_macd_flat_is_zero(self):
        line, sig, hist = fc.macd([100.0] * 60)
        self.assertAlmostEqual(hist[-1], 0.0)

    def test_bollinger_flat_collapses(self):
        mid, up, lo = fc.bollinger([5.0] * 30, 20, 2)
        self.assertEqual(mid[-1], 5.0)
        self.assertEqual(up[-1], 5.0)
        self.assertEqual(lo[-1], 5.0)

    def test_atr_constant_range(self):
        n = 30
        a = fc.atr([10.0] * n, [9.0] * n, [9.5] * n, 14)
        self.assertAlmostEqual(a[-1], 1.0)


class TestDecide(unittest.TestCase):
    def test_not_enough_data_is_hold(self):
        r = fc.decide(series([100.0] * 5), cfg_with())
        self.assertEqual(r["signal"], "HOLD")

    def test_flat_is_hold(self):
        for strat in ("macd", "ma", "bb", "combo"):
            r = fc.decide(series([100.0] * 80), cfg_with(strategy=strat))
            self.assertEqual(r["signal"], "HOLD", strat)

    def _scan(self, closes, cfg, want):
        for end in range(60, len(closes) + 1):
            r = fc.decide(series(closes[:end]), cfg)
            if r["signal"] == want:
                return r
        return None

    def test_macd_buy_has_stop_below_and_target_above(self):
        closes = [100.0 - i * 0.3 for i in range(60)] + [82.0 + i * 0.5 for i in range(1, 30)]
        cfg = cfg_with(strategy="macd", rsi_buy_max=100, symbol="USDJPY")
        r = self._scan(closes, cfg, "BUY")
        self.assertIsNotNone(r, "BUYが出るはず")
        self.assertLess(r["stop"], r["price"])
        self.assertGreater(r["target"], r["price"])
        self.assertGreater(r["target_pips"], r["stop_pips"])  # tp_atr > sl_atr

    def test_macd_sell_mirror(self):
        closes = [100.0 + i * 0.3 for i in range(60)] + [118.0 - i * 0.5 for i in range(1, 30)]
        cfg = cfg_with(strategy="macd", rsi_sell_min=0, symbol="EURUSD")
        r = self._scan(closes, cfg, "SELL")
        self.assertIsNotNone(r, "SELLが出るはず")
        self.assertGreater(r["stop"], r["price"])
        self.assertLess(r["target"], r["price"])

    def test_rsi_filter_blocks_buy(self):
        closes = [100.0 - i * 0.3 for i in range(60)] + [82.0 + i * 0.5 for i in range(1, 30)]
        cfg = cfg_with(strategy="macd", rsi_buy_max=0)  # RSIは常に0より大きい → 見送り
        self.assertIsNone(self._scan(closes, cfg, "BUY"))

    def test_all_strategies_run(self):
        closes = [100.0 - i * 0.3 for i in range(60)] + [82.0 + i * 0.5 for i in range(1, 30)]
        for strat in ("macd", "ma", "bb", "combo"):
            r = fc.decide(series(closes), cfg_with(strategy=strat))
            self.assertIn(r["signal"], ("BUY", "SELL", "HOLD"))


class TestHelpers(unittest.TestCase):
    def test_pair_label_flags(self):
        self.assertEqual(fc.pair_label("USDJPY"), "🇺🇸/🇯🇵")
        self.assertEqual(fc.pair_label("EURUSD"), "🇪🇺/🇺🇸")

    def test_pip_size(self):
        self.assertEqual(fc.pip_size("USDJPY"), 0.01)
        self.assertEqual(fc.pip_size("EURUSD"), 0.0001)

    def test_drop_open_bar(self):
        c = series([1.0, 2.0, 3.0], step=900)  # ts 0, 900, 1800
        self.assertEqual(len(fc.drop_open_bar(c, "15m", now=1800 + 901)), 3)  # 最後の足は確定済み
        self.assertEqual(len(fc.drop_open_bar(c, "15m", now=1800 + 100)), 2)  # 形成中 → 除外

    def test_format_message_contains_levels(self):
        r = {"signal": "BUY", "price": 158.3, "reason": "x", "stop": 157.9, "stop_pips": 40.0,
             "target": 159.1, "target_pips": 80.0, "rsi": 55.0, "macd": 0.01}
        msg = fc.format_message("USDJPY", r)
        self.assertIn("損切り目安 157.900", msg)
        self.assertIn("利確目安 159.100", msg)

    def test_send_all_none_enabled(self):
        cfg = cfg_with(notify={"mac": False, "line": False, "slack": False, "mail": False})
        self.assertEqual(fc.send_all(cfg, "t", "b"), {})

    def test_slack_payload(self):
        cfg = cfg_with(slack={"webhook_url": "https://hooks.example/x"})
        captured = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return Resp()

        with mock.patch("fxcore.urllib.request.urlopen", side_effect=fake_urlopen):
            fc.send_slack(cfg, "BUY USDJPY")
        self.assertEqual(captured["url"], "https://hooks.example/x")
        self.assertEqual(captured["body"], {"text": "BUY USDJPY"})

    def test_line_payload(self):
        cfg = cfg_with(line={"channel_access_token": "TOKEN", "user_id": "U123"})
        captured = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.get_header("Authorization")
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return Resp()

        with mock.patch("fxcore.urllib.request.urlopen", side_effect=fake_urlopen):
            fc.send_line(cfg, "hello")
        self.assertEqual(captured["auth"], "Bearer TOKEN")
        self.assertEqual(captured["body"]["to"], "U123")
        self.assertEqual(captured["body"]["messages"][0]["text"], "hello")

    def test_unconfigured_channel_reports_error(self):
        cfg = cfg_with(notify={"mac": False, "line": True, "slack": False, "mail": False})
        res = fc.send_all(cfg, "t", "b")
        self.assertTrue(res["line"].startswith("error"))

    def test_process_dedupes_same_bar(self):
        closes = [100.0 - i * 0.3 for i in range(60)] + [82.0 + i * 0.5 for i in range(1, 30)]
        candles = series(closes, step=900)
        # 最後の足を確定扱いにするため、長い間隔の候補を渡す
        fake = lambda sym, itv, rng: candles
        cfg = cfg_with(strategy="macd", rsi_buy_max=100, interval="15m")
        tmp = tempfile.mkdtemp()
        with mock.patch.object(fc, "STATE_PATH", os.path.join(tmp, "s.json")), \
             mock.patch.object(fc, "DATA_DIR", tmp), \
             mock.patch.object(fc, "send_all", return_value={"mac": "ok"}) as sa, \
             mock.patch.object(fc, "drop_open_bar", side_effect=lambda c, i, now=None: c):
            state = {}
            # 最初に出たBUYのバーを探す
            hit = None
            for end in range(60, len(closes) + 1):
                cs = series(closes[:end], step=900)
                with mock.patch.object(fc, "fetch_candles", side_effect=lambda *a: cs):
                    r = fc.process(cfg, state)
                if r["signal"] == "BUY":
                    hit = end
                    break
            self.assertIsNotNone(hit)
            self.assertEqual(sa.call_count, 1)
            cs = series(closes[:hit], step=900)
            with mock.patch.object(fc, "fetch_candles", side_effect=lambda *a: cs):
                fc.process(cfg, state)
            self.assertEqual(sa.call_count, 1, "同じ足では二重通知しない")


class TestPreAlert(unittest.TestCase):
    """形成中の足でMACDヒストグラムがゼロ線に近づいたときの予告。"""

    def _candles(self, n=60, base=100.0):
        now = time.time()
        # 最後の足は形成中（まだ確定していない）になるよう現在時刻を基準にする
        return [(int(now) - 900 * (n - 1 - i) - 100, base + 0.1 + (0.2 if i % 2 else 0), base - 0.1,
                 base + (0.1 if i % 2 else -0.1)) for i in range(n)]

    def _hist(self, n, prev, last):
        h = [-0.5] * n
        h[-2], h[-1] = prev, last
        return h

    def test_ma_strategy_has_no_pre_alert(self):
        cfg = cfg_with(strategy="ma")
        self.assertIsNone(fc.pre_alert(self._candles(), cfg))

    def test_buy_pre_alert_when_negative_hist_rises_toward_zero(self):
        cfg = cfg_with(strategy="macd", rsi_buy_max=100)
        c = self._candles()
        with mock.patch.object(fc, "macd", return_value=(None, None, self._hist(len(c), -0.05, -0.02))):
            r = fc.pre_alert(c, cfg)
        self.assertIsNotNone(r)
        self.assertEqual(r["signal"], "BUY")
        self.assertLess(r["stop"], r["price"])
        self.assertGreater(r["target"], r["price"])
        self.assertIn("確定前", r["reason"])

    def test_no_pre_alert_when_far_from_zero(self):
        cfg = cfg_with(strategy="macd", rsi_buy_max=100)
        c = self._candles()
        with mock.patch.object(fc, "macd", return_value=(None, None, self._hist(len(c), -0.5, -0.4))):
            self.assertIsNone(fc.pre_alert(c, cfg))

    def test_no_pre_alert_when_already_crossed(self):
        cfg = cfg_with(strategy="macd", rsi_buy_max=100)
        c = self._candles()
        with mock.patch.object(fc, "macd", return_value=(None, None, self._hist(len(c), -0.05, 0.01))):
            self.assertIsNone(fc.pre_alert(c, cfg))

    def test_sell_pre_alert_mirror(self):
        cfg = cfg_with(strategy="macd", rsi_sell_min=0)
        c = self._candles()
        hist = [0.5] * len(c)
        hist[-2], hist[-1] = 0.05, 0.02
        with mock.patch.object(fc, "macd", return_value=(None, None, hist)):
            r = fc.pre_alert(c, cfg)
        self.assertIsNotNone(r)
        self.assertEqual(r["signal"], "SELL")

    def test_process_sends_pre_alert_once_and_logs_it(self):
        cfg = cfg_with(strategy="macd", rsi_buy_max=100)
        c = self._candles()
        tmp = tempfile.mkdtemp()
        hist = self._hist(len(c), -0.05, -0.02)
        with mock.patch.object(fc, "STATE_PATH", os.path.join(tmp, "s.json")), \
             mock.patch.object(fc, "DATA_DIR", tmp), \
             mock.patch.object(fc, "fetch_candles", return_value=c), \
             mock.patch.object(fc, "macd", return_value=(None, None, hist)), \
             mock.patch.object(fc, "send_all", return_value={"mac": "ok"}) as sa:
            state = {}
            r1 = fc.process(cfg, state)
            r2 = fc.process(cfg, state)  # 同じ形成中の足では再通知しない
        self.assertEqual(r1["signal"], "HOLD")
        self.assertIsNotNone(r1["pre"])
        self.assertEqual(sa.call_count, 1)
        self.assertIn("予告", sa.call_args[0][1])
        self.assertIsNone(r2["pre_sent"])
        with open(os.path.join(tmp, "notify_log.csv"), encoding="utf-8") as fh:
            rows = fh.read().splitlines()
        self.assertEqual(len(rows), 2)  # ヘッダ + 1件
        self.assertIn(",pre,", rows[1])
        self.assertIn("mac=ok", rows[1])

    def test_pre_alert_not_repeated_within_same_15m_bar(self):
        """形成中の足の時刻が毎分変わっても、同じ15分足では予告を1回だけ送る。"""
        cfg = cfg_with(strategy="macd", rsi_buy_max=100)
        c = self._candles()
        base = c[-1][0] - c[-1][0] % 900
        tmp = tempfile.mkdtemp()
        hist = self._hist(len(c), -0.05, -0.02)
        with mock.patch.object(fc, "STATE_PATH", os.path.join(tmp, "s.json")), \
             mock.patch.object(fc, "DATA_DIR", tmp), \
             mock.patch.object(fc, "macd", return_value=(None, None, hist)), \
             mock.patch.object(fc, "send_all", return_value={"mac": "ok"}) as sa:
            state = {}
            for off in (100, 160, 220):  # 同じ15分の中で取得時刻だけ変わる
                shifted = c[:-1] + [(base + off,) + tuple(c[-1][1:])]
                with mock.patch.object(fc, "fetch_candles", return_value=shifted):
                    fc.process(cfg, state)
        self.assertEqual(sa.call_count, 1)


class TestSignalBasis(unittest.TestCase):
    """過去のサインと、通知・ログに出す根拠。"""

    def test_history_matches_decide_and_has_basis(self):
        cfg = cfg_with(strategy="macd", rsi_buy_max=100, rsi_sell_min=0)
        candles = series([100 + 2 * math.sin(i / 5) for i in range(200)])
        hits = fc.signal_history(candles, cfg)
        self.assertGreater(len(hits), 0)
        for k, r in hits.items():
            self.assertEqual(r["signal"], fc.decide(candles[:k + 1], cfg)["signal"])
            self.assertTrue(r["basis"])
            self.assertTrue(any("MACD" in b for b in r["basis"]))

    def test_message_and_log_text_include_basis(self):
        r = {"signal": "BUY", "reason": "X", "price": 1.0, "stop": None, "rsi": 50, "macd": 0,
             "basis": ["根拠A"]}
        self.assertIn("・根拠A", fc.format_message("USDJPY", r))
        self.assertEqual(fc.record_text(r), "X | 根拠A")


class TestLogOrder(unittest.TestCase):
    """ログは新しいものを先頭に追記する。"""

    def test_newest_row_is_first_after_header(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "n.csv")
        header = ["time", "x"]
        fc._prepend_csv(path, header, ["first", "1"])
        fc._prepend_csv(path, header, ["second", "2"])
        with open(path, encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows, [header, ["second", "2"], ["first", "1"]])

    def test_log_notify_and_signal_write_newest_first(self):
        cfg = cfg_with()
        tmp = tempfile.mkdtemp()
        res = {"signal": "BUY", "price": 1.0, "stop": None, "target": None, "rsi": 50,
               "reason": "r", "basis": [], "bar_time": 1793000000}
        with mock.patch.object(fc, "DATA_DIR", tmp):
            fc.log_notify("pre", "USDJPY", dict(res, reason="old"), {"mac": "ok"})
            fc.log_notify("confirmed", "USDJPY", dict(res, reason="new"), {"mac": "ok"})
            fc.log_signal("USDJPY", dict(res, reason="old"))
            fc.log_signal("USDJPY", dict(res, reason="new"))
            with open(os.path.join(tmp, "notify_log.csv"), encoding="utf-8") as fh:
                n = list(csv.reader(fh))
            with open(os.path.join(tmp, "signals.csv"), encoding="utf-8") as fh:
                s = list(csv.reader(fh))
        self.assertEqual(n[1][1], "confirmed")
        self.assertEqual(n[2][1], "pre")
        self.assertEqual(s[1][7], "new")
        self.assertEqual(s[2][7], "old")


class TestLogDir(unittest.TestCase):
    """ログの保存先は設定 log_dir で変えられる。"""

    def test_logs_go_to_configured_folder_and_it_is_created(self):
        base = tempfile.mkdtemp()
        target = os.path.join(base, "nested", "FXログ")
        res = {"signal": "SELL", "price": 1.0, "stop": None, "target": None, "rsi": 50,
               "reason": "r", "basis": [], "bar_time": 1793000000}
        cfg = cfg_with(log_dir=target)
        with mock.patch.object(fc, "DATA_DIR", os.path.join(base, "default")):
            fc.log_signal("USDJPY", res, cfg)
            fc.log_notify("confirmed", "USDJPY", res, {"mac": "ok"}, cfg)
        self.assertTrue(os.path.exists(os.path.join(target, "signals.csv")))
        self.assertTrue(os.path.exists(os.path.join(target, "notify_log.csv")))
        self.assertFalse(os.path.exists(os.path.join(base, "default", "signals.csv")))

    def test_empty_or_unset_uses_data_dir(self):
        base = tempfile.mkdtemp()
        with mock.patch.object(fc, "DATA_DIR", base):
            self.assertEqual(fc.log_file("signals.csv", cfg_with(log_dir="")), os.path.join(base, "signals.csv"))
            self.assertEqual(fc.log_file("signals.csv", None), os.path.join(base, "signals.csv"))

    def test_tilde_is_expanded(self):
        with mock.patch.object(fc, "DATA_DIR", tempfile.mkdtemp()):
            p = fc.log_file("signals.csv", cfg_with(log_dir="~/fxsignal_test_dir_unused"))
        self.assertEqual(p, os.path.join(os.path.expanduser("~/fxsignal_test_dir_unused"), "signals.csv"))
        os.rmdir(os.path.expanduser("~/fxsignal_test_dir_unused"))

    def test_unusable_folder_falls_back_to_data_dir(self):
        base = tempfile.mkdtemp()
        blocker = os.path.join(base, "file")
        open(blocker, "w").close()  # ファイルの下にはフォルダを作れない
        with mock.patch.object(fc, "DATA_DIR", base):
            p = fc.log_file("signals.csv", cfg_with(log_dir=os.path.join(blocker, "x")))
        self.assertEqual(p, os.path.join(base, "signals.csv"))


if __name__ == "__main__":
    unittest.main()
