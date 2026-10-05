"""Tests for video_digest's Whisper-vs-captions number cross-check.

Run:  python3 plugins/video-digest/tests/test_compare.py     (stdlib unittest; no network, no whisper-cli)

The fixtures are fictional passages built to reproduce the cases that used to give false
numeric-critical flags: a date written "March 16, 2027" (the old extractor read 16.2027), "By the
mid-20th century" at a window edge where the platform captions wrote "midentth", and a Spanish year
said twice ("1996... año 96", which the old extractor joined into 19961996). The pre-fix extractor
is copied below (old_canon_nums) so each fixture is shown to reproduce the original artifact.

Optional: VIDEO_DIGEST_REPLAY=<digest folder> also replays a real digest of yours and checks that
changed numbers are caught (at least 95 % of them).
"""
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import video_digest as vd  # noqa: E402

WINDOW, THRESH = 15, 0.72
REPLAY = Path(os.environ.get("VIDEO_DIGEST_REPLAY", "") or "/nonexistent").expanduser()


def old_canon_nums(s):
    """The extractor before the fix (whitespace and separators joined runs)."""
    out = []
    for m in re.finditer(r"\d[\d.,\s]*\d|\d", s):
        tok = re.sub(r"\s", "", m.group(0))
        tok = re.sub(r"[.,](?=\d{3}(?:\D|$))", "", tok)
        tok = tok.replace(",", ".")
        out.append(tok.rstrip("."))
    return out


def row_at(rows, t):
    hit = [r for r in rows if r["t"] == t]
    assert hit, f"no row at {t}s"
    return hit[0]


