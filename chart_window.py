"""価格チャートの専用表示ウィンドウ（macOSネイティブ: AppKit / pyobjc）。

標準入力のJSON（chart.build_payload の結果）を読み、ローソク足・移動平均・MACDを描く。
マウス位置の足に縦のガイド線を出し、その足の値を表示する。Esc / Cmd+W で閉じる。
ブラウザは使わず、ファイルも作らない。自動売買はしません。

AppKit は main() の中で読み込むので、下の計算関数はそれなしでテストできる。
"""
import json
import sys
from datetime import datetime

W, H = 1100, 680
ML, MR = 80, 100
PT, PB = 78, 440        # 価格パネルの上端・下端（画面の上から下へ）
MT2, MB2 = 480, 625     # MACDパネルの上端・下端
COLOR_UP, COLOR_DOWN = "#30D158", "#FF453A"
COLOR_MACD, COLOR_SIGNAL = "#64D2FF", "#FF9F0A"
COLOR_BG, COLOR_GRID = "#16181d", "#2c2f36"
COLOR_TEXT, COLOR_SUB = "#f2f2f7", "#8e8e93"


# ---------- 計算（AppKit非依存） ----------

def slot_width(n):
    return (W - ML - MR) / n


def x_of(i, n):
    return ML + (i + 0.5) * slot_width(n)


def index_at(x, n):
    """マウスのx座標に対応する足の番号（範囲外は端に丸める）。"""
    i = int((x - ML) // slot_width(n))
    return min(max(i, 0), n - 1)


def price_range(bars):
    hi = max(b[2] for b in bars)
    lo = min(b[3] for b in bars)
    pad = (hi - lo) * 0.05 or hi * 0.001
    return hi + pad, lo - pad


def y_price(p, hi, lo):
    return PT + (hi - p) / (hi - lo) * (PB - PT)


def price_at_y(y, hi, lo):
    """y_price の逆変換: 画面のy座標から価格。"""
    return hi - (y - PT) / (PB - PT) * (hi - lo)


def guide_label(y, hi, lo, m):
    """横ガイド線の位置の値。価格パネルなら価格、MACDパネルならMACD値、それ以外は None。"""
    if PT <= y <= PB:
        return fmt_price(price_at_y(y, hi, lo))
    if m is not None and MT2 <= y <= MB2:
        return "{:+.4f}".format(m - (y - MT2) * 2 * m / (MB2 - MT2))
    return None


def macd_limit(macd):
    """MACDパネルの上下の振れ幅。値がなければ None。"""
    vals = [v for k in ("line", "signal", "hist") for v in macd[k] if v is not None]
    if not vals:
        return None
    return max(abs(v) for v in vals) * 1.1 or 1e-6


def y_macd(v, m):
    return MT2 + (m - v) / (2 * m) * (MB2 - MT2)


def runs(values, to_xy):
    """None で切れた区間ごとの折れ線座標 [x0, y0, x1, y1, ...] を返す（2点以上のみ）。"""
    out, cur = [], []
    for i, v in enumerate(values):
        if v is None:
            if len(cur) >= 4:
                out.append(cur)
            cur = []
        else:
            cur.extend(to_xy(i, v))
    if len(cur) >= 4:
        out.append(cur)
    return out


def fmt_price(p):
    return "{:.3f}".format(p) if p >= 20 else "{:.5f}".format(p)


def fmt_val(v, price=True):
    if v is None:
        return "-"
    return fmt_price(v) if price else "{:+.4f}".format(v)


def fmt_time(ts, interval):
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%Y/%m/%d") if interval == "1d" else dt.strftime("%Y/%m/%d %H:%M")


def tooltip_lines(data, i):
    """カーソル位置の足の値（ガイド線の横に出す文字）。"""
    ts, o, h, l, c = data["bars"][i]
    lines = [fmt_time(ts, data["interval"]),
             "始 {}  高 {}  安 {}  終 {}".format(fmt_price(o), fmt_price(h), fmt_price(l), fmt_price(c))]
    for ma in data["mas"]:
        lines.append("{}MA({}本) {}".format(ma["name"], ma["period"], fmt_val(ma["values"][i])))
    m = data["macd"]
    lines.append("MACD {}  シグナル {}  ヒスト {}".format(
        fmt_val(m["line"][i], False), fmt_val(m["signal"][i], False), fmt_val(m["hist"][i], False)))
    for s in data.get("signals", []):
        if s["i"] == i:
            lines.append("【{}サイン】{}".format("買い" if s["signal"] == "BUY" else "売り", s["reason"]))
            lines.extend("  ・" + b for b in s["basis"])
    return lines


# ---------- 描画（AppKit） ----------

def _ak():
    import AppKit
    return AppKit


def _color(hex_):
    ak = _ak()
    h = hex_.lstrip("#")
    r, g, b = (int(h[k:k + 2], 16) / 255.0 for k in (0, 2, 4))
    return ak.NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0)


