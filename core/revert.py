"""
revert.py

Restores video metadata from a snapshot CSV. Run this if something goes wrong.

Finds the most recent snapshot_*.csv automatically, or use --snapshot FILE.

Flags:
  --snapshot FILE   Use specific snapshot file
  --only VID1,VID2  Only revert these video IDs
  --dry-run         Preview, don't change
  --limit N         Revert first N videos from snapshot

Note: This does NOT delete posted comments. For that, delete manually in Studio.
"""

import argparse
import csv
import glob
import random
import sys
import time
from datetime import datetime

from googleapiclient.errors import HttpError

from _helpers import (
    load_config,
    load_youtube_client,
    with_retry,
    classify_error,
    handle_auth_error_and_exit,
    handle_quota_error_and_exit,
    assert_expected_channel,
    read_completed_ids_from_log,
    append_log,
)

REVERT_LOG_FILE = "revert_log.csv"
LOG_FIELDS = ["timestamp", "video_id", "status", "reason", "error_message", "dry_run"]
SUCCESS_STATUSES = {"reverted"}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--snapshot", type=str, default=None)
    p.add_argument("--only", type=str, default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def find_latest_snapshot():
    files = sorted(glob.glob("snapshot_*.csv"), reverse=True)
    if not files:
        print("ERROR: No snapshot_*.csv files found in this folder.")
        print("Cannot revert without a snapshot.")
        sys.exit(1)
    return files[0]


def log(video_id, status, reason="", error_message="", dry_run=False):
    append_log(REVERT_LOG_FILE, {
        "timestamp": datetime.now().isoformat(),
        "video_id": video_id,
        "status": status,
        "reason": reason,
        "error_message": error_message[:500] if error_message else "",
        "dry_run": str(dry_run),
    }, LOG_FIELDS)


def main():
    args = parse_args()

    snapshot_file = args.snapshot or find_latest_snapshot()
    print(f"Using snapshot: {snapshot_file}")

    rows = []
    with open(snapshot_file, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    print(f"Snapshot contains {len(rows)} videos.")

    # Filters
    if args.only:
        only_ids = set(args.only.split(","))
        rows = [r for r in rows if r["video_id"] in only_ids]
        print(f"Filtering to {len(rows)} videos per --only.")

    # Idempotency on revert: skip already-reverted unless --force
    if not args.force and not args.dry_run:
        already_reverted = read_completed_ids_from_log(REVERT_LOG_FILE, SUCCESS_STATUSES)
        if already_reverted:
            print(f"Found {len(already_reverted)} videos already reverted, skipping.")
            rows = [r for r in rows if r["video_id"] not in already_reverted]

    if args.limit:
        rows = rows[:args.limit]

    if not rows:
        print("Nothing to revert.")
        sys.exit(0)

    if args.dry_run:
        print()
        print("DRY RUN MODE, no changes will be made")
    else:
        print()
        print("WARNING: This will OVERWRITE current YouTube metadata with")
        print("the values from the snapshot. Existing changes will be erased.")
        confirm = input("Proceed? Type 'yes' to confirm: ").strip().lower()
        if confirm != "yes":
            print("Cancelled.")
            sys.exit(0)

    youtube = load_youtube_client()

    # Wrong-channel guard: confirm we are on the intended channel before writing.
    config = load_config()
    assert_expected_channel(youtube, config)

    reverted = 0
    errors = 0

    for i, row in enumerate(rows, 1):
        vid = row["video_id"]
        old_title = row["title"]
        old_description = row["description"]
        old_tags = row["tags_joined"].split("|||") if row["tags_joined"] else []
        old_category_id = row["category_id"] or "22"

        snippet = {
            "title": old_title,
            "description": old_description,
            "tags": old_tags,
            "categoryId": old_category_id,
        }
        if row.get("default_language"):
            snippet["defaultLanguage"] = row["default_language"]
        if row.get("default_audio_language"):
            snippet["defaultAudioLanguage"] = row["default_audio_language"]

        print(f"[{i}/{len(rows)}] Reverting {vid}")
        print(f"  Restoring title: {old_title[:60]}")

        if args.dry_run:
            log(vid, "dry_run_ok", "", "", True)
            continue

        def do_revert():
            return youtube.videos().update(
                part="snippet",
                body={"id": vid, "snippet": snippet},
            ).execute()

        result, err = with_retry(do_revert, max_attempts=3, base_delay=2.0)

        if err is not None:
            category, status, reason = classify_error(err)
            if category == "auth":
                log(vid, "error", "auth", str(err), False)
                handle_auth_error_and_exit(err)
            if category == "quota":
                # Daily allowance used up: stop cleanly, resume tomorrow.
                log(vid, "halted", "quota", str(err), False)
                handle_quota_error_and_exit(err)
            print(f"  ERROR: {reason} (status {status})")
            log(vid, "error", reason, str(err), False)
            errors += 1
            time.sleep(random.uniform(1.0, 2.0))
            continue

        print(f"  Reverted.")
        log(vid, "reverted", "", "", False)
        reverted += 1
        time.sleep(random.uniform(1.0, 2.0))

    print()
    print("=" * 70)
    print(f"Done. Reverted: {reverted}, Errors: {errors}")
    print(f"Log: {REVERT_LOG_FILE}")
    print("=" * 70)
    print()
    print("Reminder: this does NOT remove comments posted via post_comments.py.")
    print("Delete comments manually in YouTube Studio if needed.")


if __name__ == "__main__":
    main()
