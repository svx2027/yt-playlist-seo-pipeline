"""
verify_truth.py  -  THE GATE THAT ASKS "IS THIS TRUE?", not "is this well-formed?".

WHY THIS FILE EXISTS (read this before you touch the chapter pipeline).
An early module shipped 27 live videos with defective chapters and EVERY gate passed them.
validate.py's shape checks: first stamp 0:00, at least 3, strictly ascending, >=10s gaps, title
<=100, no dashes, no control chars. Not one rule asked whether the CHAPTER LABEL IS TRUE OF THE
AUDIO AT THAT TIMESTAMP. So a structurally perfect, semantically wrong artifact sailed through:
  - 26 labels described a topic that happens at a DIFFERENT time ("Multiplying by 11 and 12" over
    a divide-by-5 segment) because build_chapters SAMPLES long transcripts, the model GUESSES the
    time, and snap() then pins the guess to a real cue, making the timestamp real and the label
    a lie.
  - 21 chapters landed inside the teacher's end-of-video sales pitch, so a student clicking a
    chapter got an ad.
  - 4 videos were chaptered off auto-TRANSLATED captions that were literally "subscribe subscribe
    button" gibberish, while a clean native-language track sat unused.
Only an adversarial audit that READ THE SOURCE caught it. This file makes that check standing,
deterministic and cheap, so the next module cannot repeat it.

THE THREE TRUTH CHECKS (all deterministic; no model, no cost, runs in seconds):
  1. SOURCE TRUST      - is the timed transcript we chaptered from actually usable, or is it
                         auto-translated garbage?
  2. PROMO LANDING     - does any chapter sit at/after the promo onset (a sales pitch)?
  3. LABEL DRIFT       - do the label's distinctive words appear NOWHERE in the audio at that
                         timestamp but strongly ELSEWHERE in the video? That is the exact
                         signature of a model-guessed time, and it is what we shipped.
  4. LABEL HYGIENE     - exam name / year / module word / dangling function word / ASR typo.

DESIGN RULE: this gate is CONSERVATIVE on purpose. A false BLOCKER costs a human 60 seconds; a
false PASS costs a live defect on a client channel. Where it cannot be sure it says REVIEW, and a
REVIEW is a prompt for the model-based skeptic, not a silent pass.

    python3 ../../core/verify_truth.py                 # every in-scope video; exit 1 on a BLOCKER
    python3 ../../core/verify_truth.py --json out.json
"""
import argparse, csv, json, os, re, sys

CUE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,]\d{3}\s*-->")

# --- 0. SOURCE RESOLUTION ------------------------------------------------------------------
# THE PROVENANCE TRAP THIS EXISTS TO CLOSE. whisper_timed.py used to write its output as
# <id>.en.vtt, which is the SAME NAME YouTube's auto-translated track uses, so THE FILENAME
# COULD NOT TELL YOU WHERE A TRANSCRIPT CAME FROM. Measured on an early 125-video fence: 28 of
# the .en.vtt files were whisper output and 97 were YouTube ASR, separable only by content
# signature. Worse, build_chapters globbed "<id>.hi.vtt" first, so the remediation had to HIDE
# the clean .hi.vtt (renaming it .hi.vtt.moved) to force the builder onto whisper: a convention
# no script knew about, that any transcript re-fetch would silently undo. Whisper now writes
# <id>.whisper.vtt and the filename IS the provenance.
#
# WHY THERE IS NO "en-orig MEANS THE AUDIO IS ENGLISH" RULE. It was drafted and the data killed
# it: on this channel, hundreds of videos expose an en-orig track ALONGSIDE a Devanagari hi
# track, and on one real video en.vtt and en-orig.vtt were byte-identical (0% Devanagari) while
# hi.vtt was 45.7% Devanagari. en-orig is just a copy of the translation here. The rule would
# have fired on exactly 0 videos and been wrong in principle. Trust is driven by
# config.transcripts.audio_lang instead: the native ASR track of the SPOKEN language is
# trusted, and an .en.vtt on a non-English channel is an auto-translation and is not.
WHISPER_EXT = "whisper.vtt"

KIND_WHISPER = "whisper-english"        # our own whisper timed vtt: English, clean, trusted
KIND_NATIVE = "native-%s"               # the channel's spoken-language ASR track
KIND_NATIVE_EN = "native-english"       # an English channel's own English track
KIND_LEGACY = "legacy-english-view"     # pre-16-Jul entries with no recorded source (see below)


def resolve_timed_source(base, video_id, audio_lang="en"):
    """Which timed transcript may we build chapters from? Returns (path, kind) or (None, reason).

    Order: whisper timed vtt > the native ASR track > REFUSE. An auto-translated track is never
    a chapter source: an earlier module chaptered 4 videos off .en.vtt files that were literally
    "subscribe subscribe button" gibberish while a clean .hi.vtt sat unused, and one went live.

    ⚠️ PORTABILITY: `audio_lang` has NO SAFE DEFAULT (see check_module()'s identical note). "en" is
    only a fallback for a caller that forgets to pass it explicitly; config.transcripts.audio_lang
    should always be set per channel, and every real caller in this pipeline does pass it.
    """
    tdir = os.path.join(base, "transcripts")
    whisper = os.path.join(tdir, "%s.%s" % (video_id, WHISPER_EXT))
    if os.path.exists(whisper):
        return whisper, KIND_WHISPER
    native = os.path.join(tdir, "%s.%s.vtt" % (video_id, audio_lang))
    if os.path.exists(native):
        return native, (KIND_NATIVE_EN if audio_lang == "en" else KIND_NATIVE % audio_lang)
    en = os.path.join(tdir, "%s.en.vtt" % video_id)
    if os.path.exists(en):
        return None, ("only an auto-translated %s.en.vtt exists (the audio is '%s'); an "
                      "auto-translated track is NOT a chapter source. Run whisper_timed."
                      % (video_id, audio_lang))
    return None, "no timed transcript on disk for %s" % video_id


def resolve_text_source(base, video_id):
    """The PLAIN-TEXT transcript a writer should ground titles/descriptions in.

    WHY THIS EXISTS (found during a pre-flight, 16 Jul 2026). Whisper-first was about to buy us
    nothing for the copy that actually ships. `whisper_timed.py` writes a timed `.whisper.vtt`,
    but `write_descriptions.py` and `extract_subtopics.py` both read `transcripts/<id>.txt`, a
    DIFFERENT file that the whisper run never touches. On one module that meant ~12 hours of
    whisper compute would improve the CHAPTERS while all 91 titles and descriptions were still
    generated from YouTube's Devanagari auto-caption text: the same class of untrustworthy source
    this file exists to distrust. Prefers our own clean English whisper text, falls back to the
    legacy `.txt` so every module already shipped is unaffected.
    """
    tdir = os.path.join(base, "transcripts")
    for p in (os.path.join(tdir, "%s.whisper.txt" % video_id),
              os.path.join(tdir, "%s.txt" % video_id)):
        if os.path.exists(p):
            return p
    return None


def english_view(base, video_id):
    """The English-text view of a video, for the English-only detectors (PROMO, label_drift).

    A Devanagari track defeats an English regex completely, so promo and drift are judged on
    English text even when the BUILD source is the Hindi track. These are different questions and
    they legitimately read different files: source_trust asks "may we build from this", promo and
    drift ask "does this English label match this English rendering of the same audio". The two
    tracks are timestamp-aligned because they are ASR of the same audio.
    """
    tdir = os.path.join(base, "transcripts")
    for p in (os.path.join(tdir, "%s.%s" % (video_id, WHISPER_EXT)),
              os.path.join(tdir, "%s.en.vtt" % video_id)):
        if os.path.exists(p):
            return p
    return None


