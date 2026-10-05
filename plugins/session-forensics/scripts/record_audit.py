"""record_audit.py - shared core of the record-keeping audit (proactivity.py, proact2.py, announce.py,
qshare.py, turns.py). Not run directly.

"The record" is what you ask sessions to keep current, in three categories:
  notes  writes to a notes file, by name (defaults session-notes.md — session-guard's file — and session-context.md; --notes-file, repeatable).
         Matched case-insensitively anywhere in a write's target path or command text.
  kb     writes into a knowledge base you keep (--kb REGEX, repeatable; none by default).
  mem    writes to Claude Code's auto-memory: the per-project memory folder
         (~/.claude/projects/<project>/memory/) and the folder named by `autoMemoryDirectory` in
         ~/.claude/settings.json, if set (--memory REGEX replaces these).
A turn "asked" for upkeep when the user's prompt matches --ask (default: the notes-file names plus
knowledge base / KB / memory / wrap up / hand off / durable / insight / up to date). Slash commands and
task notifications never count as asking. A wrap-up is the last turn before an idle gap of at least
--wrap-gap-hours (default 2) or the session's end; it is compliant when the notes file was written
unasked in it or in the turns just before (--wrap-window, default 3 turns including the wrap-up).

Writes are read from tool inputs: Write/Edit/MultiEdit/NotebookEdit target paths, Bash/PowerShell
command text that contains a writing verb (>, >>, tee, sed -i, cp, mv, git commit/push, a heredoc,
python, pwsh, Set-Content, Add-Content, Out-File) and is not a plain read, and the body of a script
the session wrote with Write/Edit and then ran from a shell command (Windows sessions run much of
their shell work that way; the body is in the transcript as the Write input). Subagent transcripts
and headless (claude -p) runs are left out: they do not keep the record. A session's last turn counts
as a wrap-up only once it has been idle for the gap, so a live session's last turn is not one yet.
All of it can be set in a JSON file given with --config (keys: notes_files, kb_patterns,
memory_patterns, ask_regex, wrap_gap_hours, wrap_window); flags win over the file.

Stdlib only, Python 3.8+.
"""
import collections
import datetime
import glob
import json
import os
import re
import sys

