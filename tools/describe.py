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
import json
import re
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
#
# Kept for the headline warning only. The real check is audit_absent() below,
# which is derived from the document rather than from this list - a hand-written
# vocabulary only catches the contradictions somebody thought of, and the one
# that cost this project was "Pearl embellishments", which is not on it.
CONTRADICTION_TERMS = [
    "zip", "button", "snap", "drawcord", "drawstring", "belt loop", "mesh",
    "sheer", "stripe", "piping", "reflective", "embroidery", "logo", "hood",
    "collar", "thumbhole", "gusset", "lining", "grommet", "buckle",
]

# Attributes step 0 measured that name construction, and are specific enough to
# contradict a NOT-PRESENT claim. Deliberately not garment_type - "bra" is three
# letters and substring-matches half the language - and deliberately not the
# boolean fields, which are only used when TRUE: `adjusters: false` makes
# "Adjustable straps" a CORRECT absence, and dropping it would be the mistake
# this function exists to prevent, in reverse.
ATTR_FIELDS = ("strap_style", "closure", "neckline", "padding", "band",
               "support_level", "coverage")

# Words too common to carry meaning inside a garment feature name.
STOP = {"the", "and", "with", "for", "this", "that", "from", "into", "onto",
        "detail", "details", "style", "styles", "type", "types", "fabric",
        "front", "back", "side", "outer", "inner", "left", "right", "medium",
        "standard", "smooth", "matte", "wide", "unknown", "none", "true",
        "false", "visible", "small", "large"}

# Properties of a textile that no photograph can show. A model asked to
# adjudicate them answers from the category, not the image, so "does NOT have
# moisture-wicking fabric" is a guess dressed as an observation - and it is
# being sent to an image generator, which cannot draw the absence of wicking
# either way. Noise, not signal, so it does not travel.
UNSEEABLE = ("wicking", "moisture", "breathable", "compression", "stretch",
             "quick dry", "quick-dry", "antimicrobial", "anti-odor",
             "anti-odour", "uv protection", "spf", "recycled", "sustainable",
             "seamless construction")


# Phrases that assert which way round the garment is, rather than what it is
# built from. The describe pass is looking at one photograph and guessing:
# runs/20260820_112558 came back "two visible seams on the upper back" and "two
# back panels forming the criss-cross straps" for a garment photographed front
# up. Read on its own that is a harmless mistake; copied into a prompt it is an
# instruction, and seven of that run's ten candidates came back showing the
# reverse face.
#
# Location words are NOT here on purpose. "Back panel" describes a panel that
# exists whichever side is up, and stripping it would throw away real
# construction - the half of this file that suppresses invention. Only a claim
# about the VIEWPOINT travels badly, so only a claim about the viewpoint goes.
ORIENTATION = ("shown from", "viewed from", "seen from", "from the back",
               "from the front", "from behind", "back view", "front view",
               "rear view", "reverse side", "inside out", "wrong side",
               "we are looking at", "this is the back", "this is the front",
               "the interior is visible", "facing away")


def strip_orientation(text: str) -> tuple[str, list[str]]:
    """Remove sentences that claim which face of the garment is toward the
    camera. Returns (text, removed)."""
    kept, removed = [], []
    for line in text.splitlines():
        # Sentence-wise, so one bad clause does not cost a whole bullet of
        # genuine construction. Splitting on '. ' keeps the markdown intact.
        parts = re.split(r"(?<=\.)\s+", line)
        good = [p for p in parts if not any(o in p.lower() for o in ORIENTATION)]
        removed += [p.strip() for p in parts if p not in good and p.strip()]
        kept.append(" ".join(good) if good else "")
    return "\n".join(kept), removed


