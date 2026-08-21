"""
revert_playlist.py: restore the playlist's OWN title + description from a backup.

playlist_update.py refuses an empty description, and some playlists originally had
none, so reverting the playlist needs its own tiny script. Reads a
playlist_backup_*.json (old_title, old_description, possibly empty), backs up the
CURRENT values to a playlist_prerevert_*.json first (distinct prefix so it is NOT
mistaken for a restore source), then restores. Wrong-channel guard + dry-run, like
every other write. Never deletes anything.

Run (channel folder, venv active):
    python3 ../../core/revert_playlist.py --dry-run
    python3 ../../core/revert_playlist.py --backup playlist_backup_20260710_214823.json
"""

import argparse
import glob
import json
import sys
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from _helpers import assert_expected_channel, classify_error, handle_quota_error_and_exit

CONFIG_PATH = "config.json"
TOKEN_PATH = "token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]


def get_service():
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backup", default=None, help="which playlist_backup_*.json to restore from")
    args = ap.parse_args()

    cfg = json.load(open(CONFIG_PATH, encoding="utf-8"))
    # Restore SOURCE: only the original 'playlist_backup_*' files, never a prerevert one.
    src = args.backup
    if not src:
        cands = sorted(glob.glob("playlist_backup_*.json"))
        if not cands:
            sys.exit("ERROR: no playlist_backup_*.json found to restore from.")
        src = cands[0]  # oldest = the earliest (original) backup
    backup = json.load(open(src, encoding="utf-8"))
    old_title = backup.get("old_title", "")
    old_desc = backup.get("old_description", "")
    if not old_title:
        sys.exit(f"ERROR: {src} has no old_title; refusing to restore a blank title.")

    pid = cfg["playlist_id"]
    yt = get_service()
    assert_expected_channel(yt, cfg)

    cur = yt.playlists().list(part="snippet", id=pid).execute()["items"][0]["snippet"]
    print(f"Restore source: {src}")
    print(f"Current title      : {cur.get('title', '')}")
    print(f"Current desc chars : {len(cur.get('description', ''))}")
    print(f"\nRestore title      : {old_title}")
    print(f"Restore desc chars : {len(old_desc)}{'  (EMPTY, original had no description)' if not old_desc else ''}")

    if args.dry_run:
        print("\n[DRY RUN] would restore the playlist.")
        return
    if input("\nProceed? Type 'yes' to confirm: ").strip().lower() != "yes":
        sys.exit("Aborted.")

    # Back up the CURRENT values first, under a distinct prefix.
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    cb = f"playlist_prerevert_{ts}.json"
    with open(cb, "w", encoding="utf-8") as f:
        json.dump({"playlist_id": pid, "old_title": cur.get("title", ""),
                   "old_description": cur.get("description", "")}, f, indent=2, ensure_ascii=False)
    print(f"Current values backed up to: {cb}")

    body = {"id": pid, "snippet": {"title": old_title, "description": old_desc}}
    if cur.get("defaultLanguage"):
        body["snippet"]["defaultLanguage"] = cur["defaultLanguage"]
    try:
        yt.playlists().update(part="snippet", body=body).execute()
        print("\nSuccess. Playlist restored to its original title and description.")
    except HttpError as e:
        category, status, reason = classify_error(e)
        if category == "quota":
            handle_quota_error_and_exit(e)
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()
