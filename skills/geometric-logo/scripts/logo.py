#!/usr/bin/env python3
"""logo: build a flat icon from a geometric-construction spec.

    python3 logo.py SPEC.json [-o OUTDIR] [--no-png] [--scale 2]

The spec describes the mark as primitives (circles, rings, vesicas, tangent
arcs, hulls...) combined with boolean operations. The outline is computed
from that geometry, and the same primitives are drawn back as the
construction overlay, so the guides are always the true source of the shape.

Writes to OUTDIR (default: next to the spec, named after it):
    icon.svg          final mark, one evenodd path, single colour
    poster.svg        the mark centred on a paper-coloured 4:5 canvas
    construction.svg  4:5 card: grid paper, construction, concept, thumbnail
    sizes.svg         the mark at 128..16 px, positive and reversed
    *.png             rasterised copies when a rasteriser is available
    report.txt        lint: radii in use, thin parts, clogged gaps, balance

Needs shapely (python3 -m pip install shapely). PNG output uses the first of
rsvg-convert, cairosvg, Chrome/Chromium/Edge (headless) or macOS qlmanage.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

import numpy as np

try:
    from shapely import affinity
    from shapely.geometry import LineString, MultiPolygon, Point, Polygon
    from shapely.ops import unary_union
except ImportError:  # pragma: no cover
    sys.exit("logo needs shapely:  python3 -m pip install shapely")

PHI = (1 + 5 ** 0.5) / 2
QS = 64            # segments per quarter circle when approximating curves
ARC_STEP = 1.5     # degrees between sampled points on arcs we sample ourselves
EMPTY = Polygon()

PAPER = "#F3F0E7"
INK = "#45463A"


class SpecError(Exception):
    pass


# ---------------------------------------------------------------- geometry

def polar(c, r, deg):
    a = math.radians(deg)
    return (c[0] + r * math.cos(a), c[1] + r * math.sin(a))


def arc_pts(c, r, a0, a1):
    n = max(2, int(abs(a1 - a0) / ARC_STEP) + 1)
    return [polar(c, r, a0 + (a1 - a0) * i / (n - 1)) for i in range(n)]


def bulge_arc(p, q, s):
    """Arc from p to q with sagitta s (positive bulges to the right of the walk p -> q, on screen).

    Returns (points from p to q, centre, radius); a straight segment has centre None.
    """
    h = math.dist(p, q) / 2
    if abs(s) < 1e-9 or h < 1e-9:
        return [p, q], None, None
    ux, uy = (q[0] - p[0]) / (2 * h), (q[1] - p[1]) / (2 * h)
    nx, ny = -uy, ux                           # right-hand side of the walk, on screen
    m = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
    r = (h * h + s * s) / (2 * abs(s))
    k = s - math.copysign(r, s)
    c = (m[0] + k * nx, m[1] + k * ny)
    apex = (m[0] + s * nx, m[1] + s * ny)

    def ang(pt):
        return math.degrees(math.atan2(pt[1] - c[1], pt[0] - c[0]))

    a0 = ang(p)
    sweep = (ang(q) - a0) % 360
    if (ang(apex) - a0) % 360 > sweep:
        sweep -= 360
    pts = arc_pts(c, r, a0, a0 + sweep)
    pts[0], pts[-1] = p, q                     # share exact end points: no slivers at the tips
    return pts, c, r


def disc(c, r, qs=QS):
    return Point(c).buffer(r, quad_segs=qs) if r > 1e-6 else Point(c)


def polys(geom):
    """Polygon parts of any geometry (drops stray lines/points)."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if hasattr(geom, "geoms"):
        out = []
        for g in geom.geoms:
            out += polys(g)
        return out
    return []


def clean(geom):
    parts = polys(geom)
    if not parts:
        return EMPTY
    g = MultiPolygon(parts) if len(parts) > 1 else parts[0]
    return g if g.is_valid else g.buffer(0)


def soften(geom, r):
    """Round every corner, convex and concave, with radius r."""
    if r <= 0:
        return geom
    return clean(geom.buffer(-r, quad_segs=QS).buffer(2 * r, quad_segs=QS).buffer(-r, quad_segs=QS))


def stroke_geom(pts, widths, cap="round", join="round", closed=False):
    cap = {"butt": "flat", "flat": "flat", "square": "square"}.get(cap, "round")
    join = {"miter": "mitre", "mitre": "mitre", "bevel": "bevel"}.get(join, "round")
    if closed:
        pts = list(pts) + [pts[0]]
    if len(widths) == 1:
        return clean(LineString(pts).buffer(widths[0] / 2, cap_style=cap, join_style=join, quad_segs=QS))
    # variable width: union of hulls of consecutive discs along the path
    seg = [math.dist(a, b) for a, b in zip(pts, pts[1:])]
    total = sum(seg) or 1.0
    samples, acc = [], 0.0
    for (a, b), L in zip(zip(pts, pts[1:]), seg):
        n = max(1, int(L / 0.4))
        for i in range(n):
            t = i / n
            samples.append(((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), (acc + L * t) / total))
        acc += L
    samples.append((pts[-1], 1.0))

    def width_at(u):
        k = len(widths) - 1
        i = min(int(u * k), k - 1)
        f = u * k - i
        return widths[i] + (widths[i + 1] - widths[i]) * f

    discs = [disc(p, width_at(u) / 2, 16) for p, u in samples]
    hulls = [unary_union([a, b]).convex_hull for a, b in zip(discs, discs[1:])]
    return clean(unary_union(hulls))


def svg_arc(p0, rx, ry, rot, large, sweep, p1):
    """SVG endpoint arc -> sampled points (excluding p0)."""
    if rx == 0 or ry == 0 or p0 == p1:
        return [p1]
    cphi, sphi = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    dx2, dy2 = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1p, y1p = cphi * dx2 + sphi * dy2, -sphi * dx2 + cphi * dy2
    rx, ry = abs(rx), abs(ry)
    lam = x1p ** 2 / rx ** 2 + y1p ** 2 / ry ** 2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / den)) * (-1 if large == sweep else 1)
    cxp, cyp = coef * rx * y1p / ry, -coef * ry * x1p / rx
    cx = cphi * cxp - sphi * cyp + (p0[0] + p1[0]) / 2
    cy = sphi * cxp + cphi * cyp + (p0[1] + p1[1]) / 2

    def ang(ux, uy, vx, vy):
        return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)

    th1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dth = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dth > 0:
        dth -= 2 * math.pi
    elif sweep and dth < 0:
        dth += 2 * math.pi
    n = max(4, int(abs(math.degrees(dth)) / ARC_STEP))
    out = []
    for k in range(1, n + 1):
        t = th1 + dth * k / n
        out.append((cx + rx * math.cos(t) * cphi - ry * math.sin(t) * sphi,
                    cy + rx * math.cos(t) * sphi + ry * math.sin(t) * cphi))
    return out


_PATH_TOK = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")


