"""FXシグナル共通ロジック（標準ライブラリのみ・Python 3.9+）

- データ取得（Yahoo Finance 公開チャートAPI）
- 指標: SMA / EMA / RSI / MACD / ボリンジャーバンド / ATR
- 戦略: macd / ma / bb / combo
- 売買の目安（ATRベースのストップロス・利確）
- 通知: macOS通知 / LINE(Messaging API) / Slack(Webhook) / メール(SMTP)
"""
import csv
import json
import math
import os
import smtplib
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText

# 実行時データはアプリ外（外付けボリュームはGUIアプリから読めないことがあるため）
DATA_DIR = os.environ.get("FXSIGNAL_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "FXSignal")
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}=X?interval={itv}&range={rng}"
INTERVAL_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "1d": 86400}
FLAGS = {"USD": "🇺🇸", "JPY": "🇯🇵", "EUR": "🇪🇺", "GBP": "🇬🇧", "AUD": "🇦🇺",
         "CHF": "🇨🇭", "CAD": "🇨🇦", "NZD": "🇳🇿"}
SIGNAL_ICON = {"BUY": "🟢買い", "SELL": "🔴売り", "HOLD": "⚪待機"}

DEFAULT_CONFIG = {
    "symbol": "USDJPY",
    "interval": "15m",
    "range": "5d",
    "poll_seconds": 60,
    "strategy": "combo",          # macd | ma | bb | combo
    "ma": [9, 21],
    "macd": [12, 26, 9],
    "bb": [20, 2.0],
    "rsi_period": 14,
    "rsi_buy_max": 70,
    "rsi_sell_min": 30,
    "atr_period": 14,
    "sl_atr": 1.5,                # ストップロス = ATR × 1.5
    "tp_atr": 2.0,                # 利確 = ATR × 2.0
    "pre_alert": {"enabled": True, "atr_ratio": 0.2},  # 確定前の予告（macd/combo戦略のみ）
    "log_dir": "",  # ログ（signals.csv / notify_log.csv）の保存先。空=実行データフォルダ。例: "~/Documents/FXログ"
    "notify": {"mac": True, "line": False, "slack": False, "mail": False},
    "line": {"channel_access_token": "", "user_id": ""},
    "slack": {"webhook_url": ""},
    "mail": {"smtp_host": "", "smtp_port": 587, "username": "", "password": "",
             "from": "", "to": ""},
}


# ---------- 設定・状態 ----------
def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            user = json.load(fh)
    except (OSError, ValueError):
        user = {}
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


# ---------- データ ----------
def fetch_candles(symbol="USDJPY", interval="15m", rng="5d"):
    """[(unix_time, high, low, close), ...] を返す。"""
    url = YAHOO_URL.format(sym=symbol, itv=interval, rng=rng)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as res:
        data = json.load(res)
    result = data["chart"]["result"][0]
    q = result["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(result["timestamp"]):
        h, l, c = q["high"][i], q["low"][i], q["close"][i]
        if None in (h, l, c):
            continue
        out.append((ts, float(h), float(l), float(c)))
    return out


def drop_open_bar(candles, interval, now=None):
    """形成中（まだ確定していない）最新足を除外する。"""
    now = time.time() if now is None else now
    sec = INTERVAL_SEC.get(interval, 900)
    if candles and candles[-1][0] + sec > now:
        return candles[:-1]
    return candles


def pip_size(symbol):
    return 0.01 if "JPY" in symbol.upper() else 0.0001


def pair_label(symbol):
    """USDJPY -> 🇺🇸/🇯🇵 （米ドル=アメリカ国旗、日本円=日の丸）"""
    s = symbol.upper()
    return "{}/{}".format(FLAGS.get(s[:3], s[:3]), FLAGS.get(s[3:6], s[3:6]))


# ---------- 指標 ----------
def sma(values, n):
    out = [None] * len(values)
    for i in range(n - 1, len(values)):
        out[i] = sum(values[i - n + 1:i + 1]) / n
    return out


def ema(values, n):
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2.0 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values, n=14):
    out = [None] * len(values)
    if len(values) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    out[n] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def macd(values, fast=12, slow=26, sig=9):
    """(MACD線, シグナル線, ヒストグラム) を返す。"""
    ef, es = ema(values, fast), ema(values, slow)
    line = [None if (a is None or b is None) else a - b for a, b in zip(ef, es)]
    start = next((i for i, v in enumerate(line) if v is not None), len(line))
    signal = [None] * len(line)
    for j, v in enumerate(ema(line[start:], sig)):
        signal[start + j] = v
    hist = [None if (a is None or b is None) else a - b for a, b in zip(line, signal)]
    return line, signal, hist