# --- 1. SOURCE TRUST --------------------------------------------------------------------
# An auto-TRANSLATED YouTube caption of a Hindi lesson degenerates into engagement filler.
# Real maths teaching never looks like this.
GARBAGE_MARKERS = re.compile(r"\bsubscribe\b|\bsubscribe button\b|\blike and share\b|\bbell icon\b", re.I)
GARBLE_PER_1K_WORDS = 3.0      # more than 3 'subscribe' per 1000 words = not a real transcript
# CALIBRATION NOTE (this threshold was WRONG on the first cut and flagged 100+ good transcripts):
# natural Hinglish teaching speech is only ~9-15% unique words, which is NORMAL, not degenerate.
# A unique-word ratio is a bad loop detector. Only a truly collapsed transcript goes below 5%.
# The real signature of the auto-translated garbage is the 'subscribe' spam above, not this.
MIN_UNIQUE_RATIO = 0.05
# A Devanagari transcript has almost NO [a-z] words, so the Latin word-count test below reads it
# as empty. Measured 16 Jul 2026: pointed at the file build_chapters actually picks, the Latin-only
# test REJECTED 36 of the 97 hi-sourced videos in one module with "almost no words" (one real
# video: 730 real cues, 15 Latin words). It was rejecting the exact clean native track this file
# says to PREFER, and the 36-vs-97 split was not principled: it just tracked how many stray
# English maths terms leaked into each track. A native track is therefore judged on ITS OWN script.
DEV_RANGE = ("ऀ", "ॿ")
MIN_NATIVE_CUES = 50           # a real lesson has hundreds; this only catches an empty/stub file
MIN_NATIVE_CHARS = 400

# --- 2. PROMO ---------------------------------------------------------------------------
# The teacher's outro pitch. A chapter must never point at this: the student clicked to learn.
PROMO = re.compile(
    r"\bjoin (the |my |these |our )?(live )?batch|\benroll\b|\benrollment\b|recordings? (are )?available|"
    r"\bbooster course\b|\binvite code\b|\bacademy subscription\b|\bprice hike\b|\bscholarship\b|"
    r"link (i'?ll |i will )?put|link in (the )?description|one year subscription|"
    r"live ranking|free of cost|"
    # --- ADDED 17 Jul 2026 by a calibration pass (a detector's recall is a NUMBER, not a feeling).
    # The list above was written from one module's CAPTION tracks and knew exactly ONE of this
    # teacher's pitches (a named test-series product, deliberately not hardcoded here — see the
    # portability note below). Reading 15 whisper tails BY CONTENT (blind to this regex) found a
    # SECOND, entirely different pitch script it was 100% blind to: a competitor-platform
    # subscription pitch, built around a referral-code call-to-action and a phrase this file
    # special-cases below. Every phrase in this regex was reverse-engineered from a REAL tail, not
    # invented; none is reproduced verbatim in this comment, on purpose. Measured on all 224 live
    # chaptered videos at the time: recall 5/8 -> 8/8 of read-confirmed pitches and 12/21 -> 14/21
    # of that module's promo chapters, with ZERO new false alarms.
    # ⚠️ PORTABILITY: this whole PROMO list is CALIBRATED to one teacher's actual pitch scripts,
    # not a generic list. The first-cut version above knew only one branded product name (omitted
    # here so this file stays free of any real brand); it took reading real tails BY CONTENT to
    # find the others. A new channel starts this detector at zero recall and must reseed it by
    # reading a sample of its own videos' tails, blind to this regex, the same way.
    r"\breferral code\b|\biconic\b|\bjoin (uh )?(any |my |these |the )?classes\b|\blooking to join\b|"
    r"\bfree classes\b|\bdaily (life|live) classes\b|\bfull length (mocks|marks)\b|\bmath mocks\b|"
    r"\bquestions in the app\b|\bin the app and website\b|\bno book (is )?(needed|required)\b|"
    r"\b\d+ plus percentile\b|\bsubscriptions?\b|"
    # A phrase tied to that competitor platform's audio rendering is the single highest-value
    # phrase here (+2 chapters in that module on its own; deliberately not spelled out in this
    # comment). It is NOT admitted bare: measured, the bare form false-fires on one real video's
    # 0:00 stamp, in a teaching-context sentence that happens to share the same words. Admitted
    # only in its selling constructions: a generic lint on domain content produces false FAILs,
    # and false FAILs are what train an operator to switch a rule off.
    r"\bat an academy\b|\ban academy (app|subscription|practice)\b|\bcovered at an academy\b|"
    # --- ADDED 24 Jul 2026 (a later module). A THIRD pitch script the two above were 100% blind to:
    # an "advanced-batch course announcement" the teacher runs near the end of a problem-set-heavy
    # video series (a schedule-change notice naming a start day, an exam-distance countdown, and a
    # description of the harder question mix coming up). It is NON-TEACHING and a chapter must
    # never open on it, but it names no branded product, "subscription", or "enroll", so
    # opens_in_promo passed 7 chapters that OPENED on it and only label_drift flagged them (the
    # label's teaching words are absent because the segment is an announcement). Every phrase in
    # this regex is reverse-engineered from a real tail from that module, not reproduced verbatim.
    # MEASURED: recall 7/7 of that module's announcement chapters; precision across all 340 live
    # chaptered videos at the time = 1 flag (one real video whose chapter title was itself a
    # course-schedule label, not teaching), 0 real-teaching false positives. A firing test watches
    # this fire.
    r"moving closer to (the exam|math)|months away from (the exam|math)|revised format|"
    r"from this sunday onwards|till full year|i will completely be taking|"
    r"questions are not asked here|only good medium|lots and lots of (questions|practice)|"
    r"good high quality questions", re.I)
# CALIBRATION NOTE: the question is NOT "does a pitch follow this chapter" (one always does, at the
# end of most videos, and the final-answer chapter legitimately sits just before it). The question
# is "does this chapter OPEN in a pitch", because that is what the student lands on when they click.
# Judged on the chapter's first PROMO_WINDOW seconds only. Verified by hand against an early
# 125-video fence: a 20s window cleanly separates the real promo chapters from final-answer
# chapters with an outro after.
PROMO_WINDOW = 20
PROMO_MIN_HITS = 1

# --- 4. HYGIENE -------------------------------------------------------------------------
# CALIBRATION NOTE: articles (the/a/an) are a NORMAL start ("The n/D Ratio for Percentage Change")
# and flagging them was a false positive. Only a leading PREPOSITION/conjunction means the phrase
# got chopped, which is the real defect we shipped ("Averages 1: of AP Series").
DANGLING = re.compile(r"^(?:of|in|for|with|and|to|by|on|at|from)\b", re.I)
ASR_TYPO = re.compile(r"\ballegations?\b", re.I)          # ASR mishears "alligation"
# A label may legitimately CONTAIN an operator ("A+B+C+D less than 30", "nCr equals nCn minus r").
# It may never OPEN on one: that means the expression's first term was dropped.
#
# ⚠️ THE SYMBOL FORM WAS ONLY HALF THE CLASS, AND THE AUDIT FOUND THE OTHER HALF WITHIN THE HOUR.
# The very same defect also arrives SPELLED OUT: a real case had the model return "Plus b plus c
# plus d equal to 9", where it again dropped the leading A but wrote the operator as a word, so a
# rule matching only "+" walked straight past it. This is the general lesson ("ask what the SAME
# damage looks like in the neighbouring notation") failing to be applied far enough on the first
# pass. An operator WORD is only a defect at the START of a label; "A plus B" is fine, "Plus b" is
# a fragment.
# The WORD form needs the fragment's signature, not just the word: an operator followed by a bare
# variable or number ("Plus b plus c", "Minus 3y squared"). Matching the word alone false-fired on
# perfectly good labels like "Plus-minus rule for surds" and "Equals sign in identities", and a
# rule that cries wolf on legitimate maths is a rule the next operator switches off.
OPENS_ON_OPERATOR = re.compile(
    r"^\s*(?:[+*/=<>^%,;:)\]}-]"
    r"|(?:plus|minus|times|equals?|into|divided\s+by|upon)\s+[a-z0-9]{1,2}\b)", re.I)
