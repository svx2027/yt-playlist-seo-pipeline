# Safety architecture

This pipeline can rewrite the title, description, and tags on every video in a
real playlist, on a real channel. Every guard below exists because a script
with write access to a live channel is dangerous by default, not because of a
single incident. This page is split into two honest halves: what is runnable
code in this repo right now, and what is designed but not shipped here yet.
Nothing in the second half is invented — it is drawn from what
`prompts/EXECUTION_COPILOT.md` and `core/config.template.json` already commit
to in writing, and from the incident record in `docs/LEARNINGS_REGISTER.md`.

## Live in this repo today

**Snapshot before anything.** `core/snapshot.py` reads every video currently in
the playlist and writes its title, description, tags, category, and language
fields to a timestamped `snapshot_*.csv` before any other script touches
anything. The script calls this file "the rescue file" and refuses to run
past an empty playlist. Nothing downstream is allowed to skip this step by
convention alone — `revert.py` and `revert_playlist.py` simply have nothing to
restore from if it wasn't taken.

**Wrong-channel guard.** `core/_helpers.py`'s `assert_expected_channel()` asks
YouTube "who am I logged in as?" (`channels().list(mine=True)`) and compares
the answer against `expected_channel_id` in `config.json`. Every script that
changes something calls this before its first write; a mismatch — or a
missing config field — stops the run with nothing touched. Read-only scripts
(`snapshot.py`, `extract.py`) don't need it because they change nothing.

**Dry-run plus an explicit typed confirmation.** `core/revert.py` and
`core/revert_playlist.py` both default to previewing what they would restore.
A real run requires the operator to type `yes` at a prompt after seeing the
before/after values printed to the screen — there is no flag that skips this
for a live run.

**Idempotent, resumable retries.** `core/_helpers.py`'s `with_retry()` retries
a transient error (HTTP 429/500/502/503/504) with exponential backoff, and
`classify_error()` treats a quota error as its own category: `revert.py` stops
the whole run cleanly on `quotaExceeded`/`rateLimitExceeded`/
`dailyLimitExceeded` rather than burning retries against a wall that won't
open until tomorrow. Every write is logged to a CSV as it happens
(`read_completed_ids_from_log()` / `append_log()`), so re-running the same
command after a quota halt, a crash, or a Ctrl-C skips whatever already
succeeded instead of repeating it.

**Restore, never delete, and never confuse a restore point with its own
backup.** `revert.py` restores video metadata from a snapshot and never issues
a delete call anywhere in this repo. `revert_playlist.py` restores the
playlist's own title/description, and — because a restore can itself need
undoing — backs up the *current* values to a `playlist_prerevert_*.json`
file, under a deliberately distinct filename prefix, before writing anything,
so a prerevert backup is never mistaken for the original restore source on a
later run.

**A content-level truth gate, not just a shape check.** `core/verify_truth.py`
asks whether a generated chapter label is actually true of the audio at its
own timestamp, because a structurally perfect chapter list (right count,
ascending order, correct minimum gaps) can still be pointing at the wrong
content — see [`docs/DEFECT_INJECTION_EXAMPLE.md`](DEFECT_INJECTION_EXAMPLE.md)
for a runnable example of the exact incident this exists to catch, and a
watched false negative from the test that proves it.

**Draft-only generation.** `core/write_descriptions.py` writes every proposed
title, description, and tag set to a CSV. Nothing it produces is pushed to
YouTube by anything in this repo today.

## Designed, not yet shipped in this repo

`core/config.template.json` already reserves a `batch` block
(`size`, `quota_daily_budget`) and a full `csv_paths` set — `proposed`,
`approved`, `update_log`, `comments_log` — for a live push path.
`prompts/EXECUTION_COPILOT.md` commits to what that path does in writing:
"dry-run → ONE canary → verify the canary live → rest of the batch," with
only rows marked `APPROVED` in the CSV eligible to push, and a final
machine-diff verify pass afterward. `docs/LEARNINGS_REGISTER.md` records real
incidents against that exact design — R87 ("the canary must be targetable,"
on `push.py`'s `--only <video_id>` flag) and R84 (a control character that
made a live description silently stop matching the approved one, caught by
`verify_diff`).

None of that is fiction — it's the documented design this repo is built
toward, and `_helpers.py`'s own module docstring lists the sibling scripts it
was written to share code with: `update_videos.py`, `post_comments.py`, and
`verify.py`. But as of this writing, **none of those three files exist in
this repo's `core/`.** The only two scripts here that write to YouTube at all
are `revert.py` and `revert_playlist.py`, and both exist to restore a
snapshot, not to publish a new rewrite. Until the push path lands, treat any
mention of a live canary-then-batch push against this specific repo as the
target design, not a claim about what you can run today.

| Guard | Where | Live in this repo? |
|---|---|---|
| Snapshot before any change | `core/snapshot.py` | Yes |
| Wrong-channel guard | `core/_helpers.py` (`assert_expected_channel`) | Yes |
| Dry-run + explicit typed confirmation | `core/revert.py`, `core/revert_playlist.py` | Yes |
| Idempotent resume on re-run | `core/revert.py` (`read_completed_ids_from_log`) | Yes |
| Quota-aware halt (never retries into a wall) | `core/_helpers.py` (`handle_quota_error_and_exit`) | Yes |
| Content-truth gate on generated chapters | `core/verify_truth.py` | Yes |
| Draft-only description/tag generation | `core/write_descriptions.py` | Yes |
| Canary-then-batch live push | designed in `prompts/EXECUTION_COPILOT.md` + `core/config.template.json`'s `batch`/`csv_paths` | Not yet ported (no `update_videos.py`/`post_comments.py` here) |
| Machine-diff verify against the approved CSV | referenced in `core/_helpers.py`'s docstring and `docs/LEARNINGS_REGISTER.md` (R84) | Not yet ported (no `verify.py` here) |

This page gets updated the day those scripts land in `core/` — see the commit
history for progress in the meantime.