def bollinger(values, n=20, k=2.0):
    """(中心線, 上限, 下限) を返す。"""
    mid = sma(values, n)
    up = [None] * len(values)
    lo = [None] * len(values)
    for i in range(n - 1, len(values)):
        window = values[i - n + 1:i + 1]
        sd = math.sqrt(sum((x - mid[i]) ** 2 for x in window) / n)
        up[i] = mid[i] + k * sd
        lo[i] = mid[i] - k * sd
    return mid, up, lo


def atr(highs, lows, closes, n=14):
    tr = [None] * len(closes)
    for i in range(1, len(closes)):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    prev = sum(tr[1:n + 1]) / n
    out[n] = prev
    for i in range(n + 1, len(closes)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


# ---------- 判定 ----------
def decide(candles, cfg):
    """確定足の最新バーでシグナルを判定する。

    戦略:
      macd  : MACDがシグナル線を上抜け(BUY)/下抜け(SELL)
      ma    : 短期SMAが長期SMAを上抜け/下抜け
      bb    : ボリンジャーバンド下限から反発(BUY)/上限から反落(SELL)
      combo : MACDクロス + 中心線より上(BUY)/下(SELL) + バンド内 （推奨）
    いずれも RSI で過熱・売られ過ぎを除外する。
    """
    highs = [c[1] for c in candles]
    lows = [c[2] for c in candles]
    closes = [c[3] for c in candles]
    n = len(closes)
    ma_f, ma_s = cfg["ma"]
    m_f, m_s, m_g = cfg["macd"]
    bb_n, bb_k = cfg["bb"]
    need = max(m_s + m_g, ma_s, bb_n, cfg["rsi_period"], cfg["atr_period"]) + 3
    if n < need:
        return {"signal": "HOLD", "reason": "データ不足", "price": closes[-1] if closes else None}

    i, p = n - 1, n - 2
    r = rsi(closes, cfg["rsi_period"])
    _, _, hist = macd(closes, m_f, m_s, m_g)
    mid, up, lo = bollinger(closes, bb_n, bb_k)
    a = atr(highs, lows, closes, cfg["atr_period"])
    fa, sa = sma(closes, ma_f), sma(closes, ma_s)

    macd_up = hist[p] <= 0 < hist[i]
    macd_dn = hist[p] >= 0 > hist[i]
    ma_up = fa[p] <= sa[p] and fa[i] > sa[i]
    ma_dn = fa[p] >= sa[p] and fa[i] < sa[i]
    bb_up = closes[p] < lo[p] and closes[i] >= lo[i]
    bb_dn = closes[p] > up[p] and closes[i] <= up[i]

    strat = cfg["strategy"]
    if strat == "macd":
        trig_b, trig_s, why = macd_up, macd_dn, "MACDクロス"
    elif strat == "ma":
        trig_b, trig_s, why = ma_up, ma_dn, "移動平均クロス"
    elif strat == "bb":
        trig_b, trig_s, why = bb_up, bb_dn, "BBバンド反発"
    else:  # combo
        trig_b = macd_up and closes[i] > mid[i] and closes[i] < up[i]
        trig_s = macd_dn and closes[i] < mid[i] and closes[i] > lo[i]
        why = "MACDクロス+トレンド+バンド内"

    rv = r[i]
    rsi_ok_b = rv is not None and rv < cfg["rsi_buy_max"]
    rsi_ok_s = rv is not None and rv > cfg["rsi_sell_min"]
    price = closes[i]
    signal, reason = "HOLD", "条件不成立"
    if trig_b and rsi_ok_b:
        signal, reason = "BUY", why + " & RSI過熱なし"
    elif trig_s and rsi_ok_s:
        signal, reason = "SELL", why + " & RSI売られ過ぎなし"
    elif trig_b or trig_s:
        reason = why + "だがRSIフィルターで見送り"

    basis = _basis(signal, strat, cfg, closes, hist, fa, sa, mid, up, lo, rv) if signal != "HOLD" else []
    stop = target = stop_pips = target_pips = None
    if signal != "HOLD" and a[i] is not None:
        d = 1 if signal == "BUY" else -1
        stop = price - d * cfg["sl_atr"] * a[i]
        target = price + d * cfg["tp_atr"] * a[i]
        pip = pip_size(cfg.get("symbol", "USDJPY"))
        stop_pips = abs(price - stop) / pip
        target_pips = abs(target - price) / pip

    def rd(x, k=3):
        return None if x is None else round(x, k)

    return {
        "signal": signal, "reason": reason, "price": price,
        "rsi": rd(rv, 1), "macd": rd(hist[i], 5),
        "bb_upper": rd(up[i]), "bb_mid": rd(mid[i]), "bb_lower": rd(lo[i]),
        "atr": rd(a[i], 4),
        "stop": rd(stop), "target": rd(target),
        "stop_pips": rd(stop_pips, 1), "target_pips": rd(target_pips, 1),
        "basis": basis,
        "bar_time": candles[i][0],
    }


def _basis(signal, strat, cfg, closes, hist, fa, sa, mid, up, lo, rv):
    """シグナルの根拠を日本語の箇条書きで返す（通知・ログ・チャート表示用）。"""
    i, p = len(closes) - 1, len(closes) - 2
    buy = signal == "BUY"
    out = []
    if strat in ("macd", "combo"):
        out.append("MACDヒストグラム {:+.5f} → {:+.5f}（ゼロ線を{}）".format(
            hist[p], hist[i], "上抜け" if buy else "下抜け"))
    if strat == "ma":
        out.append("短期MA({}) {:.3f} が長期MA({}) {:.3f} を{}".format(
            cfg["ma"][0], fa[i], cfg["ma"][1], sa[i], "上抜け" if buy else "下抜け"))
    if strat == "bb":
        edge = lo[i] if buy else up[i]
        out.append("終値 {:.3f}（前足 {:.3f}）がBB{} {:.3f} を{}".format(
            closes[i], closes[p], "下限" if buy else "上限", edge, "上回って反発" if buy else "下回って反落"))
    if strat == "combo":
        out.append("終値 {:.3f} は中心線 {:.3f} の{}、BB {:.3f}〜{:.3f} 内".format(
            closes[i], mid[i], "上" if buy else "下", lo[i], up[i]))
    limit = "{}未満で買われ過ぎなし".format(cfg["rsi_buy_max"]) if buy else "{}超で売られ過ぎなし".format(cfg["rsi_sell_min"])
    out.append("RSI {:.1f}（{}）".format(rv, limit))
    return out


def signal_history(candles, cfg):
    """過去の各確定足で判定し、シグナルが出た足だけを {足の番号: decideの結果} で返す。

    candles は確定足のみ（fetch_candles の形式 (time, high, low, close)）。
    """
    out = {}
    for k in range(len(candles)):
        r = decide(candles[:k + 1], cfg)
        if r["signal"] != "HOLD":
            out[k] = r
    return out


def record_text(res):
    """ログ用の1項目: 理由と根拠を | で繋ぐ。"""
    return " | ".join([res.get("reason") or ""] + list(res.get("basis") or []))


# ---------- 予告（確定前） ----------
def _levels(signal, price, atr_v, cfg):
    """損切り・利確の目安 (stop, target, stop_pips, target_pips)。ATRが無ければ全部 None。"""
    if atr_v is None:
        return None, None, None, None
    d = 1 if signal == "BUY" else -1
    stop = price - d * cfg["sl_atr"] * atr_v
    target = price + d * cfg["tp_atr"] * atr_v
    pip = pip_size(cfg.get("symbol", "USDJPY"))
    return (round(stop, 3), round(target, 3),
            round(abs(price - stop) / pip, 1), round(abs(target - price) / pip, 1))


def pre_alert(candles, cfg):
    """形成中の足（candles の最後）で、MACDヒストグラムがゼロ線に接近したら予告を返す。

    確定したシグナルではない。接近の条件:
      BUY : ヒストグラムが負のまま、直前足より上昇（ゼロ線へ向かう）かつ |値| ≤ ATR × atr_ratio
      SELL: 正のまま、直前足より下降かつ値 ≤ ATR × atr_ratio
    対象は macd / combo 戦略のみ。RSI・combo のバンド条件も確定時と同じに掛ける。
    戻り値は decide と同じ形式の dict、または None。
    """
    if cfg["strategy"] not in ("macd", "combo") or not candles:
        return None
    highs = [c[1] for c in candles]
    lows = [c[2] for c in candles]
    closes = [c[3] for c in candles]
    n = len(closes)
    ma_f, ma_s = cfg["ma"]
    m_f, m_s, m_g = cfg["macd"]
    bb_n, bb_k = cfg["bb"]
    need = max(m_s + m_g, ma_s, bb_n, cfg["rsi_period"], cfg["atr_period"]) + 3
    if n < need:
        return None

    i, p = n - 1, n - 2
    _, _, hist = macd(closes, m_f, m_s, m_g)
    mid, up, lo = bollinger(closes, bb_n, bb_k)
    a = atr(highs, lows, closes, cfg["atr_period"])
    r = rsi(closes, cfg["rsi_period"])
    hp, hn = hist[p], hist[i]
    av, rv, mv, uv, lv = a[i], r[i], mid[i], up[i], lo[i]
    if hp is None or hn is None or av is None or rv is None or mv is None or uv is None or lv is None:
        return None
    thr = cfg["pre_alert"]["atr_ratio"] * av
    price = closes[i]

    if hp < 0 and hn < 0 and hn > hp and -hn <= thr:
        signal = "BUY"
    elif hp > 0 and hn > 0 and hn < hp and hn <= thr:
        signal = "SELL"
    else:
        return None

    if cfg["strategy"] == "combo":  # 確定時のバンド・中心線条件と揃える
        if signal == "BUY" and not (mv < price < uv):
            return None
        if signal == "SELL" and not (lv < price < mv):
            return None
    if signal == "BUY" and not rv < cfg["rsi_buy_max"]:
        return None
    if signal == "SELL" and not rv > cfg["rsi_sell_min"]:
        return None

    stop, target, stop_pips, target_pips = _levels(signal, price, av, cfg)
    basis = [
        "MACDヒストグラム {:+.5f} → {:+.5f}（ゼロ線まで残り {:.5f}、ATR×{}以内）".format(
            hp, hn, abs(hn), cfg["pre_alert"]["atr_ratio"]),
        "RSI {:.1f}".format(rv),
    ]
    return {
        "signal": signal, "reason": "MACDがゼロ線に接近中（確定前の予告）", "price": price,
        "rsi": round(rv, 1), "macd": round(hn, 5), "stop": stop, "target": target,
        "stop_pips": stop_pips, "target_pips": target_pips, "basis": basis, "bar_time": candles[i][0],
    }


# ---------- 通知 ----------
def format_message(symbol, res):
    lines = ["{} {} @ {:.3f}".format(res["signal"], symbol, res["price"]), res["reason"]]
    if res.get("stop") is not None:
        lines.append("損切り目安 {:.3f} (約{}pips)".format(res["stop"], res["stop_pips"]))
        lines.append("利確目安 {:.3f} (約{}pips)".format(res["target"], res["target_pips"]))
    lines.append("RSI {}  MACD {}".format(res["rsi"], res["macd"]))
    if res.get("basis"):
        lines.append("根拠:")
        lines.extend("・" + b for b in res["basis"])
    return "\n".join(lines)


def send_mac(title, body):
    subprocess.run(
        ["osascript", "-e", "on run argv",
         "-e", "display notification (item 1 of argv) with title (item 2 of argv) sound name \"Glass\"",
         "-e", "end run", body, title],
        check=False, timeout=10)


def send_line(cfg, text):
    c = cfg["line"]
    if not c["channel_access_token"] or not c["user_id"]:
        raise RuntimeError("LINEのトークン/ユーザーIDが未設定")
    body = json.dumps({"to": c["user_id"], "messages": [{"type": "text", "text": text}]}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push", data=body, method="POST",
        headers={"Authorization": "Bearer " + c["channel_access_token"],
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10):
        pass


def send_slack(cfg, text):
    url = cfg["slack"]["webhook_url"]
    if not url:
        raise RuntimeError("Slack Webhook URLが未設定")
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10):
        pass


def send_mail(cfg, subject, text):
    m = cfg["mail"]
    if not m["smtp_host"] or not m["to"]:
        raise RuntimeError("メールのSMTP設定が未入力")
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = m["from"] or m["username"]
    msg["To"] = m["to"]
    with smtplib.SMTP(m["smtp_host"], int(m["smtp_port"]), timeout=15) as s:
        s.ehlo()
        if int(m["smtp_port"]) != 25:
            s.starttls()
            s.ehlo()
        if m["username"]:
            s.login(m["username"], m["password"])
        s.send_message(msg)


def send_all(cfg, title, text):
    """有効な通知先すべてに送る。戻り値: {channel: 'ok' or エラー文}"""
    results = {}
    enabled = cfg["notify"]
    senders = {
        "mac": lambda: send_mac(title, text),
        "line": lambda: send_line(cfg, title + "\n" + text),
        "slack": lambda: send_slack(cfg, "*" + title + "*\n" + text),
        "mail": lambda: send_mail(cfg, title, text),
    }
    for ch, fn in senders.items():
        if not enabled.get(ch):
            continue
        try:
            fn()
            results[ch] = "ok"
        except Exception as e:  # 1つの失敗で他を止めない
            results[ch] = "error: {}".format(e)
    return results


def _prepend_csv(path, header, row):
    """CSVのヘッダーの直後に1行を入れる（新しいものが先頭）。一時ファイル経由で置き換える。"""
    old = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as fh:
            old = list(csv.reader(fh))
    body = old[1:] if old and old[0] == header else old
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerow(row)
        w.writerows(body)
    os.replace(tmp, path)


def _log_dir(cfg=None):
    """ログの保存先フォルダ。cfg["log_dir"] を使い、作れない場合は実行データフォルダへ戻す。"""
    want = os.path.expanduser(str((cfg or {}).get("log_dir") or "").strip())
    if want:
        try:
            os.makedirs(want, exist_ok=True)
            return want
        except OSError as e:
            sys.stderr.write("ログ保存先を作れないため既定の場所に保存します ({}): {}\n".format(want, e))
    return DATA_DIR


def log_file(name, cfg=None):
    """ログファイルの絶対パス（name は 'signals.csv' / 'notify_log.csv'）。"""
    return os.path.join(_log_dir(cfg), name)


def log_signal(symbol, res, cfg=None):
    _prepend_csv(log_file("signals.csv", cfg),
                 ["time", "symbol", "signal", "price", "stop", "target", "rsi", "reason"],
                 [datetime.now().isoformat(timespec="seconds"), symbol, res["signal"],
                  res.get("price"), res.get("stop"), res.get("target"), res.get("rsi"),
                  record_text(res)])


def log_notify(kind, symbol, res, sent, cfg=None):
    """通知を1件ずつ記録する（新しいものを先頭）。kind: 'pre'（予告）/ 'confirmed'（確定シグナル）。"""
    chans = "; ".join("{}={}".format(k, v) for k, v in (sent or {}).items()) or "送信先なし"
    _prepend_csv(log_file("notify_log.csv", cfg),
                 ["time", "kind", "symbol", "signal", "price", "stop", "target",
                  "bar_time", "channels", "reason"],
                 [datetime.now().isoformat(timespec="seconds"), kind, symbol, res["signal"],
                  res.get("price"), res.get("stop"), res.get("target"),
                  datetime.fromtimestamp(res["bar_time"]).isoformat(timespec="minutes"),
                  chans, record_text(res)])


def process(cfg, state):
    """1回分: 取得→判定→通知・記録。結果dictを返す。

    - 確定シグナル: 足が確定した直後に通知（同じ足では1回だけ）。
    - 予告: 確定前の形成中の足でMACDがゼロ線に接近したら、同じ足で1回だけ通知。
    全ての通知は notify_log.csv に記録する。
    """
    full = fetch_candles(cfg["symbol"], cfg["interval"], cfg["range"])
    candles = drop_open_bar(full, cfg["interval"])
    res = decide(candles, cfg)
    key = "{}-{}-{}".format(cfg["symbol"], cfg["interval"], cfg["strategy"])
    res["sent"] = None
    res["pre"] = None
    res["pre_sent"] = None
    if res["signal"] != "HOLD" and state.get(key) != res["bar_time"]:
        res["sent"] = send_all(cfg, "FXシグナル: {}".format(res["signal"]),
                               format_message(cfg["symbol"], res))
        log_signal(cfg["symbol"], res, cfg)
        log_notify("confirmed", cfg["symbol"], res, res["sent"], cfg)
        state[key] = res["bar_time"]
        save_state(state)
    elif cfg.get("pre_alert", {}).get("enabled") and len(full) > len(candles):
        pre = pre_alert(full, cfg)
        pkey = key + "-pre"
        if pre:  # 形成中の足の時刻は取得のたびに変わるので、足の開始時刻（区切り）に揃えて重複を防ぐ
            sec = INTERVAL_SEC.get(cfg["interval"], 900)
            pre["bar_time"] = pre["bar_time"] - pre["bar_time"] % sec
        if pre and state.get(pkey) != pre["bar_time"]:
            res["pre"] = pre
            res["pre_sent"] = send_all(cfg, "FXシグナル予告: {}（確定前）".format(pre["signal"]),
                                       "予告（確定前）\n" + format_message(cfg["symbol"], pre))
            log_notify("pre", cfg["symbol"], pre, res["pre_sent"], cfg)
            state[pkey] = pre["bar_time"]
            save_state(state)
    return res
