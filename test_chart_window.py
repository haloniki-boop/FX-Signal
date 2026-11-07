import unittest

import chart_window as cw


def sample_data(n=10):
    bars = [[1000 + i * 900, 150.0 + i, 151.0 + i, 149.0 + i, 150.5 + i] for i in range(n)]
    return {
        "symbol": "USDJPY", "interval": "15m", "period": "1日", "updated": "12:00",
        "bars": bars,
        "mas": [{"name": "短期", "period": 5, "color": "#FFD60A",
                 "values": [None] * 4 + [150.0 + i for i in range(n - 4)]}],
        "macd": {"line": [None] * n, "signal": [None] * n, "hist": [None] * n},
    }


class TestGeometry(unittest.TestCase):
    def test_index_at_maps_positions_and_clamps(self):
        n = 10
        self.assertEqual(cw.index_at(cw.ML - 50, n), 0)        # 左外は先頭
        self.assertEqual(cw.index_at(cw.W + 500, n), n - 1)    # 右外は末尾
        for i in range(n):
            self.assertEqual(cw.index_at(cw.x_of(i, n), n), i)  # 足の中心はその足

    def test_price_range_pads_extremes(self):
        hi, lo = cw.price_range([[0, 1, 10.0, 2.0, 5], [0, 5, 8.0, 1.0, 6]])
        self.assertGreater(hi, 10.0)
        self.assertLess(lo, 1.0)

    def test_y_price_top_is_high(self):
        self.assertLess(cw.y_price(10, 10, 0), cw.y_price(0, 10, 0))


class TestRuns(unittest.TestCase):
    def test_splits_at_none(self):
        pts = cw.runs([1.0, 2.0, None, 3.0, 4.0], lambda i, v: (i, v))
        self.assertEqual(pts, [[0, 1.0, 1, 2.0], [3, 3.0, 4, 4.0]])

    def test_single_point_is_dropped(self):
        self.assertEqual(cw.runs([None, 1.0, None], lambda i, v: (i, v)), [])


class TestFormatting(unittest.TestCase):
    def test_fmt_val_handles_none(self):
        self.assertEqual(cw.fmt_val(None), "-")
        self.assertEqual(cw.fmt_val(150.123456), "150.123")
        self.assertEqual(cw.fmt_val(0.5, price=False), "+0.5000")

    def test_fmt_price_precision(self):
        self.assertEqual(cw.fmt_price(158.3), "158.300")
        self.assertEqual(cw.fmt_price(1.12345), "1.12345")


class TestTooltip(unittest.TestCase):
    def test_lines_show_signal_and_basis(self):
        d = sample_data()
        d["signals"] = [{"i": 3, "signal": "SELL", "price": 153.0, "reason": "理由R", "basis": ["根拠B"]}]
        lines = cw.tooltip_lines(d, 3)
        self.assertIn("【売りサイン】理由R", lines)
        self.assertIn("  ・根拠B", lines)
        self.assertFalse(any("サイン" in l for l in cw.tooltip_lines(d, 2)))

    def test_lines_show_ohlc_and_ma(self):
        lines = cw.tooltip_lines(sample_data(), 9)
        self.assertIn("始 159.000", lines[1])
        self.assertTrue(any(l.startswith("短期MA(5本) ") for l in lines))
        self.assertTrue(any("MACD" in l for l in lines))

    def test_missing_ma_shows_dash(self):
        lines = cw.tooltip_lines(sample_data(), 0)
        self.assertIn("短期MA(5本) -", lines)


class TestMacdLimit(unittest.TestCase):
    def test_none_when_no_values(self):
        self.assertIsNone(cw.macd_limit({"line": [None], "signal": [None], "hist": [None]}))

    def test_symmetric_limit_covers_extremes(self):
        m = cw.macd_limit({"line": [0.5, -2.0], "signal": [None, None], "hist": [0.1, -0.2]})
        self.assertIsNotNone(m)
        self.assertGreaterEqual(m or 0.0, 2.0)


class TestCrosshair(unittest.TestCase):
    """横ガイド線の位置から値を求める。縦と横の線は同じカーソル位置を使う。"""

    def test_price_at_y_inverts_y_price(self):
        hi, lo = 160.0, 150.0
        for p in (150.0, 152.5, 158.31, 160.0):
            self.assertAlmostEqual(cw.price_at_y(cw.y_price(p, hi, lo), hi, lo), p, places=9)

    def test_label_in_price_panel_is_price(self):
        hi, lo = 160.0, 150.0
        y = cw.y_price(158.31, hi, lo)
        self.assertEqual(cw.guide_label(y, hi, lo, 0.1), cw.fmt_price(158.31))

    def test_label_in_macd_panel_is_macd_value(self):
        hi, lo = 160.0, 150.0
        m = 0.2
        y = cw.MT2 + (m - 0.05) / (2 * m) * (cw.MB2 - cw.MT2)  # y_macd(0.05, m) と同じ位置
        self.assertEqual(cw.guide_label(y, hi, lo, m), "{:+.4f}".format(0.05))

    def test_no_label_outside_panels(self):
        self.assertIsNone(cw.guide_label(cw.PB + 2, 160.0, 150.0, 0.2))
        self.assertIsNone(cw.guide_label(cw.MT2 - 5, 160.0, 150.0, None))


if __name__ == "__main__":
    unittest.main()
