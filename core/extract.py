"""
extract.py: pull playlist video metadata into CSV for review.

Reads playlist_id from config.json, fetches every video's title, description,
tags, category, language. Writes to descriptions_extracted.csv.

Adds a `tag_overlap_count` column: how many other videos in the playlist share
at least one tag with this video. Surfaces tag-consistency issues at a glance.

Run from inside venv:
    source venv/bin/activate
    python3 extract.py

Output: descriptions_extracted.csv in the same folder.
"""

import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request


CONFIG_PATH = "config.json"
TOKEN_PATH = "token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]


def load_config():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    if cfg.get("playlist_id", "").startswith("<"):
        sys.exit("ERROR: config.json has placeholder playlist_id. Update it first.")
    return cfg


def get_authenticated_service():
    if not os.path.exists(TOKEN_PATH):
        sys.exit("ERROR: token.json not found. Run auth_setup.py first.")
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
            print("Token refreshed automatically.")
        except Exception as e:
            sys.exit(f"ERROR: token refresh failed. Re-run auth_setup.py. Details: {e}")
    return build("youtube", "v3", credentials=creds)


def fetch_playlist_video_ids(youtube, playlist_id):
    ids = []
    page_token = None
    while True:
        resp = youtube.playlistItems().list(
            part="contentDetails,snippet",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for item in resp.get("items", []):
            ids.append({
                "video_id": item["contentDetails"]["videoId"],
                "position": item["snippet"]["position"],
            })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def fetch_video_details(youtube, video_ids):
    """Batch-fetch up to 50 video IDs at a time."""
    details = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = youtube.videos().list(
            part="snippet",
            id=",".join(batch),
        ).execute()
        for item in resp.get("items", []):
            sn = item["snippet"]
            details[item["id"]] = {
                "title": sn.get("title", ""),
                "description": sn.get("description", ""),
                "tags": sn.get("tags", []),
                "category_id": sn.get("categoryId", ""),
                "default_language": sn.get("defaultLanguage", ""),
                "default_audio_language": sn.get("defaultAudioLanguage", ""),
            }
    return details


def compute_tag_overlap(details_by_id):
    """For each video, count how many OTHER videos share at least one tag."""
    overlap = {}
    all_tags = {vid: set(d["tags"]) for vid, d in details_by_id.items()}
    for vid, tags in all_tags.items():
        if not tags:
            overlap[vid] = 0
            continue
        count = 0
        for other_vid, other_tags in all_tags.items():
            if other_vid == vid:
                continue
            if tags & other_tags:
                count += 1
        overlap[vid] = count
    return overlap


def main():
    cfg = load_config()
    youtube = get_authenticated_service()

    playlist_id = cfg["playlist_id"]
    out_path = cfg.get("csv_paths", {}).get("extracted", "descriptions_extracted.csv")

    print(f"Reading playlist: {cfg.get('playlist_name', playlist_id)}")
    print(f"Playlist ID: {playlist_id}")

    items = fetch_playlist_video_ids(youtube, playlist_id)
    if not items:
        sys.exit("ERROR: no videos found in playlist. Check playlist_id.")
    print(f"Found {len(items)} videos in playlist.")

    video_ids = [i["video_id"] for i in items]
    position_by_id = {i["video_id"]: i["position"] for i in items}

    print("Fetching full metadata for each video...")
    details = fetch_video_details(youtube, video_ids)
    overlap = compute_tag_overlap(details)

    rows = []
    for vid in video_ids:
        d = details.get(vid)
        if not d:
            print(f"  WARNING: video {vid} not accessible, skipping.")
            continue
        rows.append({
            "video_id": vid,
            "position": position_by_id.get(vid, ""),
            "title": d["title"],
            "original_description": d["description"],
            "original_tags": ", ".join(d["tags"]),
            "current_category": d["category_id"],
            "current_language": d["default_language"],
            "current_audio_language": d["default_audio_language"],
            "tag_count": len(d["tags"]),
            "tag_overlap_count": overlap.get(vid, 0),
            "description_length": len(d["description"]),
        })

    fieldnames = [
        "video_id", "position", "title",
        "original_description", "original_tags",
        "current_category", "current_language", "current_audio_language",
        "tag_count", "tag_overlap_count", "description_length",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nExtract complete. Saved to: {out_path}")
    print(f"Rows written: {len(rows)} of {len(items)} videos")
    print("\nNext step: open the CSV, scan the descriptions, then paste rows into")
    print("Claude chat for rewriting per the write_descriptions protocol.")


if __name__ == "__main__":
    main()
