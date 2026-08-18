#!/usr/bin/env python3
"""The only billed step. One call per image, run concurrently.

    python tools/generate.py --run runs/<stamp> --num 5 --resolution 2K

One image per call with its own seed, so each is an independent sample rather
than one batch the server draws together - which is what a shortlist needs.
Concurrency keeps the wave inside the harness's 900s bash timeout; ten serial
generations measured ~595s on this project.

Numbering continues from what is already in the folder, so topping up needs no
arguments. `--max-total` is a hard ceiling on images per run, enforced here
because task text is advisory and has been overridden on real runs.

Candidates are numbered by SUBMISSION order, so cand_03 is always the third seed
however the wave happens to land.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

import common as C

# The operator's ceiling on images per run folder. Set it here, or per run with
# LAYDOWN_MAX_IMAGES=8 ./run.sh ...  --max-total can lower it but never raise it,
# because the agent chooses that flag and task text has been overridden on four
# separate runs.
HARD_CAP = int(os.environ.get("LAYDOWN_MAX_IMAGES", "5"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--reference", type=Path, default=C.INPUTS / "reference_image.jpg")
    ap.add_argument("-n", "--num", type=int, default=10)
    ap.add_argument("--concurrency", type=int, default=5,
                    help="calls in flight. Lower if fal rate-limits.")
    ap.add_argument("--base-seed", type=int, default=1000,
                    help="image i uses base_seed + i, recorded in seeds.json")
    ap.add_argument("--seed", type=int,
                    help="exact seed for this call. With --num 1 this is how "
                         "you re-roll a specific draw, or deliberately keep one "
                         "while the prompt changes around it.")
    ap.add_argument("--resolution", default="4K", choices=["1K", "2K", "4K"])
    ap.add_argument("--aspect-ratio", default="3:4")
    ap.add_argument("--max-total", type=int, default=HARD_CAP,
                    help=f"images allowed per run folder, counting everything "
                         f"already on disk. Currently {HARD_CAP}. Cannot be "
                         f"raised above LAYDOWN_MAX_IMAGES ({HARD_CAP}) - that "
                         f"is the operator's ceiling, not the agent's.")
    ap.add_argument("--force", action="store_true",
                    help="generate anyway when the prompt looks incomplete")
    a = ap.parse_args()

    run = a.run
    arch = run / "archive"
    src = arch / "offset_upload.jpg"
    prompt_file = arch / "prompt.txt"
    if not src.exists():
        return print(f"Not found: {src}. Run prepare.py first.") or 1
    if not prompt_file.exists():
        return print(f"No {prompt_file}.\n"
                     f"prepare.py does not write one - look at both images, "
                     f"then write it yourself. See "
                     f"{arch / 'prompt_brief.md'}.") or 1
    if not a.reference.exists():
        return print(f"Not found: {a.reference}") or 1
    prompt = prompt_file.read_text()

    # The construction inventory rides on EVERY prompt, automatically. It is
    # measured from the cleaned source by a hosted vision model, and it is the
    # anchor against invented seams - especially its NOT-PRESENT section. It is
    # appended rather than left to the agent because instructions in this skill
    # have been skipped on four separate runs.
    # What gets injected depends on where the construction CAME from.
    #
    # From a spec sheet, both halves ship: it is authored ground truth, and its
    # negative statements are the strongest thing available - this project's
    # sheet says "No inseam with oval gusset", which is precisely the seam two
    # different VLMs invented when guessing from the photograph.
    #
    # From the photograph, only the NOT-PRESENT half ships. The positive list is
    # open-ended recall and fabricated on all five attempts, at both model
    # tiers; sending it under "reproduce exactly this" is what put a topstitched
    # seam down each leg of every candidate in a real run.
    desc_file = arch / "garment_description.md"
    if desc_file.exists():
        txt = desc_file.read_text()
        up = txt.upper()
        grounded = "spec sheet, transcribed" in txt[:200]
        absent = ""
        if "NOT PRESENT" in up:
            absent = txt[up.index("NOT PRESENT"):].split("\n", 1)[-1]
            absent = absent.split("**UNCERTAIN")[0].strip().lstrip("*- ")

        block = ["\n\n---"]
        if grounded and "**CONSTRUCTION**" in txt:
            spec = txt.split("**CONSTRUCTION**", 1)[1]
            spec = spec.split("**NOT PRESENT", 1)[0].strip()
            block += [
                "CONSTRUCTION SPEC, taken from this product's own spec sheet. "
                "This is what the garment is genuinely built with:", "", spec, "",
                "This lists what EXISTS on the garment, not what is visible from "
                "this side. Reproduce only the construction you can actually see "
                "in image 1 - do not add a seam or panel from this list that is "
                "not visibly there."]
        else:
            block += ["The garment has NO construction beyond what is visible in "
                      "image 1. Reproduce only the seams, panels and details you "
                      "can actually see - add no seam, panel line, topstitching "
                      "or feature that is not visibly present."]
        if absent:
            block += ["", f"It specifically does NOT have: {absent}"]
        prompt = prompt.rstrip() + "\n".join(block) + "\n"
        print(f"prompt carries construction "
              f"{'from the SPEC SHEET (authoritative)' if grounded else 'inferred from the photo (NOT-PRESENT only)'}"
              + (f"; absent: {absent[:60]}" if absent else ""))

    C.fix_ca_bundle()
    C.load_fal_key()
    import fal_client
    import requests

    print("Uploading both inputs once, reused by every call...")
    src_url = fal_client.upload_file(str(src))
    ref_url = fal_client.upload_file(str(a.reference))

    # Numbering continues from whatever is already here, so a top-up needs no
    # bookkeeping. Every image is a candidate: at one resolution there is no
    # such thing as a throwaway probe, and treating some as throwaway once
    # discarded the three best images of a run.
    cap = min(a.max_total, HARD_CAP)
    if a.max_total > HARD_CAP:
        print(f"--max-total {a.max_total} exceeds the operator ceiling of "
              f"{HARD_CAP}; using {HARD_CAP}. Raise LAYDOWN_MAX_IMAGES to "
              f"change it.")
    existing = sorted(int(p.stem.split("_")[1]) for p in arch.glob("cand_*.png"))
    have = len(existing)
    room = cap - have
    if room <= 0:
        print(f"Run already holds {have} image(s), at the --max-total ceiling "
              f"of {cap}. Measure and pick from those, or start a new "
              f"run folder.")
        return 1
    n = min(a.num, room)
    if n < a.num:
        print(f"Asked for {a.num}, but {have} of {cap} are already "
              f"here - generating {n}.")

    start = (max(existing) + 1) if existing else 1
    nums = list(range(start, start + n))
    if a.seed is not None:
        seeds = {i: a.seed + (i - start) for i in nums}
    else:
        seeds = {i: a.base_seed + i for i in nums}
    name = lambda i: arch / f"cand_{i:02d}.png"

    # Snapshot the prompt under its own hash. prompt.txt is a working file the
    # agent rewrites between calls, so without this a run cannot say which
    # wording produced which image - and answering that took reading file
    # mtimes on a real run. Identical prompts reuse one snapshot.
    ph = hashlib.sha256(prompt.encode()).hexdigest()[:8]
    snap = arch / f"prompt_{ph}.txt"
    if not snap.exists():
        snap.write_text(prompt)
        print(f"prompt {ph} ({len(prompt.split())} words) -> {snap.name}")
    else:
        print(f"prompt {ph} (unchanged)")

    def one(i: int):
        args = {"prompt": prompt, "image_urls": [src_url, ref_url],
                "num_images": 1, "output_format": "png",
                "resolution": a.resolution, "aspect_ratio": a.aspect_ratio,
                "seed": seeds[i]}
        for attempt in (1, 2):
            try:
                r = fal_client.subscribe(C.ENDPOINT, arguments=args, with_logs=False)
                items = r.get("images", [])
                if not items:
                    raise RuntimeError("no images returned")
                img = Image.open(requests.get(items[0]["url"], stream=True,
                                              timeout=300).raw).convert("RGB")
                return i, img, None
            except Exception as e:
                if attempt == 2:
                    return i, None, str(e)
                time.sleep(3)
        return i, None, "unreachable"

    print(f"{a.num} calls to {C.ENDPOINT} at {a.resolution} {a.aspect_ratio}, "
          f"{a.concurrency} at a time")
    got, t0 = [], time.time()
    with ThreadPoolExecutor(max_workers=max(1, a.concurrency)) as pool:
        futs = [pool.submit(one, i) for i in nums]
        for f in as_completed(futs):
            i, img, err = f.result()
            if err:
                print(f"  [{i:2}] FAILED twice: {err}", flush=True)
                continue
            p = name(i)
            img.save(p)
            got.append(i)
            print(f"  [{i:2}] {p.name}  {img.width}x{img.height}  "
                  f"+{time.time()-t0:.0f}s", flush=True)

    # Merge rather than overwrite: a split batch calls this twice, and the
    # second call must not erase the first call's records.
    #
    # Resolution is recorded per image because it decides eligibility later. A
    # probe generated at the SAME resolution as the final batch is a candidate
    # in every respect - same model, same prompt, same pixels - and discarding
    # it because of its filename threw away the three best images of a real run.
    sf = arch / "seeds.json"
    try:
        rec = json.loads(sf.read_text()) if sf.exists() else {}
    except json.JSONDecodeError:
        rec = {}
    rec.update({name(i).stem: {"seed": seeds[i], "resolution": a.resolution,
                               "prompt": ph} for i in sorted(got)})
    sf.write_text(json.dumps(rec, indent=2))

    cents = len(got) * (C.PRICE_4K if a.resolution == "4K" else 0.15) * 100
    total = have + len(got)
    print(f"\n{len(got)}/{n} in {time.time()-t0:.0f}s   "
          f"run now holds {total}/{cap}")
    if len(got) < n:
        print("  WARNING: short. Do NOT re-run to top up - every image already "
              "on disk is already billed. Measure what landed.")
    C.log(run, f"generated {len(got)} at {a.resolution}, prompt {ph} "
               f"({total} total)", cents)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
