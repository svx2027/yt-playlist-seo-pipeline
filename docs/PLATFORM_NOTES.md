# Platform notes

Specific YouTube platform behaviors this project's design has to account for — not API
reference material, but the mechanics a competent engineer would reasonably get wrong on
first instinct, with the concrete damage of getting them wrong and where the doctrine is
enforced (or not yet enforced) in this repo's code.

## Chapters render asynchronously, and can render as something other than yours

**The instinct.** Push a chapter block, read your own file back, confirm it is syntactically
valid — `0:00` first, timestamps strictly ascending, gaps wide enough, no stray control
characters — and call the chapters done. This pipeline already goes a step further than
that: `core/verify_truth.py` proves each label is actually true of the audio at its own
timestamp, not just well-formed. A machine diff that confirms the live page text matches
what was approved closes the gap one step further still (designed, not yet landed in this
repo's `core/` — see "Where this leaves the code here today" below). Stack "well formed,"
"the label is true," and "the live text matches what was approved," and it's easy to assume
nothing is left to check.

**The one real fact.** None of that proves YouTube turned the text into a clickable chapter
list. Writing a chapter block into a description doesn't make chapter markers appear —
YouTube parses the description and builds its own markers from it asynchronously, on its own
schedule, decoupled from the write itself succeeding. A check run minutes, or even days,
after a push that finds no rendered markers cannot tell "hasn't rendered yet" from "will
never render" — those are different claims, and only a second check, later, tells them
apart. Worse, YouTube can generate its own chapter markers from a video's content
independently of a creator-supplied block, and its own guess can displace a correctly
formatted, successfully saved one.

**Concrete downstream damage.** Any report that counts "chapters built" or "chapters
shipped" from the pipeline's own proposed or approved files is counting an artifact, not an
outcome. A video can carry a perfect chapter block in its live description and show no
chapters to a viewer, or show a chapter list with different labels than the ones that were
written and reviewed, because the platform's own generated set won out. Every structural
check this project runs or designs — well-formedness, the truth gate, the live-text diff —
would still report a clean pass on a video where a student can't actually use the chapters
the report claims exist.

**Where this leaves the code here today.** `core/build_chapters.py`, `core/chapter_windows.py`,
and `core/verify_truth.py` build and validate the chapter *block* — the text this pipeline
proposes and checks for truthfulness. The outcome check — reading the rendered player state
back from YouTube to see which chapter set (if either) is actually live, and re-checking on a
delay instead of trusting a single negative read — is designed but hasn't landed in this
repo's `core/` yet; see [`docs/SAFETY_ARCHITECTURE.md`](SAFETY_ARCHITECTURE.md) for the full
live-vs-designed breakdown. Until it does, "the chapters are built and true" and "the chapters
are the ones a viewer sees" are two different claims, and only the first is something this
repo can currently prove.
