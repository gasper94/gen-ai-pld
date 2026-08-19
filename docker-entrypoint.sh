#!/usr/bin/env bash
# One image in, up to four re-laid flats out.
#
# This exists so the host never has to know the harness's internal layout. The
# harness insists on inputs/off_set_image.jpg next to its own source and writes
# its picks to runs/<session>/output/, so the caller would otherwise have to
# bind-mount over the source tree and go hunting for a timestamped folder.
# Here the caller mounts a photo at /in and a folder at /out.
#
#   docker run --rm -v "$PWD/inputs:/in:ro" -v "$PWD/out:/out" \
#     -v pld-cache:/app/.cache -e FAL_KEY -e QWEN_API_KEY \
#     pld-harness /in/off_set_image.jpg
#
# Anything after the image path is passed straight to harness.py.
#
# Exit codes:
#
#    0   the run delivered what it was asked for
#    1   the run broke
#    2   this script was called wrong (no image, missing credential)
#    3   fewer images than EXPECTED_PICKS - a legitimate outcome, but not the
#        one that was asked for
#   20   no reference: nothing in library_reference/ is close enough to this
#        garment, so a human has to upload a hero. A verdict, not a fault.
set -uo pipefail

PY=/app/.venv/bin/python
IN_DIR="${IN_DIR:-/in}"
OUT_DIR="${OUT_DIR:-/out}"
EXPECTED_PICKS="${EXPECTED_PICKS:-4}"

# harness.py's code for the hero miss, mirrored here so the two cannot drift.
#
# NO_REFERENCE_EXIT=0 delivers that outcome as a clean exit instead. It is for a
# caller that reads `outcome` out of result.json and routes on it rather than on
# the exit status - Kestra fails a task on any non-zero code, so a flow that
# answers a hero miss by asking someone to upload one has to be told in a file.
# The default stays 20: a shell that never opens result.json must not read a
# miss as a run that worked.
EXIT_NO_REFERENCE=20
NO_REFERENCE_EXIT="${NO_REFERENCE_EXIT:-$EXIT_NO_REFERENCE}"

die() { echo "entrypoint: $*" >&2; exit 2; }

# --- which image are we laying out? ----------------------------------------
#
# An explicit path wins. With none, /in must hold exactly one image: guessing
# between several would mean a run that silently laid out the wrong garment,
# and the run costs real money at fal.ai.
INPUT=""
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
    INPUT="$1"
    shift
    [ -f "$INPUT" ] || die "no such image: $INPUT"
else
    [ -d "$IN_DIR" ] || die "no image given and $IN_DIR is not mounted."
    # maxdepth 1 keeps inputs/others/ out of it; reference_greyscale.jpg is an
    # output of step 0, not a candidate input, and the user's inputs folder has
    # one sitting in it from the last native run.
    mapfile -t FOUND < <(find "$IN_DIR" -maxdepth 1 -type f \
        \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \
           -o -iname '*.tif' -o -iname '*.tiff' -o -iname '*.webp' \) \
        ! -name '.*' ! -iname 'reference_greyscale.*' | sort)
    case ${#FOUND[@]} in
        0) die "no image found in $IN_DIR (looked for jpg/png/tif/webp)." ;;
        1) INPUT="${FOUND[0]}" ;;
        *) printf 'entrypoint: %d images in %s - name the one you want:\n' \
               "${#FOUND[@]}" "$IN_DIR" >&2
           printf '  %s\n' "${FOUND[@]}" >&2
           exit 2 ;;
    esac
fi

# --- gate run? --------------------------------------------------------------
#
# --reference-only stops after step 0: it picks the reference and reports
# whether the library could serve this garment at all, without starting the
# agent or spending anything at fal. The delivery below has to know, or it
# measures a run that was never going to generate against four images and
# reports it short.
REFERENCE_ONLY=""
for arg in "$@"; do
    [ "$arg" = "--reference-only" ] && REFERENCE_ONLY=1
done

# --- credentials ------------------------------------------------------------
#
# Checked here rather than discovered 40 turns in, when the agent finally calls
# generate.py and the run has already spent its context.
#
# A gate run never reaches fal, so it is not asked for the key. That is the
# point of running it as its own step: the cheap question gets answered without
# the expensive credential.
[ -n "$REFERENCE_ONLY" ] || [ -n "${FAL_KEY:-}" ] \
    || die "FAL_KEY is not set - pass it with -e FAL_KEY."
