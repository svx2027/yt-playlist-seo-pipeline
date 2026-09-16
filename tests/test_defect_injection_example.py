"""
A runnable example of this project's core testing doctrine: a gate is only proven by watching it
FAIL on the exact defect it exists to catch, using a real incident, not an invented one.

The incident this pins (see core/verify_truth.py's module docstring): build_chapters samples a
long transcript, the model GUESSES where a topic starts, and snap() pins that guess to a real
caption cue nearby. That makes the chapter's TIMESTAMP real while its LABEL stays a lie. The
shipped case was a chapter titled "Multiplying by 11 and 12" sitting over a divide-by-5 segment.
label_drift() in core/verify_truth.py is the detector built to catch it: it checks whether a
label's distinctive words actually occur in the caption text AT the chapter's own timestamp, or
only elsewhere in the video.

This test builds a synthetic caption track (no real channel data, entirely fictional) that
reproduces that exact shape, and asserts label_drift() fires on the defective label and stays
silent on a correct one covering the same synthetic video. Run it directly to see the gate work:

    python3 -m unittest tests.test_defect_injection_example -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.verify_truth import label_drift, KIND_WHISPER


def build_synthetic_cues():
    """A fictional lesson with three well-separated segments: division near 0:20-0:40,
    multiplication by eleven and twelve near 5:00-5:40, subtraction near 10:00-10:20. Segments
    are placed far apart in time on purpose - label_drift() judges a 60s window around each
    timestamp, so segments closer together would overlap the window and mask the defect this
    test injects. No real channel, video, or transcript; entirely fictional content."""
    return [
        (0, "today we divide numbers by five and check the remainder"),
        (20, "dividing fifty by five gives ten with no remainder"),
        (40, "divide sixty by five and you get a clean result"),
        (300, "now let's multiply by eleven and twelve using the shortcut method"),
        (320, "multiplying eighty by eleven gives eight hundred eighty"),
        (340, "multiplying ninety by twelve gives one thousand and eighty"),
        (600, "next we practice subtracting three digit numbers"),
        (620, "subtract two hundred from five hundred to get three hundred"),
    ]


class DefectInjectionExample(unittest.TestCase):
    def setUp(self):
        self.cues = build_synthetic_cues()
        self.duration = 700

    def test_gate_fires_on_the_real_shipped_defect_shape(self):
        """The label describes the multiplication segment (5:00) but is stamped inside the
        division segment (0:20) - the exact shape of the incident this detector was built for:
        a guessed timestamp, snapped onto a real cue, with a label that belongs to a different
        moment in the video."""
        defective_label = "Multiplying Eleven and Twelve"
        defective_timestamp = 20   # belongs at 300, mis-stamped at 20

        verdict = label_drift(
            defective_label, self.cues, defective_timestamp,
            duration=self.duration, kind=KIND_WHISPER,
        )

        self.assertIsNotNone(
            verdict,
            "label_drift did not fire on the injected defect - the gate is not protecting "
            "against the incident it exists to catch",
        )
        self.assertIn("never occur at this timestamp", verdict)

    def test_gate_stays_silent_on_the_correct_label(self):
        """Same label, correct timestamp: this must NOT fire, or the gate is just noise that
        trains an operator to ignore it."""
        correct_label = "Multiplying Eleven and Twelve"
        correct_timestamp = 300

        verdict = label_drift(
            correct_label, self.cues, correct_timestamp,
            duration=self.duration, kind=KIND_WHISPER,
        )

        self.assertIsNone(
            verdict,
            "label_drift fired on a correctly-placed label - a detector that flags good labels "
            "gets switched off by an operator, per this project's own doctrine",
        )

    def test_gate_stays_silent_on_a_different_correct_label(self):
        """A second correct pairing from the same synthetic video, to show this isn't a
        coincidence of one lucky timestamp."""
        verdict = label_drift(
            "Subtracting Three Digit Numbers", self.cues, 600,
            duration=self.duration, kind=KIND_WHISPER,
        )
        self.assertIsNone(verdict)


if __name__ == "__main__":
    unittest.main()
