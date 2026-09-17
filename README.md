# yt-playlist-seo-pipeline

Channel-agnostic YouTube playlist metadata pipeline. For one playlist at a time it
re-optimises every in-scope video's title, description, backend tags, and chapter
timestamps, grounded in each video's own transcript, then updates the playlist's own
metadata and verifies every change against what was approved. Snapshots give revert
insurance at every step.

Landing incrementally since Aug 21, 2026 (see the commit history for progress).
See [`docs/SAFETY_ARCHITECTURE.md`](docs/SAFETY_ARCHITECTURE.md) for exactly which
guardrails are live code today versus still design-only.

## What's live so far

- `core/audit_playlist.py` — zero-quota, no-auth audit of a whole playlist (duration
  spread, title-length risk, year markers, scope flags, keyword clusters, quota
  day-plan) that drives every later decision.
- `core/snapshot.py` — backs up every video's current title/description/tags/category
  before anything changes. The rescue file.
- `core/extract.py` — pulls playlist video metadata into a review CSV, with a tag-
  overlap column that surfaces tag-consistency issues at a glance.
- `core/fetch_thumbnails.py` — capture-only thumbnail download (no API quota needed,
  read-only).
- `core/fetch_transcripts.py` — pulls YouTube's own captions via yt-dlp and scores
  each one's confidence (density + language mix), queuing thin or missing transcripts
  for a Whisper fallback.
- `core/revert.py` / `core/revert_playlist.py` — restore video or playlist metadata
  from a snapshot if something goes wrong. These are the only two scripts in this
  repo today that write to YouTube, and they exist to restore, never to publish a
  rewrite: a wrong-channel guard runs before the first write, every run is a
  dry-run by default with an explicit typed confirmation for a real one, and a
  local log makes a re-run skip whatever it already reverted. Never deletes
  anything. Full detail in `docs/SAFETY_ARCHITECTURE.md`.
- `core/write_descriptions.py` — generates a proposed title, description, and tags
  per video, grounded in that video's transcript. Every piece of channel identity
  (creator, exam, links, hashtags, year tag) comes from `config.json`, never
  hardcoded. Enforces in code (not just in the prompt): the revenue link sits in
  the first 200 characters, every link from the old description still appears in
  the new one (a superset, never a silent drop), every link is actually clickable,
  and control characters the model sometimes emits get stripped before anything
  is compared against the live page. Writes drafts only — nothing is pushed here.
  Now runnable: it resolves each video's transcript source through `verify_truth`
  (imported per-video, so it fails that one video rather than crashing the run).
- `core/build_chapters.py` / `core/chapter_windows.py` — chapter timestamps. A
  short video is chaptered in one pass; a long one is cut into windows and each
  window is labelled *blind to the others*, because the alternative — sampling a
  long transcript and letting the model guess where a topic starts — is how a
  structurally perfect chapter list ends up pointing at the wrong content: the
  timestamp is real (it gets snapped to an actual transcript cue) but the label is
  a lie. The windowed path also refuses to open a chapter inside a sales pitch,
  and repairs any label that comes back truncated or identical to an earlier one
  in the same video. Now runnable: both files import `verify_truth` (below).
- `core/inject_chapters.py` — folds a built chapter block into the proposed
  description: replaces a stale block, inserts a fresh one, or strips one entirely
  from a video that got waived after already being chaptered. Runs standalone
  today (no external dependencies).
- `core/verify_truth.py` — the truth gate: is each chapter actually true of the
  audio at its own timestamp, not just well-formed? Three deterministic,
  cost-free checks feed the model skeptic a narrowed queue instead of asking it
  to re-read everything blind: **source trust** (refuses to build from an
  auto-translated caption track masquerading as the real thing), **promo
  landing** (a chapter may never open inside a sales pitch — a student who
  clicks a chapter title is promised teaching, not an ad), and **label drift**
  (do the label's distinctive words actually occur at its own timestamp, or only
  elsewhere in the video? — the signature of a model that sampled a long
  transcript, guessed a timestamp, and had the guess "snapped" onto a real cue,
  which makes the *time* real while the *label* stays a lie). It also carries the
  label-hygiene, duplicate-label, and generic-label checks that
  `build_chapters.py`/`chapter_windows.py` both import from here rather than
  reimplementing, because two copies of one rule that can silently disagree is
  not a gate. Every check that can be proven is a BLOCKER; everything else is
  narrowed into a REVIEW queue for a human or model skeptic to adjudicate.
  See [`docs/DEFECT_INJECTION_EXAMPLE.md`](docs/DEFECT_INJECTION_EXAMPLE.md)
  and `tests/test_defect_injection_example.py` for a runnable example of the
  rule this gate is built on: a check only counts once it's been watched
  fail on the real defect it exists to catch, not just pass on good input.
- `core/verify_transcripts.py` — the second opinion on a module's timed
  transcripts, by content, run independently of `verify_truth`'s own parser on
  purpose (a shared parser reproduces its own bugs instead of catching them).
  Checks coverage (a transcript that stops at 60% of the runtime silently blinds
  the windowed chapter builder for the rest of the video), script (a file whose
  name asserts English but is mostly a different script), and hallucination
  loops — judged by how the repetition is *spread* across the runtime, not by
  raw count, so a phrase a lesson is legitimately about isn't mistaken for a
  stuck decoder.

- `core/config.template.json` — the one config file every script reads from a
  per-channel working folder. Ships with placeholders, never a previous run's
  working values: a template with real-looking defaults is more dangerous than
  one that fails outright, because a failure is visible and a stale value
  silently publishing onto the wrong channel is not.

More of the engine (the canary-then-batch live push, the machine-diff verifier,
the findings-gate and pre-flight auditor, the OAuth setup script, and the
platform-outcome checks) is still landing — see the commit history for
progress, and `docs/SAFETY_ARCHITECTURE.md` for exactly what that push path is
designed to do versus what's actually runnable here today.

## Operator prompts

`prompts/` holds the two copy-paste prompts this project actually runs on, because
the intended operator is not a coder. `prompts/EXECUTION_COPILOT.md` turns any chat
model into a one-step-at-a-time hand-holder for the live run (state the phase,
state what will happen, wait for confirmation, never advance on a mismatch — the
same discipline that keeps a beginner from fumbling an OAuth grant or a live
push). `prompts/KNOWLEDGE_DESK.md` is the companion teaching prompt: a
question-and-answer desk for understanding the pipeline and rehearsing honest
answers for a client, kept deliberately separate from anything that can touch
YouTube.

## Requirements

```
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.lock
```

`fetch_transcripts.py` also needs [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) on your
PATH (`brew install yt-dlp` or `pip install yt-dlp`); everything else here needs no
external tools.

Every script in `core/` expects to be run from inside a per-channel working folder
that holds a `config.json` copied from `core/config.template.json` with every
placeholder filled in (see the template's own inline comments for what each field
does and why it has no working default).

The OAuth setup script (`auth_setup.py`) and the config-readiness checker
(`assert_config_ready.py`, referenced in `config.template.json`'s own inline
comments) haven't landed in this repo yet. Until then, `snapshot.py`,
`extract.py`, and `revert*.py` (which write to YouTube or
read authenticated data) need a `token.json` you generate yourself via the
YouTube Data API v3 OAuth flow, and you fill in `config.json` by hand against the
template's placeholders; `audit_playlist.py` and
`fetch_thumbnails.py`/`fetch_transcripts.py` need no auth at all (they read
public pages only).
