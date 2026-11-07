#!/usr/bin/env python3
"""FXシグナル CLI（1回判定 / 繰り返し監視 / 通知テスト）

  python3 fx_signal.py                 # 1回判定して表示
  python3 fx_signal.py --loop          # poll_seconds ごとに監視
  python3 fx_signal.py --test-notify   # 有効な通知先すべてにテスト送信
"""
import argparse
import sys
import time
from datetime import datetime

import fxcore as fc


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="FX売買タイミング通知")
    p.add_argument("--symbol")
    p.add_argument("--interval")
    p.add_argument("--range")
    p.add_argument("--strategy", choices=["macd", "ma", "bb", "combo"])
    p.add_argument("--loop", action="store_true")
    p.add_argument("--test-notify", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    cfg = fc.load_config()
    for k in ("symbol", "interval", "range", "strategy"):
        if getattr(args, k):
            cfg[k] = getattr(args, k)
    if args.test_notify:
        print(fc.send_all(cfg, "FXシグナル テスト", "通知テストです。届いていれば設定OKです。"))
        return
    state = fc.load_state()
    while True:
        try:
            r = fc.process(cfg, state)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print("[{}] {} {} {:.3f} 判定={} RSI={} {}".format(
                now, cfg["symbol"], cfg["interval"], r["price"], r["signal"], r["rsi"], r["reason"]))
            if r.get("sent"):
                print("  通知結果:", r["sent"])
                if r["stop"] is not None:
                    print("  損切り {} / 利確 {}".format(r["stop"], r["target"]))
        except Exception as e:  # 一時的なネットワーク障害で止めない
            print("エラー:", e, file=sys.stderr)
        if not args.loop:
            break
        time.sleep(cfg["poll_seconds"])


if __name__ == "__main__":
    main()
