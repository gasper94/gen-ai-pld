---
name: laydown
description: "Re-lay an off-set product photo so the garment sits square and wrinkle-free, keeping its real colour, texture and proportions. Write the prompt yourself from looking at the images, generate with fal.ai nano-banana-pro/edit, test what comes back, and deliver up to 4 re-laid flats on a clean white plate."
---

# Laydown

## The goal

Deliver **up to 4 images** of this garment, as re-laid flats on a clean white
plate, that pass every test below.

Cutouts are currently off. `--ship` delivers the generated flats themselves;
pass `--cutout` if transparent-background PNGs are wanted again.

Fewer than 4 is a correct answer when fewer than 4 pass. Padding the list with
something you would not defend turns a selection into a rubber stamp.

## The reference is already chosen. Do not choose one.

Before your first turn, the harness ran `tools/select_reference.py`: it scored
every image in `library_reference/` against the off-set photo, took the winner,
desaturated it, and installed it at

```
inputs/reference_greyscale.jpg
```

That is the reference. There is exactly one, the inventory names it, and
`library_reference/` is deliberately absent from the inventory because it is not
an input - it is 45 photos of other garments. Do not go looking for it, do not
weigh a second candidate, and do not swap the reference for one you prefer.

Pass it to `prepare.py` explicitly and absolutely:

```bash
--reference /Users/ulmarti/Desktop/PLD_Harness/inputs/reference_greyscale.jpg
```

The receipt is `<RUN_DIR>/reference_selection.json` - which library file was
chosen, its score out of 100, the runner-up, and a `differences` line naming
what still differs between the off-set garment and the reference. **Read that
`differences` line before writing your prompt** and quote the source filename
and score in `## Setup`. It is the one place a real mismatch between the two
images is written down, and the checklist behind the score has no field for
most of what it catches.

The reference is greyscale on purpose - it is a shape and construction
reference, never a colour target. `prepare.py` says so again in the brief.

## Start with prepare.py. Do not go exploring first.

`tools/*.py` and `harness.py` are not inputs - the harness now refuses to read
them. This page plus `--help` is all you need.

**Do not list or read previous runs either.** They are not inputs and they are
not context. Three runs spent 44%, 54% and 51% of the context window on source
and old run folders before their first real step; one of them ran out of turns
after two images, with nothing picked and no log.

**Do not `ls` the inputs.** The workspace inventory at the top of this
conversation already names every input file with its dimensions and an md5, and
it is generated fresh each run. Read the filenames off it. Two runs in a row
burned turns on `ls -la inputs/` and `ls -la inputs/others/` for names that were
sitting in their own system prompt.

Your first tool call should be `prepare.py`.

## The tools

Run everything from `tools/` with the project interpreter:

```bash
cd /Users/ulmarti/Desktop/PLD_Harness/tools && ../.venv/bin/python <script> ...
```

| | |
|---|---|
| `prepare.py` | Checks the inputs, **pre-cleans the source** (erases tags and REMOVE PINS, REMOVE HANGER, drops the background, plates it white), **inventories the construction**, and writes a prompt brief. Prints `RUN_DIR=` - carry it into everything else. Writes **no prompt**. |
| `generate.py --run R --num N --resolution 2K` | The only billed step. $0.15 an image at 1K/2K, $0.30 at 4K. Numbering continues automatically, so topping up needs no extra arguments. |
| `grade_flats.py --run R` | **Grades and picks.** Measures shape, wrinkles and backdrop, then checks with the vision model that the generation did not redraw the garment's construction. Prints a ranking and a `KEEP` list, and writes `archive/metrics.json` and `archive/grade_results.json`. Add `--profile bras` for bras. |
| `grade_flats.py --run R --ship 4` | Same, then copies the **top 4 by grade** to `output/` - the deliverable. Status is not a gate: a candidate stage 3 flagged still ships, and the flagged regions are printed per pick and written to `steps.log`. `--ship-clean-only` restores the gate; `--cutout` adds transparent-background PNGs. |
| `crop_pair.py --run R --cand NN --at waistband` | Matching 1:1 crop boxes for two images that sit differently in frame. Regions: `waistband hip crotch thigh knee hem centre left right`. |
| `contact.py --run R` | Contact sheets of every candidate, cropped to the garment and sized to survive a vision call. |
| `compare_images` | The vision tool. Two images in one call. |

## The limits

