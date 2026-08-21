"""
fetch_thumbnails.py

Downloads the thumbnail image for every video we are working on, so we have a
local copy for context (e.g. writing better titles/descriptions, or a records
of what the video looked like on a given day).

CAPTURE ONLY. This script never uploads or changes a thumbnail on YouTube.
There is deliberately NO thumbnail-setting code anywhere in this project.

How it works (no YouTube API, no quota used):
  - YouTube serves thumbnail images from a plain public web address:
        https://i.ytimg.com/vi/<VIDEO_ID>/<size>.jpg
  - We just download that file with Python's built-in web tools (urllib).
    This costs ZERO of our daily YouTube API quota — it is an ordinary
    image download, the same as your browser saving a picture.

For each video we try three sizes, best first, and keep the first that exists:
    1. maxresdefault.jpg  (highest resolution, not always present)
    2. sddefault.jpg      (medium)
    3. hqdefault.jpg      (always present as a fallback)

Where the list of videos comes from:
  - config.json tells us which CSV holds the videos (the snapshot CSV, or the
    extracted CSV). We read the 'video_id' column from that CSV.

What it writes:
  - thumbnails/<video_id>.jpg          one image per video
  - thumbnails/manifest.csv            a record of what was downloaded:
        video_id, url_used, bytes, sha256

Idempotent (safe to re-run):
  - If a video already has an image on disk AND the manifest already lists it
    with a matching sha256 checksum, we skip it. Only new or changed files are
    re-downloaded. So you can run this again after adding more videos and it
    will only fetch the missing ones.

Run it from inside a channel working folder (the folder that has config.json):
    cd channels/<your-channel>
    python3 ../../core/fetch_thumbnails.py

No credentials, no login, no token needed — this script authenticates with
nothing and changes nothing on YouTube.
"""

import csv
import glob
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request


CONFIG_FILE = "config.json"

# The public thumbnail sizes we try, best quality first.
# i.ytimg.com is YouTube's image host; no API key or quota is involved.
THUMB_SIZES = ["maxresdefault.jpg", "sddefault.jpg", "hqdefault.jpg"]

# Where images and the record file go (relative to the channel working folder).
THUMB_DIR = "thumbnails"
MANIFEST_FILE = os.path.join(THUMB_DIR, "manifest.csv")
MANIFEST_FIELDS = ["video_id", "url_used", "bytes", "sha256"]

# A polite, ordinary browser-style identifier so the image host serves us
# normally. We are only fetching public images.
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) yt-metadata-pipeline/1.0"

# YouTube returns a tiny grey placeholder image (about 120x90) when a specific
# thumbnail size does NOT exist, instead of a proper 404 error. Real thumbnails
# are always bigger than this, so any image at or below this many bytes is
# treated as "not present, try the next size down".
PLACEHOLDER_MAX_BYTES = 2000


def load_config():
    """Read config.json from the current working folder."""
    if not os.path.exists(CONFIG_FILE):
        print(f"ERROR: {CONFIG_FILE} not found.")
        print(f"You're running from: {os.getcwd()}")
        print("Run this from inside a channel working folder, e.g.:")
        print("    cd channels/<your-channel>")
        print("    python3 ../../core/fetch_thumbnails.py")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)


def find_video_id_csv(config):
    """Decide which CSV to read the video_id list from.

    We prefer the extracted CSV (descriptions_extracted.csv). If that isn't
    there, we fall back to the newest snapshot CSV. snapshot.py writes flat
    files named snapshot_<timestamp>.csv in the current folder (not a
    subdirectory), so that is exactly what we glob for here. Either file has
    a 'video_id' column as its first column, which is all we need.
    """
    csv_paths = config.get("csv_paths", {})

    # First choice: the extracted metadata CSV.
    extracted = csv_paths.get("extracted", "descriptions_extracted.csv")
    if extracted and os.path.exists(extracted):
        return extracted

    # Second choice: the newest snapshot_*.csv in this folder.
    candidates = sorted(glob.glob("snapshot_*.csv"), key=os.path.getmtime, reverse=True)
    return candidates[0] if candidates else None


def read_video_ids(csv_path):
    """Return the list of video_ids from the 'video_id' column of a CSV.

    Duplicates are removed but the original order is kept.
    """
    ids = []
    seen = set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "video_id" not in (reader.fieldnames or []):
            print(f"ERROR: {csv_path} has no 'video_id' column.")
            print(f"Columns found: {reader.fieldnames}")
            sys.exit(1)
        for row in reader:
            vid = (row.get("video_id") or "").strip()
            if vid and vid not in seen:
                seen.add(vid)
                ids.append(vid)
    return ids


