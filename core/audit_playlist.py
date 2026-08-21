"""
audit_playlist.py  -  Phase 0a: audit the ENTIRE playlist before anything else.

Runs FIRST, before OAuth, before the config is even filled. Zero YouTube API
quota, no auth: one yt-dlp flat-playlist read. Produces the facts every later
decision hangs off:

  - video count, duration spread (and the 1hr+ list that needs H:MM:SS chapters
    and a whisper time budget)
  - title-length distribution + which titles would break the 100-char cap once
    the suffix is added
  - year markers already in titles (the replace-vs-factual decision)
  - foreign-channel, private and deleted entries  -  the mixed-dump scope flags
    that catch a mixed-topic playlist early
  - duplicate video ids
  - a title keyword histogram as raw material for the module map (Phase 3)
  - the quota day-plan estimate (push + comments at 50 units each vs the 9,000
    unit daily budget)

Writes into --outdir:
  audit_videos.csv   one row per video (id, title, len, duration, channel, flags)
  audit_report.md    the human summary (feed to Phase 3 and the config interview)

Run:
    python3 core/audit_playlist.py --url "<playlist url>" --outdir channels/<run> \
        --suffix " | MATH 2026"
"""

import argparse
import csv
import json
import os
import re
import statistics
import subprocess
import sys
from collections import Counter
from datetime import date

STOPWORDS = set("""a an and are as at be by for from how in is it of on or the to with
your you what why when we our i part class new all can do does most best complete full
ep episode video videos ka ke ki ko se aur hai kya""".split())

YEAR_PAT = re.compile(r"\b([A-Z]{2,6})\s*(20\d{2})\b")
BARE_YEAR = re.compile(r"\b20\d{2}\b")


def fetch(url):
    p = subprocess.run(["yt-dlp", "--no-warnings", "--flat-playlist", "-J", url],
                       capture_output=True, text=True)
    if not p.stdout.strip():
        sys.exit(f"ERROR: yt-dlp returned nothing. stderr tail: {p.stderr[-400:]}")
    return json.loads(p.stdout)


def hms(sec):
    if sec is None:
        return "?"
    sec = int(sec)
    if sec >= 3600:
        return f"{sec//3600}:{(sec%3600)//60:02d}:{sec%60:02d}"
    return f"{sec//60}:{sec%60:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--suffix", default="", help="planned title suffix, e.g. ' | MATH 2026'")
    args = ap.parse_args()

    data = fetch(args.url)
    title = data.get("title", "(unknown playlist)")
    uploader = data.get("uploader") or data.get("channel") or "?"
    entries = data.get("entries") or []
    os.makedirs(args.outdir, exist_ok=True)

    rows, seen, dupes = [], set(), []
    channels = Counter()
    unavailable = []
    for e in entries:
        vid = e.get("id", "")
        t = e.get("title", "") or ""
        dur = e.get("duration")
        ch = e.get("channel") or e.get("uploader") or ""
        if vid in seen:
            dupes.append(vid)
        seen.add(vid)
        if t in ("[Private video]", "[Deleted video]"):
            unavailable.append((vid, t))
        if ch:
            channels[ch] += 1
        flags = []
        if args.suffix and not t.endswith(args.suffix):
            if len(t) + len(args.suffix) > 100:
                flags.append("suffix_would_exceed_100")
        if len(t) > 100:
            flags.append("already_over_100")
        if dur and dur >= 3600:
            flags.append("hour_plus")
        rows.append({"video_id": vid, "title": t, "title_len": len(t),
                     "duration_s": dur if dur is not None else "",
                     "duration": hms(dur), "channel": ch,
                     "flags": ";".join(flags)})

    durs = [r["duration_s"] for r in rows if r["duration_s"] != ""]
    total_h = sum(durs) / 3600 if durs else 0
    buckets = Counter()
    for d in durs:
        buckets["under 20 min" if d < 1200 else "20-60 min" if d < 3600
                else "1-2 hr" if d < 7200 else "over 2 hr"] += 1
    hour_plus = [r for r in rows if "hour_plus" in r["flags"]]
    suffix_risk = [r for r in rows if "suffix_would_exceed_100" in r["flags"]]

    # year markers in titles
    marker_counts = Counter()
    for r in rows:
        for word, yr in YEAR_PAT.findall(r["title"]):
            marker_counts[f"{word} {yr}"] += 1
        for yr in BARE_YEAR.findall(r["title"]):
            marker_counts.setdefault(f"(bare) {yr}", 0)

    # foreign channels (the mixed-dump detector)
    main_channel = channels.most_common(1)[0][0] if channels else "?"
    foreign = [r for r in rows if r["channel"] and r["channel"] != main_channel]

    # keyword histogram -> module-map raw material
    words = Counter()
    for r in rows:
        for w in re.findall(r"[A-Za-z]{3,}", r["title"].lower()):
            if w not in STOPWORDS:
                words[w] += 1

    # quota day-plan (videos.update=50+1 fetch, commentThreads.insert=50)
    n = len(rows)
    push_units = n * 51
    comment_units = n * 50
    budget = 9000
    push_days = -(-push_units // budget)
    comment_days = -(-comment_units // budget)

    csv_path = os.path.join(args.outdir, "audit_videos.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)

    L = []
    L.append(f"# Playlist audit  -  {title}")
    L.append(f"Generated {date.today().isoformat()} by audit_playlist.py (zero quota, no auth)\n")
    L.append(f"- Uploader: {uploader}")
    L.append(f"- Videos: {n}  (unavailable: {len(unavailable)}, duplicates: {len(dupes)})")
    L.append(f"- Total runtime: {total_h:.1f} hours, median {hms(statistics.median(durs)) if durs else '?'}")
    for b in ["under 20 min", "20-60 min", "1-2 hr", "over 2 hr"]:
        L.append(f"    {b:<12} {buckets.get(b, 0)}")
    L.append(f"- 1hr+ videos (H:MM:SS chapters + whisper budget if caption-less): {len(hour_plus)}")
    L.append(f"- Titles that would exceed 100 chars with suffix '{args.suffix}': {len(suffix_risk)}"
             + ("  -> need hand-rewritten proposed_title" if suffix_risk else ""))
    L.append(f"- Year markers in titles: "
             + (", ".join(f"{k} x{v}" for k, v in marker_counts.most_common(8)) or "none"))
    L.append(f"- Foreign-channel videos (scope flags): {len(foreign)}")
    for r in foreign[:10]:
        L.append(f"    {r['video_id']}  [{r['channel']}]  {r['title'][:60]}")
    L.append(f"- Quota day-plan at {budget}/day: push {push_units} units (~{push_days} days), "
             f"comments {comment_units} units (~{comment_days} days), reads are negligible.")
    L.append("\n## Top title keywords (module-map raw material for Phase 3)")
    for w_, c in words.most_common(30):
        L.append(f"    {w_:<18} {c}")
    L.append(f"\nPer-video table: {os.path.basename(csv_path)}")
    report = "\n".join(L)

    md_path = os.path.join(args.outdir, "audit_report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(report)
    print(f"\nWritten: {csv_path}\nWritten: {md_path}")


if __name__ == "__main__":
    main()
