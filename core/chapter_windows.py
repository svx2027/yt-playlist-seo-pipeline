"""
chapter_windows.py  -  the windowed chapter engine. Channel-agnostic.

THE BUG THIS EXISTS TO FIX. build_chapters used to SAMPLE a long transcript (head + middle +
tail) before prompting. On a 20-minute video that is harmless. On the 3h02m module-3 video
(149,921 chars) the model never saw most of the timeline, so it inferred a plausible topic list
and GUESSED where each topic starts; snap() then pinned every guess to a REAL transcript cue.
That made the TIMESTAMPS real and the LABELS a lie, and the block validated clean: 7 of 12 labels
pointed at the wrong content ("Averages: Concept of Deviation" over a compound-interest segment).
It was not one video: 26 LIVE Phase A labels had the same defect.

THE FIX: never ask the model to place a topic in a timeline it cannot see. Cut the video into
windows, hand each window ONLY its own timestamped cues, and require BOTH the label and the start
time to come from the text of that window. The model physically cannot mislabel a window it is
reading. Every returned timestamp is then re-checked against the real cue list before it is
accepted, and a time outside its own window is REJECTED rather than snapped (a global snap is the
very mechanism that hid the original bug).

Promoted to core/ from a channel-specific rebuild_long_chapters.py on 16 Jul 2026 and made
channel-agnostic: the module name, creator, exam and intro-label fallback now come from config.
The old file hardcoded a specific creator name, a specific module name, and a fixed intro
label, which would have silently branded another channel's chapters with this one's identity.
"""
import re

import _gemini
import verify_truth as VT

SCHEMA_LABEL = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["RELABEL", "MOVE", "DROP", "SAME"]},
        "label": {"type": "string"},
        "move_to_seconds": {"type": "integer"},
        "evidence": {"type": "string"},
    },
    "required": ["action", "label", "evidence"],
}

SCHEMA = {
    "type": "object",
    "properties": {
        "start_seconds": {"type": "integer"},
        "label": {"type": "string"},
        "evidence": {"type": "string"},
    },
    "required": ["start_seconds", "label", "evidence"],
}

PROMPT = """This is ONE WINDOW of a long {subject} lesson{creator_clause}. {speech_note}

Below is the window's transcript with REAL timestamps in seconds. Read it and answer about THIS
WINDOW ONLY. You are not being asked about the rest of the video and you must not guess about it.

1. "start_seconds": the timestamp where the main topic of this window BEGINS. It MUST be one of
   the timestamps printed below, copied exactly. Prefer the moment a NEW question or concept
   starts. If the window is mid-way through a topic that began earlier, return the FIRST
   timestamp in the window.
2. "label": 3 to 8 words naming what is actually taught AT that timestamp. Specific and concrete
   (name the concept or the question type). NEVER the exam name, the exam year or the module name:
   no "{exam}", no "{exam} {year}", no "{module}". No filler like "Introduction" or "Continued".
   No em dashes, no en dashes.
3. "evidence": 5 to 15 words quoted from the transcript AT that timestamp, proving the label.

WINDOW {i} of {n}  (video runs {dur}s in total)
{body}
"""


def plan_windows(duration, cfg_ch):
    """How many windows for a video of this length. Kept small and explicit: a window is a
    Gemini call, and the count drives the module's cost line."""
    per = cfg_ch.get("window_seconds_per_window", 180)
    lo = cfg_ch.get("min_windows", 4)
    hi = cfg_ch.get("max_windows", 14)
    return max(lo, min(hi, round((duration or 0) / per)))


# DANGLING_START lives in verify_truth: one detector, one definition.


