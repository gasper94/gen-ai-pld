#!/usr/bin/env bash
# Run the containerised harness. The docker invocation in one place, so the
# eight flags it needs are not retyped - and not pasted through an editor that
# autocorrects " into a curly quote, which bash treats as an ordinary character
# and which therefore fails as an unterminated string rather than as a bad key.
#
#   ./run-docker.sh                          # the single image in inputs/
#   ./run-docker.sh /in/other.jpg            # a specific one
#   ./run-docker.sh --reference-category bras
#
# Credentials come from .env.docker, which is gitignored. Never put a key on
# the command line: it lands in shell history and in `ps` for every user on the
# box.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

if [ -f "$HERE/.env.docker" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$HERE/.env.docker"
    set +a
fi

: "${FAL_KEY:?not set - copy .env.docker.example to .env.docker and add the key}"

# The model servers answer without real auth, but vision.py and
# match_reference.py exit when the variable is unset entirely, so the harness's
# own placeholder stands in.
: "${QWEN_API_KEY:=pick-a-long-secret-string}"

# The text model that drives the agent loop. No /v1 - harness.py appends it.
: "${QWEN_BASE_URL:=http://10.11.245.41:8091}"

# The vision model step 0 scores the reference library with. /v1 IS required
# here; that asymmetry is why the project keeps the two variables separate.
# Point this at the 8080 box to use the model the 90/100 threshold was
# calibrated against.
: "${REFMATCH_BASE_URL:=http://10.11.245.41:8091/v1}"

: "${LAYDOWN_MAX_IMAGES:=10}"
: "${MAX_ITERS:=40}"
: "${TASK:=Follow the skill, but generate all 10 images in one call at 2K, then deliver with grade_flats.py --ship 4. remove all pins important prompt.}"

export FAL_KEY QWEN_API_KEY QWEN_BASE_URL REFMATCH_BASE_URL LAYDOWN_MAX_IMAGES

mkdir -p "$HERE/out"

# An image path, if given, has to stay first: the entrypoint treats a leading
# argument that is not a flag as the input photo. With none it takes the single
# image in inputs/ and refuses to guess between several.
ARGS=()
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
    ARGS+=("$1")
    shift
fi
ARGS+=(--max-iters "$MAX_ITERS" --task "$TASK" "$@")

exec docker run --rm \
    -v "$HERE/inputs:/in:ro" \
    -v "$HERE/out:/out" \
    -v "$HERE/runs:/app/runs" \
    -v pld-cache:/app/.cache \
    -e FAL_KEY \
    -e QWEN_API_KEY \
    -e QWEN_BASE_URL \
    -e REFMATCH_BASE_URL \
    -e LAYDOWN_MAX_IMAGES \
    pld-harness "${ARGS[@]}"
