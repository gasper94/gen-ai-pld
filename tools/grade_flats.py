#!/usr/bin/env python3
"""Step 4+5 - grade every candidate against the CLEANED source and pick.

    python tools/grade_flats.py --run runs/<stamp>

Answers "which generated flat should we ship", scoring shape and wrinkles, and
checking the generation did not quietly redraw the garment's construction.
Replaces measure.py and the by-eye contact-sheet review that used to follow it.

Why it is built this way
  Instruct-edit models re-synthesize the subject rather than editing pixels, so
  every candidate is a fresh chance to invent a seam or move a pocket. Invented
  detail is usually plausible, so it survives casual review. Two consequences
  shape this script:
    - photometric metrics (background, symmetry, smoothness) say NOTHING about
      construction. A candidate can win on every metric and still be a fake.
      So construction is a separate gate, not a score component.
    - vision judges are noisy; the same image can score 100 then 60 after a
      rescale. So each grade is a majority of N independent votes.

Stages
  1. Deterministic metrics, no model: garment mask, symmetry about the garment's
     own axis, local-variance wrinkle energy, background flatness and lightness.
     Free, repeatable, cannot hallucinate.
  2. Vision grading, --votes independent calls per candidate, median taken.
     Expected edits (background swap, relight, de-wrinkling, reframing) are
     declared up front as NOT defects, or the judge reports them as failures.
  3. Construction check on native-resolution crops (waistband, hip, hem) against
     the reference. Any MISMATCH disqualifies regardless of how clean it looks.

Two notes carried over from the pipeline this replaces, because they were paid
for here and the code above does not encode them:

  * The reference MUST be <run>/archive/offset_upload.jpg, the cleaned image the
    generator actually received - never inputs/off_set_image.jpg. The raw input
    still carries the hang tag, and a construction check run against it reports
    the correctly-removed tag as "a label removed" on every candidate. That is
    why --reference defaults off the run folder and not off inputs/.

  * `wrinkles` here is an isotropic local-variance measure. common.py records
    that a metric of exactly this class was tried on this project and removed:
    it ranked the visibly smoothest candidate of a real run highest, because it
    was reading the garment's form shading rather than its creases. It is
    batch-relative and worth 45% of the grade, so treat a wrinkle ranking that
    disagrees with the contact sheet as the metric being wrong, not the sheet.

ImageMagick + sips + the project venv. Vision helpers come from vision.py.

Usage
  python tools/grade_flats.py --run runs/<stamp>
  python tools/grade_flats.py --run runs/<stamp> --votes 5 --min-grade 85
  python tools/grade_flats.py --run runs/<stamp> --no-construction
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import common as C
from vision import (
    CACHE, Client, ensure_small, image_part, parse_json_blob, settled,
    text_part, transient, DEFAULT_BASE_URL, DEFAULT_MODEL,
)

ROOT = Path(__file__).resolve().parent.parent
WORK = CACHE / "grade"
FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
# Every image is rescaled so the garment bbox is this tall before texture
# is measured, so wrinkle energy compares fabric, not source resolution.
NORM_BBOX_H = 900

RES_ORDER = {"1K": 1, "2K": 2, "4K": 3}


def auto_select(arch: Path):
    """Every generated image at the highest resolution present.

    Carried over from measure.py. A probe generated at the same resolution as
    the final batch is a candidate - same model, same prompt, same pixels.
    Excluding it by filename discarded the three best images of a real run.
    """
    sf = arch / "seeds.json"
    try:
        man = json.loads(sf.read_text()) if sf.exists() else {}
    except json.JSONDecodeError:
        man = {}
    man = {k: v for k, v in man.items() if isinstance(v, dict)}
    if not man:
        return sorted(arch.glob("cand_*.png")), "cand_*.png"
    best = max(RES_ORDER.get(v.get("resolution"), 0) for v in man.values())
    keep = [k for k, v in man.items() if RES_ORDER.get(v.get("resolution"), 0) == best]
    res = next(v["resolution"] for v in man.values()
               if RES_ORDER.get(v.get("resolution"), 0) == best)
    paths = sorted(p for p in (arch / f"{k}.png" for k in keep) if p.exists())
    dropped = len(man) - len(keep)
    print(f"grading {len(paths)} image(s) at {res}"
          + (f"; {dropped} lower-resolution probe(s) held back" if dropped else ""))
    return paths, f"images at {res}"


# --------------------------------------------------------------------------
# Stage 1 - deterministic metrics
# --------------------------------------------------------------------------

def _fx(args: list[str]) -> float:
    out = subprocess.run(["magick", *args], check=True, capture_output=True, text=True)
    return float(out.stdout.strip())


def _bbox(mask: Path) -> tuple[int, int, int, int]:
    """Bounding box of the mask's non-background content, or zeros.

    identify's %@ comes back empty for an all-one-colour mask, which the
    original indexed into unguarded and died on.
    """
    geom = subprocess.run(["magick", "identify", "-format", "%@", str(mask)],
                          check=True, capture_output=True, text=True).stdout.strip()
    m = re.match(r"(\d+)x(\d+)\+(\d+)\+(\d+)", geom)
    return tuple(int(g) for g in m.groups()) if m else (0, 0, 0, 0)


def metrics(src: Path, width: int = 800) -> dict:
    """Photometric + geometric measurements. These rank presentation quality.
    They deliberately say nothing about whether the garment is still the same
    garment - that is stage 3's job."""
    WORK.mkdir(parents=True, exist_ok=True)
    stem = src.stem
    mask = WORK / f"{stem}_mask.png"
    core = WORK / f"{stem}_core.png"
    trim = WORK / f"{stem}_trim.png"

    # Otsu adapts the garment/background split per image, so a grey backdrop
    # does not silently swallow part of the garment.
    subprocess.run(["magick", str(src), "-resize", f"{width}x", "-colorspace", "Gray",
                    "-auto-threshold", "OTSU", "-negate", str(mask)],
                   check=True, capture_output=True)
    subprocess.run(["magick", str(mask), "-trim", "+repage", str(trim)],
                   check=True, capture_output=True)
    subprocess.run(["magick", str(mask), "-morphology", "Erode", "Disk:6", str(core)],
                   check=True, capture_output=True)

    coverage = _fx([str(mask), "-format", "%[fx:mean]", "info:"])
    core_frac = max(_fx([str(core), "-format", "%[fx:mean]", "info:"]), 1e-6)

    # Garment bbox, used to put every image on the same fabric-pixels-per-inch
    # footing before measuring texture. Without this the metric mostly reports
    # source resolution: a 3072px original downscaled to 800 is smoother than a
    # 1792px one at the same output width, purely from the heavier resample.
    bw0, bh0, _, _ = _bbox(mask)
    norm = WORK / f"{stem}_norm.png"
    norm_mask = WORK / f"{stem}_normcore.png"
    scale_pct = 100.0 * NORM_BBOX_H / max(bh0, 1)
    subprocess.run(["magick", str(src), "-resize", f"{width}x",
                    "-resize", f"{scale_pct:.3f}%", "-colorspace", "Gray", str(norm)],
                   check=True, capture_output=True)
    subprocess.run(["magick", str(core), "-resize", f"{scale_pct:.3f}%", str(norm_mask)],
                   check=True, capture_output=True)
    norm_core_frac = max(_fx([str(norm_mask), "-format", "%[fx:mean]", "info:"]), 1e-6)

    # Mirror about the garment's own bounding box, not the frame: otherwise a
    # perfectly symmetric garment sitting off-centre scores as asymmetric.
    asym = _fx([str(trim), "(", "+clone", "-flop", ")",
                "-compose", "difference", "-composite", "-format", "%[fx:mean]", "info:"])

    # Wrinkles show up as local luminance variance inside the garment. The mask
    # is eroded first so the garment/background edge does not dominate, and the
    # measurement runs on the scale-normalised copy.
    wr_sum = _fx([str(norm), "-statistic", "StandardDeviation", "5x5", str(norm_mask),
                  "-compose", "multiply", "-composite", "-format", "%[fx:mean]", "info:"])

    bg_sd = _fx([str(src), "-resize", f"{width}x", "-gravity", "northwest",
                 "-crop", "60x60+0+0", "+repage", "-colorspace", "Gray",
                 "-format", "%[fx:standard_deviation]", "info:"])
    bg_lum = _fx([str(src), "-resize", f"{width}x", "-gravity", "northwest",
                  "-crop", "60x60+0+0", "+repage", "-colorspace", "Gray",
                  "-format", "%[fx:mean]", "info:"])

    bw, bh, bx, by = _bbox(mask)

    return {
        "coverage": round(coverage, 4),
        "asymmetry": round(asym, 4),
        "wrinkle_energy": round(wr_sum / norm_core_frac, 5),
        "bg_sd": round(bg_sd, 4),
        "bg_lum": round(bg_lum, 4),
        "bbox": [bw, bh, bx, by],
        "aspect": round(bw / bh, 4) if bh else 0.0,
    }


