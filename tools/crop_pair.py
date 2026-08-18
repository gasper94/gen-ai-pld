#!/usr/bin/env python3
"""Matching crop boxes for two images that do not share a coordinate system.

    python tools/crop_pair.py --run runs/<stamp> --cand 10 --at waistband

The source photo and a candidate have different pixel dimensions AND the garment
sits in a different place in each, so the same pixel box lands on different parts
of the garment. A real run hand-picked (1700,1300) on the source and (950,700) on
a candidate believing they matched; in garment-relative terms those are 28%
across / 23% down versus 1% across / 9% down - the hip against the waistband.
Every construction verdict that run produced was comparing different things.

This locates the garment in each image with the same mask the metrics use, then
converts a garment-relative region into real pixel boxes for each. Paste the two
printed lines straight into compare_images.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image

import common as C

# Normalised to the garment's own bounding box: (u0, v0, u1, v1), origin at its
# top-left, 1.0 = its full width/height. Vertical bands work for any laydown;
# the named ones below are worded for legwear.
REGIONS = {
    "waistband": (0.10, 0.00, 0.90, 0.13),
    "hip":       (0.05, 0.13, 0.95, 0.32),
    "crotch":    (0.25, 0.28, 0.75, 0.44),
    "thigh":     (0.10, 0.35, 0.90, 0.55),
    "knee":      (0.10, 0.55, 0.90, 0.72),
    "hem":       (0.05, 0.88, 0.95, 1.00),
    "centre":    (0.30, 0.42, 0.70, 0.62),
    "left":      (0.00, 0.30, 0.30, 0.70),
    "right":     (0.70, 0.30, 1.00, 0.70),
}


def bbox(path: Path) -> tuple[int, int, int, int]:
    """Garment bounding box in the image's own full-resolution pixels."""
    m, _ = C.garment_mask(path)
    W, H = Image.open(path).size
    ys, xs = np.nonzero(m)
    if not len(xs):
        raise SystemExit(f"No garment found in {path}")
    sx, sy = W / m.shape[1], H / m.shape[0]
    return (int(xs.min() * sx), int(ys.min() * sy),
            int(xs.max() * sx), int(ys.max() * sy))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", type=Path)
    ap.add_argument("--cand", type=int, help="candidate number, with --run")
    ap.add_argument("-a", "--image-a", type=Path,
                    default=C.INPUTS / "off_set_image.jpg")
    ap.add_argument("-b", "--image-b", type=Path,
                    help="second image, if not using --run/--cand")
    ap.add_argument("--at", default="waistband",
                    help=f"a region name {sorted(REGIONS)}, or four normalised "
                         f"numbers 'u0,v0,u1,v1' inside the garment bbox")
    ap.add_argument("--max-px", type=int, default=1000,
                    help="largest crop side to allow. Above ~1024 the vision "
                         "call downscales and the look stops being 1:1, so both "
                         "boxes shrink around the region centre until they fit.")
    a = ap.parse_args()

    b_path = a.image_b
    if b_path is None:
        if not (a.run and a.cand):
            return print("Need --image-b, or --run with --cand.") or 1
        b_path = a.run / "archive" / f"cand_{a.cand:02d}.png"
    for p in (a.image_a, b_path):
        if not p.exists():
            return print(f"Not found: {p}") or 1

    if a.at in REGIONS:
        u0, v0, u1, v1 = REGIONS[a.at]
    else:
        try:
            u0, v0, u1, v1 = (float(x) for x in a.at.replace(" ", "").split(","))
        except ValueError:
            return print(f"--at must be a name {sorted(REGIONS)} or "
                         f"'u0,v0,u1,v1'.") or 1

    boxes = {}
    for label, p in (("a", a.image_a), ("b", b_path)):
        x0, y0, x1, y1 = bbox(p)
        gw, gh = x1 - x0, y1 - y0
        boxes[label] = [x0 + u0 * gw, y0 + v0 * gh, (u1 - u0) * gw, (v1 - v0) * gh]
        print(f"  {p.name:22} {Image.open(p).size[0]}x{Image.open(p).size[1]}  "
              f"garment {gw}x{gh} at ({x0},{y0})")

    # Both crops must stay under max_px or the vision call resamples them and the
    # comparison is no longer 1:1. Shrink both by the SAME normalised factor, so
    # they keep showing the same part of the garment.
    worst = max(max(w, h) for _, _, w, h in boxes.values())
    if worst > a.max_px:
        k = a.max_px / worst
        for v in boxes.values():
            cx, cy = v[0] + v[2] / 2, v[1] + v[3] / 2
            v[2], v[3] = v[2] * k, v[3] * k
            v[0], v[1] = cx - v[2] / 2, cy - v[3] / 2
        print(f"  shrunk both by {k:.2f} to keep each crop 1:1 under "
              f"{a.max_px}px")

    print(f"\nregion '{a.at}'  ({u0:.2f},{v0:.2f})-({u1:.2f},{v1:.2f}) "
          f"of the garment\n")
    for label in ("a", "b"):
        x, y, w, h = (int(round(v)) for v in boxes[label])
        print(f"box_{label}=\"{x},{y},{w},{h}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
