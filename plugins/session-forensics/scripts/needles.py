"""needles.py - which Claude Code CLI build contains which system-prompt sentence?

Counts a list of sentences from the harness's system prompt in each CLI binary found in a folder
(default ~/.local/share/claude/versions, where the native installer keeps one file per version), so
you can see in which version a behavior-shaping sentence appeared or disappeared ("the requested
scope is the deliverable", "stop when the content stops", memory wording, ...). The built-in list is
a snapshot; add your own with --needle LABEL=TEXT (repeatable).

Usage: python3 needles.py [VERSIONS_DIR] [--needle LABEL=TEXT ...]

Stdlib only, Python 3.8+.
"""
import argparse
import glob
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

NEEDLES = {
    "delivering-work:scope is the deliverable": "requested scope is the deliverable",
    "delivering-work:stop short beyond ask": "Stop short of actions or changes clearly beyond",
    "delivering-work:finish whole task": "Finish the whole task, not just easy parts",
    "delivering-work:exception report+stop": "Exception: when the user is describing a problem",
    "delivering-work:offering follow-ups fine": "Offering follow-ups after the task is done is fine",
    "autonomous:operating autonomously": "You are operating autonomously",
    "autonomous:check last paragraph": "Before ending your turn, check your last paragraph",
    "writing:only final message reaches": "Only your final message reliably reaches them",
    "writing:stop when content stops": "Stop when the content stops",
    "writing:lead with answer": "Lead with the answer or outcome",
    "harness:say in a line what you are about": "say in a line what you",
    "harness:close with a short recap": "Close with a short recap",
    "harness:prefer dedicated tools": "Prefer the dedicated file/search tools",
    "automode:do work through Bash": "Do your work through the Bash tool wherever it can",
    "ctx:when you have enough info act": "When you have enough information to act, act",
    "ctx:do not re-derive": "Do not re-derive facts already established",
    "ctx:conversation summarized": "some or all of the current context is summarized",
    "memory:persistent file-based": "persistent file-based memory",
    "memory:one fact per file": "one file holding one fact",
    "memory:dont save what repo records": "Don't save what the repo already records",
    "memory:ask what was non-obvious": "ask what was non-obvious about it",
    "memory:recalled are background": "Recalled memories appearing inside",
    "memory:MEMORY.md index loaded": "is the index loaded into context each session",
    "confirm:hard to reverse or outward": "hard to reverse or outward-facing, confirm first",
    "report:faithfully": "Report outcomes faithfully",
    "pronouns:they/them": "use they/them",
}


def main():
    ap = argparse.ArgumentParser(description="Count system-prompt sentences in each Claude Code CLI binary.")
    ap.add_argument("root", nargs="?", default=os.path.join(os.path.expanduser("~"), ".local", "share", "claude", "versions"))
    ap.add_argument("--needle", action="append", default=[], metavar="LABEL=TEXT", help="add a sentence to look for")
    a = ap.parse_args()
    needles = dict(NEEDLES)
    for n in a.needle:
        label, _, text = n.partition("=")
        if text:
            needles[label] = text
    files = sorted(f for f in glob.glob(os.path.join(os.path.expanduser(a.root), "*")) if os.path.isfile(f))
    if not files:
        print("no CLI builds under %s" % a.root, file=sys.stderr)
        return 1
    print("needle".ljust(44), " ".join(os.path.basename(f).rjust(8) for f in files))
    datas = []
    for f in files:
        with open(f, "rb") as fh:
            datas.append(fh.read())
    for k, n in needles.items():
        nb = n.encode("utf-8")
        print(k.ljust(44), " ".join(str(d.count(nb)).rjust(8) for d in datas))
    return 0


if __name__ == "__main__":
    sys.exit(main())