# --------------------------------------------------------------------------
# Stage 2 - vision grading
# --------------------------------------------------------------------------

GRADE_PROMPT = """Image 1 is the REFERENCE: the real garment, photographed flat as-is.
Image 2 is a CANDIDATE: a generated ecommerce flat of that same garment.

The candidate is SUPPOSED to differ from the reference in these ways. None of these
is a defect, do not deduct for them:
  - cleaner / whiter background, softer or removed shadows
  - the garment straightened, re-centred, re-framed or rescaled
  - creases and rumples relaxed - that is the entire point of the edit
  - hangtags, pins, props or clips removed

Grade the CANDIDATE as an ecommerce product flat, on two axes.

SHAPE - is the silhouette right for a product listing?
  100 = both sides symmetric and evenly laid, legs/arms straight and parallel,
        natural garment proportions, nothing stretched, bent, pinched or warped
   50 = noticeably lopsided, one side wider or longer, a leg bowed or twisted
    0 = distorted, melted, impossible geometry, garment unrecognisable

WRINKLES - how clean is the surface?
  100 = smooth and evenly lit, reads as pressed, only soft structural shading
   50 = several visible creases or blotchy shading across panels
    0 = heavily rumpled, harsh fold shadows everywhere

Return ONE JSON object, nothing else:
{"shape": <0-100 integer>,
 "shape_issues": "<the single worst shape problem, or 'none'>",
 "wrinkles": <0-100 integer>,
 "wrinkle_issues": "<the single worst wrinkle problem, or 'none'>",
 "background": <0-100 integer, how clean and even the backdrop is>,
 "verdict": "ship" | "borderline" | "reject"}"""

CONSTRUCTION_PROMPT = """Both crops show the SAME REGION ({region}) of the SAME garment.
Image 1 is the REFERENCE (the real product). Image 2 is a GENERATED version.

A generative model redrew image 2, so it may have invented, moved or deleted
construction detail. That is what you are looking for, and only that.

Expected differences that are NOT discrepancies: background colour, brightness,
shadow softness, scale, rotation, position in frame, overall sharpness, and the
fabric lying flatter or smoother.

Report ONLY genuine construction differences: seams that were added or removed,
topstitching that appeared or vanished, a pocket or its opening moved or resized,
a waistband changed in height or structure, a hem or cuff changed in shape,
a logo/label added, removed or altered.

Return ONE JSON object, nothing else:
{{"verdict": "MATCH" | "MISMATCH",
  "detail": "<the specific construction difference, or 'none'>"}}"""


