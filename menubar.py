#!/usr/bin/env python3
"""FXシグナル メニューバー常駐アプリ（macOS / rumps）

メニューバー表示例:  🇺🇸/🇯🇵 158.320 🟢買い
クリックで損切り・利確の目安、通貨ペア・戦略・通知先の切り替えができます。
"""
import sys
from datetime import datetime

import rumps

import chart
import fxcore as fc

PAIRS = ["USDJPY", "EURUSD", "EURJPY", "GBPJPY", "AUDJPY"]
STRATEGIES = [
    ("combo", "MACD+トレンド+BB（推奨）"),
    ("macd", "MACDクロス"),
    ("ma", "移動平均クロス"),
    ("bb", "ボリンジャーバンド反発"),
]
CHANNELS = [("mac", "Mac通知"), ("line", "LINE"), ("slack", "Slack"), ("mail", "メール")]


class FXSignalMenu(rumps.App):
    def __init__(self):
        super().__init__("FX", quit_button=None)
        self.cfg = fc.load_config()
        self.state = fc.load_state()

        self.info_item = rumps.MenuItem("読み込み中…")
        self.reason_item = rumps.MenuItem("")
        self.level_item = rumps.MenuItem("")
        self.ind_item = rumps.MenuItem("")
        self.err_item = rumps.MenuItem("")

        # 設定はまとめて「設定」サブメニューに入れる
        self.settings_menu = rumps.MenuItem("設定")

        self.pair_menu = rumps.MenuItem("通貨ペア")
        self.pair_items = {}
        for p in PAIRS:
            it = rumps.MenuItem(p, callback=lambda _=None, p=p: self.set_pair(p))
            self.pair_items[p] = it
            self.pair_menu.add(it)

        self.strat_menu = rumps.MenuItem("戦略")
        self.strat_items = {}
        for key, label in STRATEGIES:
            it = rumps.MenuItem(label, callback=lambda _=None, k=key: self.set_strategy(k))
            self.strat_items[key] = it
            self.strat_menu.add(it)

        self.chart_menu = rumps.MenuItem("グラフ")
        for label, _itv, _rng, _span in chart.PERIODS:
            self.chart_menu.add(rumps.MenuItem(
                label, callback=lambda _=None, l=label: self.show_chart(l)))

        self.notify_menu = rumps.MenuItem("通知先")
        self.channel_items = {}
        for key, label in CHANNELS:
            it = rumps.MenuItem(label, callback=lambda _=None, k=key: self.toggle_channel(k))
            self.channel_items[key] = it
            self.notify_menu.add(it)

        self.pre_item = rumps.MenuItem("売買の予告通知", callback=self.toggle_pre_alert)

        self.log_menu = rumps.MenuItem("ログ保存先")
        self.log_path_item = rumps.MenuItem("")
        self.log_menu.add(self.log_path_item)
        self.log_menu.add(None)
        self.log_menu.add(rumps.MenuItem("保存先を変更…", callback=self.change_log_dir))
        self.log_menu.add(rumps.MenuItem("既定の場所に戻す", callback=self.reset_log_dir))
        self.log_menu.add(rumps.MenuItem("保存先フォルダを開く", callback=self.open_log_dir))

        self.settings_menu.add(self.pair_menu)
        self.settings_menu.add(self.strat_menu)
        self.settings_menu.add(self.notify_menu)
        self.settings_menu.add(self.pre_item)
        self.settings_menu.add(self.log_menu)
        self.settings_menu.add(None)
        self.settings_menu.add(rumps.MenuItem("設定ファイルを開く", callback=self.open_config))

        self.menu = [
            self.info_item,
            self.reason_item,
            self.level_item,
            self.ind_item,
            self.err_item,
            None,
            self.chart_menu,
            self.settings_menu,
            None,
            rumps.MenuItem("今すぐ更新", callback=lambda _=None: self.tick()),
            rumps.MenuItem("テスト通知", callback=self.test_notify),
            rumps.MenuItem("通知ログを開く", callback=self.open_notify_log),
            None,
            rumps.MenuItem("終了", callback=rumps.quit_application),
        ]
        self.refresh_checks()
        self.timer = rumps.Timer(self.tick, max(15, int(self.cfg["poll_seconds"])))
        self.timer.start()
        self.tick()

    # ---------- 表示 ----------
    def refresh_checks(self):
        for p, it in self.pair_items.items():
            it.state = 1 if p == self.cfg["symbol"] else 0
        for k, it in self.strat_items.items():
            it.state = 1 if k == self.cfg["strategy"] else 0
        for k, it in self.channel_items.items():
            it.state = 1 if self.cfg["notify"].get(k) else 0
        self.pre_item.state = 1 if self.cfg.get("pre_alert", {}).get("enabled") else 0
        import os
        self.log_path_item.title = "現在: " + os.path.dirname(fc.log_file("signals.csv", self.cfg))

    def render(self, r):
        sym = self.cfg["symbol"]
        self.title = "{} {:.3f} {}".format(fc.pair_label(sym), r["price"],
                                           fc.SIGNAL_ICON[r["signal"]])
        now = datetime.now().strftime("%H:%M:%S")
        self.info_item.title = "{}  {}足  {}  ({}更新)".format(
            sym, self.cfg["interval"], dict(STRATEGIES).get(self.cfg["strategy"]), now)
        self.reason_item.title = "判定: {}  理由: {}".format(fc.SIGNAL_ICON[r["signal"]], r["reason"])
        if r.get("stop") is not None:
            self.level_item.title = "損切り目安 {:.3f} (約{}pips) / 利確目安 {:.3f} (約{}pips)".format(
                r["stop"], r["stop_pips"], r["target"], r["target_pips"])
        else:
            self.level_item.title = "売買の目安: シグナル待ち"
        self.ind_item.title = "RSI {}  MACD {}  BB {}〜{}".format(
            r["rsi"], r["macd"], r["bb_lower"], r["bb_upper"])
        errs = [k + ": " + v for k, v in list((r.get("sent") or {}).items()) + list((r.get("pre_sent") or {}).items())
                if v != "ok"]
        self.err_item.title = ("⚠ 通知エラー " + " / ".join(errs)) if errs else ""

    # ---------- 動作 ----------
    def tick(self, _=None):
        try:
            self.cfg = fc.load_config()  # 設定（log_dir など）の変更を再起動なしで反映
            self.refresh_checks()
            r = fc.process(self.cfg, self.state)
            self.render(r)
        except Exception as e:  # 取得失敗時もアプリは落とさない
            self.title = "{} ⚠取得失敗".format(fc.pair_label(self.cfg["symbol"]))
            self.info_item.title = "データ取得に失敗: {}".format(e)
            print("エラー:", e, file=sys.stderr)

    def set_pair(self, p):
        self.cfg["symbol"] = p
        fc.save_config(self.cfg)
        self.refresh_checks()
        self.tick()

    def set_strategy(self, k):
        self.cfg["strategy"] = k
        fc.save_config(self.cfg)
        self.refresh_checks()
        self.tick()

    def toggle_channel(self, k):
        self.cfg["notify"][k] = not self.cfg["notify"].get(k, False)
        fc.save_config(self.cfg)
        self.refresh_checks()

    def toggle_pre_alert(self, _=None):
        pa = self.cfg.setdefault("pre_alert", {"enabled": True, "atr_ratio": 0.2})
        pa["enabled"] = not pa.get("enabled", True)
        fc.save_config(self.cfg)
        self.refresh_checks()

    def change_log_dir(self, _=None):
        import os
        w = rumps.Window(
            message="ログを保存するフォルダのパスを入力してください。空欄にすると既定の場所に戻ります。",
            title="ログ保存先",
            default_text=self.cfg.get("log_dir", "") or "",
            ok="保存", cancel="キャンセル", dimensions=(360, 24))
        resp = w.run()
        if not resp.clicked:
            return
        text = resp.text.strip()
        if text:
            try:
                os.makedirs(os.path.expanduser(text), exist_ok=True)
            except OSError as e:
                rumps.alert("フォルダを作れませんでした", str(e))
                return
        self.cfg["log_dir"] = text
        fc.save_config(self.cfg)
        self.refresh_checks()

    def reset_log_dir(self, _=None):
        self.cfg["log_dir"] = ""
        fc.save_config(self.cfg)
        self.refresh_checks()

    def open_log_dir(self, _=None):
        import os
        import subprocess
        subprocess.run(["open", os.path.dirname(fc.log_file("signals.csv", self.cfg))], check=False)

    def show_chart(self, label):
        try:
            chart.open_window(self.cfg, label)
        except Exception as e:
            rumps.alert("グラフを作成できませんでした", str(e))

    def open_config(self, _=None):
        import subprocess
        if not __import__("os").path.exists(fc.CONFIG_PATH):
            fc.save_config(self.cfg)
        subprocess.run(["open", "-t", fc.CONFIG_PATH], check=False)

    def open_notify_log(self, _=None):
        import subprocess
        import os
        path = fc.log_file("notify_log.csv", self.cfg)
        if not os.path.exists(path):
            open(path, "a", encoding="utf-8").close()
        subprocess.run(["open", "-t", path], check=False)

    def test_notify(self, _=None):
        res = fc.send_all(self.cfg, "FXシグナル テスト", "通知テストです。届いていれば設定OKです。")
        bad = [k + ": " + v for k, v in res.items() if v != "ok"]
        if not res:
            rumps.alert("通知先が1つも有効になっていません。")
        elif bad:
            rumps.alert("一部失敗", "\n".join(bad))
        else:
            rumps.notification("FXシグナル", "テスト送信", "有効な通知先すべてに送信しました")


if __name__ == "__main__":
    FXSignalMenu().run()