STOP = {"the", "a", "an", "and", "or", "of", "in", "for", "with", "to", "by", "on", "at", "from",
        "is", "are", "how", "what", "using", "understanding", "solving", "calculating", "finding",
        "applying", "problem", "problems", "question", "questions", "concept", "concepts", "method",
        "methods", "introduction", "intro", "basics", "part", "explained", "example", "examples"}


DEDUPE_PREFIX_CHARS = 24



ALL_MODULE_WORDS_NOTE = """Historically, five production call sites still read the raw hand list
after this function was introduced, so a fix here alone was not a fix for the pipeline."""

def all_module_words(cfg, base="."):
    """Every module name that must never be tacked onto a chapter label.

    `config.module_words` used to be a HAND-MAINTAINED list of the modules done so far
    ("Arithmetic", "Algebra", ...). That means the gate is blind to the module you are ACTUALLY
    running the moment you start a new one, which is exactly how one module shipped a defective
    label live. Union the configured list with every distinct topic in module_map.csv, so a new
    module is covered the day its rows exist and nobody has to remember to add it by hand.
    """
    words = list(cfg.get("module_words") or [])
    seen = {w.strip().lower() for w in words if w and w.strip()}
    try:
        import csv as _csv, os as _os
        with open(_os.path.join(base, "module_map.csv"), encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                t = (r.get("topic") or "").strip()
                if t and t.lower() not in seen:
                    words.append(t)
                    seen.add(t.lower())
    except (FileNotFoundError, OSError):
        pass   # no map in this folder: fall back to the configured list, never crash the build
    return words


def parse_cues(path, whisper=None):
    """[(seconds, text)] from a .vtt.

    DEDUPE IS ADJACENT-ONLY, AND THAT MATTERS. This used to dedupe against a GLOBAL set of every
    cue's first 24 chars, which silently deleted legitimately repeated speech LATER in the video.
    A sales pitch is exactly the formulaic, repeated speech that deletes: a teacher who runs the
    same pitch mid-video and again in the outro had the second occurrence's cues removed, so a
    chapter opening on the second pitch saw an EMPTY window and opens_in_promo returned None. The
    rolling-caption artifact this dedupe exists for is always ADJACENT (YouTube repeats the
    previous line in the next cue), so adjacent-only kills the artifact and keeps the evidence.

    AND THE DEDUPE IS NOW SOURCE-AWARE, BECAUSE THE FUZZY RULES ONLY BELONG TO CAPTIONS (found
    21 Jul 2026 on one module's first chunk). The 24-char-prefix and substring rules exist to
    strip YouTube's ROLLING caption artifact, where each cue re-states the tail of the one before.
    Whisper writes discrete sentences and produces no such artifact, so on a whisper source those
    two rules can only ever delete real content. MEASURED on 25 files of each kind:

        YouTube caption track : prefix rule removes 36.0%, substring rule 45.4%  (load-bearing)
        whisper timed vtt     : prefix rule removes  0.3%, substring rule  0.3%  (pure loss)

    and every whisper removal inspected was wrong, several deleting the INFORMATIVE member of the
    pair: "What is the probability that exactly 4 of them will show composite numbers" was deleted
    because the previous cue was the fragment "What is the probability that?", and two steps of an
    enumeration ("0 to 9, 10 options for the second/third place") were deleted because they share
    a 24-char prefix with the first. Deleting a cue removes a real moment from the timeline the
    chapter builder is allowed to place on, and removes the evidence the truth gate reads at that
    second, so the damage is invisible: less evidence looks exactly like nothing found.

    `whisper` defaults to reading the PROVENANCE OFF THE FILENAME (whisper output is the only
    thing ever written as <id>.whisper.vtt, and whisper_timed proves the language of its own
    output before writing it). Every caller passes a path, so all of them inherit this for free
    rather than each having to remember. Pass it explicitly only in tests.
    """
    if not os.path.exists(path):
        return []
    if whisper is None:
        whisper = str(path).endswith(WHISPER_EXT)
    raw = open(path, encoding="utf-8", errors="replace").read().splitlines()
    out, prev = [], ""
    for j, line in enumerate(raw):
        m = CUE.match(line.strip())
        if not m:
            continue
        h, mm, ss = m.groups()
        t = int(h or 0) * 3600 + int(mm) * 60 + int(ss)
        txt = " ".join(x.strip() for x in raw[j + 1:j + 3] if x.strip() and "-->" not in x)
        txt = re.sub(r"<[^>]+>", "", txt).strip()
        if not txt:
            continue
        if txt == prev:
            # An exact adjacent repeat is a repeat under either source. Dropping it costs a
            # timestamp and no information.
            continue
        if not whisper and (txt[:DEDUPE_PREFIX_CHARS] == prev[:DEDUPE_PREFIX_CHARS]
                            or (prev and txt in prev)):
            continue
        out.append((t, txt))
        prev = txt
    return out


def window_text(cues, lo, hi):
    return " ".join(x for t, x in cues if lo <= t <= hi)


def _devanagari_ratio(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if DEV_RANGE[0] <= c <= DEV_RANGE[1]) / len(letters)


def source_trust(cues, kind=KIND_LEGACY):
    """May we build chapters from this transcript? Returns (ok, reason).

    SCRIPT-AWARE ON PURPOSE. The Latin word-count path below cannot read Devanagari and was
    rejecting the clean native Hindi track this file tells us to prefer (36 of 97 measured). A
    native non-Latin track is judged on its own script instead.
    """
    text = " ".join(x for _, x in cues)
    if kind.startswith("native-") and kind != KIND_NATIVE_EN:
        # A native ASR track in a non-Latin script. The 'subscribe'-spam signature is an ENGLISH
        # artifact of auto-TRANSLATION and cannot appear here, so the only honest question a
        # deterministic check can ask is "is this a real, populated transcript".
        if len(cues) < MIN_NATIVE_CUES:
            return False, "native track has almost no cues (%d)" % len(cues)
        if len(text) < MIN_NATIVE_CHARS:
            return False, "native track has almost no text (%d chars)" % len(text)
        if _devanagari_ratio(text) < 0.20:
            return False, ("native track is only %.0f%% Devanagari: it may be the wrong track or a "
                           "translation in disguise" % (100 * _devanagari_ratio(text)))
        return True, ""
    words = re.findall(r"[a-z']+", text.lower())
    if len(words) < 50:
        return False, "transcript has almost no words (%d)" % len(words)
    per_1k = len(GARBAGE_MARKERS.findall(text)) / (len(words) / 1000.0)
    if per_1k > GARBLE_PER_1K_WORDS:
        return False, ("auto-translated engagement filler: %.1f 'subscribe'-class markers per 1000 "
                       "words. Chapter this from whisper or the native track, not this." % per_1k)
    # NOTE: a unique-word RATIO is deliberately NOT used. It scales with length (the 3h video is
    # naturally ~3% unique) and flagged good transcripts. The engagement-filler signature above is
    # the honest detector for the auto-translated garbage we actually hit.
    return True, ""


def opens_in_promo(cues, sec):
    """Does the chapter OPEN in a sales pitch? Judged on its first PROMO_WINDOW seconds, which is
    exactly what a student sees when they click it. Returns the offending phrase, or None."""
    head = window_text(cues, sec, sec + PROMO_WINDOW)
    hits = PROMO.findall(head)
    if len(hits) >= PROMO_MIN_HITS:
        m = PROMO.search(head)
        return re.sub(r"\s+", " ", head[max(0, m.start() - 30):m.start() + 70]).strip()
    return None



# --- 2b. PROMO LEAD-IN (a REVIEW signal, never a BLOCKER) -------------------------------
# THE GAP THE 20s WINDOW CANNOT SEE, MEASURED (Stage 2.6, 17 Jul 2026). opens_in_promo asks
# "does the student land in a pitch", judged on the first PROMO_WINDOW seconds, and it is right
# to: it has ZERO measured false alarms across 224 live chaptered videos. But 7 of the 21 real
# module-3 promo chapters open on the TAIL OF THE PREVIOUS ANSWER and the ad starts ~20-45s in
# and never stops. The human auditors called those promo chapters, and they were right: a student
# clicking gets 20 seconds of leftovers and then an ad.
# WHY THIS IS REVIEW AND NOT A BLOCKER. Separating "answer-tail then ad" from "the legitimate
# final-answer chapter, which ALWAYS has an outro after it" needs a judgment about whether the
# segment contains real TEACHING, and deterministic code cannot make it. Measured: as a blocker
# this newly flags 2-9 already-live chapters, and a 50%-share variant flags the auditors' OWN
# corrected chapter. So it does what this file's doctrine says: BLOCK what you can prove, QUEUE
# what you cannot. It lifts module-3 recall 14/21 -> 18/21 by handing the skeptic 2 extra
# candidates on the live 125, not by inventing 9 verdicts.
PROMO_LEAD_S = 30          # measured: 30s catches 4 of the 7; 45-60s adds 1 more at 2.5-4.5x the queue
PROMO_TAIL_QUIET_S = 45    # "and it never stops": no >=45s stretch without a pitch cue after onset


def promo_lead_in(cues, sec, seg_end):
    """Does this chapter open on teaching and then run into a pitch that never stops? Returns the
    onset offset in seconds, or None. A REVIEW signal for the skeptic, never a blocker."""
    seg = [(t, x) for t, x in cues if sec <= t < seg_end]
    hits = [t for t, x in seg if PROMO.search(x)]
    if not hits:
        return None
    onset = min(hits)
    if (onset - sec) > PROMO_LEAD_S:
        return None
    if (seg_end - max(hits)) > PROMO_TAIL_QUIET_S:
        return None                     # teaching resumes after it: this is a mid-video mention
    return onset - sec


def label_drift(label, cues, sec, duration, kind=KIND_LEGACY):
    """THE detector for the defect we shipped. If the label's distinctive words are ABSENT at the
    chapter's own timestamp but PRESENT elsewhere in the video, the label was written for a
    different moment: the model guessed the time and snap() made the guess look real.

    THIS IS A *REVIEW* SIGNAL, NOT A BLOCKER, AND THAT IS DELIBERATE. Measured on the remediated
    module it fires on ~7% of chapters and most of those are correct labels. It CAUGHT a real
    shipped mislabel ("Multiplying by 11 and 12" sitting over a divide-by-5 segment) and did not
    fire on its correction, so it is a good NARROWING signal: it turns 472 chapters into ~46
    candidates for the model skeptic to adjudicate. Deterministic code cannot settle this one; it
    can only aim the skeptic.

    KIND-AWARENESS (16 Jul 2026). The at==0/away>=4 thresholds were calibrated against HINGLISH
    CAPTION tracks, where a correct English label legitimately has none of its words spoken in
    English at that second because the teacher says it in Hindi. That tolerance is a liability on
    whisper ENGLISH text, where the label's words being absent at its own timestamp is genuinely
    suspicious: the loose threshold would let a drifted label whose words appear 3 times elsewhere
    pass silently, and since the model skeptic only adjudicates what this fires on, detector recall
    caps audit coverage. English sources therefore get the tighter bar.
    """
    if sec == 0:
        return None            # a 0:00 intro chapter legitimately never states the topic verbatim
    words = [w for w in re.findall(r"[a-z]{4,}", label.lower()) if w not in STOP]
    if len(words) < 2:
        return None
    here = window_text(cues, sec, sec + 60).lower()
    elsewhere = " ".join(x for t, x in cues if not (sec - 20 <= t <= sec + 60)).lower()
    at = sum(1 for w in words if w[:6] in here)
    away = sum(elsewhere.count(w[:6]) for w in words)
    if at:
        return None
    # The signature: NONE of the label's distinctive words occur at its own timestamp.
    if kind == KIND_WHISPER:
        if away >= 2:
            return ("label's words (%s) never occur at this timestamp but occur %d times elsewhere "
                    "in the video: the label was written for a different moment"
                    % (", ".join(words[:3]), away))
        return ("label's words (%s) never occur at this timestamp in a clean ENGLISH transcript, "
                "which a correct label normally would" % ", ".join(words[:3]))
    # Hinglish/native/legacy view: require strong presence elsewhere before calling it drift,
    # because absence at the timestamp is not evidence when the audio is not English.
    if away >= 4:
        return ("label's words (%s) never occur at this timestamp but occur %d times elsewhere in "
                "the video: the label was written for a different moment"
                % (", ".join(words[:3]), away))
    return None


# Words that are a real TOPIC when they head a term, not a module word tacked on the end.
MODULE_WORD_EXCEPTIONS = re.compile(
    r"(Arithmetic|Geometric|Harmonic) (Progression|Mean|Sequence|Series)$", re.I)


# THE YEAR RULE, TIGHTENED 17 Jul 2026, AND WHY IT IS A COPY OF validate.py's matching check.
# hygiene used a bare `20\d{2}`, so it flagged ANY label containing a year-shaped SUBSTRING:
# "Prime Factorising 882000" (the 2000 inside 882000), "Sum of 2025 and 675", "Factors of 82000".
# On a NUMBER SYSTEM module, that is most of the vocabulary. validate.py's own year check had
# ALREADY been tightened for exactly this (a real case: "one video's label contains 2025, and
# 2025 is the NUMBER used in a worked example, not a stale exam year"), and hygiene never got the
# same fix, so the two copies of one rule disagreed: the validator cleared what the truth gate
# blocked. It was found when the gate blocked a CORRECT hand-placed label during a live fix.
# A false FAIL is not harmless: it is what trains an operator to switch a rule off.
# Only an exam-YEAR CONTEXT counts, which is what validate.py's check already encodes. Reuse it;
# do not re-implement it, or the two will drift apart again.
#
# ⚠️ PORTABILITY. This used to hardcode the specific sibling exam abbreviations of one exam
# family (this channel's exam plus several others in the same admissions cycle). That made the
# rule silently blind on any other channel's exam names, and it also baked one channel's exact
# competitive-exam landscape into a supposedly channel-agnostic file. The exam's own name is
# already checked separately in hygiene() via config (see the `exam` argument below); this
# context list stays generic so a new channel does not have to touch this file to get the same
# protection.
_YR = r"20(?:0\d|1\d|2[0-5])"
_EXAM_CTX = r"(?:exam|entrance|test|syllabus|paper|prep(?:aration)?|batch|course)"
STALE_YEAR_RE = re.compile(
    r"(?:" + _EXAM_CTX + r"[^\w\n]{0,12}" + _YR + r"|" + _YR + r"[^\w\n]{0,12}" + _EXAM_CTX + r")",
    re.IGNORECASE)


# ---- label SHAPE detectors -------------------------------------------------------------------
# These live HERE, next to hygiene(), because verify_truth is the ONE place a detector is allowed
# to be defined (acceptance rule: no duplicated detector logic). The chunk sweep, the windowed
# builder's repair pass and the test suite all import these. Two copies of a rule that disagree is
# a validator that is decoration.

# A label whose head was eaten by a prefix strip, leaving it starting on a function word. This
# is the A4 shape that shipped live as "Averages 1: of AP Series".
DANGLING_START = re.compile(r"^(?:of|in|for|with|and|to|by|on|at|from|into|onto|about)\b", re.I)

# A label the model left mid-phrase. A real case (17 Jul 2026):
# "Four digit number reversed decreases by"  -  it decreases by WHAT? The chapter list is the
# navigation, and a sentence that stops dead reads as broken to the student.
TRUNCATED_TAIL_RE = re.compile(
    r"\b(?:by|of|to|with|for|in|on|at|is|are|and|or|the|a|an|from|than|into|when|if)\s*$", re.I)


def truncated_label(label):
    return bool(TRUNCATED_TAIL_RE.search((label or "").strip()))


# Filler that carries NO distinguishing information in a maths chapter list: every chapter in a
# question-practice video is "solving" something. Two labels that differ ONLY by one of these are
# the same label wearing a hat.
LABEL_FILLER_RE = re.compile(
    r"^(?:let(?:'|\u2019)?s\s+)?(?:solve|solving|calculate|calculating|find|finding|evaluate|"
    r"evaluating|determine|determining|compute|computing|simplify|simplifying|question|example)"
    r"\s+(?:for\s+|of\s+|the\s+|a\s+|an\s+)*", re.I)


def label_key(label):
    """Normalise a label to what it actually SAYS, so that two labels differing only by a filler
    verb collide.

    ⚠️ WHY THIS IS NOT JUST lower(). The repair pass was asked to distinguish a duplicate
    "X plus 1 by x" and returned "Calculate x plus 1 by x" (a real case, 17 Jul 2026).
    That satisfied an exact-match dedupe check and taught the student NOTHING: it is the same
    phrase with a verb bolted on. A check that compares raw strings will always be beaten by a
    cosmetic edit, and the model will find the cheapest edit that passes. This is the whole point:
    uniqueness is not distinguishability, so the KEY must be built from the informative words.
    """
    k = LABEL_FILLER_RE.sub("", (label or "").strip())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", k.lower())).strip()


def duplicate_labels(chapters):
    """Two chapters in ONE video carrying the SAME label. Returns {norm_label: [secs]} for the
    collisions only.

    THE WINDOWED PATH CREATES THIS BY CONSTRUCTION and it took real data to see it. Each window is
    labelled in isolation (that isolation is the whole point: it is what stops the model guessing
    at a timeline it cannot see). But a concept the teacher returns to in window 3 and window 7
    gets read honestly, and identically, both times: "x plus 1 by x" at 14:30 and again at 23:39.
    Each label is TRUE. The pair is still useless, because the student cannot tell which one to
    click: uniqueness is not distinguishability. The fix is never to invent a difference; it is to
    re-read the segment and name what THIS one actually does.
    """
    norm = {}
    for t, lab in chapters:
        norm.setdefault(label_key(lab), []).append(t)
    return {k: v for k, v in norm.items() if len(v) > 1 and k}


# --- NEAR-DUPLICATE LABELS -------------------------------------------------------------
# Words that carry no distinguishing power in a chapter label on this kind of channel.
DUP_STOP = {"the", "a", "an", "and", "or", "of", "in", "for", "with", "to", "by", "on", "at",
            "from", "is", "are", "using", "based", "problems", "problem", "questions", "question",
            "concept", "concepts", "method", "methods", "introduction", "basics", "part",
            "number", "cases", "case", "value", "values"}
# ⚠️ 0.67 WAS TUNED ON PRECISION ALONE AND HAD 5/9 RECALL. During an earlier module's fix this
# detector was hardened THREE times, every time in the direction of flagging LESS, and every time
# validated against pairs humans had ruled DISTINCT. Recall was never re-measured. A later
# independent audit then found three REAL duplicates sitting at J = 0.57, 0.50 and 0.40, under the
# knee, none of which had ever been queued or read. MEASURED over all 9 known-real duplicates from
# that module's incident record and 6 pairs humans explicitly ruled DISTINCT:
#     0.67 -> recall 5/9, 0 false alarms      (what shipped: missed 44% of real defects)
#     0.55 -> recall 7/9, 2 false alarms
#     0.50 -> recall 8/9, 2 false alarms
#     0.40 -> recall 9/9, 2 false alarms      <- chosen
# This is a REVIEW queue, not a blocker: a false alarm costs a reader one comparison, a miss ships
# a defect a student sees. So recall wins, and the two false alarms are accepted and named.
NUM_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
             "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11",
             "twelve": "12", "twenty": "20", "thirty": "30", "hundred": "100"}
