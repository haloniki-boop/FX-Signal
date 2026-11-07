#!/usr/bin/env python3
"""FXシグナル用のアイコン(1024px PNG)を標準ライブラリだけで生成する。"""
import math
import struct
import sys
import zlib

N = 1024


def png_write(path, w, h, pixels):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw.extend(pixels[y * w * 4:(y + 1) * w * 4])

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        fh.write(chunk(b"IEND", b""))


def rounded_rect_alpha(x, y, cx, cy, half, r):
    """角丸正方形の内側なら1.0、外側なら0.0（簡易アンチエイリアス）。"""
    dx = max(abs(x - cx) - (half - r), 0)
    dy = max(abs(y - cy) - (half - r), 0)
    d = math.hypot(dx, dy) - r
    return max(0.0, min(1.0, 0.5 - d))


def in_rect(x, y, x0, y0, x1, y1):
    return x0 <= x <= x1 and y0 <= y <= y1


def build():
    # 背景: 紺〜深緑のグラデーション / 角丸
    pix = bytearray(N * N * 4)
    # ローソク足(陽線=緑, 陰線=赤)と上昇ライン
    candles = [
        # x中心, 始値y, 終値y, 高値y, 安値y (1024基準)
        (250, 640, 560, 520, 680, True),
        (360, 560, 600, 540, 640, False),
        (470, 600, 470, 450, 620, True),
        (580, 470, 520, 440, 560, False),
        (690, 520, 380, 360, 540, True),
        (800, 380, 300, 280, 400, True),
    ]
    for y in range(N):
        for x in range(N):
            a = rounded_rect_alpha(x + 0.5, y + 0.5, N / 2, N / 2, N / 2 - 40, 190)
            t = y / N
            r = int(14 + 10 * t)
            g = int(32 + 40 * (1 - t))
            b = int(64 + 50 * (1 - t))
            rr, gg, bb = r, g, b
            # ローソク足
            for cxp, o, c, hi, lo, bull in candles:
                if abs(x - cxp) <= 26 and hi <= y <= lo:
                    rr, gg, bb = (48, 209, 88) if bull else (255, 69, 58)
                if abs(x - cxp) <= 4 and hi <= y <= lo:
                    rr, gg, bb = (235, 235, 235)
                if abs(x - cxp) <= 26 and min(o, c) <= y <= max(o, c):
                    rr, gg, bb = (48, 209, 88) if bull else (255, 69, 58)
            # 上昇トレンドライン
            px = (x - 200) / 650.0
            if 0 <= px <= 1:
                ly = 740 - px * 460
                if abs(y - ly) <= 5 and y > 250:
                    rr, gg, bb = (255, 214, 10)
            # 下段のドル/円マーク風の帯
            if in_rect(x, y, 210, 790, 814, 808):
                rr, gg, bb = (120, 160, 210)
            i = (y * N + x) * 4
            pix[i] = rr
            pix[i + 1] = gg
            pix[i + 2] = bb
            pix[i + 3] = int(255 * a)
    return bytes(pix)


if __name__ == "__main__":
    out = sys.argv[1]
    png_write(out, N, N, build())
    print("wrote", out)