def parse_path(d):
    """Flatten an SVG path string into even-odd filled geometry."""
    toks = _PATH_TOK.findall(d)
    i, cmd = 0, None
    cur = start = (0.0, 0.0)
    ctrl = None
    rings, pts = [], []

    def nums(k):
        nonlocal i
        vals = [float(t) for t in toks[i:i + k]]
        if len(vals) < k or any(t.isalpha() for t in toks[i:i + k]):
            raise SpecError(f"path: not enough numbers after '{cmd}'")
        i += k
        return vals

    def flush():
        nonlocal pts
        if len(pts) >= 3:
            rings.append(pts)
        pts = []

    while i < len(toks):
        if toks[i].isalpha():
            cmd = toks[i]
            i += 1
            if cmd in "Zz":
                cur = start
                flush()
                ctrl = None
                continue
        elif cmd is None:
            raise SpecError("path must start with a command")
        rel = cmd.islower()
        C = cmd.upper()
        ox, oy = cur if rel else (0.0, 0.0)
        if C == "M":
            x, y = nums(2)
            flush()
            cur = start = (ox + x, oy + y)
            pts = [cur]
            cmd = "l" if rel else "L"   # implicit lineto after moveto
            ctrl = None
        elif C == "L":
            x, y = nums(2)
            cur = (ox + x, oy + y)
            pts.append(cur)
            ctrl = None
        elif C == "H":
            (x,) = nums(1)
            cur = (ox + x, cur[1])
            pts.append(cur)
            ctrl = None
        elif C == "V":
            (y,) = nums(1)
            cur = (cur[0], oy + y)
            pts.append(cur)
            ctrl = None
        elif C in "CS":
            if C == "C":
                x1, y1, x2, y2, x, y = nums(6)
                c1 = (ox + x1, oy + y1)
            else:
                x2, y2, x, y = nums(4)
                c1 = (2 * cur[0] - ctrl[0], 2 * cur[1] - ctrl[1]) if ctrl else cur
            c2, end = (ox + x2, oy + y2), (ox + x, oy + y)
            for k in range(1, 25):
                t = k / 24
                mt = 1 - t
                pts.append((mt ** 3 * cur[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * end[0],
                            mt ** 3 * cur[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * end[1]))
            cur, ctrl = end, c2
        elif C in "QT":
            if C == "Q":
                x1, y1, x, y = nums(4)
                c1 = (ox + x1, oy + y1)
            else:
                x, y = nums(2)
                c1 = (2 * cur[0] - ctrl[0], 2 * cur[1] - ctrl[1]) if ctrl else cur
            end = (ox + x, oy + y)
            for k in range(1, 25):
                t = k / 24
                mt = 1 - t
                pts.append((mt * mt * cur[0] + 2 * mt * t * c1[0] + t * t * end[0],
                            mt * mt * cur[1] + 2 * mt * t * c1[1] + t * t * end[1]))
            cur, ctrl = end, c1
        elif C == "A":
            rx, ry, rot, large, sweep, x, y = nums(7)
            end = (ox + x, oy + y)
            pts += svg_arc(cur, rx, ry, rot, bool(large), bool(sweep), end)
            cur = end
            ctrl = None
    flush()
    geom = EMPTY
    for r in rings:
        geom = geom.symmetric_difference(Polygon(r).buffer(0))
    return clean(geom)


def tangent_lines(c1, r1, c2, r2, which="outer"):
    dx, dy = c2[0] - c1[0], c2[1] - c1[1]
    d = math.hypot(dx, dy)
    th = math.degrees(math.atan2(dy, dx))
    out = []
    if which in ("outer", "both") and d > abs(r1 - r2):
        a = math.degrees(math.acos((r1 - r2) / d))
        for s in (1, -1):
            out.append((polar(c1, r1, th + s * a), polar(c2, r2, th + s * a)))
    if which in ("inner", "both") and d > r1 + r2:
        a = math.degrees(math.acos((r1 + r2) / d))
        for s in (1, -1):
            out.append((polar(c1, r1, th + s * a), polar(c2, r2, th + s * a + 180)))
    return out


# ---------------------------------------------------------------- guides
# A guide is a tuple whose first item names its kind:
#   ("circle", c, r) ("ellipse", c, rx, ry, rot) ("line", p, q)  infinite
#   ("seg", p, q) ("axis", p, q) dashed infinite ("cross", p) ("dot", p)
#   ("poly", [pts]) open polyline  ("outline", geom) dashed outline

def tf_point(M, p):
    a, b, d, e, xo, yo = M
    return (a * p[0] + b * p[1] + xo, d * p[0] + e * p[1] + yo)


def tf_guides(guides, M):
    a, b, d, e, _, _ = M
    k = math.sqrt(abs(a * e - b * d))
    out = []
    for g in guides:
        t = g[0]
        if t == "circle":
            out.append(("circle", tf_point(M, g[1]), g[2] * k, *g[3:]))
        elif t == "ellipse":
            ang = math.radians(g[4])
            vx, vy = a * math.cos(ang) + b * math.sin(ang), d * math.cos(ang) + e * math.sin(ang)
            out.append(("ellipse", tf_point(M, g[1]), g[2] * k, g[3] * k, math.degrees(math.atan2(vy, vx))))
        elif t in ("line", "seg", "axis"):
            out.append((t, tf_point(M, g[1]), tf_point(M, g[2])))
        elif t in ("cross", "dot"):
            out.append((t, tf_point(M, g[1])))
        elif t == "poly":
            out.append(("poly", [tf_point(M, p) for p in g[1]]))
        elif t == "outline":
            out.append(("outline", affinity.affine_transform(g[1], M)))
    return out


def guide_key(g):
    def r(v):
        return round(v, 2) if isinstance(v, float) else v

    parts = []
    for v in g[1:]:
        if isinstance(v, tuple):
            parts.append(tuple(r(x) for x in v))
        elif isinstance(v, (int, float)):
            parts.append(r(float(v)))
        elif isinstance(v, list):
            parts.append(tuple(tuple(r(x) for x in p) for p in v))
        else:
            parts.append(id(v))
    if g[0] in ("line", "axis"):
        # same infinite line regardless of which two points define it
        (x1, y1), (x2, y2) = g[1], g[2]
        ang = round(math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180, 1)
        dist = round(abs((x2 - x1) * y1 - (y2 - y1) * x1) / (math.hypot(x2 - x1, y2 - y1) or 1), 2)
        return (g[0], ang, dist)
    return (g[0], *parts)


def dedup(guides):
    seen, out = set(), []
    for g in guides:
        k = guide_key(g)
        if k not in seen:
            seen.add(k)
            out.append(g)
    return out


def key_guides(guides, ink, tol=0.08):
    """Keep only the construction that shows up in the final mark.

    A circle stays when part of it lies on the outline (or, for the centre line
    of a stroke, when the stroke runs along it); a line stays when a good part
    of its tangent segment lies on the outline. Axes always stay.
    """
    import shapely
    edge = ink.boundary
    shapely.prepare(edge)
    shapely.prepare(ink)
    keep, centres = [], []
    for g in guides:
        t = g[0]
        if t == "circle":
            a = np.linspace(0, 2 * np.pi, 180, endpoint=False)
            x, y = g[1][0] + g[2] * np.cos(a), g[1][1] + g[2] * np.sin(a)
            if g[3:] == ("spine",):
                ok = shapely.contains_xy(ink, x, y).mean() >= 0.15
            else:
                ok = (shapely.distance(edge, shapely.points(x, y)) < tol).sum() >= 5   # >= 10 degrees on the outline
            if ok:
                keep.append(g)
                centres.append(g[1])
        elif t in ("line", "seg"):
            u = np.linspace(0, 1, 24)
            x = g[1][0] + (g[2][0] - g[1][0]) * u
            y = g[1][1] + (g[2][1] - g[1][1]) * u
            if (shapely.distance(edge, shapely.points(x, y)) < tol).mean() >= 0.3:
                keep.append(g)
        elif t == "axis":
            keep.append(g)
        elif t == "ellipse":
            a = np.linspace(0, 2 * np.pi, 180, endpoint=False)
            ca, sa = math.cos(math.radians(g[4])), math.sin(math.radians(g[4]))
            ex, ey = g[2] * np.cos(a), g[3] * np.sin(a)
            x, y = g[1][0] + ex * ca - ey * sa, g[1][1] + ex * sa + ey * ca
            if (shapely.distance(edge, shapely.points(x, y)) < tol).sum() >= 3:
                keep.append(g)
                centres.append(g[1])
    for g in guides:
        if g[0] == "cross" and any(math.dist(g[1], c) < 1e-3 for c in centres):
            keep.append(g)
        elif g[0] == "dot" and edge.distance(Point(g[1])) < tol:
            keep.append(g)
    return keep


def exact_path(ink, circles, tol=0.03, nd=3):
    """Outline as SVG path data using true arcs wherever it runs along a construction circle.

    Returns (d, stats) where stats gives the outline length written as arcs,
    straight lines and short polyline pieces (fillets, tapers: no exact circle).
    """
    circles = list({(round(c[0], 4), round(c[1], 4), round(r, 4)): (c, r) for c, r in circles if r > 1e-6}.values())
    C = np.array([c for c, _ in circles]) if circles else np.zeros((0, 2))
    R = np.array([r for _, r in circles]) if circles else np.zeros(0)
    parts, stats = [], {"arc": 0.0, "line": 0.0, "poly": 0.0}

    def f(v):
        return fmt(float(v), nd)

    def emit_ring(V):
        n = len(V)
        if n < 3:
            return
        def near(P):
            if not len(C):
                return np.zeros((len(P), 0), bool)
            return np.abs(np.hypot(P[:, None, 0] - C[None, :, 0], P[:, None, 1] - C[None, :, 1]) - R[None, :]) < tol

        # a vertex on the circle is not enough: a straight chord also ends on it, so the
        # midpoint of each edge must be on the circle too (an arc's short edges sag far less than tol)
        on = near(V)
        edge = near((V + np.roll(V, -1, axis=0)) / 2)          # edge k joins vertex k and k+1
        # start at a vertex where no run passes through, so runs do not wrap around the seam
        start = 0
        for k in range(n):
            if not (on[k - 1] & edge[k - 1] & on[k] & edge[k] & on[(k + 1) % n]).any():
                start = k
                break
        V = np.roll(V, -start, axis=0)
        on = np.roll(on, -start, axis=0)
        edge = np.roll(edge, -start, axis=0)
        V = np.vstack([V, V[:1]])
        on = np.vstack([on, on[:1]])
        cmds, pending = [f"M{f(V[0][0])} {f(V[0][1])}"], [V[0]]

        def flush_lines():
            nonlocal pending
            if len(pending) > 1:
                ls = LineString(pending).simplify(0.01)
                pts = list(ls.coords)[1:]
                for a, b in zip(list(ls.coords), pts):
                    L = math.dist(a, b)
                    stats["line" if L > 1.0 else "poly"] += L
                cmds.extend(f"L{f(x)} {f(y)}" for x, y in pts)
            pending = [pending[-1]]

        i = 0
        while i < n:
            S = on[i] & edge[i] & on[i + 1]
            j = i + 1
            while j < n and (S & edge[j] & on[j + 1]).any():
                S = S & edge[j] & on[j + 1]
                j += 1
            if j - i >= 2 and S.any():
                k = int(np.flatnonzero(S)[0])
                c, r = C[k], R[k]
                ang = np.unwrap(np.arctan2(V[i:j + 1, 1] - c[1], V[i:j + 1, 0] - c[0]))
                total = math.degrees(ang[-1] - ang[0])

                def on_circle(q):
                    # put the point exactly on the circle: a chord even slightly longer than the
                    # diameter makes renderers enlarge the radius and bulge the arc
                    a_ = math.atan2(q[1] - c[1], q[0] - c[0])
                    return np.array([c[0] + r * math.cos(a_), c[1] + r * math.sin(a_)])

                pending[-1] = on_circle(V[i])
                flush_lines()
                # long arcs are ill-conditioned (near 180 degrees the centre is ambiguous): <=120 degree pieces
                pieces = max(1, math.ceil(abs(total) / 120))
                splits = sorted({i + round((j - i) * q / pieces) for q in range(pieces + 1)})
                for a, b in zip(splits, splits[1:]):
                    sw = math.degrees(ang[b - i] - ang[a - i])
                    e = on_circle(V[b])
                    cmds.append(f"A{f(r)} {f(r)} 0 {1 if abs(sw) > 180 else 0} {1 if sw > 0 else 0} "
                                f"{f(e[0])} {f(e[1])}")
                stats["arc"] += abs(math.radians(total)) * r
                pending = [on_circle(V[j])]
                i = j
            else:
                pending.append(V[i + 1])
                i += 1
        flush_lines()
        parts.append("".join(cmds) + "Z")

    for poly in polys(ink):
        for ring in [poly.exterior, *poly.interiors]:
            emit_ring(np.asarray(ring.coords[:-1], float))
    return "".join(parts), stats


# ---------------------------------------------------------------- spec evaluation

class Ref:
    """What a name resolves to inside expressions: .x .y (.r) plus bounds."""

    def __init__(self, x, y, r=None, bounds=None):
        self.x, self.y, self.r = x, y, r
        if bounds:
            self.left, self.top, self.right, self.bottom = bounds
            self.w, self.h = self.right - self.left, self.bottom - self.top

    def __iter__(self):
        return iter((self.x, self.y))


SAFE = {n: getattr(math, n) for n in (
    "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sqrt", "hypot", "pi",
    "floor", "ceil", "radians", "degrees")}
SAFE.update(
    phi=PHI, abs=abs, min=min, max=max, round=round,
    sind=lambda d: math.sin(math.radians(d)),
    cosd=lambda d: math.cos(math.radians(d)),
    tand=lambda d: math.tan(math.radians(d)),
    atan2d=lambda y, x: math.degrees(math.atan2(y, x)),
    dist=lambda p, q: math.dist(tuple(p), tuple(q)),
)


class Scope:
    def __init__(self, b):
        self.b = b

    def __getitem__(self, k):
        return self.b.lookup(k)


class Builder:
    def __init__(self, spec):
        self.spec = spec
        self.size = float(spec.get("size", 100))
        self.raw = {}
        for section in ("vars", "points", "shapes"):
            for k, v in (spec.get(section) or {}).items():
                if k in self.raw:
                    raise SpecError(f"name '{k}' is defined twice (names are shared by vars, points and shapes)")
                self.raw[k] = (section, v)
        self.values = {"size": self.size, "mid": self.size / 2}
        self.shapes = {}
        self.stack = []
        self.raw_paths = 0
        self.mirrored = False
        self.warnings = []

    # ---- names and values
    def lookup(self, name):
        if name in self.values:
            return self.values[name]
        if name not in self.raw:
            raise KeyError(name)
        if name in self.stack:
            raise SpecError("circular reference: " + " -> ".join(self.stack + [name]))
        self.stack.append(name)
        try:
            section, v = self.raw[name]
            if section == "vars":
                val = self.num(v)
            elif section == "points":
                val = Ref(*self.pt(v))
            else:
                geom, _, meta = self.named(name)
                b = geom.bounds if not geom.is_empty else (0, 0, 0, 0)
                if "c" in meta:
                    val = Ref(meta["c"][0], meta["c"][1], meta.get("r"), b)
                else:
                    val = Ref((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, None, b)
        finally:
            self.stack.pop()
        self.values[name] = val
        return val

    def num(self, v):
        if isinstance(v, bool):
            raise SpecError(f"expected a number, got {v!r}")
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                r = eval(v, {"__builtins__": {}, **SAFE}, Scope(self))  # noqa: S307 - local spec expressions
            except SpecError:
                raise
            except Exception as e:
                raise SpecError(f"cannot evaluate {v!r}: {e}") from None
            if isinstance(r, Ref) and r.r is not None:
                return float(r.r)
            if not isinstance(r, (int, float)):
                raise SpecError(f"{v!r} is not a number")
            return float(r)
        raise SpecError(f"expected a number or expression, got {v!r}")

    def pt(self, v):
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return (self.num(v[0]), self.num(v[1]))
        if isinstance(v, str):
            try:
                r = eval(v, {"__builtins__": {}, **SAFE}, Scope(self))  # noqa: S307
            except SpecError:
                raise
            except Exception as e:
                raise SpecError(f"cannot evaluate point {v!r}: {e}") from None
            if isinstance(r, Ref):
                return (r.x, r.y)
            if isinstance(r, (list, tuple)) and len(r) == 2:
                return (float(r[0]), float(r[1]))
            raise SpecError(f"{v!r} is not a point")
        if isinstance(v, dict):
            if "polar" in v:
                c, r, a = v["polar"]
                return polar(self.pt(c), self.num(r), self.num(a))
            if "mid" in v:
                p, q = (self.pt(x) for x in v["mid"])
                return ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
            if "lerp" in v:
                p, q, t = v["lerp"]
                p, q, t = self.pt(p), self.pt(q), self.num(t)
                return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)
            if "at" in v:
                p = self.pt(v["at"])
                return (p[0] + self.num(v.get("dx", 0)), p[1] + self.num(v.get("dy", 0)))
            if "on" in v:
                c, r = self.circle_of(v["on"])
                return polar(c, r, self.num(v.get("deg", 0)))
            if "meet" in v:
                (p1, p2), (p3, p4) = [[self.pt(p) for p in ln] for ln in v["meet"]]
                d = (p1[0] - p2[0]) * (p3[1] - p4[1]) - (p1[1] - p2[1]) * (p3[0] - p4[0])
                if abs(d) < 1e-9:
                    raise SpecError("meet: lines are parallel")
                a = p1[0] * p2[1] - p1[1] * p2[0]
                b = p3[0] * p4[1] - p3[1] * p4[0]
                return ((a * (p3[0] - p4[0]) - (p1[0] - p2[0]) * b) / d,
                        (a * (p3[1] - p4[1]) - (p1[1] - p2[1]) * b) / d)
        raise SpecError(f"cannot read point {v!r}")

    def circle_of(self, v):
        """A circle given as a shape name, [P, r] or {"c":P,"r":r}."""
        if isinstance(v, str):
            ref = self.lookup(v) if v in self.raw else None
            if not isinstance(ref, Ref) or ref.r is None:
                raise SpecError(f"'{v}' is not a circle-like shape (circle, ring, arc or sector)")
            return (ref.x, ref.y), ref.r
        if isinstance(v, dict):
            return self.pt(v["c"]), self.num(v["r"])
        return self.pt(v[0]), self.num(v[1])

    # ---- shapes
    def named(self, name):
        if name not in self.shapes:
            if name not in self.raw or self.raw[name][0] != "shapes":
                raise SpecError(f"unknown shape '{name}'")
            self.stack.append(name)
            try:
                self.shapes[name] = self.shape(self.raw[name][1])
            except SpecError as e:
                if not str(e).startswith("in shape"):
                    raise SpecError(f"in shape '{name}': {e}") from None
                raise
            finally:
                self.stack.pop()
        return self.shapes[name]

    def shape(self, node):
        if isinstance(node, str):
            return self.named(node)
        if not isinstance(node, dict):
            raise SpecError(f"a shape must be a name or an object, got {node!r}")
        kinds = [k for k in node if k in KINDS]
        if len(kinds) != 1:
            raise SpecError(f"a shape needs exactly one of: {', '.join(sorted(KINDS))}; got keys {list(node)}")
        k = kinds[0]
        geom, guides, meta = KINDS[k](self, node[k])
        if "blend" in node:
            # closing: every concave junction becomes an arc of radius r, convex arcs stay as they are
            r = self.num(node["blend"])
            geom = clean(geom.buffer(r, quad_segs=QS).buffer(-r, quad_segs=QS))
        if "round" in node:
            geom = soften(geom, self.num(node["round"]))
        g = node.get("guide", True)
        if g is False:
            guides = []
        elif g == "circles":
            guides = [x for x in guides if x[0] in ("circle", "ellipse", "cross", "dot")]
        elif g == "lines":
            guides = [x for x in guides if x[0] in ("line", "seg", "axis")]
        return clean(geom), guides, meta

    def child(self, a):
        return self.shape(a["shape"] if isinstance(a, dict) and "shape" in a else a)

    def widths(self, w):
        ws = [self.num(x) for x in w] if isinstance(w, list) else [self.num(w)]
        if not ws or any(x < 0 for x in ws):
            raise SpecError("stroke width must be >= 0")
        return ws

    # each k_* returns (geom, guides, meta)
    def k_circle(self, a):
        c, r = self.circle_of(a)
        return disc(c, r), [("circle", c, r), ("cross", c)], {"c": c, "r": r}

    def k_ellipse(self, a):
        c = self.pt(a["c"])
        rx, ry, rot = self.num(a["rx"]), self.num(a["ry"]), self.num(a.get("rot", 0))
        g = affinity.rotate(affinity.scale(disc(c, 1), rx, ry, origin=c), rot, origin=c)
        return g, [("ellipse", c, rx, ry, rot), ("cross", c)], {"c": c}

    def k_rect(self, a):
        if "c" in a:
            c = self.pt(a["c"])
            w, h = self.num(a["w"]), self.num(a["h"])
            x0, y0 = c[0] - w / 2, c[1] - h / 2
        else:
            x0, y0, w, h = self.num(a["x"]), self.num(a["y"]), self.num(a["w"]), self.num(a["h"])
        x1, y1 = x0 + w, y0 + h
        r = a.get("r", 0)
        rs = [self.num(v) for v in r] if isinstance(r, list) else [self.num(r)] * 4
        rs = [max(0.0, min(v, w / 2, h / 2)) for v in rs]
        corners = [((x0 + rs[0], y0 + rs[0]), rs[0], 180, (x0, y0)),
                   ((x1 - rs[1], y0 + rs[1]), rs[1], 270, (x1, y0)),
                   ((x1 - rs[2], y1 - rs[2]), rs[2], 0, (x1, y1)),
                   ((x0 + rs[3], y1 - rs[3]), rs[3], 90, (x0, y1))]
        pts, guides = [], []
        for c, rr, a0, sharp in corners:
            if rr > 0:
                pts += arc_pts(c, rr, a0, a0 + 90)
                guides += [("circle", c, rr), ("cross", c)]
            else:
                pts.append(sharp)
        guides += [("line", (x0, y0), (x1, y0)), ("line", (x0, y1), (x1, y1)),
                   ("line", (x0, y0), (x0, y1)), ("line", (x1, y0), (x1, y1))]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        return Polygon(pts), guides, {"c": (cx, cy)}

    def k_poly(self, a):
        pts = [self.pt(p) for p in (a["pts"] if isinstance(a, dict) else a)]
        if len(pts) < 3:
            raise SpecError("poly needs at least 3 points")
        guides = [("line", p, q) for p, q in zip(pts, pts[1:] + pts[:1]) if p != q]
        return Polygon(pts).buffer(0), guides, {}

    def k_ring(self, a):
        if isinstance(a, list):
            c, ro, ri = self.pt(a[0]), self.num(a[1]), self.num(a[2])
        else:
            c = self.pt(a["c"])
            if "w" in a:
                ro = self.num(a["r"])
                ri = ro - self.num(a["w"])
            else:
                ro, ri = self.num(a["r_out"]), self.num(a["r_in"])
        if not 0 <= ri < ro:
            raise SpecError("ring needs 0 <= inner radius < outer radius")
        g = disc(c, ro).difference(disc(c, ri)) if ri > 0 else disc(c, ro)
        return g, [("circle", c, ro), ("circle", c, ri), ("cross", c)], {"c": c, "r": ro}

    def k_arc(self, a):
        c, r = self.pt(a["c"]), self.num(a["r"])
        a0, a1 = self.num(a["from"]), self.num(a["to"])
        pts = arc_pts(c, r, a0, a1)
        g = stroke_geom(pts, self.widths(a["w"]), a.get("cap", "round"))
        return g, [("circle", c, r, "spine"), ("cross", c)], {"c": c, "r": r}

    def k_sector(self, a):
        c, r = self.pt(a["c"]), self.num(a["r"])
        a0, a1 = self.num(a["from"]), self.num(a["to"])
        g = Polygon([c] + arc_pts(c, r, a0, a1)).buffer(0)
        guides = [("circle", c, r), ("cross", c), ("seg", c, polar(c, r, a0)), ("seg", c, polar(c, r, a1))]
        return g, guides, {"c": c, "r": r}

    def k_lune(self, a):
        """Region between two circular arcs that share the end points a and b.

        bulge = [s1, s2]: sagitta of each arc; positive bulges to the right of
        the walk a -> b (as seen on screen). Same signs give a crescent/fin,
        opposite signs a leaf/vesica, one value a D-shaped segment.
        """
        p, q = self.pt(a["a"]), self.pt(a["b"])
        bulges = a["bulge"] if isinstance(a["bulge"], list) else [a["bulge"]]
        if math.dist(p, q) < 1e-9:
            raise SpecError("lune: a and b must differ")
        guides = [("dot", p), ("dot", q), ("seg", p, q)]
        sides = []                             # each side: points from p to q
        for s in (self.num(b) for b in bulges[:2]):
            pts, c, r = bulge_arc(p, q, s)
            sides.append(pts)
            if c is not None:
                guides += [("circle", c, r), ("cross", c)]
        if len(sides) == 1:
            sides.append([p, q])
        ring = sides[0] + sides[1][::-1][1:-1]
        return Polygon(ring).buffer(0), guides, {}

    def k_arcgon(self, a):
        """Polygon whose edges are circular arcs: sails, shields, leaves, fins.

        pts: the corners, clockwise on screen. edges[i] runs from pts[i] to
        pts[i+1]: a number is its bulge (sagitta, positive to the right of the
        walk, 0 = straight), {"circle": circle} runs along that circle (minor
        arc; add "arc": "major" for the long way). Pin edges to circles to build
        concentric channels and shared curves.
        """
        pts = [self.pt(x) for x in a["pts"]]
        edges = a.get("edges", [0] * len(pts))
        if len(pts) < 2 or len(edges) != len(pts):
            raise SpecError("arcgon needs pts and one edge per point (edge i joins point i and i+1)")
        ring, guides = [], [("dot", x) for x in pts]
        for i, p in enumerate(pts):
            q = pts[(i + 1) % len(pts)]
            e = edges[i]
            if isinstance(e, dict) and "circle" in e:
                c, r = self.circle_of(e["circle"])
                a0 = math.degrees(math.atan2(p[1] - c[1], p[0] - c[0]))
                a1 = math.degrees(math.atan2(q[1] - c[1], q[0] - c[0]))
                sweep = (a1 - a0 + 180) % 360 - 180
                if e.get("arc") == "major":
                    sweep += -360 if sweep > 0 else 360
                seg = arc_pts(c, r, a0, a0 + sweep)
                seg[0], seg[-1] = p, q
                guides += [("circle", c, r), ("cross", c)]
            else:
                seg, c, r = bulge_arc(p, q, self.num(e["bulge"] if isinstance(e, dict) else e))
                guides += [("circle", c, r), ("cross", c)] if c is not None else [("line", p, q)]
            ring += seg[:-1]
        return Polygon(ring).buffer(0), guides, {}

    def k_chain(self, a):
        """Closed outline wrapped around circles like a belt over pulleys.

        Each item is [P, r] (or a circle-like shape name). r > 0: the outline
        bulges around the circle (convex, circle inside the shape); r < 0: it
        dips around it (concave notch, circle outside); r = 0: a sharp corner.
        Consecutive circles are joined by their tangent line, so the whole
        contour is G1-continuous arcs and tangents.
        """
        items = a["circles"] if isinstance(a, dict) else a
        circs = []
        for it in items:
            if isinstance(it, str):
                c, r = self.circle_of(it)
            elif isinstance(it, dict):
                c, r = self.pt(it["c"]), self.num(it["r"])
            else:
                c, r = self.pt(it[0]), self.num(it[1])
            circs.append((c, r))
        if len(circs) < 2:
            raise SpecError("chain needs at least 2 circles")
        def solve(circs):
            n = len(circs)
            tang = []
            for i in range(n):
                (ca, ra), (cb, rb) = circs[i], circs[(i + 1) % n]
                dx, dy = cb[0] - ca[0], cb[1] - ca[1]
                D = math.hypot(dx, dy)
                if D < 1e-9 or abs(rb - ra) >= D:
                    raise SpecError(f"chain: circles at {fmt(ca[0])},{fmt(ca[1])} and {fmt(cb[0])},{fmt(cb[1])} have no "
                                    "tangent of the needed kind (one sits inside the other, or a convex and a concave "
                                    "circle overlap)")
                phi = math.atan2(dy, dx) + math.acos((rb - ra) / D)
                nx, ny = math.cos(phi), math.sin(phi)
                tang.append(((ca[0] - ra * nx, ca[1] - ra * ny), (cb[0] - rb * nx, cb[1] - rb * ny)))
            sweeps = []
            for i, (c, r) in enumerate(circs):
                if abs(r) < 1e-9:
                    sweeps.append((0.0, 0.0))
                    continue
                p_in, p_out = tang[i - 1][1], tang[i][0]
                a_in = math.degrees(math.atan2(p_in[1] - c[1], p_in[0] - c[0]))
                a_out = math.degrees(math.atan2(p_out[1] - c[1], p_out[0] - c[0]))
                s = 1 if r > 0 else -1
                sweep = ((a_out - a_in) * s) % 360
                sweeps.append((a_in, 0.0 if sweep > 359.5 else sweep))
            return tang, sweeps

        def build(circs):
            # a circle that does not reach past the line joining its neighbours would make the
            # belt loop all the way round it; drop such circles and say so in the report
            circs, notes = list(circs), []
            while True:
                tang, sweeps = solve(circs)
                bad = [i for i, ((c, r), (_, sw)) in enumerate(zip(circs, sweeps))
                       if (r > 0 and sw > 330) or (r < 0 and sw > 180)]
                if not bad or len(circs) - len(bad) < 2:
                    break
                for i in reversed(bad):
                    (c, r) = circs.pop(i)
                    notes.append(f"chain circle at ({fmt(c[0])}, {fmt(c[1])}) r={fmt(r)} does not reach the outline "
                                 f"and was skipped: move it outward{' or enlarge it' if r > 0 else ''}")
            pts, guides = [], []
            for (c, r), (a_in, sweep) in zip(circs, sweeps):
                if abs(r) < 1e-9:
                    pts.append(c)
                    guides.append(("dot", c))
                    continue
                s = 1 if r > 0 else -1
                pts += arc_pts(c, abs(r), a_in, a_in + s * sweep)
                guides += [("circle", c, abs(r)), ("cross", c)]
            guides += [("line", p, q) for p, q in tang if math.dist(p, q) > 0.03 * self.size]
            return pts, guides, notes

        # the belt runs clockwise on screen (interior on the right). The order of the centres does not
        # tell the direction reliably (a big circle's centre can sit deep inside the shape), so build
        # both ways and keep the one whose outline does not cross itself, then the one that skips fewer
        # circles, then the larger one.
        def score(result):
            pts, _, notes = result
            ring = LineString(pts + pts[:1])
            return (ring.is_simple, -len(notes), Polygon(pts).buffer(0).area)

        tries = []
        for order in (circs, circs[::-1]):
            try:
                tries.append(build(order))
            except SpecError as e:
                err = e
        if not tries:
            raise err
        pts, guides, notes = max(tries, key=score)
        self.warnings += notes
        return Polygon(pts).buffer(0), guides, {}

    def k_stroke(self, a):
        pts = [self.pt(p) for p in a["pts"]]
        if len(pts) < 2:
            raise SpecError("stroke needs at least 2 points")
        g = stroke_geom(pts, self.widths(a["w"]), a.get("cap", "round"), a.get("join", "round"),
                        a.get("closed", False))
        return g, [("poly", pts)] + [("dot", p) for p in pts], {}

    def k_turtle(self, a):
        pos = self.pt(a.get("start", [0, 0]))
        hd = self.num(a.get("heading", 0))
        pts, guides = [pos], [("dot", pos)]

        def arc(r, turn):
            nonlocal pos, hd
            s = 1 if turn > 0 else -1
            c = polar(pos, r, hd + 90 * s)
            a0 = hd - 90 * s
            seg = arc_pts(c, r, a0, a0 + turn)
            pts.extend(seg[1:])
            guides.extend([("circle", c, r, *(("spine",) if "w" in a else ())), ("cross", c)])
            pos, hd = seg[-1], hd + turn

        for st in a.get("steps", []):
            if "line" in st or "fd" in st:
                L = self.num(st.get("line", st.get("fd")))
                new = polar(pos, L, hd)
                guides.append(("line", pos, new))
                pos = new
                pts.append(pos)
            elif "arc" in st:
                arc(self.num(st["arc"]), self.num(st["turn"]))
            elif "spiral" in st:
                r0 = self.num(st["spiral"])
                k = self.num(st.get("ratio", "1/phi"))
                sweep = self.num(st.get("sweep", 90)) * (1 if self.num(st.get("dir", 1)) > 0 else -1)
                for i in range(int(st.get("quarters", 4))):
                    arc(r0 * k ** i, sweep)
            elif "goto" in st:
                p = self.pt(st["goto"])
                hd = math.degrees(math.atan2(p[1] - pos[1], p[0] - pos[0]))
                guides.append(("line", pos, p))
                pos = p
                pts.append(pos)
            elif "heading" in st:
                hd = self.num(st["heading"])
            elif "turn" in st:
                hd += self.num(st["turn"])
            else:
                raise SpecError(f"unknown turtle step {st!r}")
        guides.append(("dot", pos))
        if "w" in a:
            g = stroke_geom(pts, self.widths(a["w"]), a.get("cap", "round"), a.get("join", "round"),
                            a.get("close", False))
        else:
            if len(pts) < 3:
                raise SpecError("a filled turtle path needs an area (or give it a width 'w')")
            g = Polygon(pts).buffer(0)
        return g, guides, {}

    def k_path(self, a):
        self.raw_paths += 1
        return parse_path(a if isinstance(a, str) else a["d"]), [], {}

    def k_hull(self, a):
        kids = [self.shape(x) for x in a]
        g = unary_union([k[0] for k in kids]).convex_hull
        guides = [x for k in kids for x in k[1]]
        if g.geom_type == "Polygon":
            co = list(g.exterior.coords)
            guides += [("line", p, q) for p, q in zip(co, co[1:]) if math.dist(p, q) > 0.03 * self.size]
        return g, guides, {}

    def k_union(self, a):
        kids = [self.shape(x) for x in a]
        return unary_union([k[0] for k in kids]), [x for k in kids for x in k[1]], (kids[0][2] if len(kids) == 1 else {})

    def k_subtract(self, a):
        kids = [self.shape(x) for x in a]
        g = kids[0][0]
        for k in kids[1:]:
            g = g.difference(k[0])
        return g, [x for k in kids for x in k[1]], {}

    def k_intersect(self, a):
        kids = [self.shape(x) for x in a]
        g = kids[0][0]
        for k in kids[1:]:
            g = g.intersection(k[0])
        return g, [x for k in kids for x in k[1]], {}

    def k_xor(self, a):
        kids = [self.shape(x) for x in a]
        g = kids[0][0]
        for k in kids[1:]:
            g = g.symmetric_difference(k[0])
        return g, [x for k in kids for x in k[1]], {}

    def k_offset(self, a):
        g, gd, meta = self.child(a)
        return g.buffer(self.num(a["d"]), quad_segs=QS), gd, {}

    def _xform(self, a, M, keep):
        g, gd, meta = self.child(a)
        g2, gd2 = affinity.affine_transform(g, M), tf_guides(gd, M)
        if keep:
            return unary_union([g, g2]), gd + gd2, {}
        return g2, gd2, ({"c": tf_point(M, meta["c"]), "r": meta.get("r")} if "c" in meta else {})

    def k_mirror(self, a):
        keep = a.get("keep", True)
        if "y" in a:
            y = self.num(a["y"])
            M = (1, 0, 0, -1, 0, 2 * y)
            axis = ("axis", (0, y), (1, y))
        else:
            x = self.num(a.get("x", self.size / 2))
            M = (-1, 0, 0, 1, 2 * x, 0)
            axis = ("axis", (x, 0), (x, 1))
        self.mirrored = True
        g, gd, meta = self._xform(a, M, keep)
        return g, gd + [axis], meta

    def _about(self, a):
        return self.pt(a["about"]) if "about" in a else (self.size / 2, self.size / 2)

    def k_rotate(self, a):
        cx, cy = self._about(a)
        t = math.radians(self.num(a["deg"]))
        c, s = math.cos(t), math.sin(t)
        M = (c, -s, s, c, cx - c * cx + s * cy, cy - s * cx - c * cy)
        return self._xform(a, M, a.get("copy", False))

    def k_move(self, a):
        dx, dy = (self.num(v) for v in a["by"])
        return self._xform(a, (1, 0, 0, 1, dx, dy), a.get("copy", False))

    def k_scale(self, a):
        cx, cy = self._about(a)
        k = self.num(a["k"])
        return self._xform(a, (k, 0, 0, k, cx - k * cx, cy - k * cy), a.get("copy", False))

    def k_repeat(self, a):
        cx, cy = self._about(a)
        n = int(self.num(a["n"]))
        step = self.num(a.get("step", 360 / n))
        start = self.num(a.get("start", 0))
        g0, gd0, _ = self.child(a)
        geoms, guides = [], []
        for i in range(n):
            t = math.radians(start + i * step)
            c, s = math.cos(t), math.sin(t)
            M = (c, -s, s, c, cx - c * cx + s * cy, cy - s * cx - c * cy)
            geoms.append(affinity.affine_transform(g0, M))
            guides += tf_guides(gd0, M)
        return unary_union(geoms), guides + [("cross", (cx, cy))], {"c": (cx, cy)}

    # ---- ink and guides
    def build(self):
        steps = self.spec.get("ink")
        if not steps:
            raise SpecError("spec needs an 'ink' list (what gets added and cut, in order)")
        ink, guides = EMPTY, []
        for i, st in enumerate(steps):
            if isinstance(st, str) or not any(k in st for k in ("add", "cut", "clip")):
                st = {"add": st}
            op = next(k for k in ("add", "cut", "clip") if k in st)
            try:
                g, gd, _ = self.shape(st[op])
            except SpecError as e:
                raise SpecError(f"ink step {i + 1} ({op}): {e}") from None
            if op == "add":
                gap = self.num(st.get("gap", 0))
                if gap > 0:
                    ink = ink.difference(g.buffer(gap, quad_segs=QS))
                    # the channel's far edge is the same circle grown by the gap: show it as a concentric circle
                    gd = gd + [("circle", x[1], x[2] + gap) for x in gd
                               if x[0] == "circle" and x[3:] != ("spine",) and x[2] >= 2]
                ink = ink.union(g)
            elif op == "cut":
                ink = ink.difference(g)
            else:
                ink = ink.intersection(g)
            ink = clean(ink)
            if st.get("guide", True) is not False:
                guides += gd
        if self.spec.get("soften"):
            ink = soften(ink, self.num(self.spec["soften"]))
        self.all_guides = dedup(guides)
        auto = self.all_guides if self.spec.get("show", "key") == "all" else key_guides(self.all_guides, ink)
        return ink, dedup(auto + self.extra_guides())

    def art(self, ink, color):
        """Colour layers painted in order: backdrops, the ink, then paint regions (ink clipped to a shape).

        Returns None for a plain one-colour mark.
        """
        spec = self.spec
        if not spec.get("backdrop") and not spec.get("paint"):
            return None
        layers, self.backdrop_guides = [], []
        bd = spec.get("backdrop") or []
        for item in bd if isinstance(bd, list) else [bd]:
            g, gd, _ = self.shape(item["shape"])
            layers.append((g, item.get("color", "#FFFFFF")))
            self.backdrop_guides += gd
        layers.append((ink, color))
        for item in spec.get("paint") or []:
            g, _, _ = self.shape(item["shape"])
            layers.append((clean(ink.intersection(g)), item["color"]))
        return layers

    def extra_guides(self):
        out = []
        for i, g in enumerate(self.spec.get("guides", []) or []):
            try:
                out += self.guide(g)
            except SpecError as e:
                raise SpecError(f"guide {i + 1}: {e}") from None
        return out

    def guide(self, g):
        S = self.size
        if "circle" in g:
            c, r = self.circle_of(g["circle"])
            return [("circle", c, r), ("cross", c)]
        if "line" in g:
            p, q = (self.pt(x) for x in g["line"])
            return [("line", p, q)]
        if "seg" in g:
            p, q = (self.pt(x) for x in g["seg"])
            return [("seg", p, q)]
        if "hline" in g:
            y = self.num(g["hline"])
            return [("line", (0, y), (S, y))]
        if "vline" in g:
            x = self.num(g["vline"])
            return [("line", (x, 0), (x, S))]
        if "axis" in g:
            x = S / 2 if g["axis"] is True else self.num(g["axis"])
            return [("axis", (x, 0), (x, S))]
        if "haxis" in g:
            y = S / 2 if g["haxis"] is True else self.num(g["haxis"])
            return [("axis", (0, y), (S, y))]
        if "angle" in g:   # line through P at a given direction
            p = self.pt(g["angle"][0])
            return [("line", p, polar(p, 10, self.num(g["angle"][1])))]
        if "tangent" in g:
            (c1, r1), (c2, r2) = (self.circle_of(x) for x in g["tangent"])
            lines = tangent_lines(c1, r1, c2, r2, g.get("which", "outer"))
            if not lines:
                raise SpecError("tangent: no tangent line exists for these circles")
            return [("line", p, q) for p, q in lines] + [("dot", p) for ln in lines for p in ln]
        if "tangent_at" in g:
            c, r = self.circle_of(g["tangent_at"][0])
            a = self.num(g["tangent_at"][1])
            p = polar(c, r, a)
            return [("line", p, polar(p, 10, a + 90)), ("dot", p)]
        if "radius" in g:
            c, r = self.circle_of(g["radius"][:2]) if len(g["radius"]) == 3 else self.circle_of(g["radius"][0])
            a = self.num(g["radius"][-1])
            p = polar(c, r, a)
            return [("seg", c, p), ("cross", c), ("dot", p)]
        if "point" in g:
            return [("cross", self.pt(g["point"]))]
        if "show" in g:
            return self.shape(g["show"])[1]
        if "outline" in g:
            return [("outline", self.shape(g["outline"])[0])]
        raise SpecError(f"unknown guide {g!r}")


KINDS = {name[2:]: fn for name, fn in vars(Builder).items() if name.startswith("k_")}


# ---------------------------------------------------------------- SVG output

def fmt(v, nd=2):
    s = f"{v:.{nd}f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def geom_d(geom, T=lambda p: p, nd=2):
    out = []
    for poly in polys(geom):
        for ring in [poly.exterior, *poly.interiors]:
            pts = [T(p) for p in ring.coords[:-1]]
            if len(pts) >= 3:
                out.append("M" + "L".join(f"{fmt(x, nd)} {fmt(y, nd)}" for x, y in pts) + "Z")
    return "".join(out)


def fit(bounds, box_w, box_h, cx, cy):
    x0, y0, x1, y1 = bounds
    s = min(box_w / max(x1 - x0, 1e-6), box_h / max(y1 - y0, 1e-6))
    ox = cx - s * (x0 + x1) / 2
    oy = cy - s * (y0 + y1) / 2
    return s, (lambda p: (ox + s * p[0], oy + s * p[1])), (ox, oy)


def text_width(t, px):
    w = 0.0
    for ch in t:
        o = ord(ch)
        if o >= 0x2E80:
            w += 1.0
        elif ch == " ":
            w += 0.28
        elif ch.isupper() or ch in "mwMW@%":
            w += 0.66
        elif ch in "il.,:;'!|ftjr()":
            w += 0.3
        else:
            w += 0.53
    return w * px


NO_LINE_START = set("，。、；：！？）」』】〉》,.;:!?)")


def wrap(text, px, max_w):
    lines = []
    for para in text.split("\n"):
        cur = ""
        tokens = re.findall(r"[⺀-￿]|[^\s⺀-￿]+\s*|\s+", para)
        for tok in tokens:
            if cur and text_width(cur + tok.rstrip(), px) > max_w and tok[0] not in NO_LINE_START:
                lines.append(cur.rstrip())
                cur = tok.lstrip()
            else:
                cur += tok
        lines.append(cur.rstrip())
    return lines


def layers_bounds(layers):
    return unary_union([g for g, _ in layers if not g.is_empty]).bounds


def layers_paths(layers, T=lambda p: p, nd=2):
    return "".join(f'<path fill="{c}" fill-rule="evenodd" d="{geom_d(g, T, nd)}"/>' for g, c in layers if not g.is_empty)


SANS = "'Helvetica Neue', Helvetica, Arial, 'PingFang SC', 'Hiragino Sans GB', 'Noto Sans CJK SC', sans-serif"
SERIF = "'Iowan Old Style', Palatino, Georgia, 'Songti SC', 'Noto Serif CJK SC', serif"


def icon_svg(ink, color, d=None, pad=0.06, layers_d=None, bounds=None):
    """Square viewBox centred on the mark's bbox, with `pad` of the side as margin.

    layers_d: [(path data, colour), ...] for a multi-colour icon; bounds then covers all layers.
    """
    x0, y0, x1, y1 = bounds or ink.bounds
    side = max(x1 - x0, y1 - y0) / (1 - 2 * pad)
    vx, vy = (x0 + x1 - side) / 2, (y0 + y1 - side) / 2
    body = ("".join(f'<path fill="{c}" fill-rule="evenodd" d="{dd}"/>' for dd, c in layers_d) if layers_d
            else f'<path fill="{color}" fill-rule="evenodd" d="{d or geom_d(ink, nd=3)}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{fmt(vx, 3)} {fmt(vy, 3)} {fmt(side, 3)} {fmt(side, 3)}" '
            f'width="512" height="512">{body}</svg>\n')


def poster_svg(ink, paper, color, layers=None):
    W, H = 1080, 1350
    layers = layers or [(ink, color)]
    s, T, _ = fit(layers_bounds(layers), 430, 400, W / 2, H / 2)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<defs><radialGradient id="v" cx="50%" cy="46%" r="75%"><stop offset="0" stop-color="#FFFFFF" stop-opacity=".45"/><stop offset="1" stop-color="#FFFFFF" stop-opacity="0"/></radialGradient></defs>
<rect width="{W}" height="{H}" fill="{paper}"/><rect width="{W}" height="{H}" fill="url(#v)"/>
{layers_paths(layers, T)}
</svg>
'''


def card_svg(ink, guides, spec, size, paper, color, layers=None):
    W, H = 1080, 1350
    FX, FY, FW, FH = 54, 54, 972, 1242          # frame
    GB = 1010                                   # grid area bottom
    TB = 284                                    # thumbnail box side
    s, T, (ox, oy) = fit(ink.bounds, 600, 520, W / 2, 520)

    # grid paper aligned to design units, ~14px minor pitch
    step = min((0.25, 0.5, 1, 2, 2.5, 5, 10, 20), key=lambda u: abs(s * u - 14))
    minor, major = [], []
    k0 = math.floor((FX - ox) / (s * step))
    k1 = math.ceil((FX + FW - ox) / (s * step))
    for k in range(k0, k1 + 1):
        x = ox + k * s * step
        if FX <= x <= FX + FW:
            (major if k % 5 == 0 else minor).append(f"M{fmt(x)} {FY}V{GB}")
    k0 = math.floor((FY - oy) / (s * step))
    k1 = math.ceil((GB - oy) / (s * step))
    for k in range(k0, k1 + 1):
        y = oy + k * s * step
        if FY <= y <= GB:
            (major if k % 5 == 0 else minor).append(f"M{FX} {fmt(y)}H{FX + FW}")

    def far(p, q):
        (x1, y1), (x2, y2) = T(p), T(q)
        L = math.hypot(x2 - x1, y2 - y1)
        if L < 1e-9:
            return None
        ux, uy = (x2 - x1) / L, (y2 - y1) / L
        return (x1 - ux * 3000, y1 - uy * 3000, x1 + ux * 3000, y1 + uy * 3000)

    ring_at = {}
    for g in guides:
        if g[0] == "circle":
            k = (round(g[1][0], 2), round(g[1][1], 2))
            ring_at[k] = max(ring_at.get(k, 0), g[2] * s)

    circ, lines, marks = [], [], []
    for g in guides:
        t = g[0]
        if t == "circle":
            (cx, cy), r = T(g[1]), g[2] * s
            if r > 0.5:
                circ.append(f'<circle cx="{fmt(cx)}" cy="{fmt(cy)}" r="{fmt(r)}"/>')
        elif t == "ellipse":
            (cx, cy) = T(g[1])
            circ.append(f'<ellipse cx="{fmt(cx)}" cy="{fmt(cy)}" rx="{fmt(g[2] * s)}" ry="{fmt(g[3] * s)}" '
                        f'transform="rotate({fmt(g[4])} {fmt(cx)} {fmt(cy)})"/>')
        elif t in ("line", "axis"):
            e = far(g[1], g[2])
            if e:
                cls = ' class="ax"' if t == "axis" else ""
                lines.append(f'<line{cls} x1="{fmt(e[0])}" y1="{fmt(e[1])}" x2="{fmt(e[2])}" y2="{fmt(e[3])}"/>')
        elif t == "seg":
            (x1, y1), (x2, y2) = T(g[1]), T(g[2])
            lines.append(f'<line class="sg" x1="{fmt(x1)}" y1="{fmt(y1)}" x2="{fmt(x2)}" y2="{fmt(y2)}"/>')
        elif t == "poly":
            pts = " ".join(f"{fmt(x)},{fmt(y)}" for x, y in map(T, g[1]))
            lines.append(f'<polyline class="sg" points="{pts}"/>')
        elif t == "outline":
            lines.append(f'<path class="ol" d="{geom_d(g[1], T)}"/>')
        elif t == "cross":
            x, y = T(g[1])
            r = ring_at.get((round(g[1][0], 2), round(g[1][1], 2)), 40)
            k = max(2.5, min(6, r * 0.3))
            marks.append(f"M{fmt(x - k)} {fmt(y)}H{fmt(x + k)}M{fmt(x)} {fmt(y - k)}V{fmt(y + k)}")
        elif t == "dot":
            x, y = T(g[1])
            marks.append(f"M{fmt(x - 2.4)} {fmt(y)}a2.4 2.4 0 1 0 4.8 0a2.4 2.4 0 1 0 -4.8 0")

    # header: small mark + title
    title = spec.get("title", spec.get("name", ""))
    header = ""
    if spec.get("header", True) and title:
        tw = text_width(title, 34)
        hx = W / 2 - (34 + 12 + tw) / 2
        _, Th, _ = fit(layers_bounds(layers or [(ink, color)]), 34, 34, hx + 17, 112)
        header = (layers_paths(layers or [(ink, color)], Th) +
                  f'<text x="{fmt(hx + 46)}" y="124" font-family="{SERIF}" font-size="34" font-weight="600" '
                  f'fill="#3B3A31">{html.escape(title)}</text>')

    # concept text
    concept = spec.get("concept", "")
    label = spec.get("label", "CONCEPT")
    tx, tmax = 112, W - FX - TB - 112 - 44
    px, lh = 25, 40
    lines_t = wrap(concept, px, tmax)
    if len(lines_t) > 4:
        px, lh = 22, 34
        lines_t = wrap(concept, px, tmax)
    if len(lines_t) > 6:
        lines_t = lines_t[:6]
        lines_t[-1] = lines_t[-1].rstrip() + "…"
    body = "".join(f'<tspan x="{tx}" dy="{0 if i == 0 else lh}">{html.escape(t)}</tspan>' for i, t in enumerate(lines_t))

    bx, by = FX + FW - TB, FY + FH - TB
    _, Tt, _ = fit(layers_bounds(layers or [(ink, color)]), TB * 0.6, TB * 0.6, bx + TB / 2, by + TB / 2)

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<defs>
<clipPath id="g"><rect x="{FX}" y="{FY}" width="{FW}" height="{GB - FY}"/></clipPath>
<linearGradient id="f" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{paper}" stop-opacity="0"/><stop offset="1" stop-color="{paper}"/></linearGradient>
<style>
.mi{{stroke:#E7E3D6;stroke-width:.7;fill:none}} .ma{{stroke:#DCD6C5;stroke-width:.9;fill:none}}
.c circle,.c ellipse{{fill:none;stroke:#77736A;stroke-width:1.25;stroke-opacity:.62}}
.l line,.l polyline,.l path{{fill:none;stroke:#86827A;stroke-width:1.05;stroke-opacity:.6}}
.l .ax{{stroke:#5F5C53;stroke-dasharray:7 6;stroke-opacity:.75}} .l .sg{{stroke:#6B675E;stroke-opacity:.75}}
.l .ol{{stroke:#5F5C53;stroke-dasharray:4 4}}
.m{{fill:none;stroke:#57544B;stroke-width:1.3;stroke-opacity:.85}}
</style>
</defs>
<rect width="{W}" height="{H}" fill="{paper}"/>
<g clip-path="url(#g)">
<path class="mi" d="{''.join(minor)}"/><path class="ma" d="{''.join(major)}"/>
<path fill="#BFBBAB" fill-opacity=".9" stroke="#8C887B" stroke-width="1" fill-rule="evenodd" d="{geom_d(ink, T)}"/>
<g class="c">{''.join(circ)}</g>
<g class="l">{''.join(lines)}</g>
<path class="m" d="{''.join(marks)}"/>
</g>
<rect x="{FX}" y="{GB - 170}" width="{FW}" height="171" fill="url(#f)"/>
<rect x="{FX}" y="{FY}" width="{FW}" height="{FH}" fill="none" stroke="#D9D3C3" stroke-width="1.5"/>
{header}
<text x="{tx}" y="1086" font-family="{SANS}" font-size="20" letter-spacing="4" fill="#47453D">{html.escape(label)}</text>
<text x="{tx}" y="1140" font-family="{SANS}" font-size="{px}" fill="#7B786D">{body}</text>
<rect x="{bx}" y="{by}" width="{TB}" height="{TB}" fill="none" stroke="#D9D3C3" stroke-width="1.5"/>
{layers_paths(layers or [(ink, color)], Tt)}
</svg>
'''


def compare_svg(ink, image_path, paper, color, d=None, layers_d=None, bounds=None):
    """Original | rebuilt | overlay, for regularising an existing icon. The original is scaled so its ink
    bbox lands on the rebuilt mark's bbox (same width, same centre). With a backdrop (an app-icon tile)
    the whole image is taken as the tile: crop the screenshot to the tile first."""
    import base64
    import measure
    from PIL import Image
    W0, H0 = Image.open(image_path).size
    if bounds:
        ox0, oy0, ox1, oy1 = 0, 0, W0, H0
        x0, y0, x1, y1 = bounds
    else:
        og, _, s = measure.ink_geometry(image_path)
        ox0, oy0, ox1, oy1 = (v / s for v in og.bounds)           # original ink bbox in image pixels
        x0, y0, x1, y1 = ink.bounds
    k = (x1 - x0) / max(ox1 - ox0, 1e-9)
    ix = (x0 + x1) / 2 - k * (ox0 + ox1) / 2
    iy = (y0 + y1) / 2 - k * (oy0 + oy1) / 2
    with open(image_path, "rb") as f:
        mime = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
        href = f"data:{mime};base64," + base64.b64encode(f.read()).decode()
    pad = 0.12 * max(x1 - x0, y1 - y0)
    vb = (x0 - pad, y0 - pad, (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad)
    P, gapx, top = 420, 30, 60
    W, H = 3 * P + 4 * gapx, P + top + 30
    layers_d = layers_d or [(d or geom_d(ink, nd=3), color)]
    rebuilt = "".join(f'<path fill="{c}" fill-rule="evenodd" d="{dd}"/>' for dd, c in layers_d)
    outline = "".join(f'<path fill="none" stroke="#E5484D" stroke-width="{fmt(((x1 - x0) * 1.24) / 420 * 1.6, 3)}" '
                      f'd="{dd}"/>' for dd, _ in layers_d)
    img = (f'<image href="{href}" x="{fmt(ix, 3)}" y="{fmt(iy, 3)}" width="{fmt(k * W0, 3)}" '
           f'height="{fmt(k * H0, 3)}" preserveAspectRatio="none"/>')
    panels = [
        ("ORIGINAL", img),
        ("REBUILT", rebuilt),
        ("OVERLAY", f'<g opacity=".45">{img}</g>{outline}'),
    ]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
           f'<rect width="{W}" height="{H}" fill="{paper}"/>']
    for i, (label, body) in enumerate(panels):
        px = gapx + i * (P + gapx)
        out.append(f'<text x="{px}" y="36" font-family="{SANS}" font-size="15" letter-spacing="3" fill="#47453D">{label}</text>')
        out.append(f'<rect x="{px}" y="{top}" width="{P}" height="{P}" fill="#FFFFFF" stroke="#D9D3C3"/>')
        out.append(f'<svg x="{px}" y="{top}" width="{P}" height="{P}" viewBox="{" ".join(fmt(v, 3) for v in vb)}">{body}</svg>')
    out.append("</svg>\n")
    return "\n".join(out), W, H


def sizes_svg(ink, paper, color, layers=None):
    sizes = [128, 64, 48, 32, 24, 16]
    gap, pad = 36, 40
    W = pad * 2 + sum(sizes) + gap * (len(sizes) - 1)
    rowh = 128
    H = pad * 3 + rowh * 2 + 30
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
           f'<rect width="{W}" height="{H}" fill="{paper}"/>',
           f'<rect y="{pad * 2 + rowh}" width="{W}" height="{H - pad * 2 - rowh}" fill="{color}"/>']
    for row, (fg, y0) in enumerate(((color, pad), (paper, pad * 3 + rowh))):
        x = pad
        for sz in sizes:
            cy = y0 + rowh / 2
            _, T, _ = fit(layers_bounds(layers) if layers else ink.bounds, sz * 0.92, sz * 0.92, x + sz / 2, cy)
            out.append(layers_paths(layers, T) if layers else f'<path fill="{fg}" fill-rule="evenodd" d="{geom_d(ink, T)}"/>')
            if row == 0:
                out.append(f'<text x="{x + sz / 2}" y="{y0 + rowh + 26}" text-anchor="middle" font-family="{SANS}" '
                           f'font-size="13" fill="#8A877C">{sz}px</text>')
            x += sz + gap
    out.append("</svg>\n")
    return "\n".join(out), W, H


# ---------------------------------------------------------------- rasterising

def find_chrome():
    cands = [os.environ.get("CHROME"),
             "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
             "/Applications/Chromium.app/Contents/MacOS/Chromium",
             "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
             "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]
    cands += [shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge")]
    return next((c for c in cands if c and os.path.exists(c)), None)


def rasterize(svg, png, w, h, scale):
    """Best-effort SVG -> PNG. Returns the tool used, or None."""
    if shutil.which("rsvg-convert"):
        r = subprocess.run(["rsvg-convert", "-z", str(scale), "-o", png, svg], capture_output=True)
        if r.returncode == 0:
            return "rsvg-convert"
    try:
        import cairosvg  # type: ignore
        cairosvg.svg2png(url=svg, write_to=png, scale=scale)
        return "cairosvg"
    except Exception:
        pass
    chrome = find_chrome()
    if chrome:
        with tempfile.TemporaryDirectory() as prof:
            r = subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                                "--no-default-browser-check", f"--user-data-dir={prof}",
                                f"--window-size={w},{h}", f"--force-device-scale-factor={scale}",
                                f"--screenshot={os.path.abspath(png)}", "file://" + os.path.abspath(svg)],
                               capture_output=True, timeout=90)
        if r.returncode == 0 and os.path.exists(png):
            return "chrome"
    if shutil.which("qlmanage"):
        d = tempfile.mkdtemp()
        subprocess.run(["qlmanage", "-t", "-s", str(int(max(w, h) * scale)), "-o", d, svg], capture_output=True)
        out = os.path.join(d, os.path.basename(svg) + ".png")
        if os.path.exists(out):
            shutil.move(out, png)
            return "qlmanage"
    return None


# ---------------------------------------------------------------- lint

def lint(b, ink, guides, spec):
    S = b.size
    out = []
    if ink.is_empty:
        return ["ink is empty: nothing was drawn"]
    x0, y0, x1, y1 = ink.bounds
    bw, bh = x1 - x0, y1 - y0
    dx, dy = (x0 + x1) / 2 - S / 2, (y0 + y1) / 2 - S / 2
    out.append(f"ink bbox     x {x0:.1f}..{x1:.1f}  y {y0:.1f}..{y1:.1f}  ({bw:.1f} x {bh:.1f}); "
               f"bbox centre is ({dx:+.1f}, {dy:+.1f}) from the artboard centre")
    cen = ink.centroid
    out.append(f"visual mass  centroid at ({cen.x:.1f}, {cen.y:.1f}); ink covers {100 * ink.area / (bw * bh):.0f}% of its bbox")
    parts = polys(ink)
    holes = sum(len(p.interiors) for p in parts)
    tiny = [p for p in parts if p.area < 0.0015 * S * S]
    out.append(f"pieces       {len(parts)} piece(s), {holes} hole(s)"
               + (f"; {len(tiny)} tiny island(s) near " + ", ".join(f"({p.centroid.x:.0f},{p.centroid.y:.0f})" for p in tiny[:5])
                  + " will vanish at small sizes" if tiny else ""))

    radii = sorted({round(g[2], 2) for g in guides if g[0] == "circle"})
    if radii:
        note = "  -> consider consolidating to <= 5 related sizes" if len(radii) > 6 else ""
        out.append(f"radii        {len(radii)} distinct: " + ", ".join(fmt(r) for r in radii[:16])
                   + (" ..." if len(radii) > 16 else "") + note)
    centres = [g[1] for g in guides if g[0] == "circle"]
    off = [c for c in centres if abs(c[0] * 2 - round(c[0] * 2)) > 0.01 or abs(c[1] * 2 - round(c[1] * 2)) > 0.01]
    if centres:
        out.append(f"grid         {len(centres) - len(off)}/{len(centres)} circle centres sit on the 0.5-unit grid"
                   + (" (derived tangent/polar points are fine off-grid)" if off else ""))

    t = b.num(spec.get("min_feature", S * 0.02))
    opened = ink.buffer(-t / 2, quad_segs=32).buffer(t / 2, quad_segs=32)
    thin = [p for p in polys(ink.difference(opened)) if p.area > 1.5 * t * t]
    out.append(f"thin parts   " + ("none narrower than " + fmt(t) if not thin else
               f"{len(thin)} spot(s) narrower than {fmt(t)} near " +
               ", ".join(f"({p.centroid.x:.0f},{p.centroid.y:.0f})" for p in thin[:6])))
    tg = b.num(spec.get("min_gap", S * 0.012))
    closed = ink.buffer(tg / 2, quad_segs=32).buffer(-tg / 2, quad_segs=32)
    clog = [p for p in polys(closed.difference(ink)) if p.area > 1.5 * tg * tg]
    out.append(f"tight gaps   " + ("none narrower than " + fmt(tg) if not clog else
               f"{len(clog)} gap(s) narrower than {fmt(tg)} near " +
               ", ".join(f"({p.centroid.x:.0f},{p.centroid.y:.0f})" for p in clog[:6]) + " will clog when small"))
    n_c = sum(1 for g in guides if g[0] in ("circle", "ellipse"))
    n_l = sum(1 for g in guides if g[0] in ("line", "axis", "seg"))
    out.append(f"guides       {n_c} circles, {n_l} lines" + (f"; {b.raw_paths} raw path(s) carry no construction"
                                                             if b.raw_paths else ""))
    out += [f"warning      {w}" for w in dict.fromkeys(b.warnings)]
    return out


# ---------------------------------------------------------------- external SVG input

def svg_to_geom(path):
    """Flatten a flat (one or two colour) SVG into ink geometry for review.

    Shapes are painted in document order: a white fill cuts, fill="none" is skipped,
    any other fill adds. Transforms are ignored (reported), which is fine for the
    flat exports that vector models produce.
    """
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    parent = {c: p for p in root.iter() for c in p}
    notes = []

    def attr(el, name):
        while el is not None:
            style = dict(kv.split(":", 1) for kv in el.get("style", "").replace(" ", "").split(";") if ":" in kv)
            if name in style:
                return style[name]
            if el.get(name) is not None:
                return el.get(name)
            el = parent.get(el)
        return None

    white = {"#fff", "#ffffff", "white", "rgb(255,255,255)"}
    ink = EMPTY
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        f = lambda k, d=0.0: float(el.get(k, d) or d)  # noqa: E731
        if tag == "path":
            g = parse_path(el.get("d", ""))
        elif tag == "circle":
            g = disc((f("cx"), f("cy")), f("r"))
        elif tag == "ellipse":
            g = affinity.scale(disc((f("cx"), f("cy")), 1), f("rx"), f("ry"), origin=(f("cx"), f("cy")))
        elif tag == "rect":
            x, y, w, h = f("x"), f("y"), f("width"), f("height")
            rx = f("rx", f("ry"))
            g = Polygon([(x, y), (x + w, y), (x + w, y + h), (x, y + h)])
            if rx > 0:
                g = g.buffer(-rx, join_style="mitre").buffer(rx, quad_segs=QS)
        elif tag in ("polygon", "polyline"):
            nums = [float(v) for v in re.findall(r"-?\d*\.?\d+(?:e-?\d+)?", el.get("points", ""))]
            pts = list(zip(nums[0::2], nums[1::2]))
            g = Polygon(pts).buffer(0) if len(pts) >= 3 else EMPTY
        else:
            continue
        e = el
        while e is not None:
            if e.get("transform"):
                notes.append("transform attributes were ignored")
                break
            e = parent.get(e)
        fill = (attr(el, "fill") or "#000").strip().lower()
        if fill == "none":
            continue
        ink = ink.difference(g) if fill in white else ink.union(g)
        ink = clean(ink)
    return ink, sorted(set(notes))


# ---------------------------------------------------------------- main

def load_spec(path):
    src = open(path, encoding="utf-8").read()
    # strip // comments outside strings, then trailing commas
    out, i, in_str = [], 0, False
    while i < len(src):
        ch = src[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < len(src):
                out.append(src[i + 1])
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif src.startswith("//", i):
            while i < len(src) and src[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    text = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        sys.exit(f"{path}: invalid JSON at line {e.lineno} col {e.colno}: {e.msg}")


def review_svg(args):
    """The review half of the pipeline for an SVG that was not constructed here."""
    ink, notes = svg_to_geom(args.svg)
    if ink.is_empty:
        sys.exit("svg: nothing filled was found")
    x0, y0, x1, y1 = ink.bounds
    s = 88.0 / max(x1 - x0, y1 - y0)
    ink = affinity.affine_transform(ink, (s, 0, 0, s, 50 - s * (x0 + x1) / 2, 50 - s * (y0 + y1) / 2))
    name = os.path.splitext(os.path.basename(args.svg))[0]
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.svg)), name + "-review")
    os.makedirs(out, exist_ok=True)
    files = {"icon.svg": (icon_svg(ink, INK, pad=args.icon_pad), 512, 512),
             "poster.svg": (poster_svg(ink, PAPER, INK), 1080, 1350)}
    sz, sw, sh = sizes_svg(ink, PAPER, INK)
    files["sizes.svg"] = (sz, sw, sh)
    for fn, (svg, _, _) in files.items():
        with open(os.path.join(out, fn), "w", encoding="utf-8") as f:
            f.write(svg)
    report = ["source       external SVG, reviewed only: no construction card (it was not built from circles here)"]
    report += [f"note         {n}" for n in notes] + lint(Builder({}), ink, [], {})
    with open(os.path.join(out, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    tool = None
    if not args.no_png:
        for fn, (_, w, h) in files.items():
            if fn != "icon.svg":
                tool = rasterize(os.path.join(out, fn), os.path.join(out, fn[:-4] + ".png"), w, h,
                                 1 if fn == "sizes.svg" else args.scale) or tool
    print(f"wrote {out}/  " + " ".join(files) + " report.txt" + (f"  (+ PNGs via {tool})" if tool else ""))
    print("\n".join("  " + line for line in report))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", nargs="?")
    ap.add_argument("--svg", metavar="FILE",
                    help="review an SVG made elsewhere (e.g. by an external vector provider): poster, sizes, report")
    ap.add_argument("-o", "--out", help="output directory")
    ap.add_argument("--no-png", action="store_true", help="skip rasterising")
    ap.add_argument("--scale", type=float, default=1.0, help="PNG scale for poster/card (default 1)")
    ap.add_argument("--icon-pad", type=float, default=0.06,
                    help="margin around the mark in icon.svg, as a share of its side (0 = full bleed, e.g. app icons)")
    ap.add_argument("--icon-png", type=int, metavar="PX", help="also write icon.png at PX x PX (e.g. 1024)")
    ap.add_argument("--compare", metavar="IMAGE",
                    help="an existing icon this spec regularises: also write compare.svg/png (original | rebuilt | overlay)")
    ap.add_argument("--all-guides", action="store_true",
                    help="draw every construction circle and line, not only those that end up on the outline")
    args = ap.parse_args()

    if args.svg:
        review_svg(args)
        return
    if not args.spec:
        ap.error("give a spec file, or --svg FILE")
    spec = load_spec(args.spec)
    if args.all_guides:
        spec["show"] = "all"
    name = spec.get("name") or os.path.splitext(os.path.basename(args.spec))[0]
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(args.spec)), name)
    os.makedirs(out, exist_ok=True)

    b = Builder(spec)
    try:
        ink, guides = b.build()
    except SpecError as e:
        sys.exit(f"spec error: {e}")
    if ink.is_empty:
        sys.exit("spec error: the ink is empty (check that 'add' steps come before 'cut' steps)")

    colors = spec.get("colors", {})
    paper, color = colors.get("paper", PAPER), colors.get("ink", INK)
    try:
        layers = b.art(ink, color)
    except SpecError as e:
        sys.exit(f"spec error in backdrop/paint: {e}")
    circles = [(g[1], g[2]) for g in b.all_guides + getattr(b, "backdrop_guides", []) if g[0] == "circle"]
    d_exact, st = exact_path(ink, circles)
    layers_d = [(exact_path(g, circles)[0], c) for g, c in layers] if layers else None
    bounds = layers_bounds(layers) if layers else None
    files = {
        "icon.svg": (icon_svg(ink, color, d_exact, pad=args.icon_pad, layers_d=layers_d, bounds=bounds), 512, 512),
        "poster.svg": (poster_svg(ink, paper, color, layers), 1080, 1350),
        "construction.svg": (card_svg(ink, guides, spec, b.size, paper, color, layers), 1080, 1350),
    }
    sz, sw, sh = sizes_svg(ink, paper, color, layers)
    files["sizes.svg"] = (sz, sw, sh)
    if args.compare:
        files["compare.svg"] = compare_svg(ink, args.compare, paper, color, d_exact, layers_d,
                                           bounds if spec.get("backdrop") else None)
    for fn, (svg, _, _) in files.items():
        with open(os.path.join(out, fn), "w", encoding="utf-8") as f:
            f.write(svg)

    report = lint(b, ink, guides, spec)
    tot = sum(st.values()) or 1
    report.insert(0, f"icon.svg     outline written as {100 * st['arc'] / tot:.0f}% exact arcs, "
                     f"{100 * st['line'] / tot:.0f}% straight lines, {100 * st['poly'] / tot:.0f}% polyline "
                     "(fillets from blend/soften/round and tapered strokes have no exact circle)")
    with open(os.path.join(out, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")

    tool = None
    if not args.no_png:
        for fn, (_, w, h) in files.items():
            if fn == "icon.svg":
                if args.icon_png:
                    with open(os.path.join(out, fn), encoding="utf-8") as f_:
                        big = f_.read().replace('width="512" height="512"', f'width="{args.icon_png}" height="{args.icon_png}"', 1)
                    tmp = os.path.join(out, "_icon_px.svg")
                    with open(tmp, "w", encoding="utf-8") as f_:
                        f_.write(big)
                    tool = rasterize(tmp, os.path.join(out, "icon.png"), args.icon_png, args.icon_png, 1) or tool
                    os.remove(tmp)
                continue
            scale = 1 if fn == "sizes.svg" else args.scale
            tool = rasterize(os.path.join(out, fn), os.path.join(out, fn[:-4] + ".png"), w, h, scale) or tool
    print(f"wrote {out}/  " + " ".join(files) + " report.txt"
          + (f"  (+ PNGs via {tool})" if tool else ("  (no rasteriser found; SVG only)" if not args.no_png else "")))
    print("\n".join("  " + line for line in report))


if __name__ == "__main__":
    main()