NEAR_DUP_JACCARD = 0.40


def _dup_sig(label):
    """⚠️ DIGITS ARE CONTENT, NOT NOISE, IN A MATHS CHAPTER LABEL. The first version extracted
    only [a-z]+, so "Sum of 19 with four dice" and "Sum of 21 with four dice" reduced to the same
    signature {sum, four, dice} and were flagged as duplicates: on that one video, five pairs the
    human adjudicators had ruled DISTINCT for exactly this reason (the target sum IS the question).
    A detector that discards the tokens carrying the difference manufactures collisions, and a
    reviewer sent to adjudicate invented pairs learns to distrust the queue."""
    return frozenset(w for w in re.findall(r"[a-z0-9]+", (label or "").lower())
                     if w not in DUP_STOP)


def near_duplicate_labels(chapters, threshold=NEAR_DUP_JACCARD):
    """Pairs of chapters in ONE video whose labels a student could not tell apart. REVIEW, not
    BLOCK: the detector narrows, a reader decides.

    ⚠️ WHY THE EXACT-MATCH VERSION WAS NOT ENOUGH, MEASURED TWICE. `duplicate_labels` normalises
    and compares for EQUALITY. A later audit found 22 near-duplicate pairs on a module where the
    exact detector reported ZERO, and then the fix for the four it confirmed INTRODUCED a new
    pair: "Maximum intersection points of diagonals" at 0:00 against "Maximum number of
    intersection points of diagonals" at 7:37. Those differ by one stopword, so exact matching
    cannot see them, and the replacement label had been validated against hygiene but not against
    its own siblings. Fixing the instances twice without fixing the DETECTOR is why the class
    survived two passes.

    Subset containment counts as a collision ("Linear Races" inside "Linear Races Head Starts"),
    because the shorter label tells a student nothing the longer one does not.

    CALIBRATED, not chosen. Pairs flagged across the whole live playlist:
        threshold 0.50 -> 240 pairs / 129 videos      (too noisy to adjudicate)
        threshold 0.60 -> 169 pairs / 97 videos
        threshold 0.67 -> 137 pairs / 75 videos       <- the knee; 0.75 gives the same
    Above 0.67 the count stops moving because subset containment dominates, so 0.67 buys the
    widest net that costs nothing extra to adjudicate.
    """
    out = []
    for i in range(len(chapters)):
        t1, l1 = chapters[i]
        s1 = _dup_sig(l1)
        if not s1:
            continue
        for j in range(i + 1, len(chapters)):
            t2, l2 = chapters[j]
            s2 = _dup_sig(l2)
            if not s2:
                continue
            # ⚠️ A DIFFERING NUMERAL IS THE WHOLE DIFFERENCE, AND WORD-OVERLAP CANNOT WEIGH IT.
            # "5 distinct balls to 3 similar boxes" and "8 distinct balls to 3 similar boxes"
            # score 0.71 and are two different questions; human adjudicators ruled them DISTINCT
            # for exactly that reason. In a maths chapter list the number IS the identity of the
            # question, so when both labels carry numerals and those numerals differ, they are
            # distinguishable whatever the surrounding words do.
            # SPELLED-OUT NUMBERS ARE NUMERALS TOO. "Four different books into five identical
            # boxes" vs "Five different books into three similar boxes" are two different
            # questions, and a digits-only rule cannot see that, so it flagged them.
            n1 = {NUM_WORDS.get(w, w) for w in s1 if w.isdigit() or w in NUM_WORDS}
            n2 = {NUM_WORDS.get(w, w) for w in s2 if w.isdigit() or w in NUM_WORDS}
            if n1 and n2 and n1 != n2:
                continue
            # CONTAINMENT NEEDS A SIZE FLOOR. A label that reduces to two or three tokens after
            # stopwords sits inside almost any longer label by accident: "Cases for A equal to B"
            # -> {a, b, equal} is a subset of "A+B+C+D less than or equal to 30", and those are
            # two different questions (the human adjudicators said so). Containment only means
            # "the short one tells you nothing extra" when the short one is a real share of the
            # long one, so require at least half its tokens.
            short, long_ = (s1, s2) if len(s1) <= len(s2) else (s2, s1)
            contained = short <= long_ and len(short) / len(long_) >= 0.5
            if contained or (len(s1 & s2) / len(s1 | s2)) >= threshold:
                out.append((t1, l1, t2, l2))
    return out