def _attrs(size, color, bold=False):
    ak = _ak()
    font = ak.NSFont.boldSystemFontOfSize_(size) if bold else ak.NSFont.systemFontOfSize_(size)
    return {ak.NSFontAttributeName: font, ak.NSForegroundColorAttributeName: _color(color)}


def _text(s, x, y, size=12, color=COLOR_TEXT, bold=False, align="left", mid=False):
    """(x, y) は文字の左上（右揃え・中央揃えなら基準点）。mid=True なら y を文字の高さの中央とみなす。"""
    from Foundation import NSString
    attrs = _attrs(size, color, bold)
    ns = NSString.stringWithString_(s)
    sz = ns.sizeWithAttributes_(attrs)
    if align == "right":
        x -= sz.width
    elif align == "center":
        x -= sz.width / 2
    if mid:
        y -= sz.height / 2
    ns.drawAtPoint_withAttributes_(_ak().NSMakePoint(x, y), attrs)


def _line(x1, y1, x2, y2, color, width=1.0, dash=None):
    ak = _ak()
    p = ak.NSBezierPath.bezierPath()
    p.moveToPoint_(ak.NSMakePoint(x1, y1))
    p.lineToPoint_(ak.NSMakePoint(x2, y2))
    p.setLineWidth_(width)
    if dash:
        p.setLineDash_count_phase_(list(dash), len(dash), 0.0)
    _color(color).setStroke()
    p.stroke()


def _polyline(flat, color, width=2.0):
    ak = _ak()
    p = ak.NSBezierPath.bezierPath()
    p.moveToPoint_(ak.NSMakePoint(flat[0], flat[1]))
    for k in range(2, len(flat), 2):
        p.lineToPoint_(ak.NSMakePoint(flat[k], flat[k + 1]))
    p.setLineWidth_(width)
    _color(color).setStroke()
    p.stroke()


def _rect(x, y, w, h, fill=None, stroke=None):
    ak = _ak()
    r = ak.NSMakeRect(x, y, w, h)
    if fill:
        _color(fill).setFill()
        ak.NSBezierPath.fillRect_(r)
    if stroke:
        _color(stroke).setStroke()
        ak.NSBezierPath.strokeRect_(r)


def draw_chart(data, cross_x, cross_y=None):
    bars = data["bars"]
    n = len(bars)
    hi, lo = price_range(bars)
    body_w = max(1.0, slot_width(n) * 0.7)
    xs = lambda i: x_of(i, n)
    yp = lambda p: y_price(p, hi, lo)

    # タイトル（上端y=14）と凡例（上端y=43）は別の行
    _text("{}  {}足  {}（{}更新）".format(data["symbol"], data["interval"], data["period"], data["updated"]),
          ML, 14, size=18, bold=True)
    lx = ML
    for ma in data["mas"]:
        _line(lx, 51, lx + 22, 51, ma["color"], 3)
        _text("{}MA({}本)".format(ma["name"], ma["period"]), lx + 28, 43, size=13, color=ma["color"])
        lx += 170

    # 価格軸
    for k in range(6):
        p = lo + (hi - lo) * k / 5
        y = yp(p)
        _line(ML, y, W - MR, y, COLOR_GRID)
        _text(fmt_price(p), ML - 8, y, size=11, color=COLOR_SUB, align="right", mid=True)

    # ローソク足
    for i, (ts, o, h, l, c) in enumerate(bars):
        col = COLOR_UP if c >= o else COLOR_DOWN
        x = xs(i)
        _line(x, yp(h), x, yp(l), col, 1.0)
        top, bot = yp(max(o, c)), yp(min(o, c))
        _rect(x - body_w / 2, top, body_w, max(1.0, bot - top), fill=col)

    # 移動平均（ローソク足の上に重ねる）
    for ma in data["mas"]:
        for pts in runs(ma["values"], lambda i, v: (xs(i), yp(v))):
            _polyline(pts, ma["color"], 2.0)

    draw_signals(data, n, hi, lo)

    # 最新値
    last = bars[-1][4]
    y = yp(last)
    _line(ML, y, W - MR, y, COLOR_TEXT, 1.0, dash=(4, 4))
    _text(fmt_price(last), W - MR + 6, y, size=12, mid=True)

    # 時間軸
    ticks = min(8, n)
    for k in range(ticks):
        i = int(k * (n - 1) / max(1, ticks - 1))
        _text(fmt_time(bars[i][0], data["interval"]), xs(i), MB2 + 18, size=11, color=COLOR_SUB, align="center")

    draw_macd(data["macd"], n)

    if cross_x is not None and ML <= cross_x <= W - MR:
        draw_guide(data, cross_x)
    if cross_y is not None and PT <= cross_y <= MB2:
        draw_hguide(cross_y, guide_label(cross_y, hi, lo, macd_limit(data["macd"])))


