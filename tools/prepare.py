#!/usr/bin/env python3
"""Step 1 - check the inputs and make the upload copy. Writes NO prompt.

    python tools/prepare.py

The prompt is the agent's job, deliberately. This script used to carry a
hardcoded prompt plus a lookup table asserting what each garment type looks
like - so the wording was fixed at authoring time by someone who had not seen
the images. That table described a bralette's neckline and straps while the
actual input was a pair of leggings.

It now does only what is mechanical: confirm both inputs, report what they are,
write the downscaled copy that gets uploaded, and leave a brief of the clauses
the prompt must cover. The agent looks at the two images, then writes
archive/prompt.txt itself.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from PIL import Image

import common as C

BRIEF = """\
# Prompt brief

Look at these two, with `compare_images`, BEFORE writing anything:

    {run}/archive/offset_upload.jpg     <- the product, CLEANED
    {ref_path}                          <- the lay reference

**Use the upload, never `inputs/off_set_image.jpg`.** The upload is the only
image the model receives: tag erased, background dropped, plate white. The raw
input still has the hang tag and a real-world background, and a run that
described it asked for the tag to "stay in place" - all four candidates then
grew a tag that was not in the image sent.

Then write `{run}/archive/prompt.txt` with a bash heredoc, covering every clause
below in your own words, describing what you actually saw.

Do not copy this file. It is a checklist, not a prompt.

## Ask for ONE thing: re-lay the garment. Change nothing else.

The deliverable is a cutout on a transparent background - the retouch team sets
placement, scale, canvas and plate afterwards. **Do not ask for centring,
margins, scale or framing.** Nothing measures them, and every clause you spend
on them is a clause the model spends repainting a product that was already
right.

## Must cover

1. **Which image is which.** Image 1 is the product photo, image 2 the lay
   reference. The model reads them in that order.
2. **Everything visual comes from image 1** - colourway, fabric, texture, print,
   seams, topstitching, hardware. Invent nothing not visible in image 1.

   **The construction inventory already exists** at
   `archive/garment_description.md`, and `generate.py` appends it to every
   prompt automatically. When `inputs/Design_BOM.png` is present it is
   transcribed from that spec sheet and is authoritative; otherwise it is
   inferred from the photo and only its NOT-PRESENT half is sent, because the
   positive half fabricates.

   **Do not describe seams yourself.** Two different vision models, asked what
   this garment has, both invented a seam running down each leg - the spec
   sheet says "No inseam". Your prompt needs one line saying the construction is
   reproduced exactly as specified and nothing is added; the inventory does the
   rest.

   **If you do describe construction yourself, be specific.** A generic "keep the seams" is weak;
   walk the garment and list what you can actually see, with where it sits and
   how it is stitched - the waistband join and whether it is topstitched, each
   panel line, pocket openings and their edges, the centre-front and inseam
   seams, the coverstitch at each hem, any elastic edge, gusset or logo. Say
   they must stay **sharp, continuous, and the same colour and stitch type as
   in image 1** - tonal thread stays tonal, and no seam may be smoothed away,
   softened, doubled or moved. This is the clause that decides whether the
   product survives, and it is the first thing lost when a model is told to
   smooth a garment.
3. **Image 2 shows only how the garment should be ARRANGED.** {ref_note} It is
   not a shape, colour, fabric or framing reference. If it is built differently
   from image 1 - a different number of straps, closures or panels - ignore all
   of that. The output has exactly the parts visible in image 1, no more and no
   fewer.
4. **The lay itself - this is the actual job.** Name what is untidy in image 1
   and what square looks like for THIS garment: legs parallel and closed rather
   than splayed, straps flat and symmetric, hems level, no twists or folds.
5. **Flatness.** It stays laid flat as in image 1. No volume, body, 3D shaping,
   draping or a worn look. The model adds volume unless told not to.
6. **Wrinkle-free, always.** The garment must read as freshly steamed and
   pressed - no creases, no rumples, no fold lines, no puckering, no shadows
   cast by folds. Say this explicitly every time; it does not happen by
   default.

   Be precise about what stays, or the model smooths the product away with the
   creases: **seams, topstitching, panel lines, pockets, elastic edges, the
   waistband join and the fabric's own knit or weave all remain, sharp.** Only
   the temporary creases from handling go.
7. **Proportions unchanged.** The garment keeps image 1's real dimensions - the
   same length, the same waistband or band width. Closing the lay must not
   stretch or slim the product.
8. **Background: it is already clean. Keep it.** Image 1 has been pre-cleaned
   before you ever see it - a segmentation model dropped the real-world
   background and plated it pure white, and an eraser removed the hang tag and
   any pins. There is nothing left to remove.

   So ask only that the background **stays** a plain, even, seamless WHITE
   studio plate, and that nothing is added to it.

   **Never ask for a transparent or removed background.** The model cannot
   output an alpha channel, so it paints a transparency checkerboard into the
   pixels instead - a real run came back with a literal grey-and-white checker
   pattern, which counted as 1587-3135 background specks and drove every score
   below -400. Transparency is produced locally afterwards by `cutout.py`.
   `generate.py` refuses a prompt that asks for one.

   **Do not ask for the tag to be removed either** - it is already gone, and
   naming it invites the model to invent one to erase.
