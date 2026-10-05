"""proactivity.py - did sessions keep the record current without being asked?

Per session, ISO week, Claude Code version and model: turns, turns that wrote the notes file / the
knowledge base / memory unasked vs asked, prompts that asked, and wrap-up compliance (was the notes
file written unasked at the end of each stretch of work). Then every prompt that asked for upkeep,
with whether the turns just before had already done it unasked. What counts as "the record" is set
with --notes-file / --kb / --memory (record_audit.py explains each).

Usage: python3 proactivity.py [DIR-or-FILE ...] [--notes-file NAME] [--kb REGEX] [--memory REGEX] ...
       (default DIR: ~/.claude/projects, searched recursively for *.jsonl, subagent transcripts included)

Stdlib only, Python 3.8+.
"""
import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import record_audit as ra  # noqa: E402


def table(allturns, keyfn, label, order=None):
    print("\nPER %s  turns | unasked-turn writes notes/kb/mem | asks | WRAP-UPS: n, notes-unasked%%, kb-unasked%%" % label)
    agg = collections.defaultdict(collections.Counter)
    for t in allturns:
        c = agg[keyfn(t)]
        c["turns"] += 1
        if t["asked"]:
            c["asks"] += 1
        else:
            for cat in t["cats"]:
                c["u" + cat] += 1
        if t["wrap"]:
            c["wrap"] += 1
            if t["wrap_notes"] and not t["wrap_asked"]:
                c["wnotes"] += 1
            if t["wrap_kb"] and not t["wrap_asked"]:
                c["wkb"] += 1
            if t["wrap_asked"]:
                c["wasked"] += 1
    for k in sorted(agg, key=order or (lambda s: ra.vkey(s) if s and s[0].isdigit() else [0, s])):
        c = agg[k]
        n = c["turns"]
        w = c["wrap"] or 1
        print("%-22s turns=%4d  unasked notes=%3d (%4.1f%%) kb=%3d mem=%3d | asks=%3d | wrap n=%3d notes=%4.0f%% kb=%4.0f%% asked-at-wrap=%d" % (
            k, n, c["unotes"], 100 * c["unotes"] / n, c["ukb"], c["umem"], c["asks"], c["wrap"],
            100 * c["wnotes"] / w, 100 * c["wkb"] / w, c["wasked"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="*", help="transcript folders or files (default ~/.claude/projects)")
    ra.add_args(ap)
    a = ap.parse_args()
    cfg = ra.config_from(a)
    print("RECORD: " + cfg.describe())
    allturns = []
    print("\nPER SESSION  (turns | notes unasked/asked | kb unasked/asked | mem unasked/asked | asks | wrap-ups with notes written unasked)")
    for f in sorted(ra.iter_files(a.paths), key=os.path.getmtime):
        try:
            title, turns = ra.analyze(f, cfg)
        except Exception as ex:
            print("skip", f, ex)
            continue
        if len(turns) < 3:
            continue
        allturns += turns

        def cnt(cat, asked):
            return sum(1 for t in turns if cat in t["cats"] and t["asked"] == asked)
        vers = sorted({t["ver"] for t in turns if t["ver"]}, key=ra.vkey)
        wraps = [t for t in turns if t["wrap"]]
        wu = sum(1 for t in wraps if t["wrap_notes"] and not t["wrap_asked"])
        models = sorted({(t["model"] or "?").replace("claude-", "") for t in turns})
        print("%s..%s %s %-28s turns=%4d notes=%d/%d kb=%d/%d mem=%d/%d asks=%d wrap=%d/%d vers=%s..%s %s" % (
            turns[0]["ts"][:10], turns[-1]["ts"][:10], os.path.basename(f)[:8], str(title)[:28], len(turns),
            cnt("notes", False), cnt("notes", True), cnt("kb", False), cnt("kb", True), cnt("mem", False), cnt("mem", True),
            sum(1 for t in turns if t["asked"]), wu, len(wraps), vers[0] if vers else "?", vers[-1] if vers else "?", "+".join(models)))

    table(allturns, lambda t: ra.week(t["ts"]), "ISO WEEK", order=str)   # 'YYYY-Www' sorts by date as text
    table(allturns, lambda t: t["ver"] or "?", "CLAUDE CODE VERSION")
    table(allturns, lambda t: (t["model"] or "?").replace("claude-", ""), "MODEL")

    print("\nASKED PROMPTS (prior 3 turns' unasked writes = already handled?)")
    for i, t in enumerate(allturns):
        if not t["asked"]:
            continue
        prev = [p for p in allturns[max(0, i - 3):i] if p["session"] == t["session"] and not p["asked"]]
        done = set().union(*[p["cats"] for p in prev]) if prev else set()
        print("%s %s v%s %s prior-unasked=%s this-turn=%s | %s" % (
            t["ts"][:16], t["session"], t["ver"], (t["model"] or "?").replace("claude-", ""),
            sorted(done) or "-", sorted(t["cats"]) or "-", t["prompt"][:105]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
