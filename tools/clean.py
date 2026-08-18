#!/usr/bin/env python3
"""Automatic pre-clean: erase tags and pins, drop the background, plate white.

    python tools/clean.py --run runs/<stamp>

This runs before any re-lay, and it exists to give the generative pass less to
do. Every job the re-lay model is asked to perform is a chance for it to drift
the colour or invent a seam, and background removal is not a job it should have:
a segmentation model does it without touching a single garment pixel.

Two steps, deliberately in this order:

  1. `fal-ai/image-editing/object-removal` - erases the hang tag, price ticket
     and other paperwork by text prompt. GENERATIVE, so it runs FIRST, while the
     real surrounding fabric is still there to reconstruct from. Skipped unless
     --remove is given.

     **--remove names PAPER, never hardware.** It used to end in "safety pins,
     clips", and on a pair of navy leggings the eraser read the two reflective
     thigh-pocket zips as clips and painted them out - 4.11% of the frame
     spliced back, colour drift 0.8, so nothing warned. describe.py then
     inventoried the CLEANED image, wrote "Two drop-in pockets" and listed
     "Zippered Pockets" under NOT PRESENT, generate.py appended that to all ten
     prompts, and the stage 3 construction gate compares candidates against this
     file - so every check downstream agreed the zips had never existed. Four
     zipper-less flats shipped. Two pin heads left at a waistband corner are a
     retouch afterthought; an erased zip is a different product.
  2. `fal-ai/birefnet/v2` - alpha matte, composited onto white. Pure
     segmentation: it decides which pixels are garment, it does not repaint
     them.

The result is written as `archive/offset_upload.jpg`, which is what the re-lay
prompt then sees. Garment colour is measured before and after and reported, so a
generative step that shifted the product is visible rather than silent.
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path

import numpy as np
from PIL import Image

import common as C

ERASER = "fal-ai/image-editing/object-removal"
MATTE = "fal-ai/birefnet/v2"


def garment_colour(path: Path) -> np.ndarray:
    m, _ = C.garment_mask(path)
    return C.garment_rgb(path, m)


def splice(full: Image.Image, erased: Image.Image, min_frac=2e-5,
           feather=6.0) -> tuple[Image.Image, float]:
    """Put ONLY the erased patch back into the full-resolution original.

    The eraser silently returns a much smaller image - measured 3072x4096 in,
    880x1184 out, a 3.5x loss. Accepting that would hand the re-lay model a
    soft, quarter-size photograph, which is the opposite of preserving the
    product.

    So the erase runs small, and the result is used only where it actually
    changed something. Everything else stays the original's own pixels at full
    resolution. The changed region is found by differencing, keeping blobs big
    enough to be a tag rather than resampling noise, and feathering the seam.
    """
    from scipy import ndimage
    up = erased.resize(full.size, Image.LANCZOS)
    a = np.asarray(full, dtype=np.float32)
    b = np.asarray(up, dtype=np.float32)

    d = ndimage.gaussian_filter(np.abs(a - b).mean(axis=2), 3.0)
    changed = d > 18.0
    lab, n = ndimage.label(changed)
    keep = np.zeros_like(changed)
    if n:
        sizes = ndimage.sum(changed, lab, range(1, n + 1))
        for i, sz in enumerate(sizes, 1):
            if sz >= min_frac * changed.size:      # tag-sized, not noise
                keep |= lab == i
    if not keep.any():
        return full, 0.0

    keep = ndimage.binary_dilation(keep, np.ones((25, 25)))
    m = ndimage.gaussian_filter(keep.astype(np.float32), feather)[..., None]
    out = a * (1 - m) + b * m
    return Image.fromarray(out.astype(np.uint8)), float(keep.mean() * 100)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", type=Path)
    ap.add_argument("--off-set", type=Path, default=C.INPUTS / "off_set_image.jpg")
    ap.add_argument("--out", type=Path,
                    help="default <run>/archive/offset_upload.jpg")
    ap.add_argument("--remove", default="pins, hanger, tags",
                    help="objects to erase by name. Paper only - naming "
                         "hardware costs product. Pass '' to skip the "
                         "generative erase and only drop the background.")
    ap.add_argument("--long-side", type=int, default=4096,
                    help="long side of the image that gets uploaded")
    ap.add_argument("--keep-steps", action="store_true",
                    help="also write the intermediate erased image")
    a = ap.parse_args()

    if not a.off_set.exists():
        return print(f"Not found: {a.off_set}") or 1
    run = a.run or C.session_run_dir()
    arch = run / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    out = a.out or arch / "offset_upload.jpg"

    C.fix_ca_bundle()
    C.load_fal_key()
    import fal_client
    import requests

    src = Image.open(a.off_set)
    icc = src.info.get("icc_profile")
    work = src.convert("RGB")
    work.thumbnail((a.long_side, a.long_side), Image.LANCZOS)
    staged = arch / "_clean_input.jpg"
    work.save(staged, quality=95, subsampling=0)
    before = garment_colour(staged)
    print(f"source        {work.width}x{work.height}  "
          f"garment RGB {before.round(0).tolist()}")

    cur = staged

    if a.remove.strip():
        print(f"erasing       '{a.remove}' via {ERASER}")
        r = fal_client.subscribe(ERASER, arguments={
            "image_url": fal_client.upload_file(str(cur)),
            "prompt": a.remove, "output_format": "png"}, with_logs=False)
        items = r.get("images", [])
        if not items:
            return print("object-removal returned no image") or 1
        img = Image.open(requests.get(items[0]["url"], stream=True,
                                      timeout=300).raw).convert("RGB")
        spliced, patch = splice(work, img)
        cur = arch / "_clean_erased.png"
        spliced.save(cur)
        after = garment_colour(cur)
        drift = float(np.linalg.norm(after - before))
        print(f"              model returned {img.width}x{img.height}; spliced "
              f"{patch:.2f}% of the frame back into the full-resolution "
              f"original")
        print(f"              -> {spliced.width}x{spliced.height}  "
              f"garment RGB {after.round(0).tolist()}  drift {drift:.1f}")
        if patch == 0.0:
            print("  NOTE: nothing changed enough to splice - no tag found, or "
                  "the eraser did nothing.")
        if drift > 8.0:
            print(f"  WARNING: the erase moved the garment colour by {drift:.1f}. "
                  f"It is a generative step; check the result before trusting it.")
        C.log(run, f"erased tags/pins, colour drift {drift:.1f}")

    print(f"matting       {MATTE}")
    r = fal_client.subscribe(MATTE, arguments={
        "image_url": fal_client.upload_file(str(cur)),
        "output_format": "png", "refine_foreground": True,
        "operating_resolution": "2048x2048"}, with_logs=False)
    url = (r.get("image") or {}).get("url") or r.get("images", [{}])[0].get("url")
    if not url:
        return print(f"birefnet returned no image: {list(r)}") or 1
    cut = Image.open(requests.get(url, stream=True, timeout=300).raw).convert("RGBA")

    # Composite onto white. The re-lay model gets a clean plate, and every
    # garment pixel is still the photograph's own - matting chooses pixels, it
    # does not repaint them.
    plate = Image.new("RGBA", cut.size, (255, 255, 255, 255))
    flat = Image.alpha_composite(plate, cut).convert("RGB")
    flat.save(out, quality=95, subsampling=0, icc_profile=icc) if icc else \
        flat.save(out, quality=95, subsampling=0)

    final = garment_colour(out)
    total_drift = float(np.linalg.norm(final - before))
    print(f"              -> {flat.width}x{flat.height}  "
          f"garment RGB {final.round(0).tolist()}  drift from source "
          f"{total_drift:.1f}")
    print(f"\nwrote {out}  ({out.stat().st_size/1e6:.1f} MB)")
    print("This is what the re-lay prompt will see. It no longer needs to "
          "remove a background or a tag - only to re-lay and de-wrinkle.")

    if not a.keep_steps:
        for p in (arch / "_clean_input.jpg", arch / "_clean_erased.png"):
            if p.exists() and p != out:
                p.unlink()
    C.log(run, f"pre-cleaned onto white, drift {total_drift:.1f} (2 calls, unpriced)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