# A 0:00 label that HONESTLY announces the segment is an offer / course plan / admin, rather than
# pretending it is teaching.
PROMO_HONEST_RE = re.compile(
    r"\b(offer|course plan|session plan|subscription|announcement|housekeeping|discount|"
    r"batch plan|enrol|what this session covers before|admin)", re.I)


def zero_promo_exempt(sec, label):
    """May a chapter at THIS second sit on a sales pitch?

    ONLY at 0:00, and ONLY if its label says so. THE OPERATOR DECIDED THIS (17 Jul 2026) and it is
    a real amendment to the promo-landing rule's intent, so it is written down rather than
    smuggled in.

    The promo-landing rule exists to stop a student CLICKING a chapter and landing in an advert.
    Nobody clicks into 0:00: it is where the video already starts. On one real video the first
    9m19s are an unbroken pitch ("there is an offer going on / 20 percent off on subscription"),
    so the platform rule (a list needs a 0:00 stamp or YouTube renders NONE of it) and the
    promo-landing rule could not both hold, and the video was shipping with no chapters at all.
    Labelling 0:00 for what it honestly is lets a student SKIP the nine minutes  -  strictly
    better for them than no chapter list.

    THE PRICE OF THE EXEMPTION IS HONESTY, and that is the whole safeguard. A 0:00 chapter on a
    pitch labelled "What This Session Covers" would be a LIE about what is there, and a lie is the
    one thing this pipeline exists to prevent; it stays a BLOCKER. The exemption is granted to the
    LABEL, not to the timestamp. Any chapter after 0:00 that opens on a pitch is still blocked,
    because that one IS clicked into.
    """
    return sec == 0 and bool(PROMO_HONEST_RE.search(label or ""))