- **One run means one folder, and one image budget.** `prepare.py` returns the
  same folder every time you call it - calling it again does not start over and
  does not reset the count. `generate.py` refuses to spend into any other
  folder. The ceiling is set by the operator, not by you; `--max-total` can
  lower it but never raise it. When the budget is spent, measure and pick from
  what you have and say if that is fewer than 4.
- **One prompt, written by you.** `prepare.py` leaves a brief in
  `archive/prompt_brief.md`. Write `archive/prompt.txt` with a bash heredoc.

  **Look at `<RUN_DIR>/archive/offset_upload.jpg`, never
  `inputs/off_set_image.jpg`.** The upload is the CLEANED image and the only one
  the model ever receives - tag erased, background dropped, plate white. The raw
  input still has the hang tag and a real-world background. A run wrote its
  prompt from the raw input, described the tag, asked that it "stay in place",
  and all four candidates grew a tag that was not in the image sent.

  ```
  compare_images(path_a="<RUN_DIR>/archive/offset_upload.jpg",
                 path_b="<the reference named in your workspace inventory>",
                 question="...")
  ```

  **The reference is `inputs/reference_greyscale.jpg`** - installed by step 0
  before your first turn, and named in the workspace inventory with its md5.
  Check the inventory rather than this page if the two ever disagree: it is
  fingerprinted and current. A run once guessed `reference_image.jpg` from an
  older copy of this page, found it missing, and spent two turns running `ls`
  over `inputs/` to discover a name it had already been given. `prepare.py`
  prints the reference it resolved - that line is authoritative.

  `generate.py` refuses a prompt under 120 words, one that never mentions
  greyscale, flatness, wrinkles or the background, one that asks for a
  **transparent** background (the model cannot output alpha and paints a
  checkerboard instead - ask for plain white, `cutout.py` adds transparency
  afterwards), and one that mentions a **tag, ticket, label, barcode or
  hanger** at all - there is none left to keep or remove, and naming it makes
  the model draw one.
- **Never re-run to top up a failed image.** Everything on disk is already
  billed, and re-rolling one bad candidate with a tweaked prompt reliably makes
  it worse.

`generate.py` appends `archive/garment_description.md` to every prompt
automatically. **Never describe seams or panels yourself** - two vision models,
asked what this garment has, both invented a seam down each leg, and the
product's spec sheet says "No inseam". Your prompt needs one line saying the
construction is reproduced exactly as specified and nothing is added.

Where that inventory comes from is printed by `prepare.py` and recorded in
`steps.log`:

- **`inputs/Design_BOM.png` present** - transcribed from the spec sheet, both
  halves sent, authoritative. This is what you want.
- **absent** - inferred from the photo, and only the NOT-PRESENT half is sent.
  The positive half fabricated on all five attempts across two model tiers.

The source is already clean by the time you write the prompt: measured on this
project, the hang tag and a clip were erased, the bench and shoes removed, the
plate turned pure white, garment colour drift **0.4**, full resolution kept. So
the re-lay prompt has only two jobs left - square the garment and de-wrinkle it.
Asking it to remove a background or a tag now invites it to invent one.

## The tests

`grade_flats.py --run R` runs all of them and prints a `KEEP` list. It grades in
three stages and the third one is a door, not points on a scoreboard.

**Stage 1-2, measured.** No model involved, so nothing here can be invented:

| | |
|---|---|
| `shape` | symmetry mirrored about the garment's **own** bounding box, so a symmetric garment sitting off-centre is not penalised. Absolute: an untouched real flat measures 0.06 asymmetry and scores 0, a perfectly mirrored one scores 100 |
| `wrinkles` | local luminance variance inside an eroded mask, on a copy rescaled so every garment is the same height. **Batch-relative** - 100 means "smoothest of these five", not "objectively smooth" |
| `bg` | backdrop lightness. `bg_lum` 0.99 scores 100, 0.90 scores 0 - anchored on the plate this pipeline actually produces, which sweeps 228-252 and never reaches pure white. Measured, not judged: the model rated a visibly grey backdrop 100/100 |

`grade = 45% shape + 45% wrinkles + 10% bg`, pass mark 80.

**Stage 3, the construction gate.** Three native-resolution crops of each
candidate against the same crops of the cleaned source, one vision call each,
asking only whether stitching, seams, pockets, waistband or labels changed.
A **MISMATCH** marks the candidate REJECT, but **REJECT no longer blocks
delivery** - `--ship` takes the top N by grade regardless. The gate is now a
label, not a door, so reading it is your job. On a real batch the best-looking
candidate (92.3) had three altered regions and the least-altered one graded
62.8, so the top of the ranking is not the most faithful image and never was.
Every MISMATCH that ships is printed against its pick - put those in `## Notes`.