def draw_hguide(y, label):
    """マウス位置の高さに横のガイド線を出し、右の軸の位置に値を表示する。"""
    _line(ML, y, W - MR, y, COLOR_TEXT, 1.0, dash=(3, 3))
    if label:
        _rect(W - MR + 2, y - 9, MR - 4, 18, fill="#0d0f13", stroke=COLOR_GRID)
        _text(label, W - MR + 6, y, size=12, mid=True)


def _fill_poly(flat, color):
    ak = _ak()
    p = ak.NSBezierPath.bezierPath()
    p.moveToPoint_(ak.NSMakePoint(flat[0], flat[1]))
    for k in range(2, len(flat), 2):
        p.lineToPoint_(ak.NSMakePoint(flat[k], flat[k + 1]))
    p.closePath()
    _color(color).setFill()
    p.fill()


def draw_signals(data, n, hi, lo):
    """過去の売買サイン: 買いは安値の下に上向き、売りは高値の上に下向きの三角形を描く。"""
    for s in data.get("signals", []):
        i = s["i"]
        if not 0 <= i < n:
            continue
        ts, o, h, l, c = data["bars"][i]
        x = x_of(i, n)
        if s["signal"] == "BUY":
            y = y_price(l, hi, lo) + 4
            _fill_poly([x, y, x - 6, y + 10, x + 6, y + 10], COLOR_UP)
            _text("買", x, y + 12, size=11, color=COLOR_UP, bold=True, align="center")
        else:
            y = y_price(h, hi, lo) - 4
            _fill_poly([x, y, x - 6, y - 10, x + 6, y - 10], COLOR_DOWN)
            _text("売", x, y - 28, size=11, color=COLOR_DOWN, bold=True, align="center")


def draw_macd(macd, n):
    _text("MACD(12,26,9)", ML, MT2 - 22, size=12, color=COLOR_SUB)
    _text("MACD線", ML + 130, MT2 - 22, size=12, color=COLOR_MACD)
    _text("シグナル", ML + 200, MT2 - 22, size=12, color=COLOR_SIGNAL)
    _text("ヒストグラム", ML + 270, MT2 - 22, size=12, color=COLOR_SUB)
    _rect(ML, MT2, W - ML - MR, MB2 - MT2, stroke=COLOR_GRID)

    m = macd_limit(macd)
    if m is None:
        _text("データ不足", ML + 8, MT2 + 10, size=12, color=COLOR_SUB)
        return

    y0 = y_macd(0.0, m)
    for v in (m, 0.0, -m):
        y = y_macd(v, m)
        if v == 0.0:
            _line(ML, y, W - MR, y, "#3a3d45")
        _text("{:+.4f}".format(v), ML - 8, y, size=11, color=COLOR_SUB, align="right", mid=True)

    bw = max(1.0, slot_width(n) * 0.7)
    for i, h in enumerate(macd["hist"]):
        if h is None:
            continue
        yv = y_macd(h, m)
        _rect(x_of(i, n) - bw / 2, min(y0, yv), bw, max(1.0, abs(yv - y0)),
              fill=COLOR_UP if h >= 0 else COLOR_DOWN)
    for pts in runs(macd["line"], lambda i, v: (x_of(i, n), y_macd(v, m))):
        _polyline(pts, COLOR_MACD, 1.6)
    for pts in runs(macd["signal"], lambda i, v: (x_of(i, n), y_macd(v, m))):
        _polyline(pts, COLOR_SIGNAL, 1.6)


