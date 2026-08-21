"""
snapshot.py

Backs up every video's current metadata (title, description, tags, categoryId,
defaultLanguage, defaultAudioLanguage) to a timestamped CSV file.

This is your rescue file. Keep it safe.

Run this BEFORE update_videos.py.
"""

import csv
import sys
from datetime import datetime

from _helpers import (
    load_config,
    load_youtube_client,
    get_video_ids_in_playlist,
    get_video_snippets,
)


def main():
    config = load_config()
    print(f"Reading playlist: {config['playlist_name']}")
    print(f"Playlist ID: {config['playlist_id']}")
    print()

    youtube = load_youtube_client()

    items = get_video_ids_in_playlist(youtube, config["playlist_id"])
    print(f"Found {len(items)} videos in playlist.")

    if not items:
        print("Playlist is empty. Nothing to snapshot.")
        sys.exit(0)

    video_ids = [item["video_id"] for item in items]
    print("Fetching full metadata for each video...")
    snippets = get_video_snippets(youtube, video_ids)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"snapshot_{timestamp}.csv"

    missing = []
    rows_written = 0

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "video_id",
            "title",
            "description",
            "tags_joined",
            "category_id",
            "default_language",
            "default_audio_language",
            "made_for_kids",
            "privacy_status",
            "snapshot_timestamp",
        ])
        for i, item in enumerate(items, 1):
            vid = item["video_id"]
            if vid not in snippets:
                missing.append(vid)
                print(f"  [{i}/{len(items)}] {vid}: NOT FOUND (private, deleted, or inaccessible)")
                continue

            s = snippets[vid]["snippet"]
            st = snippets[vid]["status"]
            tags = s.get("tags", [])

            row = [
                vid,
                s.get("title", ""),
                s.get("description", ""),
                "|||".join(tags),  # ||| separator, unlikely to appear in real tags
                s.get("categoryId", ""),
                s.get("defaultLanguage", ""),
                s.get("defaultAudioLanguage", ""),
                str(st.get("madeForKids", False)).lower(),
                st.get("privacyStatus", ""),
                timestamp,
            ]
            writer.writerow(row)
            rows_written += 1
            title_preview = s.get("title", "")[:60]
            print(f"  [{i}/{len(items)}] {title_preview}")

    print()
    print(f"Snapshot complete. Saved to: {filename}")
    print(f"Rows written: {rows_written} of {len(items)} videos")

    if missing:
        print()
        print(f"WARNING: {len(missing)} video(s) in the playlist were not accessible:")
        for vid in missing:
            print(f"  - {vid}")
        print("These could be private, deleted, or from another channel.")
        print("They will be skipped by update_videos.py and post_comments.py automatically.")

    print()
    print("KEEP THIS FILE SAFE. It is the only way to revert description changes.")


if __name__ == "__main__":
    main()