PAIR_PROMPT = """Image 1 is the REFERENCE: the real garment, photographed flat as-is.
Images 2 and 3 are two GENERATED ecommerce flats of that same garment, competing
against each other. Call them CANDIDATE 1 (image 2) and CANDIDATE 2 (image 3).

Both are supposed to differ from the reference by having a cleaner background,
softer shadows, a straightened and re-centred garment, and relaxed creases. Those
are the goal, not defects. Judge the two candidates only against each other.

You MUST choose on each axis. "tie" is only for a genuinely indistinguishable pair,
and picking it for two visibly different images is a failure to do the task.

SHAPE - which is the better product silhouette? Look at left/right symmetry, whether
the legs are straight and evenly spread, waistband evenness, and any warping,
pinching or bowing.

WRINKLES - which surface is cleaner? Look for creases, fold shadows, blotchy or
uneven shading across large flat panels.

Return ONE JSON object, nothing else:
{{"shape_better": "1" | "2" | "tie",
  "shape_why": "<the deciding difference, one short sentence>",
  "wrinkles_better": "1" | "2" | "tie",
  "wrinkles_why": "<the deciding difference, one short sentence>"}}"""


def compare_pair(client: Client, ref_small: Path,
                 a_small: Path, b_small: Path) -> dict:
    content = [text_part("Image 1 - REFERENCE:"), image_part(ref_small),
               text_part("Image 2 - CANDIDATE 1:"), image_part(a_small),
               text_part("Image 3 - CANDIDATE 2:"), image_part(b_small),
               text_part(PAIR_PROMPT)]
    return parse_json_blob(client.chat(content, max_tokens=350, temperature=0.0))