# --- GENERIC-LABEL (an orthogonal check the near-duplicate detector is blind to) ----------------
# WHY THIS EXISTS, AND WHY NEAR-DUPLICATE COULD NEVER CATCH IT (a later module, 44 numbered sets of
# unrelated problems). On a module shaped like that, the laziest thing the model can return is
# "Question 1", "Question 2", "Problem 3", "Sum 5". Measured against every gate before this one was
# written, ALL of them pass such a block:
#     near_duplicate_labels("Question 1","Question 2")  -> not flagged (Jaccard 0.333 < 0.40)
#     hygiene / duplicate_labels / truncated_label      -> pass (well formed, no exact match)
#     label_drift                                        -> TRUE (a question IS solved at that sec)
#     validate.py's own shape checks                     -> pass (all form, no meaning)
# That is a structurally perfect, semantically empty chapter list: the exact thing this pipeline
# exists to prevent, in a new costume. It is ALSO the sub-0.40 class an earlier module knowingly
# left open ("the right fix is an ORTHOGONAL check, not a lower threshold"). A lower Jaccard cannot
# reach it without flooding the queue; a DIFFERENT question does. This detector asks "does the
# label name a topic at all", which near-duplicate never asks.
#
# It is a REVIEW signal, never a BLOCKER: a container word can occasionally be the real subject
# ("Set Theory", "Number System"), so code narrows and a reader decides. Both directions measured
# in the same edit: see test_chapter_gates.py, precision run against EVERY live chapter label in
# the playlist, recall against the canonical generic forms.
GENERIC_CONTAINERS = {
    "question", "questions", "q", "problem", "problems", "prob", "sum", "sums",
    "example", "examples", "eg", "ex", "exercise", "exercises", "illustration",
    "illustrations", "practice", "solution", "solutions", "set", "sets", "sample",
    "samples", "quant", "quants", "qs", "ques",
}
# Index tokens and content-free filler: removed before deciding whether anything of substance
# remains. NOT topic words. "part" is here because "Part 3" is an index, not a subject.
GENERIC_FILLER = {
    "part", "parts", "no", "number", "num", "contd", "continued", "cont", "type",
    "based", "solving", "solved", "solve", "doing", "revision", "revise", "misc",
    "miscellaneous", "assorted", "mixed", "more", "another", "next", "this", "these",
    "some", "few", "level", "advance", "advanced", "basic", "with", "on", "of", "the",
    "a", "an", "and", "for", "to", "by", "from", "in", "at",
}
_ROMAN_INDEX = {"ii", "iii", "iv", "vi", "vii", "viii", "ix", "xi", "xii", "xiii", "xiv", "xv"}
_ORDINALS = {"first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
             "ninth", "tenth", "1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th"}


def generic_label(label):
    """A chapter label that names no topic, only a container noun and an index. REVIEW, not BLOCK.

    Returns a one-line reason string if the label is generic, else None. A label is generic when,
    after stripping index numerals, roman/ordinal indices and content-free filler, EITHER nothing
    is left ("Question 1" -> {}), OR every surviving token is a bare container noun that names no
    subject ("Practice Set" -> {practice, set}). A single genuine topic token clears it
    ("Sum of 19 with four dice" keeps {dice}; "Algebra: Maximizing a Quadratic" keeps {maximizing,
    quadratic}), so a real maths label whose numbers ARE the question is never touched."""
    toks = re.findall(r"[a-z]+|[0-9]+", (label or "").lower())
    content = [t for t in toks
               if not t.isdigit()
               and t not in NUM_WORDS
               and t not in _ROMAN_INDEX
               and t not in _ORDINALS
               and t not in GENERIC_FILLER]
    if not content:
        return "names no topic, only an index (e.g. 'Question 1')"
    if all(t in GENERIC_CONTAINERS for t in content):
        return "only container word(s) {%s}, names no topic" % ", ".join(sorted(set(content)))
    return None


