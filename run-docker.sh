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

# Read the key files the way harness.py's load_dotenv() does, rather than
# sourcing them. This project's own .env is written `FAL_KEY = value`, with
# spaces, which is fine for a tolerant parser and is a syntax error to the
# shell - sourcing it ran `FAL_KEY` as a command and reported the key as unset.
#
# Anything already exported wins, so `FAL_KEY=... ./run-docker.sh` overrides
# the file for one run.
load_env() {
    local f="$1" line k v
    [ -f "$f" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        case "$line" in ''|\#*) continue ;; esac
        [ "${line#*=}" = "$line" ] && continue
        k="${line%%=*}"; v="${line#*=}"
        k="${k#"${k%%[![:space:]]*}"}"; k="${k%"${k##*[![:space:]]}"}"
        v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"
        v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
        # An `if`, not `[ -z ... ] && export ...`: the && form evaluates to 1
        # when the variable is already set, that is the last status in the loop
        # body, and under `set -e` the script then exits silently - which looks
        # exactly like the run starting and doing nothing.
        case "$k" in
            [A-Za-z_]*)
                if [ -z "${!k:-}" ]; then
                    export "$k=$v"
                fi
                ;;
        esac
    done < "$f"
}

# .env.docker first so it can override the .env the native run.sh path uses.
load_env "$HERE/.env.docker"
load_env "$HERE/.env"

: "${FAL_KEY:?not set - put FAL_KEY in .env or .env.docker}"

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