[ -n "${QWEN_API_KEY:-}" ] || die "QWEN_API_KEY is not set - pass it with -e QWEN_API_KEY.
  Both models read it; on the host it comes from .qwen_key, which is
  deliberately not baked into the image."

# --- one run, one folder ----------------------------------------------------
#
# The image budget is enforced by counting images in the run folder, so the
# stamp is set once here and inherited by every tool the agent starts.
export LAYDOWN_SESSION="${LAYDOWN_SESSION:-$(date +%Y%m%d_%H%M%S)}"
export LAYDOWN_MAX_IMAGES="${LAYDOWN_MAX_IMAGES:-5}"

RUN_DIR="/app/runs/$LAYDOWN_SESSION"
mkdir -p /app/inputs "$RUN_DIR" "$OUT_DIR" || die "cannot write to $OUT_DIR"

# The harness reads inputs/off_set_image.jpg by name. Copy rather than symlink:
# step 0 writes its chosen reference back into inputs/, so this directory has
# to be writable even when the caller mounted their photos read-only.
cp "$INPUT" /app/inputs/off_set_image.jpg || die "could not stage $INPUT"

echo "  input     $INPUT"
echo "  session   $LAYDOWN_SESSION  (max $LAYDOWN_MAX_IMAGES images)"
echo "  text      ${QWEN_BASE_URL:-http://10.11.245.41:8091 (default)}"
echo "  vision    ${REFMATCH_BASE_URL:-http://10.11.243.169:8080/v1 (default)}"
echo

# --yolo is required, not a preference: Approver.ok() refuses every mutating
# tool when stdin is not a TTY, so without it the agent is denied on its first
# real step and burns the run arguing with itself.
"$PY" /app/harness.py --skill-file /app/task/SKILL.md --yolo "$@"
RC=$?

