"""
fetch_transcripts.py

Downloads YouTube's own captions (subtitles) for the videos we are working on and turns them
into a clean plain-text transcript file per video. Primary path (yt-dlp, captions only, no video,
zero API quota).

CONFIDENCE + WHISPER FALLBACK:
  Caption tracks can be thin or the wrong-language ASR. For every video we now record a
  CONFIDENCE score:
    - chars_per_min = cleaned chars / (duration minutes). Below MIN_CPM (~300) the captions are too
      thin for the video length, or a wrong-language ASR ran -> confidence LOW -> needs Whisper.
    - devanagari_pct = share of letters that are Devanagari (a quick Hindi-vs-English read).
  A LOW or missing transcript is the queue for the Whisper + LLM-cleanup fallback (a later phase).

LANGUAGES come from config['transcripts']['langs'], plus 'en-orig' is always requested as a
probe: if a video exposes an en-orig track, English is the original spoken language.

Idempotent: a video already TRANSCRIPT_OK on disk is skipped. No credentials, no login, no quota;
this script changes nothing on YouTube, it only reads public captions.

Run from inside a channel working folder (the folder with config.json):
    python3 ../../core/fetch_transcripts.py --limit 10     # pilot on the first 10
    python3 ../../core/fetch_transcripts.py --only <id>     # one video
    python3 ../../core/fetch_transcripts.py                 # all in scope
"""

import argparse
import csv
import glob
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time


CONFIG_FILE = "config.json"

TRANSCRIPT_DIR = "transcripts"
STATUS_FILE = os.path.join(TRANSCRIPT_DIR, "transcripts_status.csv")
STATUS_FIELDS = ["video_id", "status", "txt_chars", "duration_s", "chars_per_min",
                 "devanagari_pct", "confidence", "langs_found", "note"]

# Fallback caption languages if config has none. 'en-orig' is appended as a probe.
DEFAULT_SUB_LANGS = ["hi", "en"]

# A clean transcript must clear this absolute floor...
MIN_OK_CHARS = 500
# ...and be dense enough against the video's duration. Under ~300 chars/minute the captions are
# too thin or a wrong-language ASR ran: flag for the Whisper fallback.
MIN_CPM = 300

STATUS_OK = "TRANSCRIPT_OK"
STATUS_NO_CAPTIONS = "NO_CAPTIONS"        # yt-dlp confirmed the video has no caption tracks
STATUS_LIVE_CHAT_ONLY = "LIVE_CHAT_ONLY"
STATUS_FETCH_ERROR = "FETCH_ERROR"
STATUS_RATE_LIMITED = "RATE_LIMITED"     # HTTP 429: captions may exist; retry a later sitting

# Slower than a light pilot run: YouTube 429s the caption endpoint aggressively at volume.
SLEEP_MIN = 6.0
SLEEP_MAX = 12.0

# Browser impersonation (needs curl_cffi in yt-dlp's env) makes yt-dlp look like a real browser
# and greatly reduces HTTP 429 / bot-detection. Set to "" to disable.
# List targets with: yt-dlp --list-impersonate-targets
IMPERSONATE = "chrome"

# Authenticated caption fetch via a logged-in browser's cookies (yt-dlp --cookies-from-browser).
# Authenticated requests get a far higher YouTube rate limit than anonymous ones, which is the
# reliable fix for bulk 429s. Set to "" to disable. Format: "firefox" or "firefox:PROFILE_PATH".
COOKIES_FROM_BROWSER = "firefox"


def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"ERROR: {CONFIG_FILE} not found. Run from inside a channel working folder.")
        print(f"You're running from: {os.getcwd()}")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)


def check_yt_dlp():
    path = shutil.which("yt-dlp")
    if path:
        return path
    print("ERROR: yt-dlp is not installed (or not on your PATH).")
    print("Install with:  brew install yt-dlp   then check:  yt-dlp --version")
    sys.exit(1)


