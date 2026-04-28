# Barriers_RFEA → LimeSurvey conversion notes

This directory now contains a Python generator that emits a LimeSurvey-importable `.lss` from `index.html`.

## Files

| File | Purpose |
|---|---|
| `convert_to_limesurvey.py` | Generator script (Python 3.7+, stdlib only) |
| `barriers_rfea.lss` | Generated LimeSurvey export — import this into LimeSurvey 6.x |
| `index.html` | Source survey (read-only input to the generator) |
| `Barriers_RF.png` | Branding image — must be uploaded post-import (see below) |
| `bws_reset.js` | BWS state-reset script — must be uploaded post-import (see below) |

## Regenerate

```bash
python convert_to_limesurvey.py
# or with overrides:
python convert_to_limesurvey.py --output ./out.lss --db-version 649 --survey-id 123456
```

The output is byte-identical across re-runs (deterministic IDs).

## Survey contents

- **8 question groups** (1 demographics + 7 barrier categories), one per page
- **80 pre-created questions** in the `.lss`. After relevance filtering, a typical respondent sees ~59:
  - 6–8 demographics (depending on whether "Other" is picked for industry/education)
  - 14 Q_most/Q_least (2 per section × 7 sections)
  - 37 visible comparisons (2N-3 per section)
- **310 answer options** (demographic dropdowns + barrier lists + scale options)
- **14 question_attributes** (1 `em_validation_q` + 1 `em_validation_q_tip` per Q_least)

## Import

1. Spin up LimeSurvey 6.x. Match production version if known; otherwise use the floating tag for a smoke test:

   ```bash
   docker run -d -p 8080:8080 martialblog/limesurvey:6-apache
   ```

   Default admin credentials are usually `admin` / `password` for the martialblog image — check that image's README before relying on this.

2. Log in → **Surveys → Create or import a new survey → Import a survey** → upload `barriers_rfea.lss`.

3. Note the **survey ID (SID)** assigned on import — you'll need it in the next two steps.

## Prerequisites and manual post-import steps

### 0. XSS filter prerequisite (do this before every `--replace` run)

LimeSurvey's **"Filter HTML for XSS"** setting (Global Settings → Security) **must be Off** before import. If it is On, LimeSurvey will silently strip the `<script>` tag from group descriptions, and the BWS state-reset script will never load.

Check this setting before every `--replace` run — it can be reset by LimeSurvey updates or admin changes.

### 1. Substitute `{SID}` placeholder in the welcome text

The generator writes a literal placeholder `{SID}` inside the welcome image URL and group description script src:

```html
<img src="upload/surveys/{SID}/images/Barriers_RF.png" ...>
```

```html
<script src="/upload/surveys/{SID}/files/bws_reset.js"></script>
```

These are unavoidable because LimeSurvey assigns the SID on import. `lime_import.py` substitutes `{SID}` in the welcome text automatically post-import; group descriptions with `{SID}` must be verified after import as well.

In LimeSurvey: **Settings → Text elements → Welcome message** → find `{SID}` in the source HTML and replace with the actual numeric SID.

### 2. Upload both required files

Two files must be uploaded after every `--replace` run. `--replace` deletes the survey directory server-side, so both files must be re-uploaded each time. Upload via **LimeSurvey admin → Resources → Files → Upload**:

| File | Destination path |
|---|---|
| `Barriers_RF.png` | `upload/surveys/<SID>/images/` |
| `bws_reset.js` | `upload/surveys/<SID>/files/` |

- `Barriers_RF.png` — branding image shown on the welcome page
- `bws_reset.js` — BWS state-reset script loaded by each barrier-section group description; without it, changing Most/Least selections will not clear stale comparison answers

## Verification walkthrough (do this before going live)

In the LimeSurvey UI:

1. **Survey overview**: confirm 8 groups in this order — Demographics, Main Category Barriers, Economic Barriers, Environmental Barriers, Technological Barriers, Operational Barriers, Social Barriers, Policy Barriers. Confirm 80 questions total.

2. **Welcome page render**: confirm the methodology list (4 numbered steps) and the 4-point scale description appear. The image will be broken until step 2 above is complete.

3. **Demographics conditional fields**: open survey preview.
   - Pick Industry = **Other** → "Please specify Industry Sector" appears as required.
   - Switch to any other industry → the field hides and is no longer enforced.
   - Same for Education = **Other**.

