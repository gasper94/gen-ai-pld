#!/usr/bin/env python3
"""Step 0 - choose the lay reference, deterministically, before the agent runs.

    python tools/select_reference.py --run runs/20260817_140159

Picking the reference used to be a human step: look at the off-set photo, find
the closest garment in library_reference/, desaturate it, drop it in inputs/.
This does exactly that, and does it the same way every time.

It is two pieces:

  match_reference.py   scores every library image against the query and returns
                       ONE winner or an honest "no match". It is the judgement.
  this file            installs that winner - greyscale, full resolution - at
                       the single path the pipeline reads, records where it came
                       from, and gets out of the way. It is the plumbing.

Two things it insists on, both because of how the rest of the pipeline behaves:

  * GREYSCALE. prepare.py branches on the reference's mode: `L` makes the brief
    tell the model to ignore the reference's tone, anything else warns that
    colour may bleed from it. The reference is a shape and construction
    reference only, so it is desaturated here rather than being left as a
    second, competing colour source. --colour opts out.
  * ONE reference at a time. The agent reads its inputs off a fingerprinted
    inventory, and two files called reference-something are two candidates. Any
    other reference*.jpg in inputs/ is moved aside into inputs/others/ (moved,
    not deleted) unless --no-stash.

Exit codes:  0 installed   2 no match, nothing installed   1 broke
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image

import common as C

Image.MAX_IMAGE_PIXELS = None

MATCHER = Path(__file__).with_name("match_reference.py")

# The one path the pipeline reads. prepare.py takes --reference explicitly and
# the SKILL tells the agent to pass it, so this name only has to be stable, not
# guessed - but it stays the name the workspace has always used, so an old
# command line still works.
CANON = "reference_greyscale.jpg"


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def stash_other_references(inputs: Path, keep: str) -> list[str]:
    """Move any other reference*.* out of inputs/ and into inputs/others/.

    Moved, never deleted: one of these is usually the hand-picked reference from
    a previous garment, and it is the operator's file, not ours.
    """
    others = inputs / "others"
    moved = []
    for p in sorted(inputs.glob("reference*")):
        if not p.is_file() or p.name == keep:
            continue
        others.mkdir(parents=True, exist_ok=True)
        dst = others / p.name
        n = 1
        while dst.exists():
            dst = others / f"{p.stem}_prev{n}{p.suffix}"
            n += 1
        shutil.move(str(p), str(dst))
        moved.append(f"{p.name} -> others/{dst.name}")
    return moved


def install(src: Path, dst: Path, greyscale: bool) -> tuple[bool, str]:
    """Write the winner to dst. Returns (changed, description).

    Skips the write when the bytes would be identical, so a re-run does not
    churn the mtime - the harness fingerprints inputs by md5 and an mtime that
    moves for no reason reads as a changed input.
    """
    with Image.open(src) as im:
        out = im.convert("L") if greyscale else im.convert("RGB")
    # Hidden, so a temp file left behind by a crash is neither stashed as a
    # stray reference nor listed in the workspace inventory.
    tmp = dst.with_name(f".{dst.stem}.tmp.jpg")
    out.save(tmp, quality=95, subsampling=0)
    changed = not (dst.exists() and md5(dst) == md5(tmp))
    if changed:
        tmp.replace(dst)
    else:
        tmp.unlink()
    with Image.open(dst) as check:
        desc = f"{check.width}x{check.height} mode={check.mode}"
    return changed, desc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--query", type=Path, default=C.INPUTS / "off_set_image.jpg",
                    help="the off-set photo the reference has to match")
    ap.add_argument("--library", type=Path, default=C.ROOT / "library_reference")
    ap.add_argument("--inputs", type=Path, default=C.INPUTS,
                    help="folder the chosen reference is installed into")
    ap.add_argument("--name", default=CANON,
                    help=f"filename to install as (default {CANON})")
    ap.add_argument("--run", type=Path, default=None,
                    help="run folder for the provenance record and steps.log "
                         "(default: this session's folder)")
    ap.add_argument("--category", default=None,
                    help="force a library subfolder instead of letting the "
                         "query's own garment_type pick one")
    ap.add_argument("--threshold", type=float, default=90.0)
    ap.add_argument("--colour", "--color", dest="colour", action="store_true",
                    help="install the reference in colour; the default "
                         "desaturates it so it cannot act as a colour target")
    ap.add_argument("--no-stash", dest="stash", action="store_false",
                    help="leave any other reference* files in inputs/ alone")
    ap.add_argument("--dry-run", action="store_true",
                    help="match and report, install nothing")
    ap.add_argument("--matcher-arg", action="append", default=[], metavar="ARG",
                    help="pass an extra argument straight to match_reference.py "
                         "(repeatable, e.g. --matcher-arg --color-weight "
                         "--matcher-arg 0)")
    a = ap.parse_args()

    query = a.query.resolve()
    library = a.library.resolve()
    if not query.exists():
        print(f"query not found: {query}", file=sys.stderr)
        return 1
    if not library.is_dir():
        print(f"library not found: {library}", file=sys.stderr)
        return 1

    run = (a.run or C.session_run_dir()).resolve()
    run.mkdir(parents=True, exist_ok=True)

    # --- the judgement ---------------------------------------------------
    cmd = [sys.executable, str(MATCHER),
           "--query", str(query),
           "--library", str(library),
           "--out-dir", str(run),
           "--threshold", str(a.threshold)]
    if a.category:
        cmd += ["--category", a.category]
    cmd += a.matcher_arg

    # Flushed, because the child writes straight to the same terminal and an
    # unflushed header lands after all of the matcher's output.
    print(f"reference selection: {query.name} vs {library.name}/", flush=True)
    rc = subprocess.run(cmd).returncode
    results = run / "match_results.json"

    if rc == 1 or not results.exists():
        print(f"\nmatcher failed (exit {rc}); no reference installed.", file=sys.stderr)
        return 1

    res = json.loads(results.read_text())

    if rc == 2 or not res.get("match_found"):
        best = (res.get("ranked") or [{}])[0]
        print("\nNO REFERENCE SELECTED - nothing in the library matched this garment.")
        print(f"  closest    {best.get('_file', '?')} at "
              f"{best.get('score', 0):.1f} (needed {a.threshold:.0f})")
        print(f"  detail     {results}")
        print("  nothing was installed; inputs/ is unchanged.")
        C.log(run, f"reference NOT selected (best "
                   f"{best.get('score', 0):.1f} < {a.threshold:.0f})")
        return 2

    src = Path(res["match_path"]) if res.get("match_path") else None
    if not src or not src.exists():
        # Older matcher records carried only the basename. Fall back to finding
        # it, but say so - a silent guess about which category folder a file
        # came from is exactly the class of error this pipeline is built around.
        hits = sorted(library.rglob(res["match"]))
        if len(hits) != 1:
            print(f"\ncannot resolve {res['match']!r} to one file under "
                  f"{library} ({len(hits)} candidates)", file=sys.stderr)
            return 1
        src = hits[0]
        print(f"  (resolved {res['match']} by search: {src})")

    # --- the plumbing ----------------------------------------------------
    dst = (a.inputs / a.name).resolve()
    verdict = res.get("verdict") or {}
    ranked = res.get("ranked") or []
    runner = next((r for r in ranked if r.get("_file") != res["match"]), {})

    if a.dry_run:
        print(f"\nDRY RUN - would install {src} -> {dst}"
              f"{'' if a.colour else ' (as greyscale)'}")
        return 0

    a.inputs.mkdir(parents=True, exist_ok=True)
    moved = stash_other_references(a.inputs, a.name) if a.stash else []
    changed, desc = install(src, dst, greyscale=not a.colour)

    record = {
        "selected_at": datetime.now().isoformat(timespec="seconds"),
        "query": str(query),
        "query_md5": md5(query),
        "query_attrs": res.get("query_attrs"),
        "library_root": res.get("library_root"),
        "library_used": res.get("library_used"),
        "library_count": res.get("library_count"),
        "source": str(src),
        "source_md5": md5(src),
        "installed": str(dst),
        "installed_md5": md5(dst),
        "installed_desc": desc,
        "greyscale": not a.colour,
        "rewritten": changed,
        "stashed": moved,
        "score": res.get("match_score"),
        "threshold": res.get("threshold"),
        "model": res.get("model"),
        "model_confidence": res.get("model_confidence"),
        "model_vetoed": res.get("model_vetoed"),
        "n_qualifying": res.get("n_qualifying"),
        "runner_up": {"file": runner.get("_file"), "score": runner.get("score")},
        "reason": verdict.get("reason"),
        "differences": verdict.get("differences"),
        "match_results": str(results),
        "contact_sheet": str(run / "result_top_matches.jpg"),
    }
    prov = run / "reference_selection.json"
    prov.write_text(json.dumps(record, indent=2, default=str))

    print("\nREFERENCE SELECTED")
    print(f"  source       {src.parent.name}/{src.name}")
    print(f"  score        {res.get('match_score')}/100"
          + (f"   model confidence {res['model_confidence']}"
             if res.get("model_confidence") is not None else ""))
    print(f"  installed    {dst}")
    print(f"               {desc}  md5:{record['installed_md5'][:8]}"
          f"{'' if changed else '  (already identical, not rewritten)'}")
    if runner:
        print(f"  runner-up    {runner.get('_file')} "
              f"({runner.get('score', 0):.1f})")
    if record["differences"]:
        # The one line of the model's prose worth reading: what still differs
        # between the query and the pick. The checklist has no field for most of
        # it, so this is the only place a real difference gets named.
        print(f"  differences  {record['differences']}")
    for m in moved:
        print(f"  stashed      {m}")
    print(f"  provenance   {prov}")

    C.log(run, f"reference {src.name[:28]} ({res.get('match_score')}/100)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