DEFAULT_NOTES = ["session-notes.md", "session-context.md"]   # session-guard's default first
GENERIC_ASK = r"knowledge ?base|knowledgebase|\bkb\b|\bmemory\b|wrap.?up|hand.?off|durable|insight|up.?to.?date"
WRITEV = re.compile(r"(>|>>|\btee\b|sed -i|\bcp\b|\bmv\b|git commit|git push|<<|python|pwsh|Set-Content|Add-Content|Out-File)", re.I)
READONLY = re.compile(r"^\s*(cat|head|tail|grep|rg|ls|sed -n|git (log|status|pull|diff|show)|wc|find|stat|Get-Content|Select-String)\b")
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def default_root():
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def auto_memory_dir():
    """autoMemoryDirectory from ~/.claude/settings.json (or $CLAUDE_CONFIG_DIR/settings.json), or None."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    try:
        with open(os.path.join(base, "settings.json"), encoding="utf-8") as fh:
            d = json.load(fh).get("autoMemoryDirectory")
        return os.path.expanduser(d) if isinstance(d, str) and d.strip() else None
    except Exception:
        return None


def default_memory_patterns():
    pats = [r"projects[/\\][^/\\\s\"']+[/\\]memory[/\\]"]
    d = auto_memory_dir()
    if d:
        name = os.path.basename(d.rstrip("/\\"))
        if name:
            pats.append(r"(^|[/\\\s\"'~])" + re.escape(name) + r"[/\\]")
    return pats


def stem_words(name):
    """'session-context.md' -> 'session.?context' (how people say it in a prompt)."""
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", name)
    return ".?".join(re.escape(p) for p in re.split(r"[-_ .]+", stem) if p)


class Config(object):
    def __init__(self, notes_files=None, kb_patterns=None, memory_patterns=None, ask_regex=None,
                 wrap_gap_hours=2.0, wrap_window=3):
        self.notes_files = list(notes_files or DEFAULT_NOTES)
        self.kb_patterns = list(kb_patterns or [])
        self.memory_patterns = list(memory_patterns) if memory_patterns else default_memory_patterns()
        self.ask_regex = ask_regex or "|".join([stem_words(n) for n in self.notes_files if stem_words(n)] + [GENERIC_ASK])
        self.wrap_gap_hours = float(wrap_gap_hours)
        self.wrap_window = max(1, int(wrap_window))
        self.t_notes = re.compile("|".join(re.escape(n) for n in self.notes_files), re.I)
        self.t_kb = re.compile("|".join("(?:%s)" % p for p in self.kb_patterns), re.I) if self.kb_patterns else None
        self.t_mem = re.compile("|".join("(?:%s)" % p for p in self.memory_patterns), re.I) if self.memory_patterns else None
        self.ask = re.compile(self.ask_regex, re.I)
        stems = [stem_words(n) for n in self.notes_files if stem_words(n)]
        self.say = re.compile("|".join(stems + [r"context file", r"\bmemory\b", r"knowledge.?base", r"\bKB\b"]), re.I)
        self.say_notes = re.compile("|".join(stems + [r"context file", r"notes file"]), re.I)

    def describe(self):
        return ("notes files: %s | kb: %s | memory: %s | wrap-up: >= %gh idle, window %d turns" % (
            ", ".join(self.notes_files), " | ".join(self.kb_patterns) or "not configured (--kb)",
            " | ".join(self.memory_patterns), self.wrap_gap_hours, self.wrap_window))


def add_args(ap):
    g = ap.add_argument_group("what counts as the record")
    g.add_argument("--notes-file", action="append", metavar="NAME",
                   help="notes file name sessions should keep current (repeatable; defaults session-notes.md and session-context.md)")
    g.add_argument("--kb", action="append", metavar="REGEX", help="path pattern of your knowledge base (repeatable)")
    g.add_argument("--memory", action="append", metavar="REGEX",
                   help="path pattern of the memory folder (repeatable; default: Claude Code's auto-memory folders)")
    g.add_argument("--ask", metavar="REGEX", help="prompt pattern that counts as asking for upkeep")
    g.add_argument("--wrap-gap-hours", type=float, metavar="H", help="idle gap that ends a stretch of work (default 2)")
    g.add_argument("--wrap-window", type=int, metavar="N", help="turns up to the wrap-up that may hold the write (default 3)")
    g.add_argument("--config", metavar="FILE.json", help="the same settings as JSON (flags win)")
    return ap


def config_from(args):
    d = {}
    if getattr(args, "config", None):
        with open(os.path.expanduser(args.config), encoding="utf-8") as fh:
            d = json.load(fh)
    pick = lambda flag, key: flag if flag not in (None, []) else d.get(key)
    kw = dict(notes_files=pick(args.notes_file, "notes_files"), kb_patterns=pick(args.kb, "kb_patterns"),
              memory_patterns=pick(args.memory, "memory_patterns"), ask_regex=pick(args.ask, "ask_regex"))
    for flag, key in ((args.wrap_gap_hours, "wrap_gap_hours"), (args.wrap_window, "wrap_window")):
        v = pick(flag, key)
        if v is not None:
            kw[key] = v
    return Config(**kw)


# ---------------------------------------------------------------- transcript helpers

def strip_reminders(t):
    return REMINDER.sub("", t)


def user_text(msg):
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def has_tool_result(msg):
    c = msg.get("content")
    return isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


def ts2dt(ts):
    return datetime.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")


def week(ts):
    iso = ts2dt(ts).isocalendar()
    return "%d-W%02d" % (iso[0], iso[1])


def vkey(s):
    try:
        return [int(x) for x in str(s).split(".")]
    except Exception:
        return [0]


def version_band(v, bands):
    """Band label for a CLI version given band start versions: 'A <2.1.228', 'B 2.1.228..2.1.250', ...,
    the last one '>=X' (the letter keeps the labels in order when sorted). The exact version when no
    bands are given."""
    if not bands:
        return v or "?"
    k = vkey(v or "0")
    starts = sorted(bands, key=vkey)
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if k < vkey(starts[0]):
        return "A <%s" % starts[0]
    for i, (lo, hi) in enumerate(zip(starts, starts[1:])):
        if vkey(lo) <= k < vkey(hi):
            return "%s %s..%s" % (letters[min(i + 1, 25)], lo, hi)
    return "%s >=%s" % (letters[min(len(starts), 25)], starts[-1])


def parse_bands(s):
    return [b.strip() for b in (s or "").split(",") if b.strip()]


def tilde(p):
    home = os.path.expanduser("~")
    return p.replace(home, "~") if p else p


def iter_files(paths):
    """*.jsonl under each directory (recursively), or the files themselves."""
    files = []
    for a in paths or [default_root()]:
        a = os.path.expanduser(a)
        files += glob.glob(os.path.join(a, "**", "*.jsonl"), recursive=True) if os.path.isdir(a) else [a]
    return files


def is_write_command(cmd):
    return bool(WRITEV.search(cmd)) and not (READONLY.match(cmd) and not re.search(r"git (commit|push)|>|tee|sed -i", cmd))


def classify_text(s, cfg):
    cats = set()
    if cfg.t_notes.search(s):
        cats.add("notes")
    if cfg.t_kb is not None and cfg.t_kb.search(s):
        cats.add("kb")
    if cfg.t_mem is not None and cfg.t_mem.search(s):
        cats.add("mem")
    return cats


def classify_write(name, inp, cfg):
    """Which record categories a tool call writes to (a set of 'notes', 'kb', 'mem')."""
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return classify_text(inp.get("file_path") or inp.get("notebook_path") or "", cfg)
    if name in ("Bash", "PowerShell"):
        s = inp.get("command") or ""
        return classify_text(s, cfg) if is_write_command(s) else set()
    return set()


SCRIPT_EXT = re.compile(r"\.(py|ps1|sh|bat|cmd|js)$", re.I)
BODY_WRITE = re.compile(r"""open\([^)]*['"][wa]|write_text|\.write\(|Set-Content|Add-Content|Out-File|WriteAllText|os\.replace|shutil\.(copy|move)|\btee\b|sed -i""", re.I)


def script_writes(name, inp, bodies, cfg):
    """Record writes made by running a script the session wrote earlier: a Write/Edit of a script file
    stores its body in `bodies`; a later shell command that names the script (path or file name)
    classifies that body, when the body writes files at all."""
    p = (inp.get("file_path") or "").replace("\\", "/").lower()
    if name in ("Write", "Edit") and SCRIPT_EXT.search(p):
        bodies[p] = (inp.get("content") or "") if name == "Write" else bodies.get(p, "") + "\n" + (inp.get("new_string") or "")
        return set()
    if name not in ("Bash", "PowerShell"):
        return set()
    cmd = (inp.get("command") or "").replace("\\", "/").lower()
    cats = set()
    for sp, body in bodies.items():
        base = sp.rsplit("/", 1)[-1]
        if (sp in cmd or re.search(r"(^|[\s/\"'])" + re.escape(base) + r"($|[\s\"';|&)])", cmd)) and BODY_WRITE.search(body):
            cats |= classify_text(body, cfg)
    return cats


def is_subagent(path):
    return "subagents" in path.replace("\\", "/").split("/")


def analyze(path, cfg):
    """(title, turns) for one transcript. A turn starts at a real user prompt; each carries ts, ver, model,
    prompt, asked, cats (record writes during the turn), slash, notif, compact_before, session, last_ts,
    wrap, and for wrap-ups wrap_notes / wrap_kb / wrap_asked."""
    turns, cur, title, compact_pending = [], None, None, False
    bodies, eps = {}, collections.Counter()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if not isinstance(e, dict):
                continue
            if e.get("customTitle"):
                title = e["customTitle"]
            if e.get("entrypoint"):
                eps[e["entrypoint"]] += 1
            ty = e.get("type")
            if ty == "user":
                if e.get("isCompactSummary"):
                    compact_pending = True
                    continue
                if e.get("isMeta"):
                    continue
                msg = e.get("message", {}) or {}
                if has_tool_result(msg):
                    continue
                txt = strip_reminders(user_text(msg)).strip()
                if not txt or txt.startswith("<local-command") or not e.get("timestamp"):
                    continue
                slash = txt.startswith("<command-name>")
                notif = txt.startswith("<task-notification>")
                cur = dict(ts=e.get("timestamp"), ver=e.get("version"), model=None, prompt=txt[:160].replace("\n", " "),
                           asked=bool(cfg.ask.search(txt)) and not slash and not notif, cats=set(), slash=slash, notif=notif,
                           compact_before=compact_pending, session=os.path.basename(path)[:8], last_ts=e.get("timestamp"))
                compact_pending = False
                turns.append(cur)
            elif ty == "assistant" and cur is not None:
                m = e.get("message", {}) or {}
                if m.get("model") and not str(m.get("model")).startswith("<"):
                    cur["model"] = m["model"]
                if e.get("timestamp"):
                    cur["last_ts"] = e["timestamp"]
                for b in m.get("content", []) or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        cur["cats"] |= classify_write(b.get("name"), inp, cfg) | script_writes(b.get("name"), inp, bodies, cfg)
    # subagents and headless runs never keep the session record (a resumed subagent can pass the 3-turn floor)
    if is_subagent(path) or (eps and eps.most_common(1)[0][0] == "sdk-cli"):
        return title, []
    # the session's last turn is a wrap-up only once it has been idle for the gap (not while it is live)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    for i, t in enumerate(turns):
        nxt = turns[i + 1]["ts"] if i + 1 < len(turns) else None
        gap_h = ((ts2dt(nxt) if nxt else now) - ts2dt(t["last_ts"])).total_seconds() / 3600
        t["wrap"] = gap_h >= cfg.wrap_gap_hours
        if t["wrap"]:
            window = turns[max(0, i - cfg.wrap_window + 1):i + 1]
            t["wrap_notes"] = any("notes" in w["cats"] for w in window)
            t["wrap_kb"] = any("kb" in w["cats"] for w in window)
            t["wrap_asked"] = any(w["asked"] for w in window)
    return title, turns