def polish(label):
    """Trim filler that pushes the real subject to the right. A chapter list is scanned, not read.

    ⚠️ THIS STRIP RE-CREATED A KNOWN DEFECT AND THE HYGIENE GATE CAUGHT IT (17 Jul 2026, module 4
    chunk 1). "Solving for x cube plus y cube" -> "For x cube plus y cube"; "Solving for common
    ratio in GP" -> "For common ratio in GP". That is EXACTLY what shipped live as "Averages 1: of
    AP Series": a prefix strip ate the head of the phrase and left a dangling preposition, and
    nothing checked that a label read like English. `retitle`'s clean_subtopic was fixed for this
    in module 3; this function was promoted out of rebuild_long_chapters.py still carrying it.
    A fix applied to one copy of a pattern is not applied to the pattern.
    So: strip ONLY when what remains still reads like a phrase; otherwise keep the mildly
    redundant original, which is always better than an ungrammatical one.
    """
    # NOTE the alternation order: 'an' MUST precede 'a', or "Solving an escalator" leaves an
    # orphaned "n escalator". Longest alternative first, always.
    # Strip the PREPOSITION WITH THE VERB ("Solving for x cube" -> "x cube"), not the verb alone,
    # which is what left the dangling "For x cube". Then, belt and braces, refuse any strip whose
    # remainder still starts with a function word.
    # ⚠️ "a"/"an" IS SOMETIMES THE ACTUAL VARIABLE NAME, NOT AN ARTICLE. "Solving for a When x
    # equals minus 3y" stripped to "When x equals minus 3y" (Stage 5 audit, 18 Jul 2026,
    # on a real video): the single letter "a" here IS the thing being solved for, and "When" starts a
    # new clause, not a noun the article modifies. A real article is always followed by a NOUN
    # PHRASE ("a quadratic", "an escalator"); it is never followed by a clause-starter like
    # when/if/where/given. The negative lookahead below is the same distinction a native reader
    # makes instantly and a bare regex cannot: only strip the article when what follows it is not
    # itself the start of a subordinate clause.
    # The lookahead must sit RIGHT AFTER the article word, with \s* INSIDE it -- putting \s*
    # OUTSIDE the lookahead let the regex engine backtrack to a zero-width match (matching just
    # "a", not "a "), so the lookahead inspected the wrong position and let "When" slip through
    # anyway. Anchoring \s* inside the lookahead means it always inspects the real next word.
    stripped = re.sub(
        r"^(?:Solving|Finding|Calculating)\s+(?:for\s+|of\s+)?"
        r"(?:\b(?:an|a|the)\b(?!\s*(?:when|if|where|given|such|whose)\b)\s*)?",
        "", label, flags=re.I).strip()
    if stripped and not VT.DANGLING_START.match(stripped):
        label = stripped
    label = re.sub(r"\s+problem$|\s+question$", "", label, flags=re.I)
    # The ASR consistently mis-hears the maths term "alligation" as "allegation" (a legal word).
    # It must never ship in a chapter label on a maths lesson.
    label = re.sub(r"\ballegation\b", "Alligation", label, flags=re.I)
    return (label[:1].upper() + label[1:]).strip()


# all_module_words now lives in verify_truth: it needs only csv+os, while THIS module
# imports _gemini, so half the gate scripts could not afford to import it and quietly fell
# back to the raw hand list. Re-exported here so every existing caller keeps working.
all_module_words = VT.all_module_words


def strip_module_words(label, module_words, exam, year):
    """Remove an exam/module word that has been TACKED ON to a label, never one that is part of
    the actual topic.

    ⚠️ THIS FUNCTION SHIPPED BROKEN ON 16 JUL AND A PRE-FLIGHT AUDIT CAUGHT IT THE SAME DAY.
    The first version stripped a module word ANYWHERE in the label, so with "Module 3" in
    config.module_words it turned "Sum of an Arithmetic Progression" into "Sum of an Progression":
    broken English in a live chapter label, which is exactly the same defect shape (one real video
    shipped "Averages 1: of AP Series" and sat live because nothing checked that a title read like
    English). Module 4 has 6 videos whose titles name a progression, including "Arithmetic
    Progression 1" and "Arithmetic Progression 2", so it would have fired on this channel.

    Worse, it DISAGREED WITH ITS OWN GATE: verify_truth.hygiene() already protects
    "Arithmetic Progression/Mean/Sequence/Series" via MODULE_WORD_EXCEPTIONS, so the validator
    was defending the exact term the writer was destroying. Both now read the SAME exception.
    """
    protected = VT.MODULE_WORD_EXCEPTIONS.search(label)
    if protected:
        # The module word is the real topic here ("Sum of an Arithmetic Progression"). The only
        # thing worth removing is an exam tag, which is never part of a maths term.
        for p in (r"\b%s\b\s*%s" % (re.escape(exam), re.escape(str(year))), r"\b%s\b" % re.escape(exam)):
            if exam:
                label = re.sub(r"\s*" + p, "", label, flags=re.I)
        return re.sub(r"\s{2,}", " ", label).strip(" :-,")
    pats = []
    if exam:
        pats += [r"\b%s\b\s*%s" % (re.escape(exam), re.escape(str(year))), r"\b%s\b" % re.escape(exam)]
    # Only strip a module word where it is TACKED ON: leading ("<Exam> Module 3: Cost Price") or
    # trailing ("Cost Price Module 3"). A module word in the MIDDLE is part of the topic.
    for w in (module_words or []):
        pats += [r"^\s*%s\b[\s:,-]*" % re.escape(w), r"[\s:,-]*\b%s\s*$" % re.escape(w)]
    for p in pats:
        label = re.sub(p, " ", label, flags=re.I)
    return re.sub(r"\s{2,}", " ", label).strip(" :-,")