def load_manifest():
    """Read the existing manifest.csv (if any) into {video_id: {...row...}}."""
    existing = {}
    if not os.path.exists(MANIFEST_FILE):
        return existing
    try:
        with open(MANIFEST_FILE, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                vid = row.get("video_id")
                if vid:
                    existing[vid] = row
    except Exception:
        # A damaged manifest should not stop us — we just treat it as empty,
        # which means everything is re-downloaded (safe, only costs bandwidth).
        pass
    return existing


def write_manifest(rows_by_id):
    """Write the whole manifest back out, sorted by video_id for stability."""
    with open(MANIFEST_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for vid in sorted(rows_by_id.keys()):
            writer.writerow(rows_by_id[vid])


def sha256_of_file(path):
    """Return the sha256 checksum of a file on disk, as a hex string."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def try_download(video_id):
    """Try each thumbnail size for one video, best quality first.

    Returns (image_bytes, url_used) for the first real image found, or
    (None, None) if none of the sizes gave us a proper thumbnail.
    """
    for size in THUMB_SIZES:
        url = f"https://i.ytimg.com/vi/{video_id}/{size}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            # 404 = this size truly doesn't exist; move on to the next size.
            if e.code == 404:
                continue
            # Any other HTTP problem: note it and try the next size.
            print(f"    ({size}: HTTP {e.code}, trying next size)")
            continue
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"    ({size}: network problem: {e}, trying next size)")
            continue

        # YouTube sometimes returns a tiny grey placeholder instead of a 404.
        # If the image is suspiciously small, treat it as "not present".
        if len(data) <= PLACEHOLDER_MAX_BYTES:
            continue

        return data, url

    return None, None


def main():
    config = load_config()

    print(f"Channel: {config.get('channel_name', '(unnamed)')}")
    print()

    csv_path = find_video_id_csv(config)
    if not csv_path:
        print("ERROR: Could not find a CSV with video IDs.")
        print("Expected the extracted CSV or a snapshot CSV to exist first.")
        print("Run extract.py (or snapshot.py) before this script.")
        sys.exit(1)

    print(f"Reading video IDs from: {csv_path}")
    video_ids = read_video_ids(csv_path)
    print(f"Found {len(video_ids)} videos.")
    if not video_ids:
        print("Nothing to do.")
        sys.exit(0)

    # Make sure the output folder exists.
    os.makedirs(THUMB_DIR, exist_ok=True)

    manifest = load_manifest()

    downloaded = 0
    skipped = 0
    failed = 0

    print()
    for i, vid in enumerate(video_ids, 1):
        out_path = os.path.join(THUMB_DIR, f"{vid}.jpg")

        # Idempotency: skip if the file is already on disk AND the manifest
        # records the same checksum (i.e. nothing changed since last run).
        if os.path.exists(out_path) and vid in manifest:
            recorded_sha = manifest[vid].get("sha256", "")
            if recorded_sha and recorded_sha == sha256_of_file(out_path):
                print(f"[{i}/{len(video_ids)}] {vid}: already have it, skipping")
                skipped += 1
                continue

        print(f"[{i}/{len(video_ids)}] {vid}: downloading...")
        data, url_used = try_download(vid)

        if data is None:
            print(f"    could not find any thumbnail for {vid}")
            failed += 1
            continue

        # Save the image to disk.
        with open(out_path, "wb") as f:
            f.write(data)

        sha = sha256_of_file(out_path)
        manifest[vid] = {
            "video_id": vid,
            "url_used": url_used,
            "bytes": str(len(data)),
            "sha256": sha,
        }
        size_label = url_used.rsplit("/", 1)[-1]  # e.g. maxresdefault.jpg
        print(f"    saved {out_path} ({len(data):,} bytes, {size_label})")
        downloaded += 1

        # Write the manifest after each success so a crash mid-run does not
        # lose the record of what we already have.
        write_manifest(manifest)

        # Be polite to the image host — a short pause between downloads.
        time.sleep(0.5)

    # Final manifest write (covers the case where nothing new was downloaded).
    write_manifest(manifest)

    print()
    print("=" * 70)
    print(f"Done. Downloaded: {downloaded}, Skipped (already had): {skipped}, "
          f"Failed: {failed}")
    print(f"Images folder: {THUMB_DIR}/")
    print(f"Record file:   {MANIFEST_FILE}")
    print("=" * 70)
    print()
    print("Reminder: this is CAPTURE ONLY. Nothing was changed on YouTube.")


if __name__ == "__main__":
    main()
