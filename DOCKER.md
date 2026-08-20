# Running the harness in a container

One image in, up to four re-laid flats out.

    docker build -t pld-harness .

    docker run --rm \
      -v "$PWD/inputs:/in:ro" \
      -v "$PWD/out:/out" \
      -v pld-cache:/app/.cache \
      -e FAL_KEY="$(grep -o 'FAL_KEY.*' .env | cut -d= -f2 | tr -d ' \"')" \
      -e QWEN_API_KEY="$(cat .qwen_key)" \
      -e LAYDOWN_MAX_IMAGES=10 \
      pld-harness /in/off_set_image.jpg

The picks land in `out/`, and `out/logs/` gets `steps.log`, `LOG.md`,
`transcript.jsonl`, `reference_selection.json`, `match_results.json` and
`result_top_matches.jpg` - the run's own account of what it decided.

Drop the image path and the entrypoint takes the single image in `/in`. It
refuses to guess when there are several: choosing the wrong one costs a full
run at fal.ai.

Anything after the image path goes straight to `harness.py`:

    docker run ... pld-harness /in/photo.jpg --reference-category bras --max-iters 60

## The container is a client, not a system

Nothing is bundled but the code and the 45-image reference library. Three
services stay outside it and every one of them has to be reachable from inside
the container:

| what | env var | default |
|---|---|---|
| text model (the agent) | `QWEN_BASE_URL` | `http://10.11.245.41:8091` |
| vision model (step 0, grading) | `REFMATCH_BASE_URL` | `http://10.11.245.41:8091/v1` |
| fal.ai (the billed generation) | `FAL_KEY` | none - required |

`QWEN_API_KEY` is read by both models. On the host it comes from `.qwen_key`;
that file and `.env` are in `.dockerignore` on purpose, because anyone who can
pull an image can read its layers. Pass them with `-e` instead.

The two LAN endpoints resolve from inside the container over Docker's NAT with
no extra flags - verified against both servers.

## Volumes

`-v pld-cache:/app/.cache` is worth doing. Cold, step 0 spends ~2 minutes
describing all 45 library images; warm, it is seconds. The README also notes
that deleting the cache shifts borderline match scores, since the model
re-describes each image.

`/in` can be read-only. `/app/inputs` inside the container cannot - step 0
writes its chosen reference back as `inputs/reference_greyscale.jpg`, which is
why the entrypoint copies your photo in rather than mounting over that folder.

Add `-v "$PWD/runs:/app/runs"` to keep full run folders, including every
generated candidate in `archive/`. Without it you still get the picks and the
text artefacts, but the candidates that were not picked are gone with the
container.

**On colima:** a bind mount only works if the host path is one colima shares
with its VM - `$HOME` by default. A folder under `/tmp` silently appears empty
inside the container.

## Image budget

`LAYDOWN_MAX_IMAGES` defaults to 5, matching `run.sh`. The runs in `runs/` that
actually shipped four picks generated ten candidates first, so 5 is a tighter
budget than this skill has been getting. Set it deliberately.

## Exit codes

| code | meaning |
|---|---|
| 0 | four picks delivered |
| 1 | the harness ran and failed or was blocked - read `out/logs/` |
| 2 | misconfigured: missing credential, no image, several images |
| 3 | the run finished but shipped fewer than `EXPECTED_PICKS` (default 4) |
| 20 | no reference: nothing in the library is close enough to this garment |

Fewer than four is a legitimate answer - the skill says padding a shortlist
turns a selection into a rubber stamp - but it is not what you asked for, so it
does not exit 0. Set `-e EXPECTED_PICKS=1` if you want any result to pass.

20 is a verdict, not a fault, and it has its own code for that reason. The
matcher looked at all 45 library images and none of them is close enough to lay
this garment against, so a person has to upload a hero for the style. It used to
leave as 1, which put a correct answer in the same list as an unreachable model
server - and a failure list that fills with non-failures stops being read.

Every run writes `out/result.json` with an `outcome` field - `ok`,
`reference_selected`, `no_reference`, `short` or `error` - plus the exit code it
came from. A caller that can only fail or succeed a step should route on that
field and run the container with `-e NO_REFERENCE_EXIT=0`, which delivers the
hero miss as a clean exit with the answer still in the file. Kestra is the case
this exists for: it reds a task on any non-zero code, so "ask someone to upload a
hero" has to arrive as data rather than as a failure.

## Asking before you spend: `--reference-only`

    docker run --rm -v "$PWD/inputs:/in:ro" -v "$PWD/out:/out" \
      -v pld-cache:/app/.cache -e QWEN_API_KEY pld-harness \
      /in/off_set_image.jpg --reference-only

Runs step 0 and stops: pre-clean, match, install the reference, write the
receipt, never start the agent. Exit 0 if the library can serve this garment, 20
if it cannot. No fal spend, no `FAL_KEY` required, and the text model is not
contacted at all - step 0 only talks to the vision endpoint.

Whatever the answer, `/out` gets `reference_selection.json` (with `match_found`),
`match_results.json` (every score), `result_top_matches.jpg` (what came closest)
and `result.json`. That is the whole output of a gate step: run it first, and
only start a generating run when it says yes.

## What the image patches, and why

The harness source is byte-identical to the copy that runs natively on macOS.
Three macOS-only assumptions are absorbed by the image instead, so nothing had
to fork:

- **`sips`** - `tools/vision.py` and `tools/match_reference.py` downscale
  through it under `check=True`. `docker/sips` reimplements the one flag form
  they use (`-Z <max> <src> --out <dst>`) on PIL, and exits non-zero on any
  other invocation rather than quietly doing something else.
- **`/System/Library/Fonts/Supplemental/Arial.ttf`** - passed to ImageMagick's
  `-font` in three places. The image symlinks Liberation Sans, which is
  metric-compatible with Arial, to that exact path.
- **`magick`** - ImageMagick 7 on the current base provides it. `docker/magick`
  is installed only when the base has ImageMagick 6, which splits it into
  `convert`/`identify`/`montage`; it is inert on a base that ships IM7.

`task/SKILL.md` hardcodes an absolute workspace path in two instructions to the
agent. The Dockerfile rewrites those to `/app` in the image, and fails the
build if any `/Users/` path survives, rather than patching the file in the repo
and breaking the native run.

`--yolo` is not optional and the entrypoint always passes it: `Approver.ok()`
denies every mutating tool when stdin is not a TTY, so without it the agent is
refused on its first real step.