def run_tournament(client: Client, ref_small: Path, rows: list[dict],
                   concurrency: int) -> dict:
    """Round-robin, every pair judged in BOTH orders.

    Absolute 0-100 rubric scoring saturates: this model returned exactly 100 on
    every axis for every candidate across 12 calls, including one with a visibly
    grey backdrop. Relative judgement still discriminates, and running each pair
    both ways lets position bias be measured rather than assumed away."""
    names = [r["name"] for r in rows]
    by_name = {r["name"]: r for r in rows}
    jobs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            jobs.append((names[i], names[j]))
            jobs.append((names[j], names[i]))     # same pair, swapped order

    wins = {n: {"shape": 0.0, "wrinkles": 0.0} for n in names}
    played = {n: 0 for n in names}
    pos_pick = {"1": 0, "2": 0, "tie": 0}
    reasons = []

    def one(a: str, b: str):
        return a, b, compare_pair(client, ref_small,
                                  by_name[a]["_small"], by_name[b]["_small"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = [pool.submit(one, a, b) for a, b in jobs]
        for fut in concurrent.futures.as_completed(futs):
            try:
                a, b, v = fut.result()
            except Exception:  # noqa: BLE001 - a dropped bout is survivable
                continue
            played[a] += 1
            played[b] += 1
            for axis, key in (("shape", "shape_better"), ("wrinkles", "wrinkles_better")):
                pick = str(v.get(key, "tie")).strip().lower()
                if axis == "shape":
                    pos_pick[pick if pick in pos_pick else "tie"] += 1
                if pick == "1":
                    wins[a][axis] += 1
                elif pick == "2":
                    wins[b][axis] += 1
                else:
                    wins[a][axis] += 0.5
                    wins[b][axis] += 0.5
            reasons.append({"a": a, "b": b, **v})

    for n in names:
        g = max(played[n], 1)
        by_name[n]["shape"] = round(100.0 * wins[n]["shape"] / g, 1)
        by_name[n]["wrinkles"] = round(100.0 * wins[n]["wrinkles"] / g, 1)
        by_name[n]["bouts"] = played[n]
    return {"position_picks": pos_pick, "bouts": reasons}


def grade_once(client: Client, ref_small: Path, cand_small: Path) -> dict:
    content = [text_part("Image 1 - REFERENCE:"), image_part(ref_small),
               text_part("Image 2 - CANDIDATE:"), image_part(cand_small),
               text_part(GRADE_PROMPT)]
    return parse_json_blob(client.chat(content, max_tokens=400, temperature=0.0))


def grade_voted(client: Client, ref_small: Path, cand_small: Path, votes: int) -> dict:
    """Median of N independent grades. A single vision score is noisy evidence,
    not a measurement, so one call is never enough to rank on."""
    got = []
    for _ in range(votes):
        try:
            got.append(grade_once(client, ref_small, cand_small))
        except Exception:  # noqa: BLE001 - a dropped vote is survivable
            continue
    if not got:
        raise RuntimeError("every grading vote failed")

    def med(key: str) -> float:
        vals = [float(g[key]) for g in got if isinstance(g.get(key), (int, float))]
        return round(statistics.median(vals), 1) if vals else 0.0

    verdicts = [str(g.get("verdict", "")).lower() for g in got]
    return {
        "shape": med("shape"),
        "wrinkles": med("wrinkles"),
        "background": med("background"),
        "shape_spread": round(max([float(g["shape"]) for g in got]) -
                              min([float(g["shape"]) for g in got]), 1),
        "wrinkle_spread": round(max([float(g["wrinkles"]) for g in got]) -
                                min([float(g["wrinkles"]) for g in got]), 1),
        "verdict": max(set(verdicts), key=verdicts.count) if verdicts else "unknown",
        "shape_issues": got[0].get("shape_issues", ""),
        "wrinkle_issues": got[0].get("wrinkle_issues", ""),
        "votes_counted": len(got),
    }


# --------------------------------------------------------------------------
# Stage 3 - construction integrity on native-resolution crops
# --------------------------------------------------------------------------

REGIONS = {           # name -> (top, bottom) as a fraction of the garment bbox
    "waistband": (0.00, 0.22),
    "hip/pocket": (0.18, 0.45),
    "hem": (0.80, 1.00),
}

# Bras are wider than tall and have no waistband or hem, so the leggings bands
# land on empty plate. profiles/ already splits the two categories.
REGIONS_BY_PROFILE = {
    "leggings": REGIONS,
    "bras": {"band": (0.62, 1.00), "cups/centre": (0.25, 0.68),
             "straps": (0.00, 0.30)},
}


def crop_region(src: Path, span: tuple[float, float], tag: str) -> Path:
    """Crop at native resolution. Judging a whole 2K frame downscaled into the
    model loses exactly the fine detail this check exists to find."""
    WORK.mkdir(parents=True, exist_ok=True)
    mask = WORK / f"{src.stem}_mask.png"
    if not mask.exists():
        metrics(src)
    bw, bh, bx, by = _bbox(mask)
    if bh == 0:
        raise RuntimeError(f"no garment found in {src.name}")

    full_w = int(subprocess.run(["magick", "identify", "-format", "%w", str(src)],
                                check=True, capture_output=True, text=True).stdout)
    mask_w = int(subprocess.run(["magick", "identify", "-format", "%w", str(mask)],
                                check=True, capture_output=True, text=True).stdout)
    s = full_w / mask_w                       # mask was measured at 800px wide
    x, y = int(bx * s), int((by + span[0] * bh) * s)
    w, h = int(bw * s), int((span[1] - span[0]) * bh * s)

    out = WORK / f"{src.stem}_{tag}.jpg"
    subprocess.run(["magick", str(src), "-crop", f"{w}x{h}+{x}+{y}", "+repage",
                    "-resize", "1024x1024>", str(out)],
                   check=True, capture_output=True)
    return out


def check_construction(client: Client, ref: Path, cand: Path, regions: dict) -> list[dict]:
    out = []
    for i, (name, span) in enumerate(regions.items()):
        tag = f"r{i}"
        try:
            rc = crop_region(ref, span, tag)
            cc = crop_region(cand, span, tag)
        except Exception as e:  # noqa: BLE001
            out.append({"verdict": "ERROR", "detail": str(e)[:120], "region": name})
            continue
        content = [text_part(f"Image 1 - REFERENCE {name}:"), image_part(rc),
                   text_part(f"Image 2 - GENERATED {name}:"), image_part(cc),
                   text_part(CONSTRUCTION_PROMPT.format(region=name))]
        try:
            v = parse_json_blob(client.chat(content, max_tokens=250, temperature=0.0))
        except Exception as e:  # noqa: BLE001
            v = {"verdict": "ERROR", "detail": str(e)[:120]}
        v["region"] = name
        out.append(v)
    return out


# --------------------------------------------------------------------------

def normalise(vals: list[float], lower_is_better: bool) -> list[float]:
    """Map a raw metric onto 0-100 within this batch. Relative by construction:
    with everything equally good the spread is meaningless, so these are shown
    as context and used for tie-breaking, never as the headline grade."""
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return [100.0] * len(vals)
    out = [(v - lo) / (hi - lo) for v in vals]
    return [round((1 - o if lower_is_better else o) * 100, 1) for o in out]


def build_sheet(ref_small: Path, rows: list[dict], out: Path) -> Path:
    tiles = WORK / "tiles"
    if tiles.exists():
        shutil.rmtree(tiles)
    tiles.mkdir(parents=True)
    subprocess.run(["magick", str(ref_small), "-resize", "420x560", "-gravity", "north",
                    "-background", "white", "-splice", "0x46", "-font", FONT,
                    "-pointsize", "28", "-fill", "red", "-annotate", "+0+8",
                    "REFERENCE", str(tiles / "00.jpg")], check=True, capture_output=True)
    for i, r in enumerate(rows, start=1):
        colour = {"PASS": "blue", "REJECT": "red"}.get(r["status"], "gray50")
        label = f'{r["name"]}  {r["grade"]:.0f}  {r["status"]}'
        subprocess.run(["magick", str(r["_small"]), "-resize", "420x560",
                        "-gravity", "north", "-background", "white", "-splice", "0x46",
                        "-font", FONT, "-pointsize", "26", "-fill", colour,
                        "-annotate", "+0+8", label, str(tiles / f"{i:02d}.jpg")],
                       check=True, capture_output=True)
    subprocess.run(["montage", *sorted(str(p) for p in tiles.glob("*.jpg")),
                    "-font", FONT, "-tile", f"{len(rows) + 1}x", "-geometry", "+6+6",
                    "-background", "gray90", str(out)], check=True, capture_output=True)
    return out


def write_metrics_json(arch: Path, rows: list[dict], ref: Path, args) -> Path:
    """Write archive/metrics.json - the run's machine-readable verdict.

    Nothing consumes this automatically any more; review.py, which used to
    cross-check picks against it, is no longer part of the pipeline. It stays
    because it is the only per-candidate record with the construction verdicts
    in it, and `## Results` in the log has to be written from measured numbers
    rather than remembered ones.
    """
    out = {
        "schema": "grade",
        "reference": str(ref),
        "min_grade": args.min_grade,
        "judge": args.judge,
        "candidates": [{
            "cand": r["name"],
            "file": str(r["path"]),
            "score": r["grade"],
            "grade": r["grade"],
            "status": r["status"],
            "reject": r["status"] != "PASS",
            "reject_why": "; ".join(r["notes"]) if r["status"] != "PASS" else "",
            "shape": r["shape"],
            "wrinkles": r["wrinkles"],
            "background": r["background"],
            "symmetry": round(1.0 - r["metrics"]["asymmetry"], 3),
            "asymmetry": r["metrics"]["asymmetry"],
            "wrinkle_energy": r["metrics"]["wrinkle_energy"],
            "bg_lum": r["metrics"]["bg_lum"],
            "construction": r.get("construction", []),
        } for r in rows],
    }
    p = arch / "metrics.json"
    p.write_text(json.dumps(out, indent=2, default=str))
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=Path, required=True,
                    help="the run folder prepare.py printed as RUN_DIR=")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--reference", type=Path, default=None,
                    help="default <run>/archive/offset_upload.jpg - the CLEANED "
                         "image the generator actually received. Do not point "
                         "this at inputs/off_set_image.jpg: the raw input still "
                         "has the hang tag, and stage 3 then reports the "
                         "correctly-removed tag as a MISMATCH on everything.")
    ap.add_argument("--candidates", type=Path, default=None,
                    help="default <run>/archive, auto-selecting every generated "
                         "image at the highest resolution present.")
    ap.add_argument("--profile", choices=sorted(REGIONS_BY_PROFILE),
                    default="leggings",
                    help="which crop regions stage 3 uses. Bras have no "
                         "waistband or hem, so the leggings bands land on plate.")
    ap.add_argument("--judge", choices=["metrics", "tournament", "absolute"],
                    default="metrics",
                    help="how shape/wrinkles are scored. 'metrics' (default) uses "
                         "the deterministic measurements. 'tournament' has the model "
                         "compare pairs - showed 100%% position bias here. 'absolute' "
                         "asks for 0-100 rubric scores - saturated at 100 for every "
                         "candidate here. Both model modes are kept for re-testing "
                         "on a better model.")
    ap.add_argument("--votes", type=int, default=3,
                    help="independent grading calls per candidate, median taken "
                         "(default 3; vision scores are too noisy for one)")
    ap.add_argument("--min-grade", "--threshold", type=float, default=80.0,
                    metavar="GRADE", help="below this a candidate is not shippable "
                                          "(default 80)")
    ap.add_argument("--asym-anchor", type=float, default=0.06,
                    help="asymmetry that scores 0 for shape. Default 0.06 was "
                         "measured on an untouched leggings flat; a different "
                         "category needs its own anchor.")
    # The imported defaults were 0.95 -> 0 and 1.00 -> 100, which on this
    # project's own output flagged "backdrop not white" on 10 candidates out of
    # 10 and zeroed the term for 6 of them. common.py records why: the plate
    # here is not white, it sweeps 228-252 (0.894-0.988), so a real clean plate
    # never reaches 1.00. A warning that fires on every candidate carries no
    # information, so the scale is anchored on the plate this pipeline actually
    # produces.
    ap.add_argument("--bg-floor", type=float, default=0.90,
                    help="backdrop lightness scoring 0 (default 0.90)")
    ap.add_argument("--bg-white", type=float, default=0.99,
                    help="backdrop lightness scoring 100 (default 0.99)")
    ap.add_argument("--no-construction", action="store_true",
                    help="skip the crop-level construction integrity check")
    ap.add_argument("--ship", type=int, default=0, metavar="N",
                    help="copy the top N candidates BY GRADE to <run>/output/, "
                         "regardless of status. The grade has no construction "
                         "term, so a pick that redrew the garment can outrank "
                         "one that did not; every MISMATCH shipped is printed "
                         "and logged.")
    ap.add_argument("--ship-clean-only", action="store_true",
                    help="restore the gate: ship only PASS candidates, so a "
                         "construction MISMATCH never reaches output/.")
    ap.add_argument("--cutout", action="store_true",
                    help="also write a transparent-background *_cutout.png "
                         "beside each pick. Off by default: --ship currently "
                         "delivers the flats themselves, so the cutout step is "
                         "opt-in until it is wanted again.")
    ap.add_argument("--max-dim", type=int, default=1024)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    sys.stdout.reconfigure(line_buffering=True)

    arch = args.run / "archive"
    ref = args.reference or (arch / "offset_upload.jpg")
    if not ref.exists():
        print(f"reference not found: {ref}\n"
              f"  Run prepare.py first - it writes the cleaned upload there.",
              file=sys.stderr)
        return 1

    if args.candidates and args.candidates.is_dir():
        cands = sorted(p for p in args.candidates.iterdir()
                       if p.suffix.lower() in (".png", ".jpg", ".jpeg"))
        label = str(args.candidates)
    else:
        cands, label = auto_select(arch)
    if not cands:
        print(f"No candidates in {arch}. Run generate.py first.", file=sys.stderr)
        return 1

    client = Client(args.base_url, args.model, args.timeout)
    model = client.resolve_model()
    print(f"model: {model}")
    print(f"reference:  {ref}   (the cleaned upload, not the raw input)")
    print(f"candidates: {len(cands)}   votes/candidate: {args.votes}\n")

    # --- Stage 1 ---------------------------------------------------------
    print("stage 1: deterministic metrics (no model)")
    t0 = time.time()
    ref_m = metrics(ref)
    print(f"  reference   asym {ref_m['asymmetry']:.4f}  wrinkle "
          f"{ref_m['wrinkle_energy']:.5f}  bg_lum {ref_m['bg_lum']:.4f}")
    rows = []
    for p in cands:
        m = metrics(p)
        rows.append({"name": p.stem, "path": p, "metrics": m,
                     "_small": ensure_small(p, args.max_dim)})
        print(f"  {p.stem:<10}  asym {m['asymmetry']:.4f}  wrinkle "
              f"{m['wrinkle_energy']:.5f}  bg_lum {m['bg_lum']:.4f}  "
              f"coverage {m['coverage']:.3f}")
    print(f"  done in {time.time() - t0:.1f}s")

    sym_n = normalise([r["metrics"]["asymmetry"] for r in rows], True)
    wr_n = normalise([r["metrics"]["wrinkle_energy"] for r in rows], True)
    for r, s, w in zip(rows, sym_n, wr_n):
        r["sym_rank"], r["wr_rank"] = s, w

    # --- Stage 2 ---------------------------------------------------------
    ref_small = ensure_small(ref, args.max_dim)
    tour = {"position_picks": {}, "bouts": []}

    if args.judge == "tournament":
        n_pairs = len(rows) * (len(rows) - 1)
        print(f"\nstage 2: pairwise tournament, {n_pairs} bouts "
              f"(every pair judged in both orders)")
        t0 = time.time()
        transient(f"  running {n_pairs} comparisons ...")
        tour = run_tournament(client, ref_small, rows, args.concurrency)
        settled(f"  {n_pairs} bouts in {time.time() - t0:.1f}s")
        for r in sorted(rows, key=lambda r: -(r["shape"] + r["wrinkles"])):
            print(f"  {r['name']:<10}  shape win-rate {r['shape']:5.1f}   "
                  f"wrinkles win-rate {r['wrinkles']:5.1f}   ({r['bouts']} bouts)")
        # Both orders are judged, so a healthy judge splits its picks roughly
        # evenly between slot 1 and slot 2. A lopsided split means it is reading
        # position, not the images.
        pp = tour["position_picks"]
        total = max(pp["1"] + pp["2"], 1)
        skew = abs(pp["1"] - pp["2"]) / total
        print(f"  position check: slot-1 picks {pp['1']}, slot-2 picks {pp['2']}, "
              f"ties {pp['tie']}  -> skew {skew:.0%}")
        if skew > 0.4:
            print("  WARNING: the judge is picking by slot, not by image. These "
                  "win-rates are noise; prefer --judge metrics.")
    elif args.judge == "absolute":
        print(f"\nstage 2: absolute rubric grading, {args.votes} votes each")
        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futs = {pool.submit(grade_voted, client, ref_small, r["_small"],
                                args.votes): r for r in rows}
            for fut in concurrent.futures.as_completed(futs):
                r = futs[fut]
                try:
                    g = fut.result()
                except Exception as e:  # noqa: BLE001
                    g = {"shape": 0.0, "wrinkles": 0.0, "shape_spread": 0.0,
                         "wrinkle_spread": 0.0, "verdict": f"error: {e}"[:60],
                         "votes_counted": 0}
                r["shape"], r["wrinkles"] = g["shape"], g["wrinkles"]
                r["bouts"] = g["votes_counted"]
                r["grade_detail"] = g
                print(f"  {r['name']:<10}  shape {g['shape']:5.1f} "
                      f"(spread {g['shape_spread']:4.1f})   wrinkles {g['wrinkles']:5.1f} "
                      f"(spread {g['wrinkle_spread']:4.1f})   {g['verdict']}")
        print(f"  done in {time.time() - t0:.1f}s")
        if all(r["shape"] >= 99 and r["wrinkles"] >= 99 for r in rows):
            print("  WARNING: every candidate scored ~100 on both axes. The rubric "
                  "has saturated and cannot rank; prefer --judge metrics.")
    else:
        print("\nstage 2: scoring shape and wrinkles from the measurements")
        # Shape anchors on the reference: an untouched real flat measured 0.06
        # asymmetry, so that is the zero mark and a perfectly mirrored garment
        # is 100.
        for r in rows:
            a = r["metrics"]["asymmetry"]
            r["shape"] = round(max(0.0, min(100.0,
                                            (1 - a / args.asym_anchor) * 100)), 1)
        # Wrinkle energy spans only ~7% across this batch, too narrow for an
        # absolute scale, so it is ranked within the batch and labelled as such.
        for r, w in zip(rows, normalise([r["metrics"]["wrinkle_energy"] for r in rows],
                                        True)):
            r["wrinkles"] = w
        for r in rows:
            r["bouts"] = 0
            print(f"  {r['name']:<10}  shape {r['shape']:5.1f} (asym "
                  f"{r['metrics']['asymmetry']:.4f})   wrinkles {r['wrinkles']:5.1f} "
                  f"(energy {r['metrics']['wrinkle_energy']:.5f}, batch-relative)")

    # --- Stage 3 ---------------------------------------------------------
    regions = REGIONS_BY_PROFILE[args.profile]
    if not args.no_construction:
        print(f"\nstage 3: construction integrity on native-res crops "
              f"({', '.join(regions)})")
        t0 = time.time()
        for r in rows:
            transient(f"  {r['name']:<10}  checking ...")
            r["construction"] = check_construction(client, ref, r["path"], regions)
            bad = [c for c in r["construction"] if c.get("verdict") == "MISMATCH"]
            if bad:
                settled(f"  {r['name']:<10}  MISMATCH in "
                        f"{', '.join(c['region'] for c in bad)}")
                for c in bad:
                    print(f"                -> {c['region']}: {c.get('detail','')}")
            else:
                settled(f"  {r['name']:<10}  all regions match")
        print(f"  done in {time.time() - t0:.1f}s")
    else:
        for r in rows:
            r["construction"] = []

    # --- Combine ---------------------------------------------------------
    for r in rows:
        # Background is scored from the measurement, not the model: the model
        # rated a visibly grey backdrop 100/100.
        span = max(args.bg_white - args.bg_floor, 1e-6)
        bg = max(0.0, min(100.0,
                          (r["metrics"]["bg_lum"] - args.bg_floor) / span * 100))
        r["background"] = round(bg, 1)
        grade = 0.45 * r["shape"] + 0.45 * r["wrinkles"] + 0.10 * bg
        notes = []
        if r["metrics"]["bg_lum"] < args.bg_floor:
            notes.append(f"backdrop not a plate (bg_lum {r['metrics']['bg_lum']:.3f} "
                         f"< {args.bg_floor:.2f})")
        mism = [c for c in r.get("construction", []) if c.get("verdict") == "MISMATCH"]
        # Construction failure is disqualifying, not a deduction: a candidate that
        # redrew the garment is the wrong product however good it looks.
        if mism:
            r["status"] = "REJECT"
            notes.append("construction altered: " +
                         "; ".join(f"{c['region']} - {c.get('detail','')}" for c in mism))
        elif grade >= args.min_grade:
            r["status"] = "PASS"
        else:
            r["status"] = "BELOW"
            notes.append(f"grade {grade:.1f} < {args.min_grade:.0f}")
        r["grade"] = round(max(0.0, min(100.0, grade)), 1)
        r["notes"] = notes

    rows.sort(key=lambda r: (r["status"] == "REJECT", -r["grade"]))

    print(f"\nRANKING  (grade = 45% shape + 45% wrinkles + 10% background, "
          f"pass mark {args.min_grade:.0f})")
    print(f"  {'':3} {'grade':>6} {'status':<7} {'name':<10} {'shape':>6} {'wrink':>6} "
          f"{'bg':>5} {'sym*':>5} {'smooth*':>7}  notes")
    for i, r in enumerate(rows, start=1):
        print(f"  {i:>2}. {r['grade']:6.1f} {r['status']:<7} {r['name']:<10} "
              f"{r['shape']:6.1f} {r['wrinkles']:6.1f} {r['background']:5.0f} "
              f"{r['sym_rank']:5.0f} {r['wr_rank']:7.0f}  "
              f"{'; '.join(r['notes'])[:60]}")
    print("  shape/wrink are " + ("tournament win-rates" if args.judge == "tournament"
                                  else "measured: shape absolute, wrinkles batch-relative")
          + "; bg is measured")
    print("  * sym / smooth are deterministic, ranked within this batch only")
    least = min(rows, key=lambda r: len([c for c in r.get("construction", [])
                                         if c.get("verdict") == "MISMATCH"]))
    n_bad = len([c for c in least.get("construction", []) if c.get("verdict") == "MISMATCH"])
    if any(r["status"] == "REJECT" for r in rows):
        print(f"\n  least-altered candidate: {least['name']} ({n_bad} region(s) changed)."
              f"  Presentation rank was #{rows.index(least) + 1}.")

    winners = [r for r in rows if r["status"] == "PASS"]
    sheet = build_sheet(ref_small, rows, arch / "grade_results.jpg")
    (arch / "grade_results.json").write_text(json.dumps({
        "model": model,
        "reference": str(ref),
        "reference_metrics": ref_m,
        "votes": args.votes,
        "min_grade": args.min_grade,
        "best": winners[0]["name"] if winners else None,
        "position_check": tour["position_picks"],
        "bouts": tour["bouts"],
        "candidates": [{k: v for k, v in r.items()
                        if k not in ("_small", "path")} for r in rows],
    }, indent=2, default=str))
    mp = write_metrics_json(arch, rows, ref, args)

    print()
    if winners:
        b = winners[0]
        print(f"BEST: {b['name']}  grade {b['grade']:.1f}  "
              f"(shape {b['shape']:.0f}, wrinkles {b['wrinkles']:.0f})")
        print(f"KEEP  {','.join(r['name'] for r in winners)}"
              f"  <- only these are eligible for --ship")
    else:
        print(f"NO SHIPPABLE CANDIDATE - nothing cleared {args.min_grade:.0f} "
              f"with construction intact")
    print(f"wrote {arch / 'grade_results.json'}")
    print(f"wrote {mp}   <- per-candidate verdicts, incl. construction")
    print(f"wrote {sheet}")
    C.log(args.run, f"graded {len(rows)} ({label}), {len(winners)} shippable")

    if args.ship:
        return ship(args, winners, rows)
    return 0 if winners else 2