# --- fictional passages, timed like real Whisper segments and caption cues (start s, end s, text) ---
W_0130 = [
    (75.58, 80.24, "and winters are long. You might wonder, who would ever think of building anything here in the first"),
    (80.24, 86.2, "place? But people did. Modern settlements in towns only began to emerge here in the second half of the"),
    (86.2, 92.5, "19th century. The main reason was a fishing boom. Fueled primarily by herring and cod, the discovery of"),
    (92.5, 98.42, "rich fishing grounds in Alder and Brook bays quickly drew sailors, workers, and investors to the"),
    (98.42, 103.98, "area. Then, in the late 1800s, the arrival of the railroad connected Westmark to the rest of the country,"),
    (103.98, 109.8, "making it much easier to export timber and seafood products. By the mid-20th century,"),
    (109.96, 114.94, "widespread electric heating and the growth of major shipping industries became key drivers of"),
    (114.94, 120.92, "Westmark's population boom. But despite the growing population, the geographic reality never changed."),
    (121.16, 127.96, "Much of Westmark is still a cold coast. And somehow, in this deeply inhospitable environment,"),
]
C_0130 = [
    (72.24, 74.469, "fjords. Naturally, the climate here is"),
    (74.479, 77.429, "cold and wet and winters are long. You"),
    (77.439, 78.95, "might wonder who would ever think of"),
    (78.96, 80.39, "building anything here in the first"),
    (80.4, 82.55, "place, but people did. Modern"),
    (82.56, 84.469, "settlements and towns only began to"),
    (84.479, 86.31, "emerge here in the second half of the"),
    (86.32, 88.55, "19th century. The main reason was a"),
    (88.56, 91.03, "fishing boom fueled primarily by herring"),
    (91.04, 92.87, "and cod. The discovery of rich"),
    (92.88, 95.109, "fishing grounds in Elder and Brooke"),
    (95.119, 97.35, "bays quickly drew sailors, workers,"),
    (97.36, 99.51, "and investors to the area. Then in the"),
    (99.52, 101.99, "late 1800s, the arrival of the railroad"),
    (102.0, 103.749, "connected Westmark to the rest of the"),
    (103.759, 106.23, "country, making it much easier to export"),
    (106.24, 108.71, "timber and seafood products. By"),
    (108.72, 110.71, "the midentth century, widespread electric"),
    (110.72, 112.71, "heating and the growth of major"),
    (112.72, 114.95, "shipping industries became key drivers of"),
    (114.96, 117.27, "Westmark's population boom. But despite"),
    (117.28, 119.429, "the growing population, the geographic"),
    (119.439, 122.149, "reality never changed. Much of Westmark"),
    (122.159, 124.95, "is still a cold coast."),
    (124.96, 127.59, "And somehow in this deeply inhospitable"),
]
W_1630 = [
    (974.2, 980.2, "dozens of official permits across both counties and ports, meaning the entire North Pier rebuild"),
    (980.2, 985.8, "plan hinges directly on political relations between the two councils. And the single most important"),
    (985.8, 990.28, "thing to keep in mind, right now, this project hasn't been approved for construction."),
    (990.28, 997.0, "On March 16, 2027, Westmark's planning board only approved funding for the next phase of study."),
    (997.0, 1001.88, "Engineers, economists, and legal experts have now been tasked with taking a closer look at the"),
    (1001.88, 1007.88, "project's technical, financial, environmental, and organizational aspects. The project is still at"),
    (1007.88, 1013.88, "the preliminary assessment stage, and actual construction on the southern coast remains a long way off."),
]
C_1630 = [
    (970.88, 973.35, "local politics and red tape. The"),
    (973.36, 975.35, "project requires dozens of official"),
    (975.36, 977.99, "permits across both counties and ports,"),
    (978.0, 980.31, "meaning the entire North Pier rebuild"),
    (980.32, 982.389, "plan hinges directly on political"),
    (982.399, 984.629, "relations between the two councils. And"),
    (984.639, 986.47, "the single most important thing to keep"),
    (986.48, 988.87, "in mind right now, this project hasn't"),
    (988.88, 990.71, "been approved for construction. On"),
    (990.72, 993.59, "March 16th, 2027, Westmark's planning"),
    (993.6, 995.59, "board only approved funding for the"),
    (995.6, 997.749, "next phase of study. Engineers,"),
    (997.759, 999.829, "economists, and legal experts have now"),
    (999.839, 1001.829, "been tasked with taking a closer look at"),
    (1001.839, 1004.15, "the project's technical, financial,"),
    (1004.16, 1006.069, "environmental, and organizational"),
    (1006.079, 1008.15, "aspects. The project is still at the"),
    (1008.16, 1010.71, "preliminary assessment stage, and actual"),
    (1010.72, 1012.47, "construction on the southern coast"),
    (1012.48, 1014.87, "remains a long way off. Nevertheless,"),
]

# A long passage both transcripts agree on, for windows where one number differs.
FILLER_TEXT = ("Engineers, economists, and legal experts have now been tasked with taking "
               "a closer look at the project's technical, financial, environmental, and "
               "organizational aspects before any construction can begin on the coast.")


class Tokenizer(unittest.TestCase):
    def test_comma_space_separates_numbers(self):
        self.assertEqual(vd.canon_nums("On March 16, 2027, Westmark"), ["16", "2027"])
        self.assertEqual(vd.canon_nums("in 1996, 1997 and 1998."), ["1996", "1997", "1998"])

    def test_whitespace_never_joins(self):
        self.assertEqual(vd.canon_nums("1996 1996"), ["1996", "1996"])
        self.assertEqual(vd.canon_nums("en 1996 y en el 1996"), ["1996", "1996"])

    def test_suffix_letters_dropped(self):
        self.assertEqual(vd.canon_nums("the mid-20th century, late 1800s, 21st, 1º, 4K"),
                         ["20", "1800", "21", "1", "4"])

    def test_thousands_and_decimals(self):
        cases = {"56.000": "56000", "56,000": "56000", "1,500,000": "1500000",
                 "1.234.567": "1234567", "7.2": "7.2", "7,2": "7.2", "7.20": "7.2",
                 "1.234,5": "1234.5", "1,234.5": "1234.5", "0.5": "0.5", "3.0": "3",
                 "007": "7", "16.2026": "16.2026"}
        for text, want in cases.items():
            self.assertEqual(vd.canon_nums(text), [want], text)

    def test_dotted_sequences_split(self):
        self.assertEqual(vd.canon_nums("16.09.2026"), ["16", "9", "2026"])
        self.assertEqual(vd.canon_nums("10.0.0.12"), ["10", "0", "0", "12"])

    def test_brackets_ignored(self):
        self.assertEqual(vd.canon_nums("[música] 300 (2 applause)"), ["300"])