Read the numbers honestly:

- **`wrinkles` is the weak one.** It is an isotropic variance measure, and
  `common.py` records that a metric of exactly this class was tried here before
  and removed: it ranked the visibly *smoothest* candidate of a real run
  highest, because it was reading form shading rather than creases. It is worth
  45% of the grade. If the ranking disagrees with what you can see on
  `archive/grade_results.jpg`, the metric is wrong, not your eyes - pick past it
  and say so in `## Notes`.
- **The grade is an average, so a good axis can buy off a bad one.** Look at the
  `shape`/`wrinkles`/`bg` columns, not just the total.
- **Nothing here checks colour, length, waistband width or frame clipping.**
  Stage 1 works entirely in greyscale. If a candidate looks desaturated or
  stretched, or touches a frame edge, only you will catch it - `compare_images`
  against `archive/offset_upload.jpg`.
- **A batch where every candidate is equally bad still produces a winner**,
  because `wrinkles` is normalised within the batch. Fewer than 4 shipped is
  still a correct answer.

**Do not re-generate to fix a failing candidate.** A repair pass rerolls the
dice rather than converging: one candidate scored 50 for integrity, was re-sent
with a corrective prompt, and came back at 40, having added texture that was
never there.

Framing, position, scale and tilt are **not tested and must not be**. The
retouch team places the garment themselves. Grading framing was
actively harmful - the candidates rejected for it carried the best colour and
texture in every batch, because they were the ones that left the product alone.

## Sequence it yourself

There is no prescribed order. Generate, grade, look, pick. Top up within the
cap if too few pass. Stop when you have 4, or when the cap is reached.

Three things that have gone wrong repeatedly, worth planning around:

- **Nothing cross-checks your picks any more.** `--ship N` takes the top N of
  `KEEP` and writes them, and that is the whole of it. There is no second
  opinion between the grade and the deliverable, so the numbers in `## Results`
  have to be ones you read off `grade_flats.py`'s own output rather than ones
  you remember. A run once reasoned its way to one set of picks and then typed
  the numbers out of an example, shipping two candidates that had been rejected.
- **Short of 4? Fill from what is already generated.** Do not generate more.
  `--ship` writes what cleared and names the next best with the defect each
  carries. Look at `archive/grade_results.jpg`, take the ones you would defend,
  copy them into `output/` yourself, and record in `## Notes` what each added
  pick carries. Rejected does not mean unusable - it means the cost is named. If
  the whole batch failed the same way, that is a prompt fault and more draws
  would only buy more of it.
- **A construction MISMATCH now ships anyway, so it has to be reported.** It is
  the only check in the pipeline that can tell a re-laid garment from a redrawn
  one, and it runs on 1:1 crops precisely so it is not guessing - but it no
  longer stops anything. `## Notes` is the only place that record survives, so
  name every flagged pick and the regions it altered. `crop_pair.py --run R
  --cand NN --at waistband` puts the two crops in front of you if you want to
  judge a flag yourself before writing it up.

## Deliver

`--ship N` writes the picks to `output/`. Then write `<RUN_DIR>/LOG.md`
with a **bash heredoc**, not `write_file` - a long string argument truncates
mid-JSON and the call is rejected.

Sections: `## Setup` `## Prompt` `## Generation` `## Testing` `## Picking`
`## Results` `## Notes`

`## Setup` names the reference step 0 installed - the library filename, its
score, and the `differences` line - read off
`<RUN_DIR>/reference_selection.json`, not from memory.

`## Results` is a table: Pick | File | Grade | Shape | Wrinkles | Background |
Construction, with the reference's own asymmetry quoted underneath so it is
clear which picks actually re-laid the garment and which left it alone.

`## Notes` carries the honest caveats - a speck in a background, a candidate you
nearly picked, any `--force` and why, and whether any pick is a no-op.

## Rules that cost real runs

1. **Pass every path explicitly and absolutely.** A script falling back to a
   default input will process a different garment and report precise, plausible,
   entirely wrong numbers.
2. **Ground every number in output you actually saw.** Never report a
   measurement you did not run or a file you did not create.
3. **`cat <RUN_DIR>/steps.log`** is the whole run in a few lines, and the spend
   record to quote. Read it instead of re-deriving.
4. **Costs are calculated at published rates, never receipts.** fal exposes no
   billing API.
5. **Keep tool output small.** You have a limited context window and a run that
   fills it ends before the work does.
