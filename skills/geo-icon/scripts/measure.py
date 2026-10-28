#!/usr/bin/env python3
"""measure: read an existing icon image and report its parts in artboard units.

    python3 measure.py icon.png [--invert] [--threshold N] [--json out.json]

Used when regularising an existing icon: the numbers tell you what the icon
was trying to be (equal widths? a 1:2:3 height system? a centred group?) so
you can write a clean construction spec in geoicon.py. Nothing is traced into
the output; the spec is still written by hand.

The ink (dark by default) is fitted into the 100 x 100 artboard with its longest
side = 100 (so a full app-icon tile maps onto the whole artboard). Every ink
part is listed with its holes (light shapes cut into it, e.g. white bars on a
dark tile), each with bbox, size, centre and a shape guess. Rows of similar
parts get their spacing and size spread printed, which is where irregularities
show up.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np
from PIL import Image
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import geoicon as G  # noqa: E402


def otsu(gray):
    hist = np.bincount(gray.ravel(), minlength=256).astype(float)
    total, sum_all = gray.size, np.dot(np.arange(256), hist)
    w0 = s0 = 0.0
    best, thr = -1.0, 128
    for t in range(256):
        w0 += hist[t]
        if w0 in (0, total):
            continue
        s0 += t * hist[t]
        m0, m1 = s0 / w0, (sum_all - s0) / (total - w0)
        var = w0 * (total - w0) * (m0 - m1) ** 2
        if var > best:
            best, thr = var, t
    return thr


def ink_geometry(path, threshold=None, invert=False, work=800):
    im = Image.open(path).convert("RGBA")
    im = Image.alpha_composite(Image.new("RGBA", im.size, (255, 255, 255, 255)), im).convert("L")
    s = work / max(im.size)
    if s > 1:                         # small screenshots: upsample so edges are smoother
        im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    g = np.asarray(im)
    t = otsu(g) if threshold is None else threshold
    mask = g > t if invert else g < t
    rects = []
    for y in range(mask.shape[0]):
        d = np.diff(np.concatenate(([0], mask[y].astype(np.int8), [0])))
        rects += [box(x0, y, x1, y + 1) for x0, x1 in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1))]
    geom = unary_union(rects).simplify(0.5)
    return G.clean(geom), t, max(s, 1.0)


def describe(poly):
    x0, y0, x1, y1 = poly.bounds
    w, h = x1 - x0, y1 - y0
    fill = poly.area / max(w * h, 1e-9)
    if abs(w - h) / max(w, h) < 0.08 and 0.74 < fill < 0.83:
        kind = "circle"
    elif fill > 0.97:
        kind = "rectangle"
    elif fill > 0.84:
        r = min(w, h) / 2
        cap = (w * h - poly.area) / ((4 - math.pi) * r * r) if r > 0 else 0
        kind = "capsule (round ends)" if cap > 0.8 else f"rounded rect, corner r ~ {math.sqrt(max(w * h - poly.area, 0) / (4 - math.pi)):.1f}"
    else:
        kind = "free shape"
    return {"bbox": [round(v, 2) for v in (x0, y0, x1, y1)], "w": round(w, 2), "h": round(h, 2),
            "centre": [round((x0 + x1) / 2, 2), round((y0 + y1) / 2, 2)], "fill": round(fill, 3), "kind": kind}


def rows(items):
    """Print spacing / size spread for parts that sit side by side (same kind, similar width)."""
    out = []
    if len(items) < 3:
        return out
    xs = sorted(items, key=lambda d: d["centre"][0])
    cx = [d["centre"][0] for d in xs]
    gaps = np.diff(cx)
    ws = [d["w"] for d in xs]
    hs = [d["h"] for d in xs]
    cy = [d["centre"][1] for d in xs]
    out.append(f"  left to right: centre x {', '.join(f'{v:.1f}' for v in cx)}")
    out.append(f"  centre spacing {', '.join(f'{v:.2f}' for v in gaps)}  (spread {gaps.max() - gaps.min():.2f})")
    out.append(f"  widths  {', '.join(f'{v:.2f}' for v in ws)}  (spread {max(ws) - min(ws):.2f})")
    out.append(f"  heights {', '.join(f'{v:.2f}' for v in hs)}  "
               f"(ratios to the smallest: {', '.join(f'{v / min(hs):.2f}' for v in hs)})")
    out.append(f"  centre y {', '.join(f'{v:.1f}' for v in cy)}  (spread {max(cy) - min(cy):.2f})")
    gx0 = min(d["bbox"][0] for d in xs)
    gx1 = max(d["bbox"][2] for d in xs)
    out.append(f"  group spans x {gx0:.1f}..{gx1:.1f}: width {gx1 - gx0:.1f}, centre {(gx0 + gx1) / 2:.1f}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    ap.add_argument("--invert", action="store_true", help="the ink is light on a dark background")
    ap.add_argument("--threshold", type=int)
    ap.add_argument("--json")
    args = ap.parse_args()

    geom, t, _ = ink_geometry(args.image, args.threshold, args.invert)
    if geom.is_empty:
        sys.exit("measure: no ink found (try --invert or --threshold)")
    x0, y0, x1, y1 = geom.bounds
    s = 100.0 / max(x1 - x0, y1 - y0)
    from shapely import affinity
    geom = affinity.affine_transform(geom, (s, 0, 0, s, 50 - s * (x0 + x1) / 2, 50 - s * (y0 + y1) / 2))

    parts = sorted(G.polys(geom), key=lambda p: -p.area)
    report = {"threshold": t, "parts": []}
    lines = [f"ink threshold {t}; ink fitted into the 100 x 100 artboard (longest side = 100)"]
    for i, p in enumerate(parts, 1):
        d = describe(Polygon(p.exterior))
        holes = [describe(Polygon(h)) for h in p.interiors if Polygon(h).area > 0.5]
        d["holes"] = holes
        report["parts"].append(d)
        lines.append(f"part {i}: {d['kind']}  bbox {d['bbox']}  w {d['w']}  h {d['h']}  centre {d['centre']}")
        for j, hd in enumerate(sorted(holes, key=lambda h: h["centre"][0]), 1):
            lines.append(f"  hole {j}: {hd['kind']}  bbox {hd['bbox']}  w {hd['w']}  h {hd['h']}  centre {hd['centre']}")
        lines += rows(holes)
    if len(parts) >= 3:
        lines.append("parts side by side:")
        lines += rows([describe(Polygon(p.exterior)) for p in parts])
    print("\n".join(lines))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=1)


if __name__ == "__main__":
    main()
