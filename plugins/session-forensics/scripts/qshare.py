"""qshare.py - share of question-shaped prompts per ISO week, and how often unasked turns of each kind
(question, directive, notification/slash command) wrote the record. A session that answers questions
all week has fewer natural moments to update notes than one that is building something.

Usage: python3 qshare.py [DIR-or-FILE ...] [record options, see proactivity.py]

Stdlib only, Python 3.8+.
"""
import argparse
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import record_audit as ra  # noqa: E402

Q = re.compile(r"^(is|are|can|could|what|why|how|should|does|do|did|where|which|would|will|any|was|were)\b|\?\s*$|\?\s", re.I)


def kind(t):
    p = t["prompt"]
    if t["notif"] or t["slash"]:
        return "notif/slash"
    if p.startswith("[Image") or p.startswith("<"):
        return "other"
    return "question" if Q.search(p.strip()) else "directive"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="*", help="transcript folders or files (default ~/.claude/projects)")
    ra.add_args(ap)
    a = ap.parse_args()
    cfg = ra.config_from(a)
    allturns = []
    for f in ra.iter_files(a.paths):
        try:
            title, turns = ra.analyze(f, cfg)
        except Exception:
            continue
        if len(turns) >= 3:
            allturns += turns
    bywk = collections.defaultdict(collections.Counter)
    bykind = collections.defaultdict(collections.Counter)
    for t in allturns:
        k = kind(t)
        w = ra.week(t["ts"])
        if t["asked"]:
            continue
        bywk[w]["turns"] += 1
        bywk[w][k] += 1
        if "notes" in t["cats"]:
            bywk[w]["notes_" + k] += 1
        bykind[k]["turns"] += 1
        for cat in ("notes", "kb", "mem"):
            if cat in t["cats"]:
                bykind[k][cat] += 1
    print("RECORD: " + cfg.describe())
    print("\nUNASKED TURNS BY KIND: n | wrote notes | wrote kb | wrote mem")
    for k in sorted(bykind):
        c = bykind[k]
        n = c["turns"]
        print("  %-12s n=%4d  notes=%3d (%3.0f%%)  kb=%3d (%3.0f%%)  mem=%3d (%3.0f%%)" % (
            k, n, c["notes"], 100 * c["notes"] / n, c["kb"], 100 * c["kb"] / n, c["mem"], 100 * c["mem"] / n))
    print("\nPER WEEK: unasked turns | question share | notes-write rate on question turns / on directive turns")
    for w in sorted(bywk):
        c = bywk[w]
        n = c["turns"]
        q = c["question"] or 0
        d = c["directive"] or 0
        print("  %s n=%4d  question=%3d (%3.0f%%) directive=%3d  | notes on Q=%s  on D=%s" % (
            w, n, q, 100 * q / n, d, ("%3.0f%%" % (100 * c["notes_question"] / q)) if q else "  - ",
            ("%3.0f%%" % (100 * c["notes_directive"] / d)) if d else "  - "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