9. **Nothing cropped.** The whole garment stays inside the frame with clear space
   on every side. This is the one framing fault that matters: a clipped hem or
   strap tip cannot be retouched back.

## Inputs as measured

- off-set:    {off}
- reference:  {ref}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--off-set", type=Path, default=C.INPUTS / "off_set_image.jpg")
    ap.add_argument("--reference", type=Path, default=C.INPUTS / "reference_image.jpg")
    ap.add_argument("--upload-long-side", type=int, default=4096,
                    help="long side of the copy that gets uploaded")
    ap.add_argument("--no-clean", dest="clean", action="store_false",
                    help="skip the automatic tag/background pre-clean")
    ap.add_argument("--out", type=Path, help="run folder (default runs/<stamp>)")
    a = ap.parse_args()

    for p in (a.off_set, a.reference):
        if not p.exists():
            return print(f"Not found: {p}") or 1

    # One folder per session. Calling this twice returns the SAME folder rather
    # than a fresh one, so the image budget cannot be reset by starting over.
    # --out is ignored while a session is active. A run spent three turns
    # creating its own folder, writing a prompt into it, being refused by
    # generate.py, and redoing the work in the right place.
    run = C.session_run_dir()
    if a.out and a.out.resolve() != run.resolve():
        import os
        if os.environ.get("LAYDOWN_SESSION"):
            print(f"Ignoring --out {a.out}: this session's folder is {run}. "
                  f"One run means one folder.")
        else:
            run = a.out
    existed = (run / "archive").exists()
    (run / "archive").mkdir(parents=True, exist_ok=True)
    (run / "output").mkdir(parents=True, exist_ok=True)
    if existed:
        n = len(list((run / "archive").glob("cand_*.png")))
        print(f"REUSING the existing run folder for this session "
              f"({n} image(s) already generated). Nothing was reset.")

    off, ref = Image.open(a.off_set), Image.open(a.reference)
    off_desc = f"{a.off_set.name} {off.width}x{off.height} mode={off.mode}"
    ref_desc = f"{a.reference.name} {ref.width}x{ref.height} mode={ref.mode}"
    print(f"off-set    {off_desc}")
    print(f"reference  {ref_desc}")
    bom = C.INPUTS / "Design_BOM.png"
    print(f"spec sheet {bom.name + ' ' + str(Image.open(bom).size) if bom.exists() else 'NONE - construction will be inferred from the photo, which fabricates'}")

    if ref.mode == "L":
        note = ("The reference is GREYSCALE - say so explicitly and tell the "
                "model to ignore its tone completely. Without that clause it "
                "reads the grey as a colour target and desaturates the "
                "garment.")
        print("  reference is greyscale - the prompt MUST tell the model to "
              "ignore its tone")
    else:
        note = ("The reference is in colour, so say plainly that the colour "
                "still comes from image 1 and none of it from image 2.")
        print("  NOTE: reference is not greyscale - colour may bleed from it")

    # Pre-clean automatically: erase tags and pins, drop the background, plate
    # white. This is deterministic pipeline work, not a judgement call, and it
    # removes two whole jobs from the re-lay prompt. Measured on this project's
    # own source: tag and clip erased, bench and shoes gone, garment colour
    # drift 0.4, full 3072x4096 preserved.
    up_path = run / "archive" / "offset_upload.jpg"
    if a.clean:
        import subprocess
        r = subprocess.run([sys.executable, str(Path(__file__).with_name("clean.py")),
                            "--run", str(run), "--off-set", str(a.off_set),
                            "--long-side", str(a.upload_long_side)],
                           capture_output=True, text=True)
        print(r.stdout.rstrip() or r.stderr.rstrip())
        if r.returncode != 0 or not up_path.exists():
            print("  pre-clean failed; falling back to a plain downscaled copy.")
            a.clean = False
    if not a.clean:
        up = off.convert("RGB")
        up.thumbnail((a.upload_long_side, a.upload_long_side), Image.LANCZOS)
        up.save(up_path, quality=95, subsampling=0,
                icc_profile=off.info.get("icc_profile"))
        print(f"upload copy  {up.width}x{up.height}  "
              f"{up_path.stat().st_size/1e6:.1f} MB  (NOT pre-cleaned)")

    # Inventory the construction from the CLEANED image. generate.py appends
    # this to every prompt, so each draw is anchored to one written spec rather
    # than to whatever the re-lay model infers - which is where invented seams
    # and lost topstitching have come from.
    desc = run / "archive" / "garment_description.md"
    if a.clean and not desc.exists():
        import subprocess
        r = subprocess.run([sys.executable, str(Path(__file__).with_name("describe.py")),
                            "--run", str(run)], capture_output=True, text=True)
        print(r.stdout.rstrip() or r.stderr.rstrip())
        if not desc.exists():
            print("  no construction inventory; prompts will not carry one.")

    (run / "archive" / "prompt_brief.md").write_text(
        BRIEF.format(run=run, ref_note=note, off=off_desc, ref=ref_desc,
                    ref_path=a.reference))
    print("brief        archive/prompt_brief.md - 9 clauses the prompt must cover")
    print("NO prompt written. Look at both images, then write archive/prompt.txt.")

    w, h = Image.open(up_path).size
    C.log(run, f"prepared, upload {w}x{h}{'' if a.clean else ' (not cleaned)'}")
    print(f"\nRUN_DIR={run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
