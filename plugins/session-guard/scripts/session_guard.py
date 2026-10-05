"""session-guard: Claude Code Stop + PreCompact hook that keeps a session-notes file fresh.

The notes file (default ~/.claude/session-notes.md) is the record a new session boots from when
this one is lost or compacted: what is being worked on, decisions with their reasons, files
touched, open threads. This hook makes keeping it current mechanical instead of instruction-only.

  stop        A turn that used tools cannot end while the file is more than STALE_MIN minutes stale
              or has not been written since the session's first turn. A compaction that ran while
              the file was more than COMPACT_FRESH_MIN minutes stale forces an update at the next
              stop (tools or not). Blocks at most once per turn (stop_hook_active + per-session
              state) by printing {"decision": "block", "reason": ...} on stdout with exit 0.
  precompact  Records the compaction in the session state. A manual /compact while the file is
              more than STALE_MIN stale is bounced once (the next /compact within STALE_MIN
              proceeds). Auto-compaction is never blocked: a blocked compaction at the context
              ceiling can wedge a session.
  status      Human-readable: file age, thresholds, per-session state.

Fail-open by design: any internal error -> no output, exit 0. Never blocks via exit code 2 (the
hook command ends in `|| exit 1`, which would turn a 2 into a non-blocking 1). Stdlib only, 3.8+.

Environment:
  CLAUDE_SESSION_GUARD=off                     disable (for headless runs that keep their own record)
  CLAUDE_SESSION_NOTES=<path>                  the file to guard   (default ~/.claude/session-notes.md)
  CLAUDE_SESSION_GUARD_STATE=<dir>             state + log dir     (default ~/.claude/session-guard)
  CLAUDE_SESSION_NOTES_STALE_MIN=30            staleness that blocks a tool-using turn / bounces /compact
  CLAUDE_SESSION_NOTES_COMPACT_FRESH_MIN=10    staleness at compaction time that forces a post-compaction update
"""