def ship(args, winners: list[dict], rows: list[dict]) -> int:
    """Copy the top --ship candidates by grade to output/.

    Selection ignores status by default: the top N by grade ship whether or not
    stage 3 found altered construction. That is a deliberate operator decision,
    taken because a whole batch failing the gate left output/ empty and the run
    with nothing to show for ten paid-for images.

    What that costs, so it is not rediscovered later: the grade is 45% shape +
    45% wrinkles + 10% background and contains no construction term at all, so
    ranking by it says nothing about whether the garment was redrawn. A
    candidate that invented a seam can and does outrank one that did not - on
    the batch this was built for, the top-graded image had three altered
    regions and the least-altered was ranked sixth. Every MISMATCH is therefore
    printed per pick and written to steps.log, so a shipped defect is recorded
    rather than merely allowed.

    --ship-clean-only restores the gate.

    The picks are copied byte for byte. Only the cutout is a new file, and
    nothing is ever resampled on the way out.

    Cutouts are off unless --cutout is passed, so what lands in output/ is the
    generated flat on its own white plate. The retouch team then has no alpha
    channel to place against, which is a change to what the run delivers rather
    than a change to how it is produced - worth saying out loud in the log.
    """
    outd = args.run / "output"
    arch = args.run / "archive"

    if args.ship_clean_only:
        pool, basis = winners, "PASS only"
    else:
        # rows is sorted with REJECTs last; re-sort on grade alone so status
        # plays no part in who ships.
        pool, basis = sorted(rows, key=lambda r: -r["grade"]), "top by grade"
    picks = pool[:args.ship]
    if not picks:
        print(f"\n--ship {args.ship}: nothing to ship "
              f"({'the KEEP list is empty' if args.ship_clean_only else 'no candidates'}).")
        return 2

    outd.mkdir(parents=True, exist_ok=True)
    # Clear previous picks. Without this a second run leaves the first set
    # behind under different names and output/ no longer says what shipped.
    # This also sweeps cutouts from an earlier --cutout run, which would
    # otherwise sit in output/ looking like part of this delivery.
    for old in sorted(outd.glob("pick*.png")):      # includes _cutout.png
        old.unlink()

    print(f"\n--ship {args.ship}: writing {len(picks)} pick(s) to {outd}  ({basis})"
          + ("" if args.cutout else "  (flats only, no cutouts)"))
    shipped_bad = []
    for rank, r in enumerate(picks, 1):
        src = arch / f"{r['name']}.png"
        if not src.exists():
            print(f"  MISSING {src} - skipped")
            continue
        dst = outd / (f"pick{rank}_best_{r['name']}.png" if rank == 1
                      else f"pick{rank}_{r['name']}.png")
        shutil.copy2(src, dst)                      # untouched, full resolution
        im_w, im_h = _png_size(dst)
        print(f"  {dst.name:32} {im_w}x{im_h}  {dst.stat().st_size/1e6:.1f} MB  "
              f"grade {r['grade']:.1f}  {r['status']}")

        # The grade cannot see construction, so a pick can rank first and still
        # be a redrawn garment. Name the regions on the pick's own line - a
        # defect that only appears in the ranking table 40 lines up is a defect
        # nobody reads.
        mism = [c for c in r.get("construction", [])
                if c.get("verdict") == "MISMATCH"]
        if mism:
            shipped_bad.append((r["name"], [c["region"] for c in mism]))
            print(f"  {'':4}SHIPPED WITH ALTERED CONSTRUCTION: "
                  f"{', '.join(c['region'] for c in mism)}")
            for c in mism:
                print(f"  {'':6}{c['region']}: {c.get('detail','')[:150]}")

        # The cutout puts the garment on its own layer, so the retouch team sets
        # placement, canvas and plate themselves. That is why nothing in this
        # pipeline grades framing - and it still does not, so a pick shipped
        # without one carries whatever framing the generator chose.
        if not args.cutout:
            continue
        import cutout
        co = outd / f"{dst.stem}_cutout.png"
        try:
            info = cutout.cut(dst, co, feather=0.0, trim=False, pad=24)
            print(f"  {'  -> ' + co.name:32} {info['size'][0]}x{info['size'][1]}"
                  f"  {info['mb']:.1f} MB  transparent background")
        except Exception as e:  # noqa: BLE001 - a failed cutout must not lose the pick
            print(f"  {'  -> ' + co.name:32} FAILED: {e}")

    short = len(picks) < args.ship
    if short:
        rest = [r for r in rows if r not in picks]
        print(f"\nONLY {len(picks)} OF {args.ship} SHIPPED. "
              f"{len(rest)} more are already generated and paid for.")
        print("Next best, in order - each with what it would cost you:")
        for r in rest[:max(args.ship - len(picks) + 2, 4)]:
            # "grade N < mark" is already the note for a BELOW, so naming the
            # column "grade" too prints the number twice on the same line.
            why = [n for n in r["notes"] if not n.startswith("grade ")]
            print(f"  {r['name']:10} grade {r['grade']:6.1f}  {r['status']:<7} "
                  f"{'; '.join(why)[:70]}")
        print(f"Look at {arch.name}/grade_results.jpg, then copy the ones you "
              f"would defend into {outd.name}/ yourself and record in `## Notes` "
              f"what each carries. Do not generate more - if the batch failed "
              f"the same way repeatedly, the prompt is wrong and more draws buy "
              f"more of the same.")

    if shipped_bad:
        print(f"\n{len(shipped_bad)} of {len(picks)} shipped with construction "
              f"the vision check flagged as altered:")
        for name, regions in shipped_bad:
            print(f"  {name:10} {', '.join(regions)}")
        print(f"Per-region detail is in {arch.name}/grade_results.json. Say in "
              f"`## Notes` which picks carry this - the grade does not, and "
              f"output/ on its own cannot tell anyone.")

    flags = "".join([" (no cutouts)" if not args.cutout else "",
                     f" ({len(shipped_bad)} with altered construction)"
                     if shipped_bad else ""])
    C.log(args.run, f"shipped {len(picks)}{flags}: "
                    f"{','.join(r['name'] for r in picks)}")
    return 0 if not short else 2


def _png_size(p: Path) -> tuple[int, int]:
    out = subprocess.run(["magick", "identify", "-format", "%wx%h", str(p)],
                         check=True, capture_output=True, text=True).stdout
    w, _, h = out.partition("x")
    return int(w), int(h)


if __name__ == "__main__":
    sys.exit(main())
