# Defect injection: proving a gate by watching it fail

A gate that has never been watched fail on the exact defect it exists to catch is not a gate,
it is a guess. `tests/test_defect_injection_example.py` is a small, self-contained, runnable
example of that discipline applied to `core/verify_truth.py`.

## The incident behind it

`core/verify_truth.py`'s module docstring records the real shape this project shipped once, on
an early module, before this check existed: `build_chapters.py` samples a long transcript, the
model *guesses* where a topic starts, and a later step snaps that guess onto the nearest real
caption cue. That makes the chapter's **timestamp** real while its **label stays a guess** - a
structurally perfect, semantically wrong artifact that every shape-only validator (correct
count, ascending order, minimum gaps) waves through, because none of those checks ever ask
whether the label is *true* of the audio at its own timestamp. The recorded case: a chapter
titled "Multiplying by 11 and 12" sitting over a divide-by-5 segment.

`label_drift()` is the detector built for exactly that shape: does the label's distinctive
vocabulary actually occur in the caption text at the chapter's own timestamp, or only
elsewhere in the video?

## What the test does

`tests/test_defect_injection_example.py` builds a short, entirely fictional caption track (no
real channel, video, or transcript - three unrelated arithmetic segments placed well apart in
time) and then:

1. **Injects the real defect shape**: labels the multiplication segment's content but stamps it
   at the division segment's timestamp. Asserts `label_drift()` fires.
2. **Confirms it stays quiet on a correct label** at its correct timestamp, twice, with two
   different label/timestamp pairs from the same synthetic video. A detector that also flags
   good labels is worse than no detector: it trains an operator to stop reading its output.

Run it directly:

```
python3 -m unittest tests.test_defect_injection_example -v
```

## Why the timestamps are spread out

`label_drift()` judges roughly a 60-second window around a chapter's own timestamp. The first
draft of this test placed all three synthetic segments 20 seconds apart, and the injected
defect never fired: the "wrong" timestamp's 60-second window still overlapped the real
segment's caption text, so the detector correctly found the label's words nearby and stayed
silent — which is not a bug in the detector, it is the test failing to reproduce the incident's
actual shape. Watching that false negative and fixing the fixture (segments minutes apart
instead of seconds) is itself a small instance of this project's wider rule: a check is only as
good as the fixture used to exercise it, and a synthetic fixture that never fails on the
originally-injected defect is not proof, it is a check that has not actually been watched fail
yet.

## Extending this pattern

The same shape works for the other deterministic checks in `verify_truth.py`
(`opens_in_promo`, `source_trust`, `hygiene`, the duplicate/near-duplicate label detectors):
build a small synthetic input that reproduces the real incident's shape, assert the detector
fires on the defect and stays silent on the correct case, and keep the fixture next to the test
that exercises it rather than only in prose.
