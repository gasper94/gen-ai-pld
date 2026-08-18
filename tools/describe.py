#!/usr/bin/env python3
"""Inventory the garment's construction, so the re-lay model cannot invent any.

    python tools/describe.py --run runs/<stamp>

Runs a hosted vision model over the CLEANED source and writes
`archive/garment_description.md`. `generate.py` then appends it to every prompt
automatically, so each draw is anchored to the same written inventory rather
than to whatever the re-lay model infers from the pixels that pass.

Why this exists: invented and lost construction has been the most persistent
failure on this project - a candidate growing topstitching the product does not
have, or smoothing away a seam it does. A model told "there are exactly two
pockets, no zip, no drawcord" has far less room to hallucinate than one told
"keep the construction".

The negative inventory is the important half. Listing what is ABSENT suppresses
invention more reliably than listing what is present.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import common as C

ENDPOINT = "openrouter/router/vision"

# Transcription beats inference. Five attempts at getting a positive seam list
# out of a VLM looking at a photograph all fabricated - gemini invented an
# inseam and a side seam, claude invented seven panels and a seam down each leg
# - because a model asked what a garment has answers from what that CATEGORY
# usually has. A spec sheet removes the guess: this one states "No inseam with
# oval gusset", which is exactly the seam both models drew.
BOM_ASK = """\
This is a garment construction spec sheet. Transcribe EVERY callout verbatim,
one per line, exactly as written - stitch types, needle counts, seam allowances
and all.

Then output two sections:

**CONSTRUCTION** - the callouts rewritten as a plain list of what the garment
is built with, keeping the stitch specification for each.

**NOT PRESENT** - a comma-separated list of construction this sheet says the
garment does NOT have. Read the callouts carefully: a note reading "no inseam"
means the garment has no inseam. Add any standard feature the sheet would have
called out and did not.

Transcribe and read only. Infer nothing that is not written on the sheet.
"""

SYSTEM = ("You are a technical garment analyst writing a construction spec for "
          "a photo retoucher. You describe ONLY what is visible in this single "
          "photograph. You are looking at one side of the garment and cannot "
          "see the other - never mention a back seam, back panel, back yoke or "
          "back pocket unless it is genuinely visible in this frame. You never "
          "infer construction from what garments of this type usually have. You "
          "never describe the background, the framing or the lay.")

# The candidate vocabulary is DERIVED per garment, not hardcoded. A fixed list
# is legwear wearing a disguise: "side seam down the leg" is meaningless on a
# bra, and at scale every category would need its own hand-written list. So the
# model is asked what features garments of THIS type commonly have, and then
# adjudicates its own list against the actual photograph.
VOCAB_ASK = """\
This is a flat product photograph of a single garment.

Name the garment type in three words. Then list 30 to 40 CONSTRUCTION FEATURES
that garments of this type commonly have - closures, seams by location, panels,
pockets, trims, hardware, applied branding, edge finishes. Include the ordinary
and the optional. Do not look at whether this particular garment has them; you
are building a checklist for the category.

Output only a comma-separated list on one line, nothing else.
"""

# A minimal fallback if the vocabulary call fails. Deliberately garment-neutral.
FALLBACK = ["zip or zipper", "buttons", "snaps", "drawcord or drawstring",
            "elastic cord or toggle", "mesh or ventilation panels",
            "sheer panels", "contrast piping or binding",
            "contrast topstitching in another colour", "reflective trim",
            "colour blocking", "printed graphic", "embroidery",
            "external brand logo or wordmark", "visible label or tab outside",
            "seam taping", "gusset", "vents or slits", "ruching or gathering",
            "visible lining", "buckles or hardware", "grommets or eyelets",
            "cut-out detail", "pockets", "cuffs or ribbed trims"]

ASK = """\
This is a flat product photograph of a single garment on a white background.

## Part 1 - what it has

Terse markdown, only what you can actually see:

**Garment** - what it is, in three words.
**Colour and fabric** - colourway, and visible character (smooth knit, ribbed,
brushed, matte, sheen).
**Seams** - only seams you can actually SEE as a line in this image. Say where
each runs and how it is finished. **Do not list a seam because garments of this
type normally have one** - a run listed an inseam and a side seam on leggings
whose legs are visibly unbroken. If you are not looking straight at it, leave it
out.
**Panels** - how many are visible, and where the divisions fall. Count only
panels you can see in this frame.
**Openings and pockets** - how many, exactly where, how each is finished.
**Bands, waistband, cuffs, hems** - construction and depth, and whether
topstitched.
**Applied elements** - logos, labels, prints, hardware, and where. If none, say
none.

## Part 2 - NOT PRESENT

Go through this list one item at a time and decide for each whether it is
visible on THIS garment. Then output, under the heading `**NOT PRESENT**`, a
single comma-separated list naming every item that is absent. Do not skip items,
do not summarise, and do not add anything to that list that you can actually
see.

If an item is genuinely ambiguous, leave it out of the NOT PRESENT list and note
it under `**UNCERTAIN**` instead.

Items to adjudicate:
{checklist}

You are seeing ONE side of this garment. Do not describe or infer the side
facing away from the camera. If you cannot tell whether a seam continues around
the other side, say only what you can see.

