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
set -uo pipefail

PY=/app/.venv/bin/python
IN_DIR="${IN_DIR:-/in}"
OUT_DIR="${OUT_DIR:-/out}"
EXPECTED_PICKS="${EXPECTED_PICKS:-4}"

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

# --- credentials ------------------------------------------------------------
#
# Checked here rather than discovered 40 turns in, when the agent finally calls
# generate.py and the run has already spent its context.
[ -n "${FAL_KEY:-}" ] || die "FAL_KEY is not set - pass it with -e FAL_KEY."
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
if [ ${#PICKS[@]} -gt 0 ]; then
    cp -p "${PICKS[@]}" "$OUT_DIR"/
fi

# Flat, positional names for a caller that declared its outputs up front and
# cannot know what the picks will be called - Kestra's outputFiles is the case
# this exists for: it fails the task on a name it did not find, and the run
# folder yields pick1_best_cand_07.png, which no one can predict.
#
# The glob above is sorted, and the picks are named pick1..pick4 in grade
# order, so generated_1 is the best pick rather than an arbitrary one.
if [ -n "${OUTPUT_PATTERN:-}" ]; then
    n=0
    for p in "${PICKS[@]}"; do
        n=$((n + 1))
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

    # A machine-readable receipt for the same reason: the caller declared
    # result.json and a missing file fails the task, so it is written whatever
    # happened, including on a run that shipped nothing.
    "$PY" - "$RUN_DIR" "$OUT_DIR" "$LAYDOWN_SESSION" "${#PICKS[@]}" \
        > "$OUT_DIR/result.json" <<'PY'
import json, sys, pathlib
run, out, session, n = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], int(sys.argv[4])
rec = {"session": session, "picks": n,
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
fi

# The text artefacts are a few KB and are the only way to explain a run that
# shipped nothing, so they come out even when runs/ is not mounted.
mkdir -p "$OUT_DIR/logs"
for f in steps.log LOG.md transcript.jsonl reference_selection.json \
         match_results.json result_top_matches.jpg; do
    [ -e "$RUN_DIR/$f" ] && cp -p "$RUN_DIR/$f" "$OUT_DIR/logs/"
done

echo
echo "  delivered ${#PICKS[@]} image(s) to $OUT_DIR"
echo "  logs      $OUT_DIR/logs"

# Shipping fewer than four is a legitimate outcome - the skill says so, and
# padding the list would be worse. But it is never the outcome you asked for,
# so it must not exit 0 and read as success in a pipeline.
if [ ${#PICKS[@]} -lt "$EXPECTED_PICKS" ]; then
    echo "entrypoint: expected $EXPECTED_PICKS picks, got ${#PICKS[@]}." >&2
    echo "  See $OUT_DIR/logs/steps.log and LOG.md for what the run decided." >&2
    [ "$RC" -eq 0 ] && RC=3
fi
exit "$RC"