def hygiene(label, module_words=None, exam=None):
    """module_words comes from config (config.module_words). It was HARDCODED to
    Arithmetic/Quantitative Aptitude/Quant until 16 Jul 2026, which made this rule blind in every
    other module by construction: that is how a defect (a live "... Books Quant" chapter label)
    shipped and sat live ~20h. A gate that only works on the module it was written for is not a
    gate.

    `exam` comes from config (config.identity.exam), same reasoning: this used to hardcode one
    channel's exam name as a bare literal, which made the exam-name check silently inert on any
    other channel (it would report clean, not crash, and clean is indistinguishable from
    "nothing to find"). Passing None disables that half of the check rather than guessing."""
    bad = []
    # the EXAM NAME in a chapter label is always stuffing (the viewer already opened this exam's
    # video); a YEAR only counts in exam-year context, never as a bare number a maths label may
    # contain.
    exam_hit = bool(exam) and re.search(r"\b%s\b|\bfor %s\b" % (re.escape(exam), re.escape(exam)),
                                        label, re.I)
    if exam_hit or STALE_YEAR_RE.search(label):
        bad.append("names the exam/year")
    words = module_words or ["Arithmetic", "Quantitative Aptitude", "Quant"]
    pat = r"\s(%s)\s*$" % "|".join(re.escape(w) for w in words)
    if re.search(pat, label, re.I) and not MODULE_WORD_EXCEPTIONS.search(label):
        bad.append("module word tacked on")
    if DANGLING.match(label):
        bad.append("starts with a dangling function word")
    # A LABEL THAT OPENS ON AN OPERATOR BEGAN MID-EXPRESSION (a real case, 21 Jul 2026).
    # The MODEL returned "+B+C+D less than or equal to 30" from audio that plainly says "A plus B
    # plus C plus D is less than equal to 30": it dropped the first variable. Neither existing
    # check could see it. `truncated_label` looks for a label that STOPS mid-phrase, and DANGLING
    # looks for a leading FUNCTION WORD; a leading "+" is neither, so a visibly broken label
    # sailed into a real chapter block. This is the same family of defect (a phrase head lost)
    # arriving from the model instead of from our own cleaning, which is why it had to be caught
    # on the ARTIFACT rather than in the strip functions.
    if OPENS_ON_OPERATOR.match(label):
        bad.append("starts mid-expression on an operator (a variable was dropped)")
    if ASR_TYPO.search(label):
        bad.append("ASR typo 'allegation' (the term is 'alligation')")
    if any(c in label for c in "—–"):
        bad.append("em/en dash")
    return bad


