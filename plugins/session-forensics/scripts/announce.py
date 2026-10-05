"""announce.py - when a session updated the record without being asked, did its final reply SAY so?

Per turn: did the assistant write the notes file / knowledge base / memory (record options as in
proactivity.py), and does the last assistant text of the turn mention it? With --trailer LABEL also:
how many tool-using turns end with a closing line "LABEL: ..." (a convention you may ask sessions to
follow, e.g. --trailer Context for "Context: notes updated"), per week, prompted vs task-notification
turns. Also the temp-script share per CLI version: Windows sessions run some shell commands from a
script under %TEMP%\\claude\\...\\<name>.(py|ps1|sh|txt) (seen on CLI 2.1.266 and later); its body is
counted from the Write call that created it ("body in transcript") or read from disk while it exists.
Same sessions as proactivity.py: at least 3 turns, no subagent transcripts, no headless runs.

Usage: python3 announce.py [DIR-or-FILE ...] [--bands V1,V2,...] [--trailer LABEL] [record options]

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

TEMP = re.compile(r'([A-Za-z]:[\\/](?:[^"\s]+[\\/])*Temp[\\/]claude[\\/][^"\s]+\.(?:py|ps1|sh|txt))', re.I)


def trailer_re(label):
    """A line 'LABEL:' at the start of a line, allowing Markdown decoration (**LABEL:**, > LABEL:, `LABEL`:)."""
    return re.compile(r"^[ \t]*[*_>`]*[ \t]*" + re.escape(label) + r"[ \t]*[*_`]*[ \t]*:", re.M)


def classify_cmd(name, inp, cfg, bodies):
    cats = ra.classify_write(name, inp, cfg) | ra.script_writes(name, inp, bodies, cfg)
    cmd = (inp.get("command") or "") if name in ("Bash", "PowerShell") else ""
    temp_refs = TEMP.findall(cmd)
    resolved = inlog = 0
    for p in temp_refs:
        inlog += p.replace("\\", "/").lower() in bodies
        p2 = p.replace("/", os.sep)
        if os.path.exists(p2):
            resolved += 1
            with open(p2, encoding="utf-8", errors="replace") as fh:
                cats |= ra.classify_text(fh.read(), cfg)
    return cats, bool(temp_refs), resolved, inlog


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="*", help="transcript folders or files (default ~/.claude/projects)")
    ap.add_argument("--bands", help="comma-separated version band starts, e.g. 2.1.228,2.1.250,2.1.260")
    ap.add_argument("--trailer", metavar="LABEL", help="also count tool-using turns whose reply ends with a 'LABEL:' line")
    ra.add_args(ap)
    a = ap.parse_args()
    cfg = ra.config_from(a)
    bands = ra.parse_bands(a.bands)
    rows = []
    tempstats = collections.defaultdict(collections.Counter)
    for f in ra.iter_files(a.paths):
        try:
            if len(ra.analyze(f, cfg)[1]) < 3:
                continue
        except Exception:
            continue
        cur, bodies = None, {}
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if not isinstance(e, dict):
                    continue
                ty = e.get("type")
                if ty == "user" and not e.get("isMeta") and not e.get("isCompactSummary"):
                    msg = e.get("message", {}) or {}
                    if ra.has_tool_result(msg):
                        continue
                    txt = ra.strip_reminders(ra.user_text(msg)).strip()
                    if not txt or txt.startswith("<local-command") or not e.get("timestamp"):
                        continue
                    cur = dict(ts=e["timestamp"], ver=e.get("version"), asked=bool(cfg.ask.search(txt)) and not txt.startswith("<"),
                               cats=set(), final="", temp=0, resolved=0, inlog=0, ncmd=0, tools=0,
                               slash=txt.startswith("<command-name>"), notif=txt.startswith("<task-notification>"))
                    rows.append(cur)
                elif ty == "assistant" and cur is not None:
                    for b in (e.get("message", {}) or {}).get("content", []) or []:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "tool_use":
                            c, has_temp, res, inlog = classify_cmd(b.get("name"), b.get("input") or {}, cfg, bodies)
                            cur["cats"] |= c
                            cur["tools"] += 1
                            if b.get("name") in ("Bash", "PowerShell"):
                                cur["ncmd"] += 1
                                cur["temp"] += has_temp
                                cur["resolved"] += res
                                cur["inlog"] += inlog
                        elif b.get("type") == "text" and b.get("text", "").strip():
                            cur["final"] = b["text"]
    print("RECORD: " + cfg.describe())
    print("\nTEMP-SCRIPT USE (shell commands that run a Temp\\claude script) by version:")
    for r in rows:
        v = r["ver"] or "?"
        tempstats[v]["cmds"] += r["ncmd"]
        tempstats[v]["temp"] += r["temp"]
        tempstats[v]["resolved"] += r["resolved"]
        tempstats[v]["inlog"] += r["inlog"]
    for v in sorted(tempstats, key=ra.vkey):
        c = tempstats[v]
        if c["cmds"]:
            print("  %-9s shell cmds=%4d  via temp script=%4d (%3.0f%%)  body in transcript=%d  script still on disk=%d" % (
                v, c["cmds"], c["temp"], 100 * c["temp"] / c["cmds"], c["inlog"], c["resolved"]))
    print("\nANNOUNCE RATE: unasked turns that wrote notes/mem/kb | final text mentions it")
    agg = collections.defaultdict(collections.Counter)
    for r in rows:
        if r["asked"] or not r["cats"]:
            continue
        for key in (("week", ra.week(r["ts"])), ("version band" if bands else "version", ra.version_band(r["ver"], bands))):
            c = agg[key]
            c["wrote"] += 1
            if cfg.say.search(r["final"] or ""):
                c["said"] += 1
            if "notes" in r["cats"]:
                c["wrote_notes"] += 1
                if cfg.say_notes.search(r["final"] or ""):
                    c["said_notes"] += 1
    # weeks ('YYYY-Www') and lettered bands sort as text; exact versions numerically
    for k in sorted(agg, key=lambda s: (s[0], ra.vkey(s[1]) if s[0] == "version" else [0], s[1])):
        c = agg[k]
        n = c["wrote"]
        m = c["wrote_notes"] or 1
        print("  %-13s %-18s wrote(any)=%3d said=%3d (%3.0f%%) | wrote notes=%3d said notes=%3d (%3.0f%%)" % (
            k[0], k[1], n, c["said"], 100 * c["said"] / n, c["wrote_notes"], c["said_notes"], 100 * c["said_notes"] / m))
    if a.trailer:
        tre = trailer_re(a.trailer)
        print("\nTRAILER '%s:': tool-using turns whose final text has that line (prompted turns | task-notification turns)" % a.trailer)
        tr = collections.defaultdict(collections.Counter)
        for r in rows:
            if not r["tools"] or r["slash"]:
                continue
            c = tr[ra.week(r["ts"])]
            k = "notif" if r["notif"] else "prompt"
            c[k] += 1
            c[k + "_tr"] += bool(tre.search(r["final"] or ""))
        for w in sorted(tr):
            c = tr[w]
            print("  %s prompted=%3d trailer=%3d (%3.0f%%) | notification=%3d trailer=%3d" % (
                w, c["prompt"], c["prompt_tr"], 100 * c["prompt_tr"] / (c["prompt"] or 1), c["notif"], c["notif_tr"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