import calendar
import datetime
import json
import os
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _env_int(name, default):
    try:
        return max(0, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


STALE_MIN = _env_int("CLAUDE_SESSION_NOTES_STALE_MIN", 30)
COMPACT_FRESH_MIN = _env_int("CLAUDE_SESSION_NOTES_COMPACT_FRESH_MIN", 10)
SESSION_START_GRACE_SEC = 5 * 60      # a write within 5 min before the first turn counts as this session's
TAIL_FIRST = 512 * 1024               # transcript tail read; grows x4 until the current turn is found
TAIL_MAX = 96 * 1024 * 1024
STATE_TTL_DAYS = 30
LOG_MAX = 1024 * 1024


def disabled():
    return os.environ.get("CLAUDE_SESSION_GUARD", "").strip().lower() in ("off", "0", "false", "no")


def notes_file():
    p = os.environ.get("CLAUDE_SESSION_NOTES")
    return Path(p) if p else Path.home() / ".claude" / "session-notes.md"


def state_dir():
    p = os.environ.get("CLAUDE_SESSION_GUARD_STATE")
    d = Path(p) if p else Path.home() / ".claude" / "session-guard"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log(line):
    try:
        f = state_dir() / "guard.log"
        if f.exists() and f.stat().st_size > LOG_MAX:
            data = f.read_bytes()[-LOG_MAX // 4:]
            f.write_bytes(data[data.find(b"\n") + 1:])
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except Exception:
        pass


def _safe_sid(sid):
    return "".join(ch for ch in str(sid) if ch.isalnum() or ch in "-_.")[:80] or "unknown"


def load_state(sid):
    try:
        return json.loads((state_dir() / (_safe_sid(sid) + ".json")).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(sid, st):
    try:
        f = state_dir() / (_safe_sid(sid) + ".json")
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(str(tmp), str(f))
    except Exception:
        pass


def prune_state():
    try:
        cutoff = time.time() - STATE_TTL_DAYS * 86400
        for f in state_dir().glob("*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
    except Exception:
        pass


def parse_ts(s):
    """Transcript timestamps are ISO-8601 UTC ('2026-09-13T23:49:28.977Z') -> epoch seconds."""
    try:
        dt = datetime.datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")
        return float(calendar.timegm(dt.timetuple()))
    except Exception:
        return None


def fmt_age(sec):
    sec = max(0.0, float(sec))
    if sec < 3600:
        return "%d min" % round(sec / 60)
    if sec < 172800:
        return "%.1f h" % (sec / 3600)
    return "%.1f d" % (sec / 86400)


def fmt_when(epoch):
    if not epoch:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def _real_prompt(e):
    """A user entry that starts a turn: not a tool result, not meta, not a compaction summary."""
    if e.get("type") != "user" or e.get("isMeta") or e.get("isCompactSummary") or e.get("isSidechain"):
        return False
    content = (e.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
        return any(isinstance(b, dict) and b.get("type") in ("text", "image", "document") for b in content)
    return False


def _read_tail(path, nbytes):
    size = os.path.getsize(path)
    take = min(nbytes, size)
    with open(path, "rb") as fh:
        fh.seek(size - take)
        data = fh.read(take)
    lines = data.split(b"\n")
    truncated = take < size
    if truncated:
        lines = lines[1:]          # first line is partial
    return lines, truncated


def current_turn(transcript_path):
    """The last real user prompt in the transcript and the tool calls recorded after it.
    Returns {'uuid', 'ts', 'tools': [names]} or None. Reads only the tail of the file."""
    n = TAIL_FIRST
    while True:
        lines, truncated = _read_tail(transcript_path, n)
        idx = None
        prompt = None
        for i in range(len(lines) - 1, -1, -1):
            ln = lines[i]
            if b'"user"' not in ln:
                continue
            try:
                e = json.loads(ln.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(e, dict) and _real_prompt(e):
                idx, prompt = i, e
                break
        if idx is not None:
            tools = []
            for ln in lines[idx + 1:]:
                if b'"assistant"' not in ln or b'"tool_use"' not in ln:
                    continue
                try:
                    e = json.loads(ln.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(e, dict) or e.get("type") != "assistant" or e.get("isSidechain"):
                    continue
                for b in (e.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tools.append(str(b.get("name") or "?"))
            return {"uuid": prompt.get("uuid"), "ts": parse_ts(prompt.get("timestamp")), "tools": tools}
        if not truncated or n >= TAIL_MAX:
            return None
        n *= 4


def _tool_summary(tools):
    names = []
    for t in tools:
        if t not in names:
            names.append(t)
    return "%d tool call%s (%s)" % (len(tools), "" if len(tools) == 1 else "s", ", ".join(names[:6]) + (", ..." if len(names) > 6 else ""))


WHAT = ("Record in the notes file: what is being worked on and its status, decisions reached this turn with "
        "their reasons (alternatives rejected too), files created or changed, background work still running, "
        "and open threads or blockers. Keep it brief and scannable: a new session must be able to resume from it.")
TRAILER = ("This hook blocks once per turn; after the update, end the turn normally and say in one line of your "
           "reply that the notes were updated.")


def reason_stale(cf, age_sec, mtime, tools):
    return ("session-guard (Stop hook): %s is %s stale (last written %s) and this turn used %s. Update your "
            "session notes before your final reply. %s %s"
            % (cf, fmt_age(age_sec), fmt_when(mtime), _tool_summary(tools), WHAT, TRAILER))


def reason_untouched(cf, mtime, session_start, tools):
    return ("session-guard (Stop hook): %s has not been written since this session's first turn (%s; file last "
            "written %s) and this turn used %s. Note the previous state the file describes, then add or refresh "
            "this session's section and update its date line. %s %s"
            % (cf, fmt_when(session_start), fmt_when(mtime), _tool_summary(tools), WHAT, TRAILER))


def reason_missing(cf, tools):
    return ("session-guard (Stop hook): %s does not exist and this turn used %s. Create it: a date line and a "
            "section for this session. %s %s" % (cf, _tool_summary(tools), WHAT, TRAILER))


def reason_compaction(cf, compact_ts, trigger, stale_min):
    return ("session-guard (Stop hook): a compaction (%s) ran at %s while %s was %d min stale, so the compaction "
            "summary may now be the only record of what came before it. Before your final reply, write everything "
            "durable from before the compaction into the file -- decisions and their reasons, open threads, "
            "identifiers, files touched -- and update its date line. %s" % (trigger, fmt_when(compact_ts), cf, round(stale_min), TRAILER))


def do_stop(inp):
    sid = inp.get("session_id") or "unknown"
    if disabled():
        log("stop sid=%s allow: disabled by env" % _safe_sid(sid)[:8])
        return None
    if inp.get("stop_hook_active"):
        log("stop sid=%s allow: stop_hook_active" % _safe_sid(sid)[:8])
        return None
    tp = inp.get("transcript_path")
    if not tp or not os.path.isfile(str(tp)):
        log("stop sid=%s allow: no transcript (%s)" % (_safe_sid(sid)[:8], tp))
        return None
    turn = current_turn(str(tp))
    if not turn:
        log("stop sid=%s allow: no turn found in transcript" % _safe_sid(sid)[:8])
        return None
    turn_id = str(inp.get("prompt_id") or turn.get("uuid") or "")
    st = load_state(sid)
    if turn_id and st.get("last_block_turn") == turn_id:
        log("stop sid=%s allow: already blocked this turn" % _safe_sid(sid)[:8])
        return None
    now = time.time()
    if not st.get("session_start"):
        st["session_start"] = min(turn.get("ts") or now, now)
    cf = notes_file()
    exists = cf.exists()
    mtime = cf.stat().st_mtime if exists else 0.0
    tools = turn["tools"]
    kind = None
    reason = None

    compact_ts = st.get("compact_ts")
    if compact_ts:
        stale_at_compact = (compact_ts - mtime) / 60.0 if exists else 1e9
        if mtime >= compact_ts or stale_at_compact <= COMPACT_FRESH_MIN:
            for k in ("compact_ts", "compact_trigger", "compact_stale_min"):
                st.pop(k, None)
        else:
            kind = "compaction"
            reason = reason_compaction(cf, compact_ts, st.get("compact_trigger") or "auto", stale_at_compact)

    if reason is None and tools:
        age = now - mtime
        if not exists:
            kind, reason = "missing", reason_missing(cf, tools)
        elif mtime + SESSION_START_GRACE_SEC < st["session_start"]:
            kind, reason = "untouched", reason_untouched(cf, mtime, st["session_start"], tools)
        elif age / 60.0 > STALE_MIN:
            kind, reason = "stale", reason_stale(cf, age, mtime, tools)

    if reason is None:
        save_state(sid, st)
        log("stop sid=%s allow: tools=%d age=%s" % (_safe_sid(sid)[:8], len(tools), fmt_age(now - mtime) if exists else "missing"))
        return None

    st["last_block_turn"] = turn_id
    st["blocks"] = int(st.get("blocks") or 0) + 1
    st["last_block_at"] = now
    st["last_block_kind"] = kind
    save_state(sid, st)
    log("stop sid=%s BLOCK(%s): tools=%d age=%s" % (_safe_sid(sid)[:8], kind, len(tools), fmt_age(now - mtime) if exists else "missing"))
    short = {"stale": "%s is %s stale" % (cf.name, fmt_age(now - mtime)),
             "untouched": "%s untouched this session" % cf.name,
             "missing": "%s is missing" % cf.name,
             "compaction": "compaction ran while %s was stale" % cf.name}[kind]
    return {"decision": "block", "reason": reason,
            "systemMessage": "session-guard: turn end deferred once -- %s; asked Claude to update it." % short}


def do_precompact(inp):
    sid = inp.get("session_id") or "unknown"
    if disabled():
        log("precompact sid=%s skip: disabled by env" % _safe_sid(sid)[:8])
        return None
    trigger = str(inp.get("trigger") or "auto")
    st = load_state(sid)
    now = time.time()
    cf = notes_file()
    exists = cf.exists()
    mtime = cf.stat().st_mtime if exists else 0.0
    stale_min = (now - mtime) / 60.0 if exists else 1e9
    if trigger == "manual" and stale_min > STALE_MIN:
        last_bounce = float(st.get("compact_bounce_ts") or 0)
        if now - last_bounce > STALE_MIN * 60:
            st["compact_bounce_ts"] = now
            save_state(sid, st)
            log("precompact sid=%s BOUNCE manual: age=%s" % (_safe_sid(sid)[:8], fmt_age(now - mtime) if exists else "missing"))
            msg = ("session-guard: %s was last written %s ago (%s). Ask for a session-notes update first, then "
                   "run /compact again -- a second /compact within %d min proceeds without this check."
                   % (cf, fmt_age(now - mtime) if exists else "never", fmt_when(mtime), STALE_MIN))
            sys.stderr.write(msg + "\n")
            return {"decision": "block", "reason": msg}
    st["compact_ts"] = now
    st["compact_trigger"] = trigger
    st["compact_stale_min"] = round(min(stale_min, 1e6), 1)
    save_state(sid, st)
    log("precompact sid=%s record %s: age=%s%s" % (_safe_sid(sid)[:8], trigger, fmt_age(now - mtime) if exists else "missing",
                                                   " (will force update at next stop)" if stale_min > COMPACT_FRESH_MIN else ""))
    return None


def do_status(args):
    cf = notes_file()
    now = time.time()
    if cf.exists():
        m = cf.stat().st_mtime
        print("file:   %s  (last written %s, %s ago; stale threshold %d min, compact-fresh %d min)"
              % (cf, fmt_when(m), fmt_age(now - m), STALE_MIN, COMPACT_FRESH_MIN))
    else:
        print("file:   %s  (MISSING)" % cf)
    print("state:  %s  (disabled=%s)" % (state_dir(), disabled()))
    files = sorted(state_dir().glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[: int(args[0]) if args and args[0].isdigit() else 5]:
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            st = {}
        print("  %s  start=%s blocks=%s last=%s%s%s" % (
            f.stem[:8], fmt_when(st.get("session_start")), st.get("blocks", 0),
            (fmt_when(st.get("last_block_at")) + "(" + str(st.get("last_block_kind")) + ")") if st.get("last_block_at") else "-",
            "  compact_pending=" + fmt_when(st["compact_ts"]) if st.get("compact_ts") else "",
            "  bounced=" + fmt_when(st["compact_bounce_ts"]) if st.get("compact_bounce_ts") else ""))
    lg = state_dir() / "guard.log"
    if lg.exists():
        tail = lg.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
        print("log tail:")
        for ln in tail:
            print("  " + ln)
    return 0


def main(argv):
    cmd = (argv[1] if len(argv) > 1 else "").strip().lower()
    if cmd == "status":
        return do_status(argv[2:])
    if cmd not in ("stop", "precompact"):
        sys.stderr.write("usage: session_guard.py stop|precompact|status  (hook JSON on stdin)\n")
        return 0
    raw = ""
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        inp = json.loads(raw) if raw.strip() else {}
    except ValueError:
        inp = {}
    if not isinstance(inp, dict):
        inp = {}
    out = None
    try:
        prune_state()
        out = do_stop(inp) if cmd == "stop" else do_precompact(inp)
    except Exception as ex:          # fail open, never lock a session
        log("%s ERROR %r" % (cmd, ex))
        out = None
    if out:
        sys.stdout.write(json.dumps(out))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
