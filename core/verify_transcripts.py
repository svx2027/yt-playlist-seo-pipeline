"""
verify_transcripts.py  -  the SECOND OPINION on a module's timed transcripts, by CONTENT.

WHY THIS EXISTS. `whisper_timed.py` gates its own output (length, contiguous hallucination run,
unique-line ratio, and a consistency check against the earlier .txt). Those gates are good and
they are also the PRODUCER judging itself. Two things they cannot do:

  1. Gate 4, the consistency check, SILENTLY DOES NOT RUN on this channel's normal case. The
     reference `.txt` is Devanagari (YouTube's Hindi ASR) and the whisper output is English, so
     the ratio is "not comparable" and the run reports `gates 1-3 only`. Measured on one module:
     22 of 25 videos, i.e. the strongest content gate was inert on almost the whole module and
     said so only in a parenthetical nobody is required to read.
  2. Nothing checked COVERAGE. A transcript that stops at 60% of the runtime passes every gate
     whisper_timed has, and then the windowed chapter builder cannot see the last 40% of the
     video at all. It would look exactly like a video with nothing to chapter late on.

Before this file, each module's verification was an ad-hoc reader typed fresh into a shell. That
is the classic self-check trap (a second implementation is a second chance to be wrong, and
nothing cross-checks it), so it is now one committed, testable command.

DELIBERATELY INDEPENDENT: this module does NOT import verify_truth.parse_cues. Its whole job is
to be the second opinion, and re-using the pipeline's parser would reproduce the pipeline's bugs.
That independence is exactly how a real parser bug was found: this reader and parse_cues
disagreed on the cue count, and the difference was parse_cues deleting real speech.

WHAT IT REFUSES TO DO: report on a file that parses to ZERO cues, and print any verdict without
its denominator ("nothing found" must never print the same as "nothing looked at").

REPETITION IS JUDGED BY SPREAD, NOT BY COUNT. A hallucination loop is CONTIGUOUS; real
teaching is DISTRIBUTED. Measured on one module: five videos repeat a 5-gram 21 to 41 times and
every one is legitimate ("a plus b plus c" across 67-95% of a lesson whose whole subject is
integral solutions of a+b+c=n). A count-only rule flags all five; the spread rule clears them and
still catches a real loop, which is always packed into one stretch.

    python3 ../../core/verify_transcripts.py              # every video in the scope fence
    python3 ../../core/verify_transcripts.py --json out.json
"""
import argparse, collections, csv, json, os, re, sys

CUE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})\s+-->\s+"
                 r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})")
DEV = re.compile(r"[ऀ-ॿ]")

# Bars. Each is a REVIEW signal unless marked BLOCK.
MAX_DEVANAGARI_PCT = 10.0     # BLOCK: a .whisper.vtt asserts English
MIN_COVERAGE_PCT = 95.0       # BLOCK: the timeline the chapter builder can actually see
MIN_WORDS_PER_MIN = 60.0      # REVIEW: thin text
MAX_TOKEN_RUN = 15            # BLOCK: contiguous single-token loop
NGRAM_N = 5
NGRAM_COUNT_FLAG = 20         # only interesting if it is ALSO contiguous
NGRAM_SPREAD_LOOP = 0.30      # occurrences packed into <30% of the runtime = suspect


def read_cues(path):
    """[(start_s, end_s, text)]. Own parser on purpose: see the module docstring."""
    out, t0, t1 = [], None, None
    for line in open(path, encoding="utf-8", errors="replace"):
        s = line.strip()
        m = CUE.match(s)
        if m:
            g = m.groups()
            t0 = int(g[0] or 0) * 3600 + int(g[1]) * 60 + int(g[2])
            t1 = int(g[4] or 0) * 3600 + int(g[5]) * 60 + int(g[6])
        elif s and "-->" not in s and not s.startswith("WEBVTT") and t0 is not None:
            out.append((t0, t1, s))
    return out


