"""proact2.py - wrap-up compliance split by compactions so far, session age, CLI version band, ISO week and
whether a nested CLAUDE.md was loaded into context; plus a timeline per session of nested CLAUDE.md
loads and compactions.

A "nested CLAUDE.md" is an instruction file Claude Code loaded mid-session because the session read
files under its folder (transcript attachment type nested_memory). --nested REGEX limits that split
to matching paths (for example one repository whose CLAUDE.md you suspect is loaded twice).
--bands 2.1.250,2.1.260 groups versions into bands (default: each version on its own).

Usage: python3 proact2.py [DIR-or-FILE ...] [--bands V1,V2,...] [--nested REGEX] [record options, see proactivity.py]

Stdlib only, Python 3.8+.
"""
import argparse
import collections
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import record_audit as ra  # noqa: E402


def timeline(path):
    ev = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "nested_memory" not in line and "compact_boundary" not in line and "isCompactSummary" not in line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            a = e.get("attachment") or {}
            if a.get("type") == "nested_memory":
                ev.append((e.get("timestamp", ""), "NESTED", a.get("path") or a.get("filename")))
            elif e.get("subtype") == "compact_boundary" or e.get("isCompactSummary"):
                ev.append((e.get("timestamp", ""), "COMPACT", ""))
    return sorted(ev)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="*", help="transcript folders or files (default ~/.claude/projects)")
    ap.add_argument("--bands", help="comma-separated version band starts, e.g. 2.1.228,2.1.250,2.1.260")
    ap.add_argument("--nested", metavar="REGEX", help="count only nested CLAUDE.md paths matching this")
    ra.add_args(ap)
    a = ap.parse_args()
    cfg = ra.config_from(a)
    bands = ra.parse_bands(a.bands)
    nested_re = re.compile(a.nested, re.I) if a.nested else None
    agg = collections.defaultdict(collections.Counter)

    def add(key, t):
        c = agg[key]
        c["wrap"] += 1
        if t["wrap_asked"]:
            c["asked"] += 1
        elif t["wrap_notes"]:
            c["notes"] += 1
        else:
            c["miss"] += 1

    print("RECORD: " + cfg.describe())
    print("\nTIMELINES (nested CLAUDE.md loads and compactions)")
    for f in sorted(ra.iter_files(a.paths), key=os.path.getmtime):
        try:
            title, turns = ra.analyze(f, cfg)
        except Exception:
            continue
        if len(turns) < 3:
            continue
        ev = timeline(f)
        for ts, kind, p in ev:
            print("  %s %-8s %s %s" % (ts[:16], os.path.basename(f)[:8], kind, ra.tilde(p or "")))
        start = ra.ts2dt(turns[0]["ts"])
        for t in turns:
            if not t["wrap"]:
                continue
            ncomp = sum(1 for ts, k, _ in ev if k == "COMPACT" and ts <= t["ts"])
            # a nested copy counts while no compaction has happened after it (compaction drops it)
            nnest = sum(1 for ts, k, p in ev if k == "NESTED" and ts <= t["ts"]
                        and (nested_re is None or nested_re.search(p or ""))
                        and not any(ts2 > ts and k2 == "COMPACT" for ts2, k2, _ in ev if ts2 <= t["ts"]))
            age = (ra.ts2dt(t["ts"]) - start).days
            add(("compactions so far", "0" if ncomp == 0 else "1" if ncomp == 1 else "2+"), t)
            add(("session age", "0-1d" if age <= 1 else "2-7d" if age <= 7 else "8-30d" if age <= 30 else "31d+"), t)
            add(("version band" if bands else "version", ra.version_band(t["ver"], bands)), t)
            add(("week", ra.week(t["ts"])), t)
            add(("nested CLAUDE.md live in context", "yes" if nnest else "no"), t)
    print("\nWRAP-UPS: n | notes written unasked | asked by user | missed")
    for k in sorted(agg):
        c = agg[k]
        n = c["wrap"]
        print("%-38s %-18s n=%3d  unasked=%3d (%3.0f%%)  asked=%2d  missed=%3d (%3.0f%%)" % (
            k[0], k[1], n, c["notes"], 100 * c["notes"] / n, c["asked"], c["miss"], 100 * c["miss"] / n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