Do not describe the background, framing, pose, wrinkles, or how the garment is
laid out. Construction only.
"""

# Terms unambiguous enough that appearing in BOTH halves is a real contradiction.
# Pocket TYPES are excluded on purpose - a side-seam pocket is legitimately
# "not a patch pocket, not a welt pocket", so those co-occur harmlessly.
CONTRADICTION_TERMS = [
    "zip", "button", "snap", "drawcord", "drawstring", "belt loop", "mesh",
    "sheer", "stripe", "piping", "reflective", "embroidery", "logo", "hood",
    "collar", "thumbhole", "gusset", "lining", "grommet", "buckle",
]


def vocabulary(image: Path, model: str) -> tuple[list[str], float]:
    """Ask what features THIS category of garment commonly has."""
    import fal_client
    r = fal_client.subscribe(ENDPOINT, arguments={
        "image_urls": [fal_client.upload_file(str(image))],
        "prompt": VOCAB_ASK, "model": model,
        "temperature": 0.3, "max_tokens": 600}, with_logs=False)
    out = (r.get("output") or "").strip()
    cost = float((r.get("usage") or {}).get("cost") or 0.0)
    items = [x.strip(" .-") for x in out.replace("\n", ",").split(",")]
    items = [x for x in items if 3 < len(x) < 70]
    return (items or FALLBACK), cost


def describe(image: Path, model: str, vocab: list[str]) -> tuple[str, float]:
    import fal_client
    r = fal_client.subscribe(ENDPOINT, arguments={
        "image_urls": [fal_client.upload_file(str(image))],
        "prompt": ASK.format(checklist="\n".join(f"- {i}" for i in vocab)),
        "system_prompt": SYSTEM,
        "model": model, "temperature": 0.2, "max_tokens": 1500,
    }, with_logs=False)
    text = (r.get("output") or "").strip()
    cost = float((r.get("usage") or {}).get("cost") or 0.0)
    return text, cost


def from_bom(bom: Path, model: str) -> tuple[str, float]:
    """Read the construction off the spec sheet rather than guessing it."""
    import fal_client
    r = fal_client.subscribe(ENDPOINT, arguments={
        "image_urls": [fal_client.upload_file(str(bom))],
        "prompt": BOM_ASK, "model": model,
        "temperature": 0, "max_tokens": 1500}, with_logs=False)
    return (r.get("output") or "").strip(), \
        float((r.get("usage") or {}).get("cost") or 0.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", type=Path)
    ap.add_argument("--image", type=Path,
                    help="default <run>/archive/offset_upload.jpg, i.e. the "
                         "CLEANED source - describing the dirty original would "
                         "inventory the hang tag as construction")
    ap.add_argument("--model", default="google/gemini-2.5-flash")
    ap.add_argument("--bom", type=Path,
                    help="construction spec sheet. Defaults to "
                         "inputs/Design_BOM.png when it exists. When present it "
                         "is the AUTHORITY and the photo is not asked to supply "
                         "construction at all.")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()

    run = a.run or C.session_run_dir()
    img = a.image or run / "archive" / "offset_upload.jpg"
    if not img.exists():
        return print(f"Not found: {img}. Run prepare.py first.") or 1
    out = a.out or run / "archive" / "garment_description.md"

    C.fix_ca_bundle()
    C.load_fal_key()

    bom = a.bom if a.bom is not None else C.INPUTS / "Design_BOM.png"
    if bom.exists():
        print(f"reading spec  {bom.name} via {ENDPOINT} ({a.model})  "
              f"<- AUTHORITATIVE, not inferred from the photo")
        text, cost = from_bom(bom, a.model)
        if text:
            out.write_text(f"<!-- source: {bom.name} (spec sheet, transcribed) "
                           f"-->\n\n" + text + "\n")
            words = len(text.split())
            up = text.upper()
            print(f"              -> {out.name}  {words} words  ${cost:.4f}")
            if "NOT PRESENT" not in up:
                print("  WARNING: the sheet yielded no NOT-PRESENT section.")
            C.log(run, f"construction from spec sheet, {words} words", cost * 100)
            return 0
        print("  spec sheet yielded nothing; falling back to the photograph.")

    print(f"describing    {img.name} via {ENDPOINT} ({a.model})  "
          f"<- INFERRED from the photo, no spec sheet found")
    vocab, c1 = vocabulary(img, a.model)
    print(f"              category checklist: {len(vocab)} features derived "
          f"for this garment type")
    text, c2 = describe(img, a.model, vocab)
    cost = c1 + c2
    if not text:
        return print("The vision model returned nothing.") or 1

    out.write_text(text + "\n")
    words = len(text.split())
    print(f"              -> {out.name}  {words} words"
          + (f"  ${cost:.4f}" if cost else ""))
    up = text.upper()
    if "NOT PRESENT" not in up:
        print("  WARNING: no NOT-PRESENT section. That is the half that "
              "suppresses invention; re-run before generating.")
    else:
        tail = text[up.index("NOT PRESENT"):]
        named = sum(1 for i in vocab
                    if i.split(" or ")[0].split(",")[0].lower() in tail.lower())
        print(f"              NOT-PRESENT list covers {named}/{len(vocab)} "
              f"derived features")
        if named < len(vocab) * 0.4:
            print("  WARNING: the model adjudicated fewer than half the "
                  "checklist. The absent list is the anchor against invention.")

        # A feature named in both halves would tell the re-lay model to remove
        # something the garment actually has - worse than inventing one.
        head = text[:up.index("NOT PRESENT")].lower()
        clash = [t for t in CONTRADICTION_TERMS
                 if t in head and t in tail.lower()]
        if clash:
            print(f"  WARNING: named as present AND absent: {', '.join(clash)}. "
                  f"Fix {out.name} by hand before generating - a false absence "
                  f"tells the model to delete real construction.")
    C.log(run, f"described garment, {words} words", cost * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