def _words(s: str) -> list[str]:
    """Lowercase alphabetic tokens, crudely singularised."""
    out = []
    for w in re.findall(r"[a-z]+", str(s).lower()):
        out.append(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return out


def _squash(s: str) -> str:
    return "".join(re.findall(r"[a-z]+", str(s).lower()))


def _phrase_in(item: str, text: str) -> bool:
    """Is `item` present in `text` as a contiguous phrase, ignoring plurals?

    Contiguous, not word-by-word: a document that says "shoulder straps" and
    "armhole seams" contains both words of "shoulder seams" and claims neither.
    """
    a, b = _words(item), _words(text)
    a = [w for w in a if w not in STOP]
    if not a:
        return False
    return any(b[i:i + len(a)] == a for i in range(len(b) - len(a) + 1))


def audit_absent(text: str, attrs: dict | None = None) -> dict:
    """Which NOT-PRESENT claims are safe to send to the generator.

    The negative inventory is the half that suppresses invention, and it is
    also the half that can do the most damage: every item on it becomes "the
    garment specifically does NOT have this" in a prompt, and a false entry
    tells an image model to delete real construction.

    On runs/20260819_205617 the list named `Pearl embellishments` while Part 1
    of the same document described two pearl embellishments on the straps and
    two more on the band; it named `Side seams` while Part 1 located the pearls
    "at the bottom of the side seams"; and it named `Racerback straps` and
    `Pullover style` for a garment step 0 had measured as strap_style
    'racerback', closure 'pullover'. All four were sent.

    Three ways an item is dropped, each recorded with its reason:
      * the positive half of the same document says it IS there
      * step 0 measured an attribute that says it is there
      * no photograph could show it either way (fabric performance claims)

    Nothing is added and nothing is rewritten - the model's own text stays as
    written. This only decides what travels.
    """
    up = text.upper()
    if "NOT PRESENT" not in up:
        return {"present": text, "absent": [], "keep": [], "dropped": []}
    head = text[:up.index("NOT PRESENT")]
    tail = text[up.index("NOT PRESENT"):].split("\n", 1)[-1]
    # Stop at the UNCERTAIN section and at this function's own audit comment -
    # re-reading a file it has already annotated must not parse the annotation
    # back in as more claims.
    tail = tail.split("**UNCERTAIN")[0].split("<!--")[0].strip().lstrip("*- ")
    items = [i.strip(" .-\n*") for i in tail.split(",")]
    items = [i for i in items if 2 < len(i) < 80]

    tokens = {}
    for f in ATTR_FIELDS:
        v = (attrs or {}).get(f)
        if not isinstance(v, str):
            continue
        for w in re.findall(r"[a-z]+", v.lower()):
            if len(w) >= 5 and w not in STOP:
                tokens[w] = f

    keep, dropped = [], []
    for it in items:
        sq = _squash(it)
        why = None
        if _phrase_in(it, head):
            why = "the description's own Part 1 says it IS there"
        elif any(t in sq for t in tokens):
            t = next(t for t in tokens if t in sq)
            why = f"step 0 measured {tokens[t]} = '{(attrs or {}).get(tokens[t])}'"
        elif any(u in it.lower() for u in UNSEEABLE):
            why = "not visible in a photograph, so it was never adjudicated"
        (dropped.append((it, why)) if why else keep.append(it))
    return {"present": head, "absent": items, "keep": keep, "dropped": dropped}


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


def query_attrs(run: Path) -> dict:
    """What step 0 measured about this garment, if it ran."""
    f = Path(run) / "reference_selection.json"
    try:
        return json.loads(f.read_text()).get("query_attrs") or {}
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return {}


def report_absent(out: Path, text: str, attrs: dict,
                  orientation: list[str] | None = None) -> dict:
    """Print the audit and record it in the file.

    NOT-PRESENT claims that are contradicted stay visible in the document - it
    is the record of what the model actually said - but they are listed in a
    comment as not sent, so reading the file and reading the prompt give the
    same answer. generate.py runs the same audit and sends only what survives.

    Orientation claims are different and are REMOVED from the body before it is
    written, not just withheld: the consumer of this file is a person or an
    agent writing a prompt from it, and "shown from the back" does its damage by
    being read, not by being appended. They are listed here so the removal is
    auditable.
    """
    a = audit_absent(text, attrs)
    lines = []
    if orientation:
        print(f"  REMOVED {len(orientation)} orientation claim(s) - which face "
              f"is toward the camera is not construction, and copied into a "
              f"prompt it flips the garment:")
        for s in orientation:
            print(f"    {s[:100]}")
        lines += [f"     orientation removed: {s}" for s in orientation]
    if a["absent"]:
        print(f"              NOT-PRESENT: {len(a['keep'])} of {len(a['absent'])} "
              f"claims will be sent")
        if a["dropped"]:
            print("  DROPPED as unsafe to send - a false absence tells the model "
                  "to delete real construction:")
            for item, why in a["dropped"]:
                print(f"    {item:<32} {why}")
            lines += [f"     {i} - {w}" for i, w in a["dropped"]]
    if lines:
        note = ("\n\n<!-- audit: removed from the body, or withheld from the "
                "generator\n" + "\n".join(lines) + "\n-->\n")
        out.write_text(out.read_text().rstrip() + note)
    return a


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
            text, orient = strip_orientation(text)
            out.write_text(f"<!-- source: {bom.name} (spec sheet, transcribed) "
                           f"-->\n\n" + text + "\n")
            words = len(text.split())
            up = text.upper()
            print(f"              -> {out.name}  {words} words  ${cost:.4f}")
            if "NOT PRESENT" not in up:
                print("  WARNING: the sheet yielded no NOT-PRESENT section.")
            # A transcribed sheet is authoritative, but a transcription can
            # still contradict itself or the garment step 0 measured, and the
            # safe direction is the same either way: do not tell the model to
            # remove something that may be there.
            report_absent(out, text, query_attrs(run), orient)
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

    text, orient = strip_orientation(text)
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
            print(f"  WARNING: named as present AND absent: {', '.join(clash)}.")

        # The item-by-item audit. This is the one that acts: what it drops is
        # not sent, so the file no longer has to be fixed by hand before
        # generating - which was the previous instruction, and was never done.
        report_absent(out, text, query_attrs(run), orient)
    C.log(run, f"described garment, {words} words", cost * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