4. **Best-Worst comparisons** (the bug-fix verification from the plan's stress test):
   - Go to **Economic Barriers** (N=4 items, codes A1–A4).
   - Pick Most=A1, Least=A2.
   - **Expected visible**: `EC_mvs_A2`, `EC_mvs_A3`, `EC_mvs_A4` (3 questions) and `EC_vsl_A3`, `EC_vsl_A4` (2 questions) — total 5.
   - **`EC_mvs_A2` must be present**: this is the Most-vs-Least pair. If it's hidden, the original bug from the stress test has come back.
   - Question text on `EC_mvs_A2` should read: *"How much more challenging is **High total cost of fleet ownership** compared to **High infrastructure cost**?"* (with the actual barrier name piped in via `{EC_most.shown}`).

5. **Environmental Barriers** (N=3): Most=A1, Least=A2 → exactly 2 mvs (`EN_mvs_A2`, `EN_mvs_A3`) and 1 vsl (`EN_vsl_A3`).

6. **Most ≠ Least validation**: try selecting the same item for Most and Least → submission is blocked with "Most and Least challenging cannot be the same barrier."

7. **Piping**: every comparison question text shows the actual barrier name, never the answer code (`A1`, `A2`, etc.).

## Known limitations

These are accepted divergences from the HTML survey, all flagged in the original plan and stress-tested:

1. **Lock/unlock UX is dropped.** In the HTML, clicking "Continue to Comparison Questions" locks Q_most/Q_least; unlocking resets all comparison answers in that section. LimeSurvey has no equivalent — comparison answers persist when Most/Least change. Newly irrelevant comparisons hide via relevance equations; newly relevant ones reveal empty.

2. **Page-reveal UX differs.** With `format=G` (group-per-page), all questions in a section are on one page. Comparison questions appear inline as soon as both Q_most and Q_least are answered — there is no explicit "Continue" button. The group description ("Please answer Q1 and Q2 first…") sets the expectation.

3. **No guided tours.** The HTML's spotlight tooltip walkthroughs aren't reproduced. The welcome page methodology list and group descriptions cover the same instructional ground.

4. **No localStorage auto-save.** LimeSurvey provides "Save and resume later" via tokens — enable it on the survey settings if needed (it's already on by default in the generated file: `allowsave=Y`).

5. **Two files must be uploaded manually** after every import (see post-import step 2): `Barriers_RF.png` (branding image) and `bws_reset.js` (BWS state-reset script). Both use `{SID}` placeholders in URLs and both are wiped when `--replace` deletes the survey directory.

6. **Scale-label naming inconsistency**: the welcome page describes the scale as "Slightly more **challenging**…" but the actual answer labels read "Slightly more". Carried forward from `scaleOptions` in the JS source. Cosmetic only.

7. **Total question count nuance**: the `.lss` contains 80 questions. Response exports will have 80 columns — comparison columns will be null when their relevance was false (i.e., the question was hidden for that respondent). Filter accordingly during analysis.

8. **DBVersion**: the script defaults to `649` (LimeSurvey 6.17.x). If you're targeting an older 6.x release and the import fails, override with `--db-version <NNN>` matching your install. The DBVersion check in `import_helper.php` only branches on `< 156`, so any 6.x value should be accepted.

## Schema oracle (optional but recommended)

The plan recommends exporting a minimal hand-built survey from your production LimeSurvey instance to verify the `.lss` schema (tag/attribute names, exact DBVersion). Save it as `.reference/sample.lss` in this directory and diff against `barriers_rfea.lss` for unfamiliar field names. The most likely discrepancy is on `<surveys>` row attributes between LimeSurvey point releases — LimeSurvey is forgiving about extra/missing optional fields, but a sample lets you tune defaults.

## Architecture

The generator is a single-file Python script with three phases:

1. **Parse** — extract `descriptions`, `surveyData`, `scaleOptions` from JS via brace-balanced extraction + JS→JSON normalization (handles `\'` apostrophe escapes, single-quoted strings, unquoted keys, JS comments). Demographic `<select>` options come from `html.parser.HTMLParser` walking the static HTML.

2. **Assemble** — map the parsed model into LimeSurvey rows: 8 groups, 80 questions with relevance equations, 310 answers, 14 validation attributes.

3. **Render** — emit one `<table>` block per logical LimeSurvey table, with all string values wrapped in `<![CDATA[…]]>` (with internal `]]>` sequences split defensively).

All ID counters are deterministic (insertion-order based) so re-runs produce byte-identical output.

## If you find a bug

The five high-priority bugs identified during stress-testing are all addressed:

| # | Bug | Fix location |
|---|---|---|
| 1 | Most-vs-Least comparison would vanish from every section | `_add_section()` — mvs relevance is `_most != "Ai"` only, no longer also excludes `_least != "Ai"` |
| 2 | Counts off (plan said 80 was wrong; actually 80 is correct as a pre-creation count) | Plan + this doc clarified pre-created vs visible |
| 3 | "Other" → "OTH" code mismatch | `INDUSTRY_CODES`, `EDUCATION_CODES` remap explicitly |
| 4 | `Bachelor's`/`Master's` apostrophe in answer codes | `EDUCATION_CODES` maps to `BACH`, `MAST`, `PHD`, `PROF`, `OTH` |
| 5 | `mainCategories` had no tooltip data | `_answer_label()` guards on `has_descriptions and descriptions.get(item)` |

Subsequent issues uncovered during the live LimeSurvey import — append to this file with what you observed and how you resolved it, so future regenerations stay aligned.