# --- deliver ----------------------------------------------------------------
#
# Run unconditionally: a run that failed halfway still leaves useful picks and
# always leaves the logs that explain what went wrong.
shopt -s nullglob
PICKS=("$RUN_DIR"/output/*.png "$RUN_DIR"/output/*.jpg)

# What actually gets delivered. Normally the picks - the agent's shortlist.
#
# SHIP_CANDIDATES=1 delivers every generated candidate instead, ordered picks
# first. That exists for a caller that needs a FIXED-SIZE set: the agent is
# free to ship three of four when the fourth is a garment the model redrew
# rather than re-laid, and no instruction reliably overrides that - it read the
# skill, which says fewer than four is a correct answer, and it is right to.
#
# Shipping all of them is only defensible because these are OPTIONS for a
# person to choose from downstream, not a finished delivery. The grading is not
# discarded: it becomes the order. Position 1 is the pick the agent defended
# hardest and the last position is the one it argued against, so a reviewer
# reading top-down sees its judgement even though nothing was withheld.
DELIVER=("${PICKS[@]}")
if [ -n "${SHIP_CANDIDATES:-}" ] && [ -d "$RUN_DIR/archive" ]; then
    mapfile -t ORDERED < <("$PY" - "$RUN_DIR" <<'PY'
import pathlib, re, sys

run = pathlib.Path(sys.argv[1])
cands = {p.stem: p for p in sorted((run / "archive").glob("cand_*.png"))}

# A pick is named pick2_cand_06.png, or pick1_best_cand_10.png for the winner,
# so the candidate it came from is the trailing cand_NN. Recovering that is
# what lets the picks lead the ordering without duplicating their bytes.
ordered, seen = [], set()
for pick in sorted((run / "output").glob("pick*")):
    m = re.search(r"(cand_\d+)$", pick.stem)
    if m and m.group(1) in cands and m.group(1) not in seen:
        seen.add(m.group(1))
        ordered.append(cands[m.group(1)])

ordered += [p for stem, p in cands.items() if stem not in seen]
for p in ordered:
    print(p)
PY
    )
    if [ ${#ORDERED[@]} -gt 0 ]; then
        DELIVER=("${ORDERED[@]}")
    fi
fi

if [ ${#DELIVER[@]} -gt 0 ] && [ -z "${OUTPUT_PATTERN:-}" ]; then
    cp -p "${DELIVER[@]}" "$OUT_DIR"/
fi

# --- what happened, in one word ---------------------------------------------
#
# Settled here, before result.json is written, so the receipt and the exit code
# cannot disagree. The short-delivery MESSAGE still prints at the end, next to
# the delivery count it is talking about; only the verdict moves up.
#
# Neither a gate run nor a hero miss is measured against EXPECTED_PICKS. Both
# deliver nothing on purpose, and calling that short would bury the real answer
# under a complaint about the images it was never going to make.
SHORT=""
if [ -z "$REFERENCE_ONLY" ] && [ "$RC" -ne "$EXIT_NO_REFERENCE" ] \
   && [ ${#DELIVER[@]} -lt "$EXPECTED_PICKS" ]; then
    SHORT=1
    [ "$RC" -eq 0 ] && RC=3
fi

case "$RC" in
    0)  OUTCOME=ok
        [ -n "$REFERENCE_ONLY" ] && OUTCOME=reference_selected ;;
    "$EXIT_NO_REFERENCE") OUTCOME=no_reference ;;
    3)  OUTCOME=short ;;
    *)  OUTCOME=error ;;
esac

# Flat, positional names for a caller that declared its outputs up front and
# cannot know what the files will be called - Kestra's outputFiles is the case
# this exists for: it fails the task on a name it did not find, and the run
# folder yields pick1_best_cand_07.png, which no one can predict.
#
# The original names are deliberately NOT also copied here: six files for three
# deliverables made a working directory nobody could count, and the run folder
# still has them under their real names.
if [ -n "${OUTPUT_PATTERN:-}" ]; then
    n=0
    for p in "${DELIVER[@]}"; do
        n=$((n + 1))
        [ "$n" -gt "$EXPECTED_PICKS" ] && break
        cp -p "$p" "$OUT_DIR/${OUTPUT_PATTERN//\{n\}/$n}"
    done

    # The prompt the run actually used. archive/prompt.txt is what the agent
    # sent to fal; the --task string is the fallback when a run died before
    # writing one, so the field is never empty.
    if [ -e "$RUN_DIR/archive/prompt.txt" ]; then
        cp -p "$RUN_DIR/archive/prompt.txt" "$OUT_DIR/used_prompt.txt"
    else
        printf '%s\n' "no prompt recorded - the run did not reach generate" \
            > "$OUT_DIR/used_prompt.txt"
    fi
fi

# --- the receipt, whatever happened -----------------------------------------
#
# steps.log and LOG.md flat at the top level, created empty when the run never
# wrote them. A caller that collects files by name cannot reach into logs/, and
# the one run that most needs explaining - the one that shipped nothing - is
# exactly the run that produces neither file.
for f in steps.log LOG.md; do
    if [ -e "$RUN_DIR/$f" ]; then
        cp -p "$RUN_DIR/$f" "$OUT_DIR/$f"
    else
        : > "$OUT_DIR/$f"
    fi
done

# These used to be written only under OUTPUT_PATTERN, on the reasoning that a
# caller declaring image names is the caller that declared these too. That tied
# the machine-readable answer to a knob about image FILENAMES, which is fine
# until a caller wants the answer without wanting images - the hero-miss gate
# generates nothing by design and its whole output is this file. Written every
# run now; three small files are not worth a coupling nobody can see.
"$PY" - "$RUN_DIR" "$OUT_DIR" "$LAYDOWN_SESSION" "${#PICKS[@]}" \
    "$OUTCOME" "$RC" > "$OUT_DIR/result.json" <<'PY'
import json, sys, pathlib
run, out, session, n = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], int(sys.argv[4])
outcome, rc = sys.argv[5], int(sys.argv[6])
# `outcome` is the field a caller routes on, and the reason it exists is that
# not every non-zero exit is a failure: "no_reference" means the library had
# nothing close enough and a human has to upload a hero, which is a business
# answer that happens to end the run. `exit_code` is kept alongside it for
# anyone debugging the run rather than routing it.
#
# `picks` is the count the AGENT stood behind, which is the number worth
# reading downstream. It is deliberately not the number of delivered files:
# under SHIP_CANDIDATES those differ, and that gap is the useful signal - three
# picks against four images says the last one was shipped over an objection.
rec = {"session": session, "outcome": outcome, "exit_code": rc, "picks": n,
       "images": sorted(p.name for p in pathlib.Path(out).glob("generated_*.png"))}
for name, key in (("reference_selection.json", "reference"),
                  ("archive/grade_results.json", "grades")):
    f = run / name
    if f.exists():
        try:
            rec[key] = json.loads(f.read_text())
        except ValueError:
            pass
print(json.dumps(rec, indent=2, default=str))
PY

# --- the evidence for a hero miss -------------------------------------------
#
# Flat at the top level, not only inside logs/, and written whatever happened.
# These three ARE the deliverable of a run that found no reference: the receipt
# a caller routes on, the scores behind it, and the contact sheet a person looks
# at before deciding whether to shoot a new hero or widen the category. A
# collector that names the files it wants cannot reach into logs/ to get them.
#
# reference_selection.json is created even when step 0 never ran, on the same
# rule as steps.log and LOG.md above: declaring it must never be the reason a
# task fails. `null` says "not asked", which is neither true nor false.
for f in reference_selection.json match_results.json result_top_matches.jpg; do
    [ -e "$RUN_DIR/$f" ] && cp -p "$RUN_DIR/$f" "$OUT_DIR/$f"
done
if [ ! -e "$OUT_DIR/reference_selection.json" ]; then
    printf '{"match_found": null, "note": "step 0 did not run"}\n' \
        > "$OUT_DIR/reference_selection.json"
fi

# The text artefacts are the only way to explain a run that shipped nothing, so
# they come out even when runs/ is not mounted. All small except run.log, which
# is the full trace and can reach a few MB on a long run - worth every byte on
# the run someone is asking questions about, and dwarfed by one delivered PNG.
mkdir -p "$OUT_DIR/logs"
for f in steps.log LOG.md run.log transcript.jsonl reference_selection.json \
         match_results.json result_top_matches.jpg; do
    [ -e "$RUN_DIR/$f" ] && cp -p "$RUN_DIR/$f" "$OUT_DIR/logs/"
done

echo
if [ -n "$REFERENCE_ONLY" ]; then
    # "delivered 0 image(s)" would be the truth and still the wrong thing to
    # say: a gate run was never asked for images, and reporting the count it
    # did not produce reads as a shortfall.
    echo "  gate run  --reference-only; no images were asked for"
else
    echo "  delivered ${#DELIVER[@]} image(s) to $OUT_DIR"
fi
if [ -z "$REFERENCE_ONLY" ] && [ -n "${SHIP_CANDIDATES:-}" ]; then
    # Say it plainly. Under SHIP_CANDIDATES the delivered count no longer means
    # "images the agent stood behind", and a reviewer who assumes it does is
    # being told the wrong thing by a number that used to be trustworthy.
    echo "  of which  ${#PICKS[@]} were picked; the rest ship ranked, unfiltered"
fi
echo "  logs      $OUT_DIR/logs"

# Shipping fewer than four is a legitimate outcome - the skill says so, and
# padding the list would be worse. But it is never the outcome you asked for,
# so it must not exit 0 and read as success in a pipeline. (The verdict itself
# was settled above, before result.json was written; this is the explanation.)
if [ -n "$SHORT" ]; then
    echo "entrypoint: expected $EXPECTED_PICKS images, got ${#DELIVER[@]}." >&2
    echo "  See $OUT_DIR/logs/steps.log and LOG.md for what the run decided." >&2
fi

# The hero miss leaves by its own door. Nothing here failed: the matcher looked
# at every garment in the library, none of them was close enough to lay this one
# against, and that is the answer. Exiting 1 for it put a correct verdict in the
# same list as an unreachable model server, and a failure list that fills with
# non-failures is a list people stop reading.
if [ "$RC" -eq "$EXIT_NO_REFERENCE" ]; then
    echo
    echo "  outcome   no reference - the library has nothing close enough to"
    echo "            this garment. Someone has to upload a hero for it."
    echo "  evidence  $OUT_DIR/reference_selection.json"
    echo "            $OUT_DIR/result_top_matches.jpg  (what came closest)"
    if [ "$NO_REFERENCE_EXIT" -ne "$EXIT_NO_REFERENCE" ]; then
        echo "  exit      $NO_REFERENCE_EXIT (NO_REFERENCE_EXIT); the outcome is"
        echo "            in result.json, not in the exit status"
    fi
    exit "$NO_REFERENCE_EXIT"
fi
exit "$RC"