def find_video_id_csv(config):
    """Prefer the extracted CSV; else the newest snapshot_*.csv (both have a 'video_id' column).
    snapshot.py writes flat snapshot_<timestamp>.csv files in this folder, not a subdirectory."""
    csv_paths = config.get("csv_paths", {})
    extracted = csv_paths.get("extracted", "descriptions_extracted.csv")
    if extracted and os.path.exists(extracted):
        return extracted
    candidates = sorted(glob.glob("snapshot_*.csv"), key=os.path.getmtime, reverse=True)
    return candidates[0] if candidates else None


def read_video_ids(csv_path):
    ids, seen = [], set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "video_id" not in (reader.fieldnames or []):
            print(f"ERROR: {csv_path} has no 'video_id' column. Columns: {reader.fieldnames}")
            sys.exit(1)
        for row in reader:
            vid = (row.get("video_id") or "").strip()
            if vid and vid not in seen:
                seen.add(vid); ids.append(vid)
    return ids


def load_durations():
    """video_id -> duration in seconds, from audit_videos.csv if present (for chars/min)."""
    dur = {}
    if os.path.exists("audit_videos.csv"):
        with open("audit_videos.csv", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    dur[row["video_id"]] = int(row.get("duration_s") or 0)
                except (ValueError, KeyError):
                    pass
    return dur


def load_status():
    existing = {}
    if not os.path.exists(STATUS_FILE):
        return existing
    try:
        with open(STATUS_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("video_id"):
                    existing[row["video_id"]] = row
    except Exception:
        pass
    return existing


def write_status(rows_by_id):
    with open(STATUS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=STATUS_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for vid in sorted(rows_by_id.keys()):
            row = {k: rows_by_id[vid].get(k, "") for k in STATUS_FIELDS}
            row["video_id"] = vid
            writer.writerow(row)


TIMESTAMP_RE = re.compile(r"-->")
INLINE_TAG_RE = re.compile(r"<[^>]+>")


def clean_vtt_to_text(vtt_path):
    """Turn a .vtt caption file into clean prose (drop header/cues/timestamps/inline tags and the
    rolling-caption duplicates YouTube auto-subs produce)."""
    with open(vtt_path, encoding="utf-8", errors="replace") as f:
        raw_lines = f.readlines()
    lines = []
    for line in raw_lines:
        line = line.rstrip("\n")
        if line.startswith("WEBVTT"):
            continue
        if line.startswith(("Kind:", "Language:", "NOTE", "STYLE", "REGION")):
            continue
        if TIMESTAMP_RE.search(line):
            continue
        if line.strip().isdigit():
            continue
        line = INLINE_TAG_RE.sub("", line).strip()
        if not line:
            continue
        lines.append(line)
    deduped, prev = [], ""
    for line in lines:
        if line == prev:
            continue
        if prev and line in prev:
            continue
        if prev and prev in line:
            if deduped:
                deduped[-1] = line
            else:
                deduped.append(line)
            prev = line
            continue
        deduped.append(line); prev = line
    return "\n".join(deduped).strip()


def run_yt_dlp(video_id, sub_langs):
    """Fetch captions (only) for one video. sub_langs is a comma string (e.g. 'hi,en,en-orig')."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    out_template = os.path.join(TRANSCRIPT_DIR, "%(id)s")
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-auto-subs",
        "--write-subs",
        "--sub-langs", sub_langs,
        "--sub-format", "vtt",
        "--retries", "5",
        "--extractor-retries", "3",
        "--sleep-subtitles", "3",
        "--sleep-requests", "1",
        "-o", out_template,
        "--no-warnings",
    ]
    if IMPERSONATE:
        cmd += ["--impersonate", IMPERSONATE]
    if COOKIES_FROM_BROWSER:
        cmd += ["--cookies-from-browser", COOKIES_FROM_BROWSER]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def collect_output_files(video_id):
    # EXCLUDE <id>.whisper.vtt: that is OUR OWN whisper output, not a YouTube caption track.
    # Without this the glob would hand pick_best_vtt a file with lang_of() == "whisper" and, on a
    # video with no captions, silently derive the .txt and the confidence score FROM OUR OWN
    # TRANSCRIPT while reporting it as a fetched caption.
    vtt_files = sorted(p for p in glob.glob(os.path.join(TRANSCRIPT_DIR, f"{video_id}.*.vtt"))
                       if not p.endswith(".whisper.vtt"))
    live_chat_files = sorted(glob.glob(os.path.join(TRANSCRIPT_DIR, f"{video_id}.live_chat.json")))
    return vtt_files, live_chat_files


def pick_best_vtt(vtt_files, preference):
    """Choose the best caption file by the config-driven preference order. Returns (path, lang)."""
    def lang_of(path):
        parts = os.path.basename(path).split(".")
        return parts[-2] if len(parts) >= 3 else ""
    by_lang = {lang_of(p): p for p in vtt_files}
    for lang in preference:
        if lang in by_lang:
            return by_lang[lang], lang
    first = vtt_files[0]
    return first, lang_of(first)


def devanagari_pct(text):
    """Percent of alphabetic characters that are Devanagari (a quick Hindi-vs-English read)."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    dev = sum(1 for c in letters if "ऀ" <= c <= "ॿ")
    return 100.0 * dev / len(letters)


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def process_one(video_id, sub_langs, preference, duration_s):
    """Fetch + clean captions for one video. Returns a status row dict incl. a confidence score."""
    base = {"video_id": video_id, "status": "", "txt_chars": "0",
            "duration_s": str(duration_s or ""), "chars_per_min": "", "devanagari_pct": "",
            "confidence": "", "langs_found": "", "note": ""}
    returncode, stdout, stderr = run_yt_dlp(video_id, sub_langs)
    vtt_files, live_chat_files = collect_output_files(video_id)

    if returncode != 0 and not vtt_files:
        if live_chat_files:
            for p in live_chat_files:
                _safe_remove(p)
            return {**base, "status": STATUS_LIVE_CHAT_ONLY, "langs_found": "live_chat",
                    "confidence": "NONE", "note": "Only a live-chat replay existed; not a transcript."}
        blob = ((stderr or "") + " " + (stdout or "")).lower()
        if "429" in blob or "too many requests" in blob:
            return {**base, "status": STATUS_RATE_LIMITED, "confidence": "NONE",
                    "note": "HTTP 429 (rate limited). Captions may exist; retry a later sitting."}
        tail = (stderr or stdout or "").strip().splitlines()
        tail_text = " | ".join(tail[-3:])[:400] if tail else "(no output)"
        return {**base, "status": STATUS_FETCH_ERROR, "confidence": "NONE",
                "note": f"yt-dlp exit {returncode}: {tail_text}"}

    if not vtt_files and live_chat_files:
        for p in live_chat_files:
            _safe_remove(p)
        return {**base, "status": STATUS_LIVE_CHAT_ONLY, "langs_found": "live_chat",
                "confidence": "NONE", "note": "Only a live-chat replay existed; not a transcript."}

    if not vtt_files:
        return {**base, "status": STATUS_NO_CAPTIONS, "confidence": "NONE",
                "note": "No caption tracks. Whisper-fallback candidate."}

    langs_found = ",".join(sorted({os.path.basename(p).split(".")[-2] for p in vtt_files
                                   if len(os.path.basename(p).split(".")) >= 3}))
    best_vtt, best_lang = pick_best_vtt(vtt_files, preference)
    text = clean_vtt_to_text(best_vtt)
    txt_path = os.path.join(TRANSCRIPT_DIR, f"{video_id}.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)

    n = len(text)
    dev = devanagari_pct(text)
    cpm = (n / (duration_s / 60.0)) if duration_s else 0.0

    if n <= MIN_OK_CHARS:
        status, conf = STATUS_NO_CAPTIONS, "LOW"
        note = f"Caption present but only {n} chars after cleaning; needs whisper."
    elif duration_s and cpm < MIN_CPM:
        status, conf = STATUS_OK, "LOW"
        note = (f"Thin captions: {cpm:.0f} chars/min < {MIN_CPM} over "
                f"{duration_s // 60}m ({best_lang}); needs whisper.")
    else:
        status, conf = STATUS_OK, "OK"
        note = f"Cleaned from '{best_lang}' captions; {cpm:.0f} chars/min."

    return {**base, "status": status, "txt_chars": str(n), "chars_per_min": f"{cpm:.0f}",
            "devanagari_pct": f"{dev:.0f}", "confidence": conf,
            "langs_found": langs_found, "note": note}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="process only the first N videos (pilot)")
    ap.add_argument("--only", default=None, help="single video_id")
    args = ap.parse_args()

    config = load_config()
    check_yt_dlp()
    print(f"Channel: {config.get('channel_name', '(unnamed)')}")

    langs = config.get("transcripts", {}).get("langs") or DEFAULT_SUB_LANGS
    preference = list(dict.fromkeys(list(langs) + ["en-orig"]))  # config order, en-orig probe last
    sub_langs = ",".join(preference)
    print(f"Caption languages (from config + en-orig probe): {sub_langs}")

    csv_path = find_video_id_csv(config)
    if not csv_path:
        print("ERROR: no CSV with video IDs. Run extract.py (or snapshot.py) first.")
        sys.exit(1)
    print(f"Reading video IDs from: {csv_path}")
    video_ids = read_video_ids(csv_path)
    if args.only:
        video_ids = [args.only]
    elif args.limit:
        video_ids = video_ids[:args.limit]
    print(f"Videos to process: {len(video_ids)}")
    if not video_ids:
        sys.exit(0)

    durations = load_durations()
    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    status_rows = load_status()

    counts = {STATUS_OK: 0, STATUS_NO_CAPTIONS: 0, STATUS_LIVE_CHAT_ONLY: 0,
              STATUS_FETCH_ERROR: 0, STATUS_RATE_LIMITED: 0}
    low_conf = 0
    skipped = 0

    print()
    for i, vid in enumerate(video_ids, 1):
        txt_path = os.path.join(TRANSCRIPT_DIR, f"{vid}.txt")
        prior = status_rows.get(vid)
        if (prior and prior.get("status") == STATUS_OK
                and prior.get("confidence") == "OK" and os.path.exists(txt_path)):
            print(f"[{i}/{len(video_ids)}] {vid}: already OK, skipping")
            skipped += 1
            continue

        print(f"[{i}/{len(video_ids)}] {vid}: fetching captions...")
        row = process_one(vid, sub_langs, preference, durations.get(vid, 0))
        status_rows[vid] = row
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        if row.get("confidence") == "LOW":
            low_conf += 1
        print(f"    {row['status']} | conf={row.get('confidence','')} | "
              f"{row.get('txt_chars','0')} chars | {row.get('chars_per_min','')} cpm | "
              f"deva={row.get('devanagari_pct','')}% | langs={row.get('langs_found','')}")
        write_status(status_rows)
        if row["status"] == STATUS_RATE_LIMITED:
            print("\n  HTTP 429 rate limit hit. Halting this sitting (never loop retries on a 429).")
            print("  Wait ~30-60 min (or add impersonation/cookies), then re-run; the log skips done videos.")
            break
        if i < len(video_ids):
            time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

    write_status(status_rows)

    print("\n" + "=" * 70)
    print("Done.")
    print(f"  {STATUS_OK}:      {counts[STATUS_OK]}")
    print(f"  {STATUS_NO_CAPTIONS}:    {counts[STATUS_NO_CAPTIONS]}")
    print(f"  {STATUS_LIVE_CHAT_ONLY}: {counts[STATUS_LIVE_CHAT_ONLY]}")
    print(f"  {STATUS_FETCH_ERROR}:    {counts[STATUS_FETCH_ERROR]}")
    print(f"  {STATUS_RATE_LIMITED}:   {counts[STATUS_RATE_LIMITED]}  (429; captions likely exist, retry later)")
    print(f"  LOW confidence (needs whisper): {low_conf}")
    print(f"  Skipped (already OK):           {skipped}")
    print(f"  Status record: {STATUS_FILE}")
    print("=" * 70)
    needs = counts[STATUS_NO_CAPTIONS] + counts[STATUS_LIVE_CHAT_ONLY] + counts[STATUS_FETCH_ERROR] + low_conf
    print(f"Whisper-fallback queue (no/low/failed captions): {needs}")


if __name__ == "__main__":
    main()