class RealCases(unittest.TestCase):
    """The false numeric-critical flags this fix removes."""

    def test_date_comma_16_2027(self):
        w990 = " ".join(s[2] for s in W_1630 if 990 <= s[0] < 1005)
        self.assertIn("16.2027", old_canon_nums(vd.norm(w990)))       # the old artifact
        rows = vd.compare(W_1630, C_1630, WINDOW, THRESH)
        r = row_at(rows, 990)
        self.assertEqual(r["nums_whisper"], ["16", "2027"])
        self.assertNotEqual(r["level"], "CRITICAL-NUMBERS")
        self.assertEqual(r["num_conflicts"], [])
        self.assertEqual(r["num_one_sided"], [])
        self.assertFalse([x for x in rows if x["level"] == "CRITICAL-NUMBERS"])

    def test_twentieth_century_at_window_edge(self):
        rows = vd.compare(W_0130, C_0130, WINDOW, THRESH)
        r = row_at(rows, 90)
        self.assertTrue(r["whisper"].endswith("By the mid-20th century,"))  # at the edge
        self.assertNotEqual(r["level"], "CRITICAL-NUMBERS")
        self.assertEqual(r["num_conflicts"], [])
        # The captions really have no number there ("midentth"): listed, not critical.
        self.assertEqual([(e["source"], e["n"]) for e in r["num_one_sided"]], [("whisper", "20")])
        self.assertIn("midentth", r["num_one_sided"][0]["other"])
        self.assertFalse([x for x in rows if x["level"] == "CRITICAL-NUMBERS"])

    def test_edge_number_in_neighbouring_caption_window(self):
        # Same edge, clean captions: the number sits in the next caption window only.
        caps = [s if "midentth" not in s[2] else (s[0], s[1], "the mid-20th century, widespread electric")
                for s in C_0130]
        rows = vd.compare(W_0130, caps, WINDOW, THRESH)
        r = row_at(rows, 90)
        self.assertEqual(r["level"], "OK")
        self.assertEqual(r["num_one_sided"], [])
        self.assertTrue(all(not x["num_one_sided"] and not x["num_conflicts"] for x in rows))

    def test_spanish_year_concatenation_1996(self):
        whisper = [(0.0, 7.0, "El contrato se firmó en 1996… en el año 96 exactamente, y la obra empezó después.")]
        captions = [(0.0, 3.5, "el contrato se firmó en 1996 y en el 1996"),
                    (3.5, 7.0, "exactamente y la obra empezó después")]
        joined = " ".join(c[2] for c in captions)
        self.assertIn("19961996", old_canon_nums(vd.norm(joined)))     # the old artifact
        r = row_at(vd.compare(whisper, captions, WINDOW, THRESH), 0)
        self.assertNotEqual(r["level"], "CRITICAL-NUMBERS")
        self.assertEqual(r["num_conflicts"], [])
        self.assertEqual(r["num_one_sided"], [])