def finalise(label, module_words, exam, year):
    """Clean a raw model label into its shipping form. THE ORDER IN HERE IS THE WHOLE POINT.

    ⚠️ THIS SHIPPED "Advanced algebra" INTO A CHAPTER BLOCK AND THE SWEEP CAUGHT IT (a real video,
    module 4 chunk 1, 17 Jul 2026). The model said "solving advanced algebra question". The pipeline
    ran strip_module_words FIRST, which correctly did NOTHING: "algebra" sat in the MIDDLE of the
    phrase, and a module word in the middle is part of the topic, not a tacked-on tag. THEN polish()
    removed the leading "solving" and the trailing "question"  -  and in doing so PROMOTED "algebra"
    to the trailing position, re-creating the exact condition strip_module_words exists to catch.
    The check was correct. Its verdict simply expired, because something rewrote the string
    afterwards and nobody re-asked.

    **A GATE'S VERDICT IS ONLY TRUE OF THE STRING IT SAW.** Any transformation that runs after a
    check invalidates that check. So the cleaner runs FIRST and the checker LAST, and the whole
    thing lives in one function so the order cannot be got wrong at a call site again.
    """
    label = polish(label)                                    # rewrite first
    label = strip_module_words(label, module_words, exam, year)   # then judge what remains
    return (label[:1].upper() + label[1:]).strip(" :-,") if label else label


