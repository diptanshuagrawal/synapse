#!/usr/bin/env python3
"""Second round of warm/organic Synapse marks (same amber->coral tile).

  spark  — a 4-point insight spark + a smaller twinkle
  orbit  — a core hub with a signal orbiting it
  bloom  — five petals opening from a center (convergence, growth)
  wave   — a signal/pulse waveform through a node

Glyphs authored in a 180-box centred in the 200 tile (offset 10).

    .venv/bin/python bin/make_synapse_warm2.py           # all -> assets/warm2-<name>.png
    .venv/bin/python bin/make_synapse_warm2.py spark
"""
import os, sys, math
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets")
BASE, SS, FINAL = 200, 4096, 1024
SC = SS / BASE
OFF = 10
TOP, BOT = (255, 177, 74), (255, 106, 91)
CREAM, WHITE = (255, 243, 230), (255, 255, 255)


def P(x, y):
    return (int(round((x + OFF) * SC)), int(round((y + OFF) * SC)))


def _r(v):
    return int(round(v * SC))


def _dot(d, x, y, r, fill):
    cx, cy = P(x, y)
    rr = _r(r)
    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=fill)


def _star_pts(cx, cy, R, r):
    dg = r * 0.7071
    return [P(cx, cy - R), P(cx + dg, cy - dg), P(cx + R, cy), P(cx + dg, cy + dg),
            P(cx, cy + R), P(cx - dg, cy + dg), P(cx - R, cy), P(cx - dg, cy - dg)]


def _ell(cx, cy, a, b, phi, n=90):
    f = math.radians(phi)
    out = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x, y = a * math.cos(t), b * math.sin(t)
        rx = x * math.cos(f) - y * math.sin(f)
        ry = x * math.sin(f) + y * math.cos(f)
        out.append(P(cx + rx, cy + ry))
    return out


def draw_spark(d):
    d.polygon(_star_pts(90, 90, 62, 17), fill=CREAM)
    _dot(d, 90, 90, 11, WHITE)
    d.polygon(_star_pts(150, 48, 20, 6), fill=WHITE)


def draw_orbit(d):
    ring = _ell(90, 90, 66, 30, 28)
    d.line(ring + [ring[0]], fill=CREAM, width=_r(10), joint="curve")
    _dot(d, 90, 90, 18, CREAM)
    f = math.radians(28)
    t = math.radians(52)
    x, y = 66 * math.cos(t), 30 * math.sin(t)
    ox = 90 + x * math.cos(f) - y * math.sin(f)
    oy = 90 + x * math.sin(f) + y * math.cos(f)
    _dot(d, ox, oy, 10, WHITE)


def draw_bloom(d):
    for a in (-90, -18, 54, 126, 198):
        f = math.radians(a)
        cx, cy = 90 + 42 * math.cos(f), 90 + 42 * math.sin(f)
        pts = []
        for i in range(48):
            t = 2 * math.pi * i / 48
            x, y = 32 * math.cos(t), 14 * math.sin(t)
            rx = x * math.cos(f) - y * math.sin(f)
            ry = x * math.sin(f) + y * math.cos(f)
            pts.append(P(cx + rx, cy + ry))
        d.polygon(pts, fill=CREAM)
    _dot(d, 90, 90, 13, WHITE)


def draw_wave(d):
    pts = []
    for i in range(121):
        x = 30 + i
        y = 90 - 26 * math.sin(2 * math.pi * (x - 30) / 80)
        pts.append(P(x, y))
    d.line(pts, fill=CREAM, width=_r(12), joint="curve")
    for e in (pts[0], pts[-1]):
        rr = _r(6)
        d.ellipse([e[0] - rr, e[1] - rr, e[0] + rr, e[1] + rr], fill=CREAM)
    _dot(d, 90, 90, 12, WHITE)


CONCEPTS = {"spark": draw_spark, "orbit": draw_orbit, "bloom": draw_bloom, "wave": draw_wave}


def tile():
    grad = Image.new("RGB", (1, SS))
    gp = grad.load()
    for y in range(SS):
        t = y / (SS - 1)
        gp[0, y] = tuple(int(TOP[i] + (BOT[i] - TOP[i]) * t) for i in range(3))
    grad = grad.resize((SS, SS))
    mask = Image.new("L", (SS, SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, SS - 1, SS - 1], radius=_r(54), fill=255)
    img = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    return img


def render(which):
    os.makedirs(OUT, exist_ok=True)
    for key in which:
        img = tile()
        CONCEPTS[key](ImageDraw.Draw(img))
        out = os.path.join(OUT, f"warm2-{key}.png")
        img.resize((FINAL, FINAL), Image.LANCZOS).save(out)
        print(out)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a in CONCEPTS] or list(CONCEPTS)
    render(args)
