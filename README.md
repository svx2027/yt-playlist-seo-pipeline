# yt-playlist-seo-pipeline

Channel-agnostic YouTube playlist metadata pipeline. For one playlist at a time it
re-optimises every in-scope video's title, description, backend tags, and chapter
timestamps, grounded in each video's own transcript, then updates the playlist's own
metadata and verifies every change against what was approved. Snapshots give revert
insurance at every step.

Landing here Aug 21-27, 2026 (see the commit history for progress).

## What's live so far

- `core/audit_playlist.py` — zero-quota, no-auth audit of a whole playlist (duration
  spread, title-length risk, year markers, scope flags, keyword clusters, quota
  day-plan) that drives every later decision.
- `core/snapshot.py` — backs up every video's current title/description/tags/category
  before anything changes. The rescue file.
- `core/extract.py` — pulls playlist video metadata into a review CSV, with a tag-
  overlap column that surfaces tag-consistency issues at a glance.
- `core/fetch_thumbnails.py` — capture-only thumbnail download (no API quota, no
  YouTube write path exists anywhere in this project).
- `core/fetch_transcripts.py` — pulls YouTube's own captions via yt-dlp and scores
  each one's confidence (density + language mix), queuing thin or missing transcripts
  for a Whisper fallback.
- `core/revert.py` / `core/revert_playlist.py` — restore video or playlist metadata
  from a snapshot if something goes wrong. Never deletes anything.

More of the engine (description generation, chapter timestamps, the auditor/skeptic
verification layer) is landing over the next few days.

## Requirements

```
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.lock
```

`fetch_transcripts.py` also needs [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) on your
PATH (`brew install yt-dlp` or `pip install yt-dlp`); everything else here needs no
external tools.

Every script in `core/` expects to be run from inside a per-channel working folder
that holds a `config.json` with at least:

```json
{
  "playlist_id": "PL...",
  "playlist_name": "My playlist",
  "channel_name": "My channel",
  "expected_channel_id": "UC...",
  "csv_paths": { "extracted": "descriptions_extracted.csv" },
  "transcripts": { "langs": ["en"] }
}
```

`config.template.json` and the OAuth setup script (`auth_setup.py`) haven't landed in
this repo yet — they're a couple of days out. Until then, `snapshot.py`, `extract.py`,
and `revert*.py` (which write to YouTube or read authenticated data) need a
`token.json` you generate yourself via the YouTube Data API v3 OAuth flow;
`audit_playlist.py` and `fetch_thumbnails.py`/`fetch_transcripts.py` need no auth at
all (they read public pages only).