def build_windowed(key, model, vid, cues, duration, cfg, log=print):
    """Chapter one video window by window. Returns (chapters, flags).

    cues: [(sec, text)] from the video's TRUSTED build source (resolve_timed_source).
    Every accepted timestamp is a real cue that exists INSIDE its own window.
    """
    cfg_ch = cfg.get("chapters") or {}
    ident = cfg.get("identity") or {}
    module_words = all_module_words(cfg)   # config list UNION every module_map topic
    exam = (ident.get("exam") or "").strip()
    year = re.sub(r"\D", "", (cfg.get("title_rule") or {}).get("suffix", "")) or ""
    subject = ident.get("exam_context") or exam or "teaching"
    creator_name = (ident.get("creator_variants") or [None])[0]
    creator_clause = " taught by %s" % creator_name if creator_name else ""
    speech_note = ident.get("speech_note") or "Speech may be mixed-language; technical terms are typically in English."

    n = plan_windows(duration, cfg_ch)
    step = max(1, (duration or 0) // n)
    real = {t for t, _ in cues}
    chapters, flags = [], []

    for i in range(n):
        lo = i * step
        hi = (i + 1) * step if i < n - 1 else (duration or 0) + 1
        win = [(t, x) for t, x in cues if lo <= t < hi]
        if not win:
            continue
        body = "\n".join("[%ds] %s" % (t, x) for t, x in win)[:42000]
        prompt = PROMPT.format(subject=subject, creator_clause=creator_clause,
                               speech_note=speech_note, exam=exam or "the exam",
                               year=year or "", module=", ".join(module_words[:3]) or "the module",
                               i=i + 1, n=n, dur=duration, body=body)
        try:
            text, _ = _gemini.generate(key, model, prompt, json_mode=True, schema=SCHEMA,
                                       temperature=0.2, max_output_tokens=400, thinking_budget=0)
            d = _gemini.parse_json(text)
        except Exception as e:
            flags.append("window %d failed: %s" % (i + 1, str(e)[:70]))
            log("    win %2d: GEN_FAILED %s" % (i + 1, str(e)[:60]))
            continue

        # THE MODEL DOES NOT ALWAYS RETURN THE SHAPE THE SCHEMA DEMANDS, AND THAT KILLED A WHOLE
        # CHUNK. On one real video (17 Jul 2026) Gemini returned a JSON LIST where the schema requires
        # an object; `d.get(...)` raised AttributeError from OUTSIDE the try above, which only
        # guards the API call and the parse, not the SHAPE of what came back. Build 5 of chunk 5
        # died at video 15 and videos 16-18 were never attempted at all. A structured-output schema
        # is a REQUEST, not a guarantee: validate the shape before you index it.
        if isinstance(d, list):
            # a single-element wrapper is the model over-nesting its own answer: unwrap it.
            # anything else is genuinely ambiguous, and guessing which element is meant is how a
            # wrong label gets a real timestamp. Reject the window and say so.
            if len(d) == 1 and isinstance(d[0], dict):
                d = d[0]
            else:
                flags.append("window %d rejected: model returned a %d-item list, not one object"
                             % (i + 1, len(d)))
                log("    win %2d: REJECTED (returned a list of %d, not one object)" % (i + 1, len(d)))
                continue
        if not isinstance(d, dict):
            flags.append("window %d rejected: model returned %s, not an object" % (i + 1, type(d).__name__))
            continue
        sec = int(d.get("start_seconds", -1))
        label = re.sub(r"\s+", " ", (d.get("label") or "")).strip(" .:-")
        label = re.sub(r"[—–]", " ", label)
        if not label:
            continue
        # HARD CHECK: the model may only return a cue that really exists, inside THIS window.
        # A near-miss is tolerated to 3s (cue boundary rounding); anything else is rejected
        # outright. We never snap a wild guess to "the nearest real cue": that is exactly the
        # mechanism that made 26 live timestamps real and 26 live labels false.
        if sec not in real:
            near = min(real, key=lambda t: abs(t - sec)) if real else None
            if near is None or abs(near - sec) > 3 or not (lo <= near < hi):
                flags.append("window %d rejected: %ss is not a real cue in its own window" % (i + 1, sec))
                log("    win %2d: REJECTED %ds (not a real cue in this window)" % (i + 1, sec))
                continue
            sec = near
        if not (lo <= sec < hi):
            flags.append("window %d rejected: %ss falls outside its own window" % (i + 1, sec))
            log("    win %2d: REJECTED %ds (outside its own window)" % (i + 1, sec))
            continue
        label = finalise(label, module_words, exam, year)
        if not label:
            continue
        chapters.append((sec, label))
        log("    win %2d  %s  %-44s | %s" % (i + 1, _hms(sec), label[:44], (d.get("evidence") or "")[:40]))

    chapters.sort()
    return chapters, flags


REPAIR_PROMPT = """This is ONE SEGMENT of a {subject} lesson{creator_clause}. {speech_note}

A chapter label for this segment was rejected. {reason}

THE ONE RULE THAT MATTERS: a chapter label must be true AT ITS FIRST SECOND, because that is where
the student LANDS when they click it. It is NOT enough for the label to describe the segment's
biggest topic. This exact mistake was measured on this module: a chapter at 21:49 was renamed to
"Value of x cube minus y cube" because that IS the segment's main topic  -  but that topic starts at
23:09, so a student clicking it landed 80 seconds early, in the middle of the previous question's
arithmetic, and got the answer to something else. The old label was true and duplicated; the new one
was distinct and false. That is a worse trade, not a better one.

So choose ONE action:

1. "RELABEL"  -  only if a TRUE and DISTINCT label exists for what is being taught in the FIRST
   SECONDS of this segment (the LANDING ZONE printed below). Your evidence MUST be quoted from the
   LANDING ZONE, not from later in the segment. If your evidence comes from later, this is not a
   relabel: it is a MOVE.
2. "MOVE"  -  if the distinct topic really starts LATER in this segment, return move_to_seconds:
   the timestamp where it actually begins, copied exactly from the segment text below, and a label
   true AT THAT timestamp with evidence from there. This is usually the right answer when the
   segment covers the tail of one question and then a new one.
3. "DROP"  -  if this segment has no honest distinct topic start of its own (it is the middle of
   work already covered by the previous chapter). Dropping a chapter is a CORRECT answer. A missing
   chapter costs a student one scroll; a false one sends them to the wrong place.
4. "SAME"  -  if it genuinely teaches the same thing the same way as its twin and no honest
   distinction exists. The duplicate is then KEPT and disclosed. Never invent a difference.

LABEL RULES: 3 to 8 words, complete phrase, never ending on a preposition. Name the substance, not
a verb: "Calculate x plus 1 by x" is the SAME label as "X plus 1 by x" with a hat on. NEVER the
exam name "{exam}", the module "{module}", or filler. No em dashes, no en dashes.

LABELS ALREADY IN USE ELSEWHERE IN THIS VIDEO. Your answer must not repeat any of these, and must
not be one of these with a verb added or removed:
{siblings}

=== LANDING ZONE: the first {land}s from {a}, where a student who clicks this chapter arrives ===
{landing}

=== THE FULL SEGMENT ({a} to {b} of a {dur}s video) ===
{body}
"""


def _norm(t):
    return re.sub(r"[^a-z0-9 ]", " ", (t or "").lower())


def repair_labels(key, model, chapters, cues, duration, cfg, log=print):
    """Re-read the segments whose labels came out TRUNCATED or IDENTICAL to a sibling.

    ⚠️ BOTH DEFECTS ARE MADE BY THIS FILE'S OWN DESIGN, and only real data showed them (module 4
    chunk 1, 17 Jul 2026: 1 truncated, 4 videos with a duplicate pair, out of 127 labels).
    Each window is labelled in ISOLATION, which is precisely what stops the model inventing a
    timeline it cannot see  -  we are not giving that up. The cost of that isolation is that a
    concept the teacher returns to later gets read honestly and identically twice ("x plus 1 by x"
    at 14:30 and 23:39). Both labels are TRUE and the pair is still useless: uniqueness is
    not distinguishability.

    So the repair is NARROW: show the model ONE segment and ask what makes THIS one different,
    still requiring quoted evidence from that segment. It may answer "SAME", and when it does we
    KEEP the honest duplicate and flag it, because inventing a difference between two genuinely
    identical explanations is fabrication, and fabrication is the one thing this pipeline exists
    to prevent. A duplicate label is a wart; a made-up one is a lie.
    """
    if not chapters:
        return chapters, []
    cfg_ch = cfg.get("chapters") or {}
    ident = cfg.get("identity") or {}
    module_words = all_module_words(cfg)   # config list UNION every module_map topic
    exam = (ident.get("exam") or "").strip()
    year = re.sub(r"\D", "", (cfg.get("title_rule") or {}).get("suffix", "")) or ""
    subject = ident.get("exam_context") or exam or "teaching"
    creator_name = (ident.get("creator_variants") or [None])[0]
    creator_clause = " taught by %s" % creator_name if creator_name else ""
    speech_note = ident.get("speech_note") or "Speech may be mixed-language; technical terms are typically in English."

    dupes = VT.duplicate_labels(chapters)
    todo = {}
    for i, (t, lab) in enumerate(chapters):
        if VT.truncated_label(lab):
            todo[i] = "It stops mid-phrase (%r) and never finishes the thought." % lab
        elif len(lab.split()) < 2:
            # "solving advanced algebra question" cleans down to the single word "Advanced", which
            # is formally clean and navigationally useless. Stripping can leave a husk; re-read it.
            todo[i] = ("It is a single generic word (%r) after cleaning, which tells a student "
                       "nothing about what is taught here." % lab)
        else:
            # VT.label_key, NEVER a second inline copy of the normalisation. This line WAS a
            # hand-rolled `re.sub(r"[^a-z0-9 ]", "", lab.lower())` while duplicate_labels had moved
            # on to the verb-stripped key, so "Solve for x plus 1 by x" hashed to
            # "solve for x plus 1 by x", missed the dupes dict keyed "x plus 1 by x", and was
            # never sent for repair: the detector saw 3 collisions and the repair fixed 1.
            # Two copies of one rule that disagree = the rule is decoration. One function.
            k = VT.label_key(lab)
            # the FIRST use of a colliding label keeps it; only the later ones are re-read
            if k in dupes and t != dupes[k][0]:
                todo[i] = ("The label %r is ALREADY used at %s in this same video, so a student "
                           "cannot tell the two apart." % (lab, _hms(dupes[k][0])))
    if not todo:
        return chapters, []

    out, flags = list(chapters), []
    for i, reason in sorted(todo.items()):
        a = out[i][0]
        b = out[i + 1][0] if i + 1 < len(out) else (duration or a + 600)
        seg = [(t, x) for t, x in cues if a <= t < b]
        if not seg:
            flags.append("label repair skipped at %s: no cues in its own segment" % _hms(a))
            continue
        LAND = 30      # seconds of "where the student actually lands"
        landing = [(t, x) for t, x in seg if t < a + LAND]
        body = "\n".join("[%ds] %s" % (t, x) for t, x in seg)[:30000]
        land_txt = "\n".join("[%ds] %s" % (t, x) for t, x in landing)[:4000]
        land_words = _norm(" ".join(x for _, x in landing))

        def _siblings():
            return "\n".join("  %s  %s" % (_hms(t2), lab2)
                              for j, (t2, lab2) in enumerate(out) if j != i) or "  (none)"

        new, d, why_retry, act, moved = "", {}, None, "", None
        for attempt in (1, 2):
            rsn = reason if attempt == 1 else (
                reason + " Your previous answer %r was rejected because %s. Try again; if no true "
                "distinct label exists AT THE FIRST SECOND, answer MOVE or DROP instead of forcing "
                "a name onto it." % (new, why_retry))
            prompt = REPAIR_PROMPT.format(subject=subject, creator_clause=creator_clause,
                                          speech_note=speech_note, reason=rsn,
                                          exam=exam or "the exam",
                                          module=", ".join(module_words[:3]) or "the module",
                                          siblings=_siblings(), land=LAND, landing=land_txt,
                                          a=_hms(a), b=_hms(b), dur=duration, body=body)
            try:
                text, _ = _gemini.generate(key, model, prompt, json_mode=True, schema=SCHEMA_LABEL,
                                           temperature=0.2 if attempt == 1 else 0.5,
                                           max_output_tokens=800, thinking_budget=0)
                d = _gemini.parse_json(text)
            except Exception as e:
                flags.append("label repair at %s FAILED: %s" % (_hms(a), str(e)[:60]))
                d = {}
                break
            if isinstance(d, list):
                d = d[0] if len(d) == 1 and isinstance(d[0], dict) else {}
            if not isinstance(d, dict):
                d = {}
                break
            act = str(d.get("action") or "").upper()
            cand = re.sub(r"\s+", " ", str(d.get("label") or "")).strip(" .:-")
            cand = re.sub(r"[—–]", " ", cand)
            ev = str(d.get("evidence") or "")

            if act in ("SAME", "DROP") or not cand:
                new = cand
                break

            cand_f = finalise(cand, module_words, exam, year)
            why_retry = None
            if not cand_f:
                why_retry = "it cleaned away to nothing"
            elif VT.truncated_label(cand_f):
                why_retry = "it stopped mid-phrase again"
            elif VT.hygiene(cand_f, module_words, exam):
                why_retry = "; ".join(VT.hygiene(cand_f, module_words, exam))
            elif any(VT.label_key(cand_f) == VT.label_key(l2) for j, (t2, l2) in enumerate(out) if j != i):
                why_retry = "it still matches a sibling label once filler verbs are discounted"
            elif act == "MOVE":
                mv = int(d.get("move_to_seconds", -1))
                # ⚠️ `real` DID NOT EXIST IN THIS SCOPE AND IT CRASHED A LIVE BUILD (a real video,
                # module 5, 21 Jul 2026: "NameError: name 'real' is not defined"). It is a
                # local of build_windowed(); this function only receives `cues`. The branch fires
                # only when the model answers MOVE, which is rare, so the name sat wrong through
                # an entire module and was never executed. A per-video guard is why the cost
                # was one video instead of a dead chunk: the other 21 finished and this one was
                # recorded as CHAPTER_BUILD_CRASHED rather than silently dropped.
                real_cues = {t for t, _ in cues}
                if mv not in real_cues:
                    why_retry = "move_to_seconds %ds is not a real cue" % mv
                elif not (a < mv < b):
                    why_retry = "move_to_seconds %ds is outside this segment" % mv
                else:
                    moved = mv
            else:
                # ⚠️ THE CHECK THAT WOULD HAVE CAUGHT A REAL 21:49 MISLABEL. A RELABEL claims the label
                # is true at the chapter's FIRST SECOND. The model's word is not evidence for that,
                # and it has already been wrong: it renamed a chapter to the segment's dominant
                # topic, which began 80 seconds later, and the label read perfectly. So the quoted
                # evidence must ACTUALLY APPEAR in the landing zone's cues. This is deterministic:
                # we hold the cues, so we can check the quote rather than trust it.
                ev_words = [w for w in _norm(ev).split() if len(w) > 3]
                hit = sum(1 for w in ev_words if w in land_words)
                if ev_words and hit < max(2, len(ev_words) // 3):
                    why_retry = ("its evidence %r is not in the first %ds of the segment, so the "
                                 "label is about something taught LATER: that is a MOVE, not a "
                                 "RELABEL" % (ev[:50], LAND))
            new = cand
            if not why_retry:
                break
        if not d:
            continue

        if act == "DROP":
            flags.append("chapter at %s DROPPED by the repair pass: it has no honest distinct topic "
                         "start of its own (%s). A missing chapter costs a scroll; a false one sends "
                         "the student to the wrong place." % (_hms(a), reason.split(".")[0]))
            log("    repair %s  DROPPED (no honest distinct start)" % _hms(a))
            out[i] = None
            continue
        if not new or act == "SAME":
            flags.append("label repair at %s: the model read the segment and reports it teaches "
                         "the same thing as its twin, so the honest duplicate label %r is KEPT "
                         "rather than a difference invented (evidence: %r)"
                         % (_hms(a), out[i][1], (d.get("evidence") or "")[:70]))
            continue
        new = finalise(new, module_words, exam, year)
        why = why_retry
        if why:
            flags.append("label repair at %s rejected its own answer %r (%s); the honest original "
                         "%r is KEPT rather than trading one defect for another"
                         % (_hms(a), new, why, out[i][1]))
            log("    repair %s  REJECTED %r (%s)" % (_hms(a), new[:32], why[:44]))
            continue
        if act == "MOVE" and moved is not None:
            prev = out[i - 1][0] if i > 0 and out[i - 1] else None
            nxt = out[i + 1][0] if i + 1 < len(out) and out[i + 1] else None
            if (prev is not None and moved - prev < 75) or (nxt is not None and nxt - moved < 75):
                flags.append("label repair at %s wanted to MOVE the chapter to %s (where its topic "
                             "really starts) but that breaks the 75s spacing, so the chapter is "
                             "DROPPED rather than left pointing at the wrong content"
                             % (_hms(a), _hms(moved)))
                log("    repair %s  DROPPED (move to %s breaks spacing)" % (_hms(a), _hms(moved)))
                out[i] = None
                continue
            flags.append("chapter MOVED from %s to %s and labelled %r: its distinct topic does not "
                         "start until %s, and a chapter must be true at the second the student "
                         "LANDS on it, not merely somewhere inside its span"
                         % (_hms(a), _hms(moved), new, _hms(moved)))
            log("    repair %s  MOVED -> %s  %r" % (_hms(a), _hms(moved), new[:34]))
            out[i] = (moved, new)
            continue
        flags.append("label at %s repaired from %r to %r (%s)"
                     % (_hms(a), out[i][1], new, reason.split(".")[0]))
        log("    repair %s  %r -> %r" % (_hms(a), out[i][1][:34], new))
        out[i] = (a, new)
    return [c for c in out if c], flags


def ensure_zero_chapter(chapters, cues, cfg, duration=None):
    """YouTube ignores the WHOLE list unless the first stamp is 0:00. Do NOT drag the first
    real topic back to 0:00: on the 3h video the first question starts at 1:25 and 0:00 is the
    intro, so moving it would mislabel BOTH. Give 0:00 its own honest label instead, derived from
    what is actually said at 0:00, with a config fallback."""
    if chapters and chapters[0][0] == 0:
        return chapters, []
    cfg_ch = cfg.get("chapters") or {}
    fallback = cfg_ch.get("intro_label_fallback") or "What This Session Covers"
    opening = " ".join(x for t, x in cues if t < 90)[:400]
    # IF 0:00 IS A SALES PITCH, THE GENERIC FALLBACK WOULD BE A LIE. "What This Session Covers"
    # over "there is an offer going on / 20 percent off on subscription" claims teaching where
    # there is an advert, and a false label is worse than no label. Use the config's honest promo
    # intro instead, which is also what earns the 0:00 promo exemption (VT.zero_promo_exempt):
    # the student sees what 0:00 really is and can skip to the teaching.
    if VT.opens_in_promo(cues, 0):
        fallback = cfg_ch.get("intro_label_promo_fallback") or "Course Offer and Session Plan"
    return [(0, fallback)] + chapters, [
        "0:00 chapter added with the config fallback label %r (the first real topic starts at %s "
        "and moving it back would mislabel it). Transcript at 0:00: %r"
        % (fallback, _hms(chapters[0][0]) if chapters else "n/a", opening[:90])]


def _hms(sec):
    return "%d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)
