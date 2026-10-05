"""find_session.py - which Claude Code transcript on this machine really worked on <keyword>?

Usage:  python3 find_session.py KEYWORD [PROJECTS_ROOT]          (default root: ~/.claude/projects)
        ssh HOST 'python3 - KEYWORD' < find_session.py           (run it on another machine; Windows: py -)

Sessions are stored per machine AND per working directory (~/.claude/projects/<cwd-slug>/<id>.jsonl),
so `/resume` lists only the current directory's. A raw grep over transcripts is useless for anything
named in your CLAUDE.md or memory index: those are injected into every transcript's first turn, so
every file matches. This strips <system-reminder> blocks and counts hits in real user and assistant
text only, then prints one line per transcript (oldest first by modification time): id, size,
first -> last timestamp (UTC), user turns, hits (user/assistant), the /rename title and the first
real prompt. `<==` marks more than two hits. Resume the winner from its original directory with
`claude --resume <full id>`. The keyword is literal and case-insensitive; subagent transcripts are
not scanned.

Stdlib only, Python 3.8+.
"""
import glob
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SR = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def text_of(msg):
    c = (msg or {}).get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return c if isinstance(c, str) else ""


def scan(path, kw):
    title = first_user = first_ts = last_ts = None
    n_user = hits_user = hits_asst = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            if o.get("customTitle"):
                title = o["customTitle"]
            ts = o.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            t = o.get("type")
            if t not in ("user", "assistant"):
                continue
            txt = SR.sub("", text_of(o.get("message"))).strip()
            if t == "user":
                if txt and not txt.startswith("<"):
                    n_user += 1
                    first_user = first_user or txt[:150].replace("\n", " ")
                hits_user += len(kw.findall(txt))
            else:
                hits_asst += len(kw.findall(txt))
    return title, first_user, first_ts, last_ts, n_user, hits_user, hits_asst


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0 if len(argv) >= 2 else 2
    kw = re.compile(re.escape(argv[1]), re.I)
    root = os.path.expanduser(argv[2]) if len(argv) > 2 else os.path.join(os.path.expanduser("~"), ".claude", "projects")
    files = sorted(glob.glob(os.path.join(root, "*", "*.jsonl")), key=os.path.getmtime)
    if not files:
        print("no transcripts under %s" % root, file=sys.stderr)
        return 1
    for f in files:
        title, first_user, first_ts, last_ts, n_user, hu, ha = scan(f, kw)
        flag = "  <== " if hu + ha > 2 else "      "
        print("%s%s  %7dKB  %s -> %s  user_turns=%-3d hits(user/asst)=%d/%d  title=%r  first=%r" % (
            flag, os.path.basename(f)[:8], os.path.getsize(f) // 1024, (first_ts or "")[:16], (last_ts or "")[:16],
            n_user, hu, ha, title, first_user))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
