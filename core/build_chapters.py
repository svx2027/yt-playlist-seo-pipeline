"""
build_chapters.py  -  Phase 6b: chapter timestamps per docs/CHAPTER_TIMESTAMP_RULEBOOK.md.

Two behaviours per video:
  - HAS chapters already: keep the exact timestamps, rewrite ONLY the labels
    (SEO + curiosity + transition), grounded in the transcript at that time.
  - NO chapters: CREATE them from the timed captions per the full rulebook
    (topic-based, question-set rule, count-by-length, 0:00 hook, no recap).

Gemini proposes; this script enforces the SILENT-KILLER platform rules in code
(first stamp 0:00, >=3 chapters, >=10s gaps, strictly ascending, correct time
format) so a list can never ship broken. Labels are English only.

Reads : config.json, keywords_by_video.json, descriptions_approved.csv (titles +
        existing chapters), transcripts/<id>.<lang>.vtt (timed captions)
Writes: chapters_by_video.json  { vid: {video_type, block, chapters, flags} }

DRAFTS ONLY. A separate step folds these into the descriptions; nothing is pushed
here. Never prints the API key.

Run (channel folder, venv active):
    python3 ../../core/build_chapters.py --only VIDEO_ID
    python3 ../../core/build_chapters.py
"""

import argparse
import csv
import glob
import json
import os
import re
import sys

import _gemini
import chapter_windows
import verify_truth as VT
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# THE THREE RAILS ADDED 16 JUL 2026, each because it already cost a live defect on this channel.
# All three import their detectors from verify_truth: that file is the SINGLE SOURCE OF TRUTH for
# source resolution and promo detection, and a second copy of either would be a gate that can
# silently disagree with the gate: when the writer and the validator disagree about a rule,
# the validator is not a gate, it is decoration.
#
#   SOURCE TRUST  - build only from a resolved, trusted timed source (whisper > native ASR).
#                   An auto-translated .en.vtt is refused outright: one module chaptered 4
#                   videos off "subscribe subscribe button" gibberish and one went live.
#   WINDOWED PATH - any long video is chaptered window by window so the model only ever
#                   labels a window it can actually read. Sampling + snap() put 26 live
#                   labels on the wrong minute.
#   PROMO REFUSAL - a chapter may never OPEN in a sales pitch. 21 chapters in one module landed
#                   on the teacher's outro pitch, so a student clicking one got an ad.
CLASSIC_PROMPT_CAP = 30000     # chars of timed transcript the classic single-prompt path sends
# Per-window text cap inside vtt_timed(). WAS 220, WHICH SILENTLY SUMMARISED EVERY VIDEO:
# a 12-min video is ~24 windows, so the model saw at most 24 x 220 = 5,280 chars of a ~9,000
# char transcript (~59%). A pre-flight audit caught that the "would truncate -> go windowed"
# trigger was measured on this ALREADY-CAPPED text, so worst case ~27.8k could never exceed the
# 30k cap and the trigger COULD NEVER FIRE (a rule that cannot fire is decoration).
# At 600, a sub-threshold video (<900s, ~30 windows) fits ~18k of a ~11k transcript: full text.
WINDOW_TEXT_CAP = 600
# The classic path is only honest while the model reads essentially the whole
# transcript. Below this, route windowed regardless of how short the video is.
MIN_CLASSIC_COVERAGE = 0.90

CONFIG = "config.json"
KEYWORDS = "keywords_by_video.json"
OUT = "chapters_by_video.json"
MODEL = "gemini-2.5-flash"
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