def check_module(base):
    cfg = json.load(open(os.path.join(base, "config.json"), encoding="utf-8"))
    fence = cfg.get("in_scope_video_ids") or []
    if not fence:
        sys.exit("REFUSING: config.in_scope_video_ids is empty. Pin the slice first.")
    # ⚠️ PORTABILITY: no safe default. An earlier version of this line defaulted audio_lang to a
    # specific channel's spoken language, which silently made the resolver prefer the wrong track
    # on any channel with a different one. Getting this wrong has no crash to catch it: it just
    # trusts the wrong file. "en" is only a reasonable fallback because most of this pipeline's
    # channel-agnostic testing has been in English; config.transcripts.audio_lang should always be
    # set explicitly per channel.
    audio_lang = (cfg.get("transcripts") or {}).get("audio_lang", "en")
    exam = (cfg.get("identity") or {}).get("exam")
    module_words = all_module_words(cfg, base)
    dur = {r["video_id"]: int(float(r["duration_s"]))
           for r in csv.DictReader(open(os.path.join(base, "audit_videos.csv"), encoding="utf-8"))}
    ch = json.load(open(os.path.join(base, "chapters_by_video.json"), encoding="utf-8"))
    # A DISCLOSED WAIVER IS NOT A MISSING CHAPTER (gate-consistency with validate.py's own waiver
    # check). validate.py SKIPs a config.chapter_waived video whose reason is real; this gate did
    # NOT read the waiver list at all, so a legitimately waived short video (2 honest segments,
    # then a pitch) was a hard BLOCKER here while it PASSED validate. Two gates on the same rule
    # disagreed, and the one that blocks was the one that had never heard of the exemption the
    # operator granted. Same guard as validate: a waiver with no real reason is NOT honoured (an
    # empty waiver must not be indistinguishable from a build that silently produced nothing).
    waived = cfg.get("chapter_waived") or {}
    def _is_waived(vid):
        w = waived.get(vid) if isinstance(waived, dict) else None
        if isinstance(w, dict):
            return len((w.get("reason") or "")) >= 40
        return vid in waived if isinstance(waived, list) else False
    findings = []
    for v in fence:
        e = ch.get(v)
        if not e or not e.get("block"):
            if _is_waived(v):
                continue
            findings.append({"video_id": v, "severity": "BLOCKER", "check": "chapters",
                             "detail": "no chapter block"})
            continue

        # WHICH FILE DO WE VERIFY AGAINST? Until 16 Jul 2026 this hardcoded <id>.en.vtt while
        # build_chapters built from <id>.hi.vtt, so on 97 of the 125 videos in one module the gate
        # graded chapters against text the model never read. Builds from now on RECORD the file
        # they used and are verified against exactly that.
        src, kind = e.get("source_file"), e.get("source_kind")
        if src and not os.path.isabs(src):
            src = os.path.join(base, src)
        # A RECORDED source that has VANISHED is a hard error, never a quiet fallback.
        # (found during a pre-flight, 16 Jul.) The fallback below is for entries that never
        # recorded a source at all (an early 125-video batch predating this change). If an entry
        # DID record one and the file is now gone, falling back would verify a whisper-built
        # chapter against the auto-translated .en.vtt and report it clean: the exact defect above,
        # reopened silently, with zero findings. Fail loud instead.
        if src and not os.path.exists(src):
            findings.append({"video_id": v, "severity": "BLOCKER", "check": "source-missing",
                             "detail": "chapters were built from %r (kind=%s) but that file is GONE. "
                                       "Refusing to fall back to another track: that would verify "
                                       "these chapters against text they were never built from. "
                                       "Re-run the transcript step for this video."
                                       % (os.path.basename(src), kind)})
            continue
        if not src:
            # LEGACY PATH, DELIBERATE (operator-accepted). An early 125-video batch predates
            # source recording. Those videos are already live and were hand-remediated chapter by
            # chapter, and re-pointing them would move the frozen baseline that a later release
            # must prove it did not disturb. They keep the English view they were calibrated
            # under: byte-identical text to the pre-migration behaviour, because the whisper files
            # were renamed, not rewritten. Backlog: backfill source_file for that batch from its
            # original build record, then re-baseline with a line-item delta.
            src, kind = english_view(base, v), KIND_LEGACY
        if not src:
            findings.append({"video_id": v, "severity": "BLOCKER", "check": "source",
                             "detail": "no timed transcript to verify against"})
            continue
        cues = parse_cues(src)
        if not cues:
            findings.append({"video_id": v, "severity": "BLOCKER", "check": "source",
                             "detail": "no timed transcript to verify against"})
            continue
        ok, why = source_trust(cues, kind)
        if not ok:
            findings.append({"video_id": v, "severity": "BLOCKER", "check": "source-trust", "detail": why})
            continue

        # PROMO and DRIFT are ENGLISH-ONLY detectors: an English regex cannot see a pitch in a
        # Devanagari track. When the build source is native non-English we judge them on the
        # timestamp-aligned English view of the same audio, and we SAY SO in the finding rather
        # than reporting a video as clean that no detector could actually read.
        if kind.startswith("native-") and kind != KIND_NATIVE_EN:
            eview = english_view(base, v)
            ecues = parse_cues(eview) if eview else []
            ekind = KIND_WHISPER if (eview or "").endswith(WHISPER_EXT) else KIND_LEGACY
            if not ecues:
                findings.append({"video_id": v, "severity": "BLOCKER", "check": "detector-blind",
                                 "detail": "built from a %s track with NO English view on disk, so "
                                           "the promo and drift detectors cannot read this video at "
                                           "all. Chapter it from whisper." % kind})
                continue
            # SOURCE-TRUST THE ENGLISH VIEW TOO. The build source was trusted above, but on a
            # native-track build the promo and drift detectors read a DIFFERENT file, and nothing
            # was checking THAT one. So a video built from a clean .hi.vtt could have its ad-detector
            # and its drift-detector reading the auto-translated "subscribe subscribe button"
            # garbage this file exists to reject, and the gate would report it CLEAN: the detectors
            # find nothing in gibberish, and finding nothing looks exactly like passing.
            eok, ewhy = source_trust(ecues, ekind)
            if not eok:
                findings.append({"video_id": v, "severity": "BLOCKER", "check": "english-view-untrusted",
                                 "detail": "built from a %s track, but the ENGLISH VIEW used to "
                                           "detect promo and drift is itself untrustworthy (%s). "
                                           "Every chapter on this video is therefore unchecked for "
                                           "sales pitches and label drift. Chapter it from whisper."
                                           % (kind, ewhy)})
                continue
        else:
            ecues, ekind = cues, kind

        d = dur.get(v, 0)
        # segment ends: a chapter's segment runs to the NEXT chapter, or to the end of the video.
        _st = []
        for _l in e["block"].splitlines()[1:]:
            _m = re.match(r"^((?:\d+:)?\d+:\d{2})\s+", _l.strip())
            if _m:
                _p = [int(x) for x in _m.group(1).split(":")]
                _st.append(_p[0]*3600 + _p[1]*60 + _p[2] if len(_p) == 3 else _p[0]*60 + _p[1])
        next_sec = {_st[i]: (_st[i+1] if i+1 < len(_st) else d) for i in range(len(_st))}
        for line in e["block"].splitlines()[1:]:
            m = re.match(r"^((?:\d+:)?\d+:\d{2})\s+(.+)$", line.strip())
            if not m:
                continue
            p = [int(x) for x in m.group(1).split(":")]
            sec = p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0] * 60 + p[1]
            label = m.group(2)
            pitch = opens_in_promo(ecues, sec)
            if pitch and zero_promo_exempt(sec, label):
                # NOT a defect: the operator authorised an honestly-labelled 0:00 on a pitch so a
                # student can SKIP it (17 Jul 2026). It is still surfaced, as REVIEW, because an
                # authorised exception that becomes invisible is how an exception becomes a habit
                # -  the auditor must see every one of these and re-read the label.
                findings.append({"video_id": v, "severity": "REVIEW", "check": "promo-landing-zero",
                                 "detail": "chapter %s '%s' opens on a sales pitch but is the 0:00 "
                                           "stamp, which YouTube requires or it renders NO chapters "
                                           "at all, and its label says honestly what is there so "
                                           "the student can skip it. Authorised 17 Jul 2026. "
                                           "CONFIRM the label is still honest: \"%s\""
                                           % (m.group(1), label[:36], pitch[:80])})
            elif pitch:
                findings.append({"video_id": v, "severity": "BLOCKER", "check": "promo-landing",
                                 "detail": "chapter %s '%s' OPENS in a sales pitch, so a student "
                                           "clicking it gets an ad: \"%s\""
                                           % (m.group(1), label[:36], pitch[:80])})
            drift = label_drift(label, ecues, sec, d, ekind)
            if drift:
                findings.append({"video_id": v, "severity": "REVIEW", "check": "label-drift",
                                 # THE LABEL IS PRINTED VERBATIM, NEVER label[:40]. This queue is
                                 # what a HUMAN reads to decide whether a chapter is true, and it
                                 # was clipping at 40 chars: "Factorizing the Expression with
                                 # Higher Powers" was shown as "...with Higher P", which reads as
                                 # a truncated (defective) label and invites a NO on a chapter that
                                 # is perfectly fine. A review artifact that mangles the evidence
                                 # produces wrong verdicts, and the reviewer cannot tell.
                                 "detail": "chapter %s '%s': %s" % (m.group(1), label, drift)})
            if not pitch:
                lead = promo_lead_in(ecues, sec, next_sec.get(sec, d))
                if lead is not None:
                    findings.append({"video_id": v, "severity": "REVIEW", "check": "promo-lead-in",
                                     "detail": "chapter %s '%s' opens on teaching but a sales pitch "
                                               "starts %ds later and runs to the end of its segment. "
                                               "A student clicking it gets a few seconds of the "
                                               "previous answer and then an ad. Deterministic code "
                                               "cannot tell this from a legitimate final-answer "
                                               "chapter with an outro after it: READ THE SEGMENT."
                                               % (m.group(1), label, lead)})
            for h in hygiene(label, module_words, exam):
                findings.append({"video_id": v, "severity": "BLOCKER", "check": "label-hygiene",
                                 "detail": "chapter %s '%s': %s" % (m.group(1), label, h)})
    return fence, findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    ap.add_argument("--queue", default=None,
                    help="path for the skeptic queue (default _skeptic_queue.json). The default "
                         "name is shared across modules, so archive it before a fence repoint.")
    args = ap.parse_args()
    base = os.getcwd()
    fence, findings = check_module(base)
    blockers = [f for f in findings if f["severity"] == "BLOCKER"]
    review = [f for f in findings if f["severity"] == "REVIEW"]
    print("=" * 78)
    print("TRUTH GATE  -  is each chapter TRUE of the audio at its own timestamp?")
    print("=" * 78)
    print("  videos checked : %d" % len(fence))
    print("  BLOCKERS       : %d   (provable: promo-landing, garbage source, label hygiene)" % len(blockers))
    print("  REVIEW         : %d   (drift candidates -> hand to the model SKEPTIC to adjudicate)" % len(review))
    by = {}
    for f in blockers:
        by[f["check"]] = by.get(f["check"], 0) + 1
    for k, n in sorted(by.items(), key=lambda x: -x[1]):
        print("     BLOCKER %-16s %d" % (k, n))
    for f in blockers[:20]:
        print("  [%s] %s: %s" % (f["check"], f["video_id"], f["detail"][:106]))
    if len(blockers) > 20:
        print("  ... and %d more" % (len(blockers) - 20))
    # the skeptic's work queue: exactly the chapters deterministic code cannot settle.
    # --queue exists because the fixed name OVERWROTE the previous module's queue the moment the
    # fence repointed (an early module's queue survived only because it was archived first). The
    # queue is part of a module's audit record: archive it with the CSVs at every module boundary.
    qpath = os.path.join(base, args.queue or "_skeptic_queue.json")
    json.dump(review, open(qpath, "w", encoding="utf-8"), indent=1)
    print("  skeptic queue written: %s (%d item(s))" % (qpath, len(review)))
    if args.json:
        json.dump(findings, open(args.json, "w", encoding="utf-8"), indent=1)
    print("=" * 78)
    if blockers:
        print("TRUTH GATE: FAIL - do not push. %d provable defect(s)." % len(blockers))
    else:
        print("TRUTH GATE: PASS on everything deterministic code can prove.")
        print("            %d chapter(s) still need the model SKEPTIC to adjudicate (see queue)." % len(review))
    sys.exit(1 if blockers else 0)


if __name__ == "__main__":
    main()
