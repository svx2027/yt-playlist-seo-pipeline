"""
inject_chapters.py  -  fold the Phase 6b chapter blocks into the descriptions.

Takes each valid block from chapters_by_video.json and places it in that video's
proposed_description: REPLACES an existing "Chapters:" block (relabel case) or
INSERTS one just before the links block (create case). Nothing else in the
description is touched, so the approved hook, bullets, tags and hashtags are
preserved. Only edits descriptions_proposed.csv; re-run merge.py afterwards.

Run (channel folder, venv active):
    python3 ../../core/inject_chapters.py
"""

import csv
import json


def strip_chapters_block(desc):
    """Remove any existing 'Chapters:' section. Shared by inject() (which re-adds a fresh one
    right after) and main()'s waiver path (which removes and does NOT re-add)."""
    return "\n\n".join(p for p in desc.split("\n\n") if not p.lstrip().startswith("Chapters:"))


def inject(desc, block):
    """Place the chapter block right AFTER the overview bullets. Removes any
    existing 'Chapters:' block first, so this is idempotent and self-heals a
    mis-placed one. Must NOT key off the link, because the hook contains it."""
    parts = strip_chapters_block(desc).split("\n\n")
    overview_idx = None
    for i, p in enumerate(parts):
        if any(ln.strip().startswith("- ") for ln in p.splitlines()):
            overview_idx = i
    insert_at = (overview_idx + 1) if overview_idx is not None else 1
    parts.insert(insert_at, block)
    return "\n\n".join(parts)


def main():
    cfg = json.load(open("config.json", encoding="utf-8"))
    ch = json.load(open("chapters_by_video.json", encoding="utf-8"))
    path = cfg["csv_paths"]["proposed"]
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    fields = list(rows[0].keys())

    n = stripped = 0
    for r in rows:
        c = ch.get(r["video_id"])
        if c and c.get("valid") and c.get("block"):
            new = inject(r["proposed_description"], c["block"])
            if new != r["proposed_description"]:
                r["proposed_description"] = new
                n += 1
            continue
        # ⚠️ THE MISSING HALF of this same class of bug. A video can go VALID -> INVALID after already
        # having been injected (one real case, 18 Jul 2026: a defective 3rd chapter was
        # dropped, the video became a 2-chapter waiver, but main() only ever called inject() for
        # the valid branch and SKIPPED everything else, so the description kept the STALE
        # "Chapters:" block from before the drop -- the exact defect the correction existed to
        # remove, now shipping in the artifact that actually pushes). A waived/invalid video must
        # carry NO chapters section at all, so strip one if it is there.
        new = strip_chapters_block(r["proposed_description"])
        if new != r["proposed_description"]:
            r["proposed_description"] = new
            stripped += 1
    w = csv.DictWriter(open(path, "w", newline="", encoding="utf-8"), fieldnames=fields)
    w.writeheader()
    w.writerows(rows)
    print(f"{path}: chapters injected into {n} of {len(rows)} rows"
          + (f", stale block stripped from {stripped} waived row(s)" if stripped else ""))


if __name__ == "__main__":
    main()