def draw_guide(data, x):
    """マウス位置の足に縦のガイド線を出し、その足の値を表示する。"""
    n = len(data["bars"])
    i = index_at(x, n)
    gx = x_of(i, n)
    _line(gx, PT, gx, MB2, COLOR_TEXT, 1.0, dash=(3, 3))

    lines = tooltip_lines(data, i)
    attrs = _attrs(12, COLOR_TEXT)
    from Foundation import NSString
    width = max(NSString.stringWithString_(s).sizeWithAttributes_(attrs).width for s in lines)
    box_w, box_h = width + 16, 18 * len(lines) + 12
    # 情報欄はガイド線と反対側の端に置く（線に重ならないように）
    bx = ML + 10 if gx > W / 2 else W - MR - box_w - 10
    _rect(bx, PT + 6, box_w, box_h, fill="#0d0f13", stroke=COLOR_GRID)
    for k, s in enumerate(lines):
        _text(s, bx + 8, PT + 12 + k * 18, size=12)


# ---------- ウィンドウ（main でだけ作る） ----------

def _make_classes():
    ak = _ak()
    from objc import super as objc_super

    class ChartView(ak.NSView):
        def initWithFrame_data_(self, frame, data):
            self = objc_super(ChartView, self).initWithFrame_(frame)
            if self is None:
                return None
            self.data = data
            self.cross_x = None
            self.cross_y = None
            return self

        def isFlipped(self):
            return True  # 座標を上から下へ（計算関数と同じ向き）

        def acceptsFirstResponder(self):
            return True

        def drawRect_(self, rect):
            _rect(0, 0, W, H, fill=COLOR_BG)
            draw_chart(self.data, self.cross_x, self.cross_y)

        def updateTrackingAreas(self):
            objc_super(ChartView, self).updateTrackingAreas()
            for area in list(self.trackingAreas()):
                self.removeTrackingArea_(area)
            # 1: 出入り, 2: 移動, 0x80: 常に有効, 0x200: 表示域に追従
            opts = 0x01 | 0x02 | 0x80 | 0x200
            area = ak.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
                self.bounds(), opts, self, None)
            self.addTrackingArea_(area)

        def mouseMoved_(self, event):
            p = self.convertPoint_fromView_(event.locationInWindow(), None)
            self.cross_x = p.x
            self.cross_y = p.y
            self.setNeedsDisplay_(True)

        def mouseExited_(self, event):
            self.cross_x = None
            self.cross_y = None
            self.setNeedsDisplay_(True)

        def keyDown_(self, event):
            # Esc と Cmd+W で閉じる
            if event.keyCode() == 53 or (
                    event.modifierFlags() & (1 << 20) and event.charactersIgnoringModifiers() == "w"):
                self.window().close()
            else:
                objc_super(ChartView, self).keyDown_(event)

    class AppDelegate(ak.NSObject):
        def applicationShouldTerminateAfterLastWindowClosed_(self, app):
            return True

    return ChartView, AppDelegate


_keep = []  # PyObjC のオブジェクトが途中で解放されないように保持


def main():
    ak = _ak()
    data = json.loads(sys.stdin.read())
    ChartView, AppDelegate = _make_classes()

    app = ak.NSApplication.sharedApplication()
    app.setActivationPolicy_(ak.NSApplicationActivationPolicyAccessory)  # Dockに出さない
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)

    style = 1 | 2 | 4  # タイトルバー・閉じるボタン・最小化（サイズ変更は不可）
    win = ak.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ak.NSMakeRect(0, 0, W, H), style, ak.NSBackingStoreBuffered, False)
    win.setTitle_("FX Signal - {} {}".format(data["symbol"], data["period"]))
    win.setReleasedWhenClosed_(False)
    view = ChartView.alloc().initWithFrame_data_(ak.NSMakeRect(0, 0, W, H), data)
    win.setContentView_(view)
    win.setAcceptsMouseMovedEvents_(True)
    win.makeFirstResponder_(view)
    win.center()
    win.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    _keep.extend([delegate, win, view])
    app.run()


if __name__ == "__main__":
    main()
