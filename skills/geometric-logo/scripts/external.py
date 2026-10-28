#!/usr/bin/env python3
"""external: hand a too-complex subject to a free-form vector provider (reserved hook).

    python3 external.py --brief "..." -o out.svg [--ref image.png ...]
    python3 external.py --list

geometric-logo builds marks from circles and lines. Subjects such as dragons, kirin or
phoenixes (manes, scales, clouds) do not reduce to a handful of circles; those
go to a model that draws free-form SVG instead (Rikyu uses QuiverAI Arrow; Recraft's
vector models are another option). No provider ships with this skill. To add one,
write a function with the signature below, register it in PROVIDERS, and select it
with the GEOMETRIC_LOGO_PROVIDER environment variable. Keep API keys in environment
variables, never in the spec or the brief.

The SVG that comes back is reviewed with the same tools as a constructed mark:

    python3 logo.py --svg out.svg -o review/      # poster, sizes sheet, report (no construction card)
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Callable

# provider(brief, references, out_path) -> out_path
#   brief:      the request in plain words: subject, composition, colours, what to avoid
#   references: local image paths the user supplied (may be empty)
#   out_path:   where to write the SVG
Provider = Callable[[str, list, str], str]

PROVIDERS: dict[str, Provider] = {
    # "quiver": quiver_generate,     # e.g. POST https://api.quiver.ai/v1/svgs/generations, key in QUIVERAI_API_KEY
    # "recraft": recraft_generate,   # e.g. Recraft's text-to-vector endpoint, key in RECRAFT_API_KEY
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brief", help="what to draw, in plain words")
    ap.add_argument("--ref", action="append", default=[], help="reference image (repeatable)")
    ap.add_argument("-o", "--out", default="external.svg")
    ap.add_argument("--list", action="store_true", help="list registered providers")
    args = ap.parse_args()

    if args.list:
        print("registered providers:", ", ".join(PROVIDERS) or "none")
        return
    name = os.environ.get("GEOMETRIC_LOGO_PROVIDER", "")
    if not PROVIDERS:
        sys.exit("external: no vector provider is configured in this skill.\n"
                 "This subject is too complex for geometric construction. Either simplify it to a geometric\n"
                 "silhouette and use logo.py, or add a provider to PROVIDERS in scripts/external.py\n"
                 "(see references/external-vector.md).")
    if name not in PROVIDERS:
        sys.exit(f"external: set GEOMETRIC_LOGO_PROVIDER to one of: {', '.join(PROVIDERS)}")
    if not args.brief:
        sys.exit("external: --brief is required")
    path = PROVIDERS[name](args.brief, args.ref, args.out)
    print(f"wrote {path}  (review it with: python3 logo.py --svg {path} -o review/)")


if __name__ == "__main__":
    main()
