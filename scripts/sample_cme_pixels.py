"""Sample exact palette colours out of the captured CME screenshots.

Used because cmegroup.com started answering net::ERR_HTTP2_PROTOCOL_ERROR after
repeated navigations, and the values we still need (tab strip, yellow NOW column,
yellow/cyan diagonal of the conditional matrix) are fully determined by pixels.

Read-only. Run: python scripts/sample_cme_pixels.py
"""

import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(BACKEND, "output", "rate_page_design")

from PIL import Image  # noqa: E402


def rgb(c):
    return f"rgb({c[0]}, {c[1]}, {c[2]})"


def hexs(c):
    return f"#{c[0]:02X}{c[1]:02X}{c[2]:02X}"


def dominant(img, pred, label, limit=8):
    counts = Counter()
    for y in range(img.height):
        for x in range(img.width):
            c = img.getpixel((x, y))[:3]
            if pred(c):
                counts[c] += 1
    print(f"\n--- {label}: {sum(counts.values())} matching px, {len(counts)} distinct ---")
    for c, n in counts.most_common(limit):
        print(f"    {hexs(c):9} {rgb(c):20} x{n}")
    return counts


def in_box(img, box):
    x0, y0, x1, y1 = box
    return img.crop((x0, y0, x1, y1))


YELLOW = lambda c: c[0] > 235 and c[1] > 230 and c[2] < 215 and c[2] < c[1] - 25
CYAN = lambda c: c[2] > 225 and c[1] > 225 and c[0] < 215
GREY = lambda c: abs(c[0] - c[1]) < 8 and abs(c[1] - c[2]) < 8 and 200 < c[0] < 246
BLUE = lambda c: c[2] > 120 and c[2] > c[0] + 40 and c[1] > c[0] + 10


def main() -> int:
    cur_p = os.path.join(OUT, "cme_tool.png")
    prob_p = os.path.join(OUT, "cme_probabilities.png")

    if os.path.exists(cur_p):
        img = in_box(Image.open(cur_p).convert("RGB"), (0, 0, 1566, 1077))
        print(f"=== cme_tool.png {img.size} ===")
        # Tab strip lives at the very top of the tool (y ~ 60-95 in the capture).
        tabs = in_box(img, (120, 58, 760, 98))
        dominant(tabs, GREY, "tab strip: grey fills (active tab / borders)")
        dominant(tabs, BLUE, "tab strip: blue text (inactive tab links)")
        dominant(tabs, lambda c: c[0] < 90 and c[1] < 90 and c[2] < 90,
                 "tab strip: near-black text (active tab label)")
        # NOW column highlight sits in the lower table (y ~ 900-1000, x ~ 690-760).
        now = in_box(img, (680, 895, 770, 1010))
        dominant(now, YELLOW, "NOW column cells: yellow highlight")
        dominant(in_box(img, (120, 85, 1560, 190)), lambda c: 244 <= c[0] <= 252 and 244 <= c[1] <= 252 and 244 <= c[2] <= 252,
                 "card header band: light grey")
        dominant(in_box(img, (120, 85, 1560, 190)), GREY, "card header band: all greys")
        dominant(img, lambda c: abs(c[0] - 41) < 26 and abs(c[1] - 128) < 26 and abs(c[2] - 185) < 26,
                 "chart bars: blue near measured rgb(41,128,185)")
    else:
        print("missing", cur_p)

    if os.path.exists(prob_p):
        img = Image.open(prob_p).convert("RGB")
        print(f"\n=== cme_probabilities.png {img.size} ===")
        dominant(img, YELLOW, "conditional matrix: yellow cells")
        dominant(img, CYAN, "conditional matrix: cyan cells")
        # Table chrome: header row of FED FUND FUTURES (y ~ 80-95) and grid lines.
        dominant(in_box(img, (450, 70, 1090, 165)), lambda c: 240 <= c[0] <= 252 and 240 <= c[1] <= 252 and 240 <= c[2] <= 252,
                 "futures table: header/band greys")
        dominant(in_box(img, (0, 100, 130, 300)), GREY, "left nav: active item grey")
        dominant(img, BLUE, "left nav / links: blue")
    else:
        print("missing", prob_p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