class TruePositives(unittest.TestCase):
    """Real disagreements must still be CRITICAL."""

    def test_420_vs_240(self):
        rows = vd.compare([(0.0, 6.0, "The reservoir holds 420 billion gallons of water.")],
                          [(0.0, 6.0, "the reservoir holds 240 billion gallons of water")],
                          WINDOW, THRESH)
        r = row_at(rows, 0)
        self.assertEqual(r["level"], "CRITICAL-NUMBERS")
        self.assertEqual(r["num_conflicts"][0]["whisper"], ["420"])
        self.assertEqual(r["num_conflicts"][0]["captions"], ["240"])
        self.assertEqual(r["num_mismatch"], ["420"])
        self.assertIn("420 billion", r["num_conflicts"][0]["whisper_context"])

    def test_420_vs_240_in_long_matching_window(self):
        # Text similarity >= 0.98: the old comparator's ratio gate hid this one.
        w = f"{FILLER_TEXT} The reservoir holds 420 billion gallons of water. {FILLER_TEXT}"
        c = f"{FILLER_TEXT} The reservoir holds 240 billion gallons of water. {FILLER_TEXT}"
        r = row_at(vd.compare([(0.0, 14.0, w)], [(0.0, 14.0, c)], WINDOW, THRESH), 0)
        self.assertGreaterEqual(r["ratio"], 0.98)
        self.assertEqual(r["level"], "CRITICAL-NUMBERS")

    def test_conflict_across_window_edge(self):
        # Whisper's segment starts in window 0, the caption cue with the number in window 1.
        whisper = [(0.0, 9.0, FILLER_TEXT), (9.0, 16.5, "and the reservoir holds 420"),
                   (16.5, 24.0, "billion gallons of water for the state.")]
        captions = [(0.0, 9.0, FILLER_TEXT), (9.0, 14.9, "and the reservoir"),
                    (15.1, 18.0, "holds 240 billion gallons"), (18.0, 24.0, "of water for the state.")]
        rows = vd.compare(whisper, captions, WINDOW, THRESH)
        crit = [x for x in rows if x["level"] == "CRITICAL-NUMBERS"]
        self.assertEqual(len(crit), 1)
        self.assertEqual(crit[0]["num_conflicts"][0]["whisper"], ["420"])
        self.assertEqual(crit[0]["num_conflicts"][0]["captions"], ["240"])

    def test_spanish_decimal_vs_integer(self):
        r = row_at(vd.compare([(0.0, 5.0, "El puente mide 2,96 metros de ancho.")],
                              [(0.0, 5.0, "el puente mide 296 metros de ancho")],
                              WINDOW, THRESH), 0)
        self.assertEqual(r["level"], "CRITICAL-NUMBERS")


class Equivalences(unittest.TestCase):
    """Formatting differences that are not disagreements."""

    def one(self, whisper, captions):
        return row_at(vd.compare([(0.0, 6.0, whisper)], [(0.0, 6.0, captions)], WINDOW, THRESH), 0)

    def assertClean(self, r):
        self.assertEqual((r["level"], r["num_conflicts"], r["num_one_sided"]), ("OK", [], []))

    def test_space_grouped_thousands(self):
        self.assertClean(self.one("Llegaron 56 000 personas a la plaza.", "llegaron 56.000 personas a la plaza"))
        self.assertClean(self.one("They shipped 1,500,000 tons north.", "they shipped 1 500 000 tons north"))

    def test_year_abbreviation(self):
        self.assertClean(self.one("Prices rose sharply in the '90s across the west.",
                                  "prices rose sharply in the 1990s across the west"))

    def test_ordinal_and_decimal_zeros(self):
        self.assertClean(self.one("On the 4th of July they paid 7.20 dollars.",
                                  "on the 4 of July they paid 7.2 dollars"))

    def test_number_spoken_as_word(self):
        self.assertClean(self.one("The lake dropped to roughly one-fifth of its capacity.",
                                  "The lake dropped to roughly 1/5if of its capacity."))


class NoCaptions(unittest.TestCase):
    def test_no_caption_track(self):
        rows = vd.compare([(0.0, 5.0, "It holds 420 billion gallons.")], [], WINDOW, THRESH)
        self.assertEqual([r["level"] for r in rows], ["NO-CAPTIONS"])
        self.assertEqual(rows[0]["nums_whisper"], ["420"])


VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:04.000
The reservoir holds 240 billion gallons

00:00:04.000 --> 00:00:08.000
of water from the river.
"""
WHISPER_JSON = {"transcription": [
    {"offsets": {"from": 0, "to": 8000},
     "text": " The reservoir holds 420 billion gallons of water from the river."}]}


class CompareOnlyCLI(unittest.TestCase):
    def make_digest(self, root, vid="abcdefXYZ12"):
        d = Path(root) / vid
        d.mkdir(parents=True)
        (d / "whisper.json").write_text(json.dumps(WHISPER_JSON), encoding="utf-8")
        (d / "captions.en.vtt").write_text(VTT, encoding="utf-8")
        (d / "meta.json").write_text(json.dumps({"title": "Test vídeo", "duration": 8}), encoding="utf-8")
        return d

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "video_digest.py"), *args],
                              capture_output=True, text=True, encoding="utf-8")

    def test_digest_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self.make_digest(tmp)
            p = self.cli(str(d), "--compare-only")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("numeric-critical 1", p.stdout)
            report = (d / "report.md").read_text(encoding="utf-8")
            self.assertIn("numeric-critical: 1", report)
            self.assertIn("whisper 420 vs captions 240", report)
            rows = json.loads((d / "compare.json").read_text(encoding="utf-8"))
            self.assertEqual(rows[0]["num_conflicts"][0]["captions"], ["240"])
            self.assertFalse((d / "transcript.txt").exists())          # nothing else written

    def test_url_and_outdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self.make_digest(tmp)
            p = self.cli("https://www.youtube.com/watch?v=abcdefXYZ12", "--outdir", tmp, "--compare-only")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertTrue((d / "report.md").exists())

    def test_missing_whisper_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self.cli(tmp, "--compare-only")
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("no whisper.json", p.stderr)

    def test_full_run_checks_model_before_any_download(self):
        p = self.cli("https://www.youtube.com/watch?v=abcdefXYZ12", "--outdir", "/nonexistent",
                     "--model", "/nonexistent/model.bin")
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue("whisper model not found" in p.stderr or "missing tool" in p.stderr, p.stderr)


@unittest.skipUnless((REPLAY / "whisper.json").exists(), "set VIDEO_DIGEST_REPLAY=<digest folder> to replay a real digest")
class ReplayDigest(unittest.TestCase):
    """A real digest of yours: a number changed in either transcript is caught at least 95 % of the time."""

    @classmethod
    def setUpClass(cls):
        cls.ws = vd.load_whisper(REPLAY)
        cls.cs = vd.pick_captions(REPLAY)[1] or []

    def test_changed_numbers_are_caught(self):
        rng = random.Random(4)
        sites = [(side, i, m) for side, segs in (("captions", self.cs), ("whisper", self.ws))
                 for i, s in enumerate(segs)
                 for m in re.finditer(r"(?<![\d.,])\d{2,}(?![\d.,]\d)", s[2])]
        if len(sites) < 20:
            self.skipTest("fewer than 20 numbers in this digest")
        caught = 0
        for side, i, m in sites:
            segs = list(self.cs if side == "captions" else self.ws)
            t0, t1, text = segs[i]
            new = str(int(m.group(0)) + rng.choice((1, 2, 7, 10, 100)))
            segs[i] = (t0, t1, text[:m.start()] + new + text[m.end():])
            ws, cs = (self.ws, segs) if side == "captions" else (segs, self.cs)
            k = int(t0 // WINDOW) * WINDOW
            rows = vd.compare(ws, cs, WINDOW, THRESH)
            caught += any(r["level"] == "CRITICAL-NUMBERS" and abs(r["t"] - k) <= WINDOW for r in rows)
        self.assertGreaterEqual(caught / len(sites), 0.95, f"{caught}/{len(sites)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
