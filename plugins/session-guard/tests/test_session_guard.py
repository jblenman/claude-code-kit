"""Self-contained tests for session_guard.py: builds a fake transcript + notes file in a temp dir and
runs the hook as a subprocess the way Claude Code does (JSON on stdin). Nothing under ~/.claude is
read or written.

    python3 plugins/session-guard/tests/test_session_guard.py [real-transcript.jsonl ...]   (Windows: py ...)

Exit code 0 = all scenarios behave as documented. Real transcript paths, if given, are only timed.
Also checks the plugin shape (manifest, hooks.json, the interpreter chain) and runs
`claude plugin validate --strict` when the claude CLI is on PATH.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent          # plugins/session-guard/tests
PLUGIN = HERE.parent
SCRIPT = str(PLUGIN / "scripts" / "session_guard.py")
PY = sys.executable


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".000Z"


class Fixture:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sgtest-"))
        self.transcript = self.dir / "transcript.jsonl"
        self.notes = self.dir / "session-notes.md"
        self.state = self.dir / "state"
        self.sid = "test-" + uuid.uuid4().hex[:8]
        self.lines = []

    def env(self, **extra):
        e = dict(os.environ)
        e.pop("CLAUDE_SESSION_GUARD", None)
        e.update({"CLAUDE_SESSION_NOTES": str(self.notes), "CLAUDE_SESSION_GUARD_STATE": str(self.state),
                  "CLAUDE_SESSION_NOTES_STALE_MIN": "30", "CLAUDE_SESSION_NOTES_COMPACT_FRESH_MIN": "10"})
        e.update(extra)
        return e

    def write_notes(self, age_sec):
        self.notes.write_text("# notes\n", encoding="utf-8")
        t = time.time() - age_sec
        os.utime(str(self.notes), (t, t))

    def turn(self, age_sec, tools=("Bash", "Read"), text="do the thing"):
        """Append a user prompt (age_sec ago) + assistant tool calls + results. Returns the prompt uuid."""
        u = str(uuid.uuid4())
        ts = time.time() - age_sec
        self.lines.append({"type": "user", "uuid": u, "timestamp": iso(ts),
                           "message": {"role": "user", "content": text}})
        for i, name in enumerate(tools):
            self.lines.append({"type": "assistant", "uuid": str(uuid.uuid4()), "timestamp": iso(ts + 1 + i),
                               "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t%d" % i, "name": name, "input": {}}]}})
            self.lines.append({"type": "user", "uuid": str(uuid.uuid4()), "timestamp": iso(ts + 2 + i),
                               "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t%d" % i, "content": "ok"}]}})
        self.lines.append({"type": "assistant", "uuid": str(uuid.uuid4()), "timestamp": iso(ts + 10),
                           "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}})
        self.transcript.write_text("\n".join(json.dumps(l) for l in self.lines) + "\n", encoding="utf-8")
        return u

    def run(self, cmd, stop_hook_active=False, trigger="auto", raw=None, **envextra):
        payload = raw if raw is not None else json.dumps({
            "session_id": self.sid, "transcript_path": str(self.transcript), "cwd": str(self.dir),
            "hook_event_name": "Stop" if cmd == "stop" else "PreCompact",
            "stop_hook_active": stop_hook_active, "trigger": trigger, "custom_instructions": None})
        p = subprocess.run([PY, SCRIPT, cmd], input=payload, capture_output=True, text=True, env=self.env(**envextra), timeout=60)
        assert p.returncode == 0, "exit %d stderr=%s" % (p.returncode, p.stderr)
        out = p.stdout.strip()
        return json.loads(out) if out else None


def git_bash():
    """Git for Windows, which Claude Code runs hook commands with: (bash.exe or usr/bin/sh.exe, usr/bin) or None."""
    roots = []
    hint = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH", "").strip()
    if hint:
        roots.append(Path(hint).parent.parent)
    git = shutil.which("git")
    if git:
        roots.extend(list(Path(git).resolve().parents)[:3])
    for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(var):
            roots.append(Path(os.environ[var]) / "Git")
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Git")
    for root in roots:
        usr_bin = root / "usr" / "bin"
        if (usr_bin / "sh.exe").is_file():
            bash = root / "bin" / "bash.exe"
            return (bash if bash.is_file() else usr_bin / "sh.exe"), usr_bin
    return None


def main():
    results = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))
        print(("PASS " if cond else "FAIL ") + name + ("  " + detail if detail and not cond else ""))

    # 1. stale file + tool-using turn -> block once, then allow within the same turn
    f = Fixture(); f.write_notes(45 * 60); f.turn(60 * 60)      # session started before the last write -> 'stale' rule
    r = f.run("stop"); check("stale+tools blocks", r and r.get("decision") == "block" and "min stale" in r["reason"], repr(r))
    check("block names what to record", r and "decisions" in r["reason"] and "open threads" in r["reason"])
    check("block carries systemMessage", r and "session-guard" in (r.get("systemMessage") or ""))
    r2 = f.run("stop"); check("same turn not blocked twice", r2 is None, repr(r2))
    r3 = f.run("stop", stop_hook_active=True); check("stop_hook_active allows", r3 is None)

    # 2. after the file is updated, a new turn passes
    f.write_notes(0); f.turn(30)
    check("fresh file allows", f.run("stop") is None)

    # 3. stale but no tools -> allow
    f = Fixture(); f.write_notes(45 * 60); f.turn(60, tools=())
    check("stale without tools allows", f.run("stop") is None)

    # 4. untouched since session start (file written before the first turn), even if < 30 min stale
    f = Fixture(); f.write_notes(20 * 60); f.turn(10 * 60)
    r = f.run("stop"); check("untouched-this-session blocks", r and "has not been written since this session" in r["reason"], repr(r))
    f.write_notes(0); f.turn(5)
    check("after first write, allows", f.run("stop") is None)

    # 4b. written within 5 min before the first turn counts as this session's (grace)
    f = Fixture(); f.write_notes(12 * 60); f.turn(10 * 60)
    check("write 2 min before first turn passes grace", f.run("stop") is None)

    # 5. missing file
    f = Fixture(); f.turn(60)
    r = f.run("stop"); check("missing file blocks", r and "does not exist" in r["reason"], repr(r))

    # 6. auto compaction while stale -> never blocks compaction; next stop (no tools) is blocked; update clears it
    f = Fixture(); f.write_notes(40 * 60); f.turn(60, tools=())
    check("auto precompact never blocks", f.run("precompact", trigger="auto") is None)
    r = f.run("stop"); check("post-compaction stop blocks even without tools", r and "compaction" in r["reason"], repr(r))
    f.write_notes(0); f.turn(5, tools=())
    check("after update, compaction marker cleared", f.run("stop") is None)
    st = json.loads(next(f.state.glob(f.sid + "*.json")).read_text())
    check("state has no compact_ts left", "compact_ts" not in st, repr(st))

    # 7. auto compaction while fresh (<10 min) -> no follow-up block
    f = Fixture(); f.write_notes(5 * 60); f.turn(60, tools=())
    f.run("precompact", trigger="auto")
    check("compaction while fresh needs no follow-up", f.run("stop") is None)

    # 8. manual /compact while stale -> bounced once, second proceeds and records
    f = Fixture(); f.write_notes(45 * 60); f.turn(60, tools=())
    r = f.run("precompact", trigger="manual"); check("manual compact bounced once", r and r.get("decision") == "block", repr(r))
    r = f.run("precompact", trigger="manual"); check("second manual compact proceeds", r is None, repr(r))
    r = f.run("stop"); check("then the next stop enforces the update", r and "compaction (manual)" in r["reason"], repr(r))

    # 9. manual /compact while fresh -> proceeds
    f = Fixture(); f.write_notes(2 * 60); f.turn(60)
    check("manual compact while fresh proceeds", f.run("precompact", trigger="manual") is None)

    # 10. env off, bad input, missing transcript -> silent allow
    f = Fixture(); f.write_notes(45 * 60); f.turn(60)
    check("CLAUDE_SESSION_GUARD=off allows", f.run("stop", CLAUDE_SESSION_GUARD="off") is None)
    check("garbage stdin allows", f.run("stop", raw="not json {") is None)
    f2 = Fixture(); f2.write_notes(45 * 60)
    check("missing transcript allows", f2.run("stop") is None)

    # 11. threshold override
    f = Fixture(); f.write_notes(8 * 60); f.turn(60)
    r = f.run("stop", CLAUDE_SESSION_NOTES_STALE_MIN="5"); check("STALE_MIN override respected", r and r.get("decision") == "block", repr(r))

    # 12. prompt_id preferred for the per-turn latch
    f = Fixture(); f.write_notes(45 * 60); f.turn(60)
    payload = {"session_id": f.sid, "transcript_path": str(f.transcript), "hook_event_name": "Stop", "stop_hook_active": False, "prompt_id": "P1"}
    r = f.run("stop", raw=json.dumps(payload)); check("prompt_id turn blocks", r and r.get("decision") == "block")
    r = f.run("stop", raw=json.dumps(payload)); check("same prompt_id latched", r is None)

    # 13. big transcript: the current turn sits past a 2 MB tail
    f = Fixture(); f.write_notes(45 * 60)
    f.lines.append({"type": "user", "uuid": "old", "timestamp": iso(time.time() - 3600), "message": {"role": "user", "content": "old prompt"}})
    f.turn(120, tools=("Bash",) * 3000, text="big turn")     # ~3000 tool calls after the prompt (> 2 MB)
    t0 = time.time(); r = f.run("stop"); dt = time.time() - t0
    check("large turn found past the first tail chunk (%.2fs, %d KB)" % (dt, f.transcript.stat().st_size // 1024), r and r.get("decision") == "block" and "3000 tool calls" in r["reason"], repr(r)[:200])

    # 14. status prints the file and thresholds
    f = Fixture(); f.write_notes(60)
    p = subprocess.run([PY, SCRIPT, "status"], capture_output=True, text=True, env=f.env(), timeout=60)
    check("status shows the notes file and thresholds", p.returncode == 0 and str(f.notes) in p.stdout and "stale threshold 30 min" in p.stdout, p.stdout + p.stderr)

    # 15. real transcript, if given: timing only
    for real in sys.argv[1:]:
        f = Fixture(); f.write_notes(0)
        payload = {"session_id": f.sid, "transcript_path": real, "hook_event_name": "Stop", "stop_hook_active": False}
        t0 = time.time(); f.run("stop", raw=json.dumps(payload)); dt = time.time() - t0
        check("real transcript %s parsed in %.2fs (%d MB)" % (Path(real).name[:8], dt, os.path.getsize(real) // 1_000_000), dt < 5)

    # 16. the plugin around the script
    m = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    check("plugin.json: name session-guard, version, description, author", m.get("name") == "session-guard" and all(k in m for k in ("version", "description", "author")))
    check("no CLAUDE.md at the plugin root; session-notes skill present", not (PLUGIN / "CLAUDE.md").exists() and (PLUGIN / "skills" / "session-notes" / "SKILL.md").is_file())
    hk = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    check("hooks.json: exactly Stop and PreCompact, no matcher", set(hk) == {"Stop", "PreCompact"} and all("matcher" not in hk[e][0] for e in hk))
    for event, sub, timeout in (("Stop", "stop", 20), ("PreCompact", "precompact", 10)):
        h = hk[event][0]["hooks"][0]
        call = '"${CLAUDE_PLUGIN_ROOT}/scripts/session_guard.py" %s' % sub
        want = '"${KIT_PYTHON:-python3}" %s || python3 %s || py %s || exit 1' % (call, call, call)
        check("hooks.json %s: shell-form KIT_PYTHON||python3||py||exit 1 chain, timeout %d, statusMessage" % (event, timeout),
              h.get("type") == "command" and h.get("command") == want and h.get("timeout") == timeout and h.get("statusMessage") and "args" not in h, h.get("command"))

    # 17. the || chain under the shell Claude Code uses: one guard.log line per run
    chain = hk["Stop"][0]["hooks"][0]["command"]
    if os.name == "nt":
        gb = git_bash()
        shell = str(gb[0]) if gb else None
        if not gb:
            print("SKIP chain checks: Git Bash not found (Claude Code runs hooks with it on Windows)")
    else:
        shell = "sh" if shutil.which("sh") else None
    if shell:
        f = Fixture(); f.write_notes(45 * 60); f.turn(60)
        glog = f.state / "guard.log"
        payload = json.dumps({"session_id": f.sid, "transcript_path": str(f.transcript), "hook_event_name": "Stop", "stop_hook_active": False})
        base = dict(f.env(), CLAUDE_PLUGIN_ROOT=PLUGIN.as_posix())
        base.pop("KIT_PYTHON", None)
        if os.name == "nt":
            pybin = f.dir / "pybin"
            pybin.mkdir()
            (pybin / "python3").write_bytes(("#!/bin/sh\nexec %s \"$@\"\n" % shlex.quote(Path(PY).as_posix())).encode("utf-8"))
            os.chmod(str(pybin / "python3"), 0o755)
            base["PATH"] = str(pybin) + os.pathsep + str(gb[1]) + os.pathsep + base.get("PATH", "")

        def chain_run(e):
            before = glog.read_text(encoding="utf-8").count("\n") if glog.exists() else 0
            p = subprocess.run([shell, "-c", chain], input=payload, capture_output=True, text=True, env=e, timeout=60)
            runs = (glog.read_text(encoding="utf-8").count("\n") if glog.exists() else 0) - before
            out = json.loads(p.stdout) if p.stdout.strip() else {}
            return p.returncode, out, runs, p.stdout + p.stderr

        rc, out, runs, detail = chain_run(base)
        check("chain: KIT_PYTHON unset -> one run, block JSON, exit 0", rc == 0 and out.get("decision") == "block" and runs == 1, "runs=%d %s" % (runs, detail))
        rc, out, runs, detail = chain_run(dict(base, KIT_PYTHON=str(f.dir / "no-such-python")))   # new turn id not needed: the latch allows silently
        check("chain: KIT_PYTHON pointing nowhere -> the next interpreter runs once (latched allow), exit 0", rc == 0 and runs == 1, "runs=%d %s" % (runs, detail))
        marker = f.dir / "kit-python.log"
        kitpy = f.dir / "kitpy"
        kitpy.write_bytes(("#!/bin/sh\necho run >> %s\nexec %s \"$@\"\n" % (shlex.quote(marker.as_posix()), shlex.quote(Path(PY).as_posix()))).encode("utf-8"))
        os.chmod(str(kitpy), 0o755)
        rc, out, runs, detail = chain_run(dict(base, KIT_PYTHON=kitpy.as_posix()))
        used = marker.read_text(encoding="utf-8").count("run") if marker.exists() else 0
        check("chain: KIT_PYTHON set -> that interpreter runs the guard once", rc == 0 and runs == 1 and used == 1, "runs=%d used=%d %s" % (runs, used, detail))

    # 18. claude plugin validate, when the CLI is here
    claude = shutil.which("claude")
    if claude:
        p = subprocess.run([claude, "plugin", "validate", str(PLUGIN), "--strict"], capture_output=True, text=True, timeout=120)
        check("claude plugin validate --strict", p.returncode == 0 and "Validation passed" in p.stdout, p.stdout + p.stderr)
    else:
        print("SKIP claude plugin validate (claude not on PATH)")

    failed = [n for n, ok, _ in results if not ok]
    print("\n%d/%d passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
