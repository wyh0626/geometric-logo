# geo-icon

**An agent skill that builds icons and logos from geometric construction — circles, arcs, tangents and even channels — and proves it with a construction sheet.**

[中文说明](README.zh-CN.md)

![Construction sheets made by geo-icon](docs/images/hero.png)

The agent writes the *construction* (a small JSON spec of circles, rings, lunes, tangent-arc chains and boolean steps), and the script computes the outline from it. The same primitives are drawn back as the construction lines, so the geometry on the sheet is the real source of the shape, not decoration laid on top afterwards.

```bash
npx skills add wyh0626/geo-icon
```

## What it can do

### 1. Design a new icon from geometry

Describe what you want — *"a minimal geometric spark icon on a rounded tile"* — and the agent picks primitives, a proportion system (one module, radii in a family such as R, R/φ, R/φ²) and writes the spec. One run produces:

| File | What it is |
|---|---|
| `icon.svg` / `icon.png` | the mark; outlines that follow a construction circle are written as true SVG arcs (`A`), straight runs as `L` |
| `construction.png` | the construction sheet: grid paper, the circles and tangent lines that lie on the final outline, centre marks, concept text, thumbnail |
| `poster.png` | the mark centred on a 4:5 canvas |
| `sizes.png` | 128 → 16 px, positive and reversed, to check legibility |
| `report.txt` | lint: share of exact arcs, radii in use, parts that are too thin, gaps that will clog, stray islands |

<p align="center"><img src="docs/images/spark-construction.png" width="420" alt="Spark construction sheet"> <img src="docs/images/spark-icon.png" width="200" alt="Spark icon"></p>

*Spark: twelve capsule rays, long and short alternating every 30°, a short ray is 1/φ of a long one ([spec](skills/geo-icon/examples/spark.json)).*

<p align="center"><img src="docs/images/seal-construction.png" width="420" alt="Seal construction sheet"> <img src="docs/images/seal-icon.png" width="200" alt="Seal icon"></p>

*Seal: one outline wraps the tail, a long back arc (r = 50), the neck, the chest and a belly circle resting on the ground line; the flipper and head are set apart by one even channel, and the ball sits one gap above the snout, painted in a second colour ([spec](skills/geo-icon/examples/seal.json)).*

### 2. Regularise an existing icon

Give it an icon image. `measure.py` reports every part — widths, spacing, height ratios, centring — which shows what the icon was *trying* to be (equal widths? a 1:2:3 system? a centred group?). The agent rebuilds it on an exact grid and renders original | rebuilt | overlay, plus the construction sheet that explains the result.

![Regularising an uneven waveform icon](docs/images/regularize-compare.png)

<p align="center"><img src="docs/images/waveform-construction.png" width="420" alt="Waveform construction sheet"></p>

*Five capsule bars: every size derives from the bar width w — pitch w + w/φ, heights 1 : 2 : 3, tallest = 8w; tile corner 22.37% as on iOS ([spec](skills/geo-icon/examples/waveform.json)).*

### 3. Everything else

- **Primitives**: circle, ring, ellipse, rounded rect, polygon, sector, `lune` (two arcs through two points: leaves, fins, vesicas), `arcgon` (polygon with arc edges, edges can be pinned to a circle), `chain` (a belt around circles), `hull`, tapered strokes, `turtle` (tangent-arc chains and golden spirals), boolean ops, mirror / rotate / repeat.
- **Even channels**: `{"add": X, "gap": g}` cuts a channel of one width between overlapping parts; the sheet shows it as concentric circles.
- **Multi-colour and app icons**: `backdrop` (e.g. a rounded tile) and `paint` (colour regions); `--icon-pad 0 --icon-png 1024` writes a full-bleed App Store icon.
- **Review external SVGs**: `geoicon.py --svg file.svg` renders poster, size test and lint for an SVG made elsewhere.
- **Reserved hook for complex subjects**: dragons, kirin and other detailed subjects do not reduce to a handful of circles. `scripts/external.py` is a provider interface for a free-form vector model; none is bundled.

## Install

With the [skills CLI](https://skills.sh):

```bash
npx skills add wyh0626/geo-icon            # into the current project
npx skills add wyh0626/geo-icon -g         # for every project
```

Or copy `skills/geo-icon` into `~/.claude/skills/` (Claude Code) or your agent's skills folder.

Requirements: Python 3.9+, and

```bash
python3 -m pip install -r skills/geo-icon/requirements.txt   # shapely>=2, numpy, Pillow
```

PNG output uses the first of `rsvg-convert`, `cairosvg`, Chrome/Chromium/Edge headless or macOS `qlmanage`; without any of them you still get the SVGs.

## Use

Ask your agent, for example:

- "Design a minimal geometric logo for a bakery, a loaf built from circles."
- "Make a geometric owl icon and show me the construction sheet."
- "Regularise my app icon (icon.png): equal widths, centred, iOS corners."

Or run the scripts directly:

```bash
python3 skills/geo-icon/scripts/geoicon.py skills/geo-icon/examples/spark.json -o out/spark
python3 skills/geo-icon/scripts/measure.py my-icon.png
python3 skills/geo-icon/scripts/geoicon.py spec.json -o out --compare my-icon.png --icon-pad 0 --icon-png 1024
```

A spec looks like this:

```jsonc
{
  "name": "camera", "title": "Camera",
  "concept": "A camera drawn from concentric circles and level, straight edges.",
  "vars": {"U": 3.5, "R0": 20},
  "points": {"C": [50, 55]},
  "shapes": {
    "body":  {"rect": {"c": "C", "w": 72, "h": 46, "r": 13}},
    "hole":  {"circle": ["C", "R0"]},
    "ring":  {"ring": {"c": "C", "r_out": "R0 - U", "r_in": "R0 - 2.3*U"}},
    "glass": {"circle": ["C", "R0 - 3.3*U"]}
  },
  "ink": ["body", {"cut": "hole"}, "ring", "glass"]
}
```

The full language is in [`references/spec.md`](skills/geo-icon/references/spec.md); the design method and lessons are in [`references/method.md`](skills/geo-icon/references/method.md).

## Layout

```
skills/geo-icon/
├── SKILL.md                 workflow the agent follows
├── requirements.txt
├── scripts/
│   ├── geoicon.py           spec → icon, construction sheet, poster, size test, report
│   ├── measure.py           measure an existing icon image
│   └── external.py          reserved hook for a free-form vector provider (none bundled)
├── references/
│   ├── spec.md              the spec language
│   ├── method.md            construction method, recipes, lessons
│   └── external-vector.md   how to plug in a provider
└── examples/                spark, waveform, seal, camera, rocket, octopus, cat, bellflower, whale, sails
```

## Credits

The idea and the look of the construction sheet are inspired by the geometric logo feature of [Rikyū](https://rikyu.ai). This project is independent and not affiliated with Rikyū; it is a from-scratch reimplementation of the general method (shapes computed from circles and lines, with the construction drawn back on a grid sheet).

The example icons in this repository are original designs.

## License

[MIT](LICENSE)