CUE_TIME = re.compile(r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[.,]\d+\s*-->")
INLINE_TAG = re.compile(r"<[^>]+>")
CHAPTER_LINE = re.compile(r"^\s*((?:\d{1,2}:)?\d{1,2}:\d{2})\s+(.+?)\s*$")

SCHEMA = {
    "type": "object",
    "properties": {
        "video_type": {"type": "string"},
        "chapters": {"type": "array", "items": {
            "type": "object",
            "properties": {"start_seconds": {"type": "integer"}, "label": {"type": "string"}},
            "required": ["start_seconds", "label"]}},
        "flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["video_type", "chapters"],
}


def hms_to_sec(h, m, s):
    return int(h or 0) * 3600 + int(m) * 60 + int(s)


def fmt(sec, long_video):
    sec = int(sec)
    if long_video or sec >= 3600:
        return f"{sec//3600}:{(sec%3600)//60:02d}:{sec%60:02d}"
    return f"{sec//60}:{sec%60:02d}"


def parse_chapter_time(t):
    parts = [int(p) for p in t.split(":")]
    if len(parts) == 3:
        return parts[0]*3600 + parts[1]*60 + parts[2]
    return parts[0]*60 + parts[1]


def vtt_timed(path, window=30):
    """Parse a .vtt into ~window-second timed chunks: [(start_sec, text)].

    Handles YouTube auto-caption rolling duplicates by only keeping text that
    adds something new versus the previous line.
    """
    cues = []
    cur_start = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = CUE_TIME.match(line.strip())
            if m:
                cur_start = hms_to_sec(*m.groups())
                continue
            if cur_start is None:
                continue
            txt = INLINE_TAG.sub("", line).strip()
            if txt and not txt.startswith(("WEBVTT", "Kind:", "Language:")):
                cues.append((cur_start, txt))
    # dedupe rolling repeats
    clean, prev = [], ""
    for t, txt in cues:
        if txt == prev or (prev and txt in prev):
            continue
        clean.append((t, txt)); prev = txt
    # window
    out, buf, wstart = [], [], None
    for t, txt in clean:
        if wstart is None:
            wstart = t
        buf.append(txt)
        if t - wstart >= window:
            out.append((wstart, " ".join(buf)[:WINDOW_TEXT_CAP])); buf, wstart = [], None
    if buf:
        out.append((wstart or 0, " ".join(buf)[:WINDOW_TEXT_CAP]))
    return out


def raw_cue_chars(chunks_source_cues):
    """Total characters of real speech in the transcript, BEFORE any windowing or capping.
    This is the honest denominator for 'how much of this video can the model actually see'."""
    return sum(len(x) for _, x in chunks_source_cues)


def timed_text(chunks, long_video):
    return "\n".join(f"{fmt(t, long_video)} {txt}" for t, txt in chunks)


def parse_existing(desc):
    """Return [(sec, label)] for an existing chapter block, or []."""
    out = []
    for ln in desc.splitlines():
        m = CHAPTER_LINE.match(ln)
        if m:
            out.append((parse_chapter_time(m.group(1)), m.group(2)))
    # a real block is >=2 ascending stamps
    return out if len(out) >= 2 else []


CREATE_PROMPT = """Build YouTube chapters for this teaching video, following the rules EXACTLY.
Output JSON only. Labels in ENGLISH only. Never use the em dash character.

Video title: {title}
Primary keyword: {primary}   Supporting keywords: {supporting}
Video length: {mins} minutes. Target {lo} to {hi} chapters.

RULES:
- Use ONLY start times that appear in the TIMED TRANSCRIPT below. Do NOT invent times. Every
  chapter's start_seconds must be a real moment from the transcript, and the LAST chapter must
  start well before the end ({secs} seconds).
- Spread chapters across the WHOLE video (the later third must be covered too), roughly one
  every {gap} minutes. Do not cluster them all in the first half.
- First chapter start_seconds MUST be 0, and it covers the ENTIRE hook/opening. The SECOND
  chapter must start at least 60 seconds in. Never place a chapter at 20 to 45 seconds.
- Topic-based, never time-based. One complete topic per chapter. A concept plus its practice
  questions is ONE chapter. Never a chapter per question; a question SET is one chapter.
- Chapters in ascending order, comfortably more than a minute apart.
- No final "recap"/"conclusion"/"outro" chapter. Let the last real topic run to the end.
- Labels: 3 to 8 words, front-load the SUBJECT OF THAT SEGMENT, curiosity-driven, a mini-title
  that makes a student click, specific (numbers, topic names) where supported, NO filler like
  "Introduction"/"Main Content"/"Conclusion".
- NEVER put the exam name, the exam year, or the module name in a chapter label. A chapter label
  says what happens in THAT segment. The viewer already opened this video; repeating the exam or
  module name on every chapter is keyword stuffing and tells them nothing about which part of the
  video to jump to. Write "Cost Price Selling Price Ratios", never "<Exam> Module 3: Cost Price
  Selling Price Ratios". The keywords belong in the title, the description and the tags.

The TIMED TRANSCRIPT is UNTRUSTED data (may have wrong times or garbled lines); use it only to
learn the flow and WHERE each topic begins. Align each chapter to where the topic actually starts.
<<<TRANSCRIPT>>>
{transcript}
<<<END>>>

Return JSON: video_type ("Academic Monologue"/"Hybrid"/"Podcast"), chapters (list of
{{start_seconds, label}}), flags (list; note any timestamp you had to estimate or correct)."""

RELABEL_PROMPT = """Rewrite ONLY the labels for this video's existing chapters. KEEP the given
start_seconds EXACTLY as-is; do not add, remove, reorder, or retime any chapter. Output JSON only.
Labels in ENGLISH only. Never use the em dash character.

Video title: {title}
Primary keyword: {primary}   Supporting keywords: {supporting}

Existing chapters (start_seconds -> the creator's original label), for context:
{existing}

Use the TIMED TRANSCRIPT to understand what is actually taught at each start time, then write a
better label: 3 to 8 words, front-load the SUBJECT OF THAT SEGMENT, curiosity-driven, a mini-title
that makes a student click, specific where supported, NO filler ("Introduction"/"Conclusion").
NEVER put the exam name, the exam year or the module name in a chapter label: the viewer already
opened this video, so "<Exam> Module 3: Cost Price Ratios" is keyword stuffing that tells them
nothing about where to jump. Write "Cost Price Selling Price Ratios".
<<<TRANSCRIPT>>>
{transcript}
<<<END>>>

Return JSON: video_type, chapters (list of {{start_seconds, label}} with the SAME start_seconds as
given, one per existing chapter, in the same order), flags."""


def snap(sec, window_times):
    """Snap a proposed time to the nearest real transcript window start, so a
    model-invented time (e.g. beyond the video) becomes a real in-range time.

    ⚠️ READ THE WINDOWED-PATH NOTE ABOVE BEFORE TRUSTING THIS. snap() makes a GUESSED time real, which makes a WRONG
    label look authoritative and validate clean. It is a safety net for a small mis-estimate on a
    video the model could see in full; it is NOT a substitute for the model seeing the timeline.
    That is why the windowed path above never calls it: there, a time that is not a real cue in
    its own window is REJECTED, not snapped.
    """
    if not window_times:
        return sec
    return min(window_times, key=lambda t: abs(t - sec))


def drop_promo_chapters(chaps, vid, base="."):
    """A chapter may NEVER OPEN in a sales pitch. 21 chapters in one module landed on the
    teacher's outro pitch (batch enrollment, a branded test-series subscription,
    invite codes, "link in description"), so a student who clicked a chapter got an ad. The
    end_buffer rail only bans chapters within N seconds of the END; a pitch routinely starts
    1-2 minutes before that.

    The detector is IMPORTED from verify_truth, never re-implemented: one PROMO regex, one
    opens_in_promo, used by both the builder and the gate. Judged on the ENGLISH view, because
    the PROMO patterns are English and cannot see a pitch in a Devanagari track.
    """
    ev = VT.english_view(base, vid)
    if not ev:
        return chaps, ["PROMO CHECK SKIPPED: no English view on disk for %s, so the promo "
                       "detector could not read this video" % vid]
    ecues = VT.parse_cues(ev)
    kept, flags = [], []
    for sec, label in chaps:
        pitch = VT.opens_in_promo(ecues, sec)
        if pitch and VT.zero_promo_exempt(sec, label):
            # 0:00 ONLY, and only because the label says honestly what is there. Operator decision,
            # 17 Jul 2026: a 0:00 stamp is needed or YouTube renders NO chapters at all, and a
            # video whose opening minutes ARE the pitch cannot satisfy both rules. An honest 0:00
            # label lets the student SKIP the pitch, which beats giving them no chapter list.
            # The exemption belongs to the LABEL: see VT.zero_promo_exempt.
            flags.append("0:00 chapter KEPT on a sales pitch because its label %r says honestly "
                         "what is there, so a student can skip past it (operator decision, 17 Jul "
                         "2026; a 0:00 stamp is required or YouTube renders no chapters at all). "
                         "Transcript at 0:00: %r" % (label[:40], pitch[:60]))
            kept.append((sec, label))
            continue
        if pitch:
            flags.append("DROPPED chapter at %ds %r: it OPENS in a sales pitch, so a student "
                         "clicking it would get an ad: %r" % (sec, label[:40], pitch[:70]))
            continue
        kept.append((sec, label))
    return kept, flags


def validate_and_fix(chaps, duration, long_video, min_gap=10, hook_min=0, end_buffer=0):
    """Enforce the silent-killer + anti-skip rules. chaps: [(sec,label)].
    - first forced to 0:00
    - a second chapter inside the hook (< hook_min) is dropped (anti-skip)
    - chapters closer than min_gap, past duration, or within end_buffer of the
      end (an outro shortcut) are dropped
    Returns (fixed_list, flags)."""
    flags = []
    chaps = sorted(chaps, key=lambda c: c[0])
    if not chaps:
        return [], ["no chapters produced"]
    if chaps[0][0] != 0:
        flags.append(f"first chapter moved to 0:00 (was {chaps[0][0]}s) per platform rule")
        chaps[0] = (0, chaps[0][1])
    fixed = [chaps[0]]
    for sec, label in chaps[1:]:
        if hook_min and len(fixed) == 1 and sec < hook_min:
            flags.append(f"dropped early chapter at {sec}s (inside the 0:00 hook)")
            continue
        if sec - fixed[-1][0] < min_gap:
            flags.append(f"dropped chapter at {sec}s (under {min_gap}s from previous)")
            continue
        if end_buffer and sec >= duration - end_buffer:
            flags.append(f"dropped chapter at {sec}s (too close to the end)")
            continue
        if sec >= duration:
            continue
        fixed.append((sec, label))
    return fixed, flags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    ap.add_argument("--ids-file", default=None, help="one video_id per line (chunked runs)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true",
                    help="rebuild even if the entry is already valid. Section 10's redo loop needs "
                         "this: without it 'rebuild that video's chapters' printed a success line "
                         "and did nothing, because skip-if-valid skipped the very video the audit "
                         "had just condemned.")
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args()

    cfg = json.load(open(CONFIG, encoding="utf-8"))
    key = _gemini.load_key(cfg)
    audio_lang = (cfg.get("transcripts") or {}).get("audio_lang", "en")
    windowed_threshold = (cfg.get("chapters") or {}).get("windowed_threshold_s", 900)
    kw = json.load(open(KEYWORDS, encoding="utf-8"))["videos"]
    # Read the PROPOSED csv (the approved csv does not exist until merge; chapters must land
    # inside proposed_description BEFORE validate/approval).
    src_csv = cfg["csv_paths"].get("proposed", "descriptions_proposed.csv")
    appr = {r["video_id"]: r for r in csv.DictReader(open(src_csv, encoding="utf-8"))}

    scope = cfg.get("in_scope_video_ids") or list(appr.keys())
    if args.only:
        scope = [args.only]
    elif args.ids_file:
        scope = [l.strip() for l in open(args.ids_file) if l.strip()]
    elif args.limit:
        scope = scope[:args.limit]

    creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    yt = build("youtube", "v3", credentials=creds)
    dur = {}
    for i in range(0, len(scope), 50):
        r = yt.videos().list(part="contentDetails", id=",".join(scope[i:i+50])).execute()
        for it in r["items"]:
            m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", it["contentDetails"]["duration"])
            h, mn, s = (int(x) if x else 0 for x in m.groups())
            dur[it["id"]] = h*3600 + mn*60 + s

    # Always merge into the existing map (even with --only / chunked runs) so a per-video
    # retry or a 15-20 chunk never wipes the rest of the sweep's chapters.
    existing = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
    out = dict(existing)
    rebuilt = skipped = 0

    for i, vid in enumerate(scope, 1):
        if vid in out and out[vid].get("valid") and not args.force:
            skipped += 1
            print(f"[{i}/{len(scope)}] {vid}  already chaptered, skipping (--force to rebuild)")
            continue
        # ONE BAD VIDEO MUST NEVER KILL A CHUNK. On 17 Jul 2026 Gemini returned a JSON list
        # where the schema demanded an object; the AttributeError escaped from the WINDOWED
        # branch (which had no guard) all the way out of main(), so chunk 5 died at video 15
        # and videos 16, 17 and 18 WERE NEVER ATTEMPTED. Nothing said so: the chunk sweep
        # counted the 4 missing videos as 'fewer than 3 chapters' alongside genuinely short
        # ones, so a total build failure was reported in the same number as a cosmetic
        # shortfall (distinct facts need distinct words). The classic branch
        # already had this guard and its comment already stated the intent ('flush, and carry
        # on... a re-run picks it up'); it simply was never applied to the other path.
        # A guard on ONE call site is not a guard on the LOOP.
        try:
            rebuilt += 1
            plan = kw.get(vid, {})
            row = appr.get(vid, {})
            duration = dur.get(vid, 0)
            long_video = duration >= 3600

            # --- SOURCE TRUST. Resolve, do not glob. -------------------------------------
            # The old line here was:
            #     vtts = glob.glob(f"transcripts/{vid}.<lang>.vtt") or glob.glob(f"transcripts/{vid}.*.vtt")
            # which could not tell whisper output from an auto-TRANSLATED caption track, because
            # whisper_timed wrote both under the same <id>.en.vtt name. The resolver decides by an
            # explicit preference ladder over provenance-bearing filenames, and REFUSES rather than
            # falling through to a source it does not trust.
            src, kind = VT.resolve_timed_source(".", vid, audio_lang)
            if not src:
                out[vid] = {"video_type": "", "mode": "create", "n": 0, "valid": False,
                            "flags": ["SOURCE_UNTRUSTED: %s" % kind], "block": "",
                            "source_file": "", "source_kind": ""}
                print(f"[{i}/{len(scope)}] {vid}  SOURCE REFUSED -> not chaptered (valid=False): {kind}")
                json.dump(out, open(OUT, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
                continue
            # WHISPER-MANDATORY RAIL. A slice can DEMAND a source kind. Without this, a rule that only
            # existed as a sentence in a plan ("every module-4 video is chaptered from whisper") had no
            # teeth: a video whose whisper failed resolved to its native-caption vtt, which is a permitted kind, so
            # nothing refused and no gate fired. Unset for channels that do not need it.
            require_kind = (cfg.get("chapters") or {}).get("require_source_kind")
            if require_kind and kind != require_kind:
                out[vid] = {"video_type": "", "mode": "create", "n": 0, "valid": False,
                            "flags": ["SOURCE_UNTRUSTED: this slice requires source_kind=%r but the "
                                      "best available source is %r (%s). Run whisper_timed for this "
                                      "video; do not chapter it from a fallback track."
                                      % (require_kind, kind, os.path.basename(src))],
                            "block": "", "source_file": src, "source_kind": kind}
                print(f"[{i}/{len(scope)}] {vid}  SOURCE KIND {kind} != required {require_kind} -> refused")
                json.dump(out, open(OUT, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
                continue
            trust_cues = VT.parse_cues(src)
            ok, why = VT.source_trust(trust_cues, kind)
            if not ok:
                out[vid] = {"video_type": "", "mode": "create", "n": 0, "valid": False,
                            "flags": ["SOURCE_UNTRUSTED: %s" % why], "block": "",
                            "source_file": src, "source_kind": kind}
                print(f"[{i}/{len(scope)}] {vid}  SOURCE UNTRUSTED -> not chaptered (valid=False): {why[:70]}")
                json.dump(out, open(OUT, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
                continue

            # ADAPTIVE window so a long video's timed transcript covers the WHOLE duration instead of
            # truncating at the cap: ~120 windows regardless of length (30s windows for a 20-min
            # video, 60s for 2 hours).
            window = max(30, (duration or 0) // 120)
            chunks = vtt_timed(src, window=window)
            full_text = timed_text(chunks, long_video)
            transcript = full_text[:CLASSIC_PROMPT_CAP]
            # HARD GUARD: with no parseable timed transcript, do NOT call Gemini (it would
            # fabricate timestamps with the snap rail disabled). Mark invalid -> recovery/waiver.
            if not chunks:
                out[vid] = {"video_type": "", "mode": "create", "n": 0, "valid": False,
                            "flags": ["no timed transcript parsed - recovery or waiver needed"],
                            "block": "", "source_file": src, "source_kind": kind}
                print(f"[{i}/{len(scope)}] {vid}  NO TIMED TRANSCRIPT -> not chaptered (valid=False)")
                json.dump(out, open(OUT, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
                continue

            existing_ch = parse_existing(row.get("proposed_description", ""))
            # PROVENANCE, NOT PRESENCE. "relabel" means "the CREATOR already had chapters and we keep
            # their timestamps". If the block in the CSV is one WE injected, relabelling it means
            # re-labelling our own guesses and calling that ground truth. Only treat a block as the
            # creator's when we have no record of authoring it.
            ours = bool((existing.get(vid) or {}).get("block"))
            mode = "relabel" if (existing_ch and not ours) else "create"

            # --- WINDOWED-PATH ROUTING. Two triggers, either one forces the windowed path. --------------
            # (a) duration >= config.chapters.windowed_threshold_s.
            # (b) the classic prompt WOULD TRUNCATE this transcript. This is the real invariant:
            #     the sampled path is only safe when the model can see the WHOLE timeline, and a
            #     silent truncation is precisely how it stops being able to. Duration is a proxy;
            #     truncation is the actual condition, so it wins regardless of length.
            # COVERAGE, measured against the RAW transcript. The old test compared the capped text
            # to the cap and could never fire. What actually matters is the fraction of real speech
            # the model gets to read: below that, it is inferring a timeline it cannot see, which is
            # the exact mechanism that put 26 wrong labels on the live channel.
            raw_chars = raw_cue_chars(trust_cues)
            sent = min(len(full_text), CLASSIC_PROMPT_CAP)
            coverage = (sent / raw_chars) if raw_chars else 1.0
            truncates = len(full_text) > CLASSIC_PROMPT_CAP or coverage < MIN_CLASSIC_COVERAGE
            # ROUTE ON THE TRANSCRIPT, NEVER ON OUR OWN ARTIFACT. This used to be
            # `mode == "create" and (...)`, so once Stage 4.4 injected our chapters into the CSV,
            # parse_existing found the block, mode flipped to "relabel", and the windowed path TURNED
            # ITSELF OFF. Every Stage 5 redo of a long video silently lost the windowed-path protection that is
            # the whole reason the windowed path exists. A block WE wrote is not the creator's work.
            windowed = (duration >= windowed_threshold or truncates)
            if windowed:
                why_w = ("%ds >= %ds threshold" % (duration, windowed_threshold)
                         if duration >= windowed_threshold
                         else "the classic prompt would show the model only %.0f%% of the real "
                              "transcript (%d of %d chars), so it could not see the whole timeline"
                              % (100 * coverage, sent, raw_chars))
                print(f"[{i}/{len(scope)}] {vid}  WINDOWED path ({why_w})")
                gch, wflags = chapter_windows.build_windowed(key, args.model, vid, trust_cues,
                                                             duration, cfg)
                # Re-read any label that came out TRUNCATED or IDENTICAL to a sibling, before the 0:00
                # logic below can promote one of them. Both defects are made by the per-window design
                # itself (each window is labelled blind to the others, which is what stops the model
                # inventing a timeline), so they cannot be prompted away: they are repaired by showing
                # the model the one segment and asking what makes IT different. Chunk-1 evidence:
                # 1 truncated and 4 duplicate pairs across 127 labels.
                gch, rflags = chapter_windows.repair_labels(key, args.model, gch, trust_cues,
                                                            duration, cfg)
                wflags = wflags + rflags
                # THE TWO 0:00 RAILS USED TO FIGHT EACH OTHER AND THE MODEL LOST.
                # ensure_zero_chapter PREPENDS a generic fallback label at 0:00, then validate_and_fix
                # runs min_gap=75 and DROPS the model's real first chapter for sitting too close to
                # it. Net effect on a windowed video whose first topic starts inside 75s: the honest,
                # evidence-backed label the model read off the transcript is silently replaced by
                # "What This Session Covers". If the first real topic starts early, it IS the 0:00
                # chapter: promote it and keep its label. Only synthesise a 0:00 label when the first
                # real topic genuinely starts later (a real case: a video whose first real topic was a 1:25
                # opening question, where dragging it back to 0:00 would mislabel both).
                zflags = []
                if gch and 0 < gch[0][0] < 75:
                    zflags = ["first windowed chapter promoted from %ds to 0:00, keeping its own "
                              "transcript-read label %r (the generic 0:00 fallback would have "
                              "displaced it and min_gap would then have dropped it)"
                              % (gch[0][0], gch[0][1][:40])]
                    gch[0] = (0, gch[0][1])
                else:
                    gch, zflags = chapter_windows.ensure_zero_chapter(gch, trust_cues, cfg, duration)
                chaps, flags = validate_and_fix(gch, duration, long_video, min_gap=75, hook_min=0,
                                                end_buffer=max(45, min(180, (duration or 0) // 30)))
                flags = wflags + zflags + flags
                chaps, pflags = drop_promo_chapters(chaps, vid, base=".")
                flags += pflags
                ok = len(chaps) >= 3 and chaps[0][0] == 0 and all(
                    chaps[k][0] - chaps[k - 1][0] >= 10 for k in range(1, len(chaps)))
                block = "Chapters:\n" + "\n".join(f"{fmt(s, long_video)} {lbl}" for s, lbl in chaps)
                out[vid] = {"video_type": "long-windowed", "mode": "create-windowed", "n": len(chaps),
                            "valid": ok, "flags": flags, "block": block,
                            "source_file": src, "source_kind": kind}
                print(f"[{i}/{len(scope)}] {vid}  create-windowed {len(chaps)} chapters  "
                      f"{'OK' if ok else 'INVALID'}" + (f"  flags: {'; '.join(flags[:3])}" if flags else ""))
                json.dump(out, open(OUT, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
                continue

            window_times = [c[0] for c in chunks]
            if mode == "relabel":
                ex_str = "\n".join(f"{s} -> {lbl}" for s, lbl in existing_ch)
                prompt = RELABEL_PROMPT.format(title=row.get("proposed_title", ""),
                    primary=plan.get("primary_keyword", ""), supporting=", ".join(plan.get("supporting", [])),
                    existing=ex_str, transcript=transcript)
            else:
                mins = max(1, round(duration/60))
                # Count bands per CHAPTER_TIMESTAMP_RULEBOOK Section 6:
                # ~10-25 min: 4-6 · up to ~1hr15: 6-10 · 2hr+: 7-12
                if mins <= 30:
                    lo, hi = 4, 6
                elif mins <= 75:
                    lo, hi = 6, 10
                else:
                    lo, hi = 7, 12
                gap = max(2, round(duration / ((lo+hi)/2) / 60))
                prompt = CREATE_PROMPT.format(title=row.get("proposed_title", ""),
                    primary=plan.get("primary_keyword", ""), supporting=", ".join(plan.get("supporting", [])),
                    mins=mins, secs=duration, lo=lo, hi=hi, gap=gap, transcript=transcript)

            # One bad video must not stop the other 124. _gemini.generate RAISES on MAX_TOKENS by
            # design, and module 2's 28-video run already died this way mid-batch. Record the failure,
            # flush, and carry on: the video is left valid=False so V10 refuses to publish it and a
            # re-run picks it up.
            try:
                text, _ = _gemini.generate(key, args.model, prompt, json_mode=True, schema=SCHEMA,
                                           temperature=0.3, max_output_tokens=4000, thinking_budget=0)
                d = _gemini.parse_json(text)
            except Exception as e:
                print("  [%s] CHAPTER_GEN_FAILED: %s" % (vid, str(e)[:90]))
                out[vid] = {"video_type": "", "mode": "create", "n": 0, "valid": False,
                            "flags": ["CHAPTER_GEN_FAILED: %s" % str(e)[:120]], "block": ""}
                json.dump(out, open(OUT, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
                continue
            # Labels MUST be single-line (a newline or a stray math expression breaks the chapter
            # format on YouTube); collapse whitespace and cap length.
            gch = [(int(c["start_seconds"]), re.sub(r"\s+", " ", c["label"]).strip()[:90])
                   for c in d.get("chapters", []) if c.get("label") and str(c["label"]).strip()]

            if mode == "relabel":
                # keep the creator's exact times, apply new labels in order, force 0:00.
                labels = [l for _, l in gch]
                chaps = [(existing_ch[j][0], labels[j] if j < len(labels) else existing_ch[j][1])
                         for j in range(len(existing_ch))]
                chaps, flags = validate_and_fix(chaps, duration, long_video, min_gap=10)
            else:
                # snap invented times to real transcript windows, then enforce hook + spacing.
                snapped, seen = [], set()
                for sec, label in gch:
                    s = snap(sec, window_times)
                    if s not in seen:
                        seen.add(s); snapped.append((s, label))
                # end_buffer scales with length: 45s on a 20-min video, up to 3 min
                # on a 2-hour one (an "outro" chapter near the end is an exit door).
                # hook_min=60 / min_gap=75 are NOT loosened for short videos: a LIVE 3:27 video on
                # this channel carries chapters at 0:00 / 1:17 / 2:36 (77s and 79s gaps), so three
                # legal chapters DO fit a short runtime, and a ~40s candidate is a mid-solution
                # false start, not a topic start (handplace_short.py doctrine; a 24 Jul module-6
                # experiment that scaled these DOWN was reverted for exactly that reason). A
                # short-video shortfall is repaired by handplace_short.py (model re-reads the whole
                # transcript, places REAL cues in the legal window, or the video is disclosed-waived),
                # never by loosening the guard until the count rises.
                chaps, flags = validate_and_fix(snapped, duration, long_video,
                                                min_gap=75, hook_min=60,
                                                end_buffer=max(45, min(180, (duration or 0) // 30)))

            # The promo-refusal rule applies to BOTH modes. A relabel keeps the creator's own timestamps, and those
            # can sit on a pitch just as easily as a generated one.
            chaps, pflags = drop_promo_chapters(chaps, vid, base=".")
            flags = flags + pflags

            ok = len(chaps) >= 3 and chaps[0][0] == 0 and all(
                chaps[k][0] - chaps[k-1][0] >= 10 for k in range(1, len(chaps)))

            block = "Chapters:\n" + "\n".join(f"{fmt(s, long_video)} {lbl}" for s, lbl in chaps)
            out[vid] = {"video_type": d.get("video_type", ""), "mode": mode,
                        "n": len(chaps), "valid": ok, "flags": flags, "block": block,
                        "source_file": src, "source_kind": kind}
            print(f"[{i}/{len(scope)}] {vid}  {mode:<8} {len(chaps)} chapters  {'OK' if ok else 'INVALID'}"
                  + (f"  flags: {'; '.join(flags)}" if flags else ""))
            json.dump(out, open(OUT, "w", encoding="utf-8"), indent=2, ensure_ascii=False)  # per-video flush

        except Exception as e:
            import traceback
            print("  [%s] CHAPTER_BUILD_CRASHED: %s" % (vid, str(e)[:90]))
            out[vid] = {"video_type": "", "mode": "", "n": 0, "valid": False,
                        "flags": ["CHAPTER_BUILD_CRASHED: %s | %s"
                                  % (str(e)[:120], traceback.format_exc().strip().splitlines()[-1][:90])],
                        "block": ""}
            json.dump(out, open(OUT, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
            continue
    json.dump(out, open(OUT, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print(f"\nWrote {OUT} ({len(out)} videos; {rebuilt} rebuilt, {skipped} skipped as already-valid)")


if __name__ == "__main__":
    main()
