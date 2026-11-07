"""価格チャートのデータ準備（取得・指標計算）と、専用ウィンドウの起動。

描画は chart_window.py（Tkinter の別プロセス）が行う。ブラウザは使わず、ファイルも作らない。
標準ライブラリのみ（fxcoreの指標計算を利用）。自動売買はしません。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

import fxcore as fc

# メニューに出す期間: (表示名, 足の間隔, 取得範囲, 表示する長さ[秒])
# 取得範囲は移動平均(最長75本)を計算する分の余裕を含めて広めにとる。
DAY = 86400
PERIODS = [
    ("1日", "15m", "5d", DAY),
    ("5日", "15m", "1mo", 5 * DAY),
    ("1ヶ月", "1h", "3mo", 30 * DAY),
    ("3ヶ月", "1d", "2y", 91 * DAY),
    ("6ヶ月", "1d", "2y", 182 * DAY),
    ("1年", "1d", "2y", 365 * DAY),
]
PERIOD_BY_LABEL = {label: (itv, rng, span) for label, itv, rng, span in PERIODS}

# 移動平均: (呼び名, 期間(本数), 色)
MA_SPECS = [("短期", 5, "#FFD60A"), ("中期", 25, "#0A84FF"), ("長期", 75, "#BF5AF2")]

MACD_PARAMS = (12, 26, 9)

WINDOW_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chart_window.py")

_windows = []  # 起動した表示ウィンドウのプロセス（終了済みは掃除する）


def fetch_ohlc(symbol, interval, rng):
    """[(unix_time, open, high, low, close), ...] を返す（形成中の足は含む）。"""
    url = fc.YAHOO_URL.format(sym=symbol, itv=interval, rng=rng)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as res:
        data = json.load(res)
    result = data["chart"]["result"][0]
    q = result["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(result["timestamp"]):
        vals = (q["open"][i], q["high"][i], q["low"][i], q["close"][i])
        if None in vals:
            continue
        out.append((ts,) + tuple(float(v) for v in vals))
    return out


def drop_forming(ohlc, interval, now=None):
    """まだ確定していない最後の足を除く。"""
    now = time.time() if now is None else now
    sec = fc.INTERVAL_SEC.get(interval)
    if sec and ohlc and ohlc[-1][0] + sec > now:
        return ohlc[:-1]
    return ohlc


def build_payload(cfg, label):
    """指定期間の表示データ（JSONにできる辞書）を作る。

    bars/mas/macd は表示範囲の足と同じ長さ。指標は表示範囲より前の足も使って計算する。
    """
    interval, rng, span = PERIOD_BY_LABEL[label]
    symbol = cfg["symbol"]
    history = drop_forming(fetch_ohlc(symbol, interval, rng), interval)
    if not history:
        raise ValueError("価格データを取得できませんでした")
    start = history[-1][0] - span
    off = next(i for i, c in enumerate(history) if c[0] >= start)
    closes = [c[4] for c in history]
    line, sig, hgram = fc.macd(closes, *MACD_PARAMS)
    hits = fc.signal_history([(c[0], c[2], c[3], c[4]) for c in history], cfg)
    signals = [{"i": k - off, "signal": r["signal"], "price": r["price"], "reason": r["reason"],
                "basis": r["basis"]} for k, r in sorted(hits.items()) if k >= off]
    return {
        "symbol": symbol,
        "interval": interval,
        "period": label,
        "updated": datetime.now().strftime("%H:%M"),
        "bars": [list(c) for c in history[off:]],
        "mas": [{"name": name, "period": period, "color": color,
                 "values": fc.sma(closes, period)[off:]} for name, period, color in MA_SPECS],
        "macd": {"line": line[off:], "signal": sig[off:], "hist": hgram[off:]},
        "signals": signals,
    }


def open_window(cfg, label):
    """指定期間のチャートを専用ウィンドウで開く（ブラウザは使わない）。"""
    payload = build_payload(cfg, label)
    _windows[:] = [p for p in _windows if p.poll() is None]
    proc = subprocess.Popen([sys.executable, WINDOW_SCRIPT], stdin=subprocess.PIPE)
    if proc.stdin is not None:
        proc.stdin.write(json.dumps(payload).encode("utf-8"))
        proc.stdin.close()
    _windows.append(proc)
    return proc