def inspect(path, duration_s):
    cues = read_cues(path)
    if not cues:
        return {"ok": False, "cues": 0, "problems": ["ZERO CUES PARSED (refusing to report)"]}
    body = " ".join(c[2] for c in cues)
    # NUMERATOR AND DENOMINATOR MUST BE THE SAME SET. The first version counted Devanagari with
    # the [ऀ-ॿ] range (which includes combining vowel marks) over a denominator of str.isalpha()
    # (which excludes them), so a pure-Devanagari fixture reported "140%". The verdict was still
    # right and the number was still nonsense, and a number nobody can defend is a number nobody
    # should act on. Caught because T39b PRINTS the value it asserts on.
    script_chars = [ch for ch in body if ch.isalpha() or DEV.match(ch)]
    dev = (sum(1 for ch in script_chars if DEV.match(ch)) / len(script_chars) * 100) \
        if script_chars else 0.0
    toks = re.findall(r"[a-z']+", body.lower())
    times = []
    for t0, _, txt in cues:
        times.extend([t0] * len(re.findall(r"[a-z']+", txt.lower())))

    run = maxrun = 1
    for i in range(1, len(toks)):
        run = run + 1 if toks[i] == toks[i - 1] else 1
        maxrun = max(maxrun, run)

    grams = collections.Counter(tuple(toks[i:i + NGRAM_N]) for i in range(len(toks) - NGRAM_N + 1))
    top_gram, top_n = grams.most_common(1)[0] if grams else ((), 0)
    spread = 1.0
    if top_n >= NGRAM_COUNT_FLAG and duration_s:
        at = [times[i] for i in range(len(toks) - NGRAM_N + 1)
              if tuple(toks[i:i + NGRAM_N]) == top_gram and i < len(times)]
        if at:
            spread = (max(at) - min(at)) / duration_s

    cov = (cues[-1][1] / duration_s * 100) if duration_s else 0.0
    wpm = len(toks) / (duration_s / 60.0) if duration_s else 0.0

    problems, review = [], []
    if dev > MAX_DEVANAGARI_PCT:
        problems.append("Devanagari %.0f%% in a file whose name asserts English" % dev)
    if cov < MIN_COVERAGE_PCT:
        problems.append("covers only %.0f%% of the runtime; the builder is blind after %s"
                        % (cov, "%d:%02d" % (cues[-1][1] // 60, cues[-1][1] % 60)))
    if maxrun >= MAX_TOKEN_RUN:
        problems.append("contiguous token run x%d (hallucination loop)" % maxrun)
    if top_n >= NGRAM_COUNT_FLAG and spread < NGRAM_SPREAD_LOOP:
        problems.append("%d-gram %r x%d packed into %.0f%% of the runtime (loop)"
                        % (NGRAM_N, " ".join(top_gram), top_n, spread * 100))
    elif top_n >= NGRAM_COUNT_FLAG:
        review.append("%r repeats x%d but is spread over %.0f%% of the video, which is real "
                      "teaching, not a loop" % (" ".join(top_gram), top_n, spread * 100))
    if wpm and wpm < MIN_WORDS_PER_MIN:
        review.append("only %.0f words/min" % wpm)

    return {"ok": not problems, "cues": len(cues), "words": len(toks), "devanagari_pct": round(dev, 1),
            "coverage_pct": round(cov, 1), "words_per_min": round(wpm), "max_token_run": maxrun,
            "top_ngram": " ".join(top_gram), "top_ngram_n": top_n,
            "top_ngram_spread_pct": round(spread * 100), "problems": problems, "review": review}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    ap.add_argument("--base", default=".")
    args = ap.parse_args()

    cfg = json.load(open(os.path.join(args.base, "config.json"), encoding="utf-8"))
    fence = cfg.get("in_scope_video_ids") or []
    if not fence:
        sys.exit("REFUSING: config.in_scope_video_ids is empty. Pin the slice first.")

    dur = {}
    ap_csv = os.path.join(args.base, "audit_videos.csv")
    if os.path.exists(ap_csv):
        for r in csv.DictReader(open(ap_csv, encoding="utf-8")):
            if r.get("duration_s"):
                dur[r["video_id"]] = float(r["duration_s"])
    for fn in sorted(os.listdir(args.base)):
        if fn.startswith("_durations") and fn.endswith(".json"):
            for k, v in (json.load(open(os.path.join(args.base, fn), encoding="utf-8")) or {}).items():
                dur.setdefault(k, float(v))

    out, missing = {}, []
    for v in fence:
        p = os.path.join(args.base, "transcripts", "%s.whisper.vtt" % v)
        if not os.path.exists(p):
            missing.append(v); continue
        out[v] = inspect(p, dur.get(v, 0.0))

    examined = len(out)
    print("=" * 78)
    print("TRANSCRIPT VERIFICATION  -  independent read, %d of %d in the fence EXAMINED"
          % (examined, len(fence)))
    print("=" * 78)
    if missing:
        print("  NO whisper.vtt (%d): %s" % (len(missing), ", ".join(missing[:8])))
    if not examined:
        sys.exit("REFUSING: examined 0 transcripts. That is not a pass.")

    cov = [o["coverage_pct"] for o in out.values()]
    dv = [o["devanagari_pct"] for o in out.values()]
    print("  cues examined : %d" % sum(o["cues"] for o in out.values()))
    print("  coverage      : min %.1f%%  median %.1f%%   (bar %.0f%%)"
          % (min(cov), sorted(cov)[len(cov) // 2], MIN_COVERAGE_PCT))
    print("  Devanagari    : max %.1f%%                  (bar %.0f%%)" % (max(dv), MAX_DEVANAGARI_PCT))
    print("  worst run     : %d tokens                  (bar %d)"
          % (max(o["max_token_run"] for o in out.values()), MAX_TOKEN_RUN))

    blockers = {v: o for v, o in out.items() if o["problems"]}
    reviews = {v: o for v, o in out.items() if o["review"] and not o["problems"]}
    print("\n  BLOCKERS: %d" % len(blockers))
    for v, o in blockers.items():
        for p in o["problems"]:
            print("     %s  %s" % (v, p))
    print("  REVIEW (cleared, shown so the exception never becomes invisible): %d" % len(reviews))
    for v, o in reviews.items():
        for r in o["review"]:
            print("     %s  %s" % (v, r))

    if args.json:
        json.dump(out, open(args.json, "w", encoding="utf-8"), indent=1)
        print("\n  wrote %s" % args.json)
    print("=" * 78)
    sys.exit(1 if (blockers or missing) else 0)


if __name__ == "__main__":
    main()
