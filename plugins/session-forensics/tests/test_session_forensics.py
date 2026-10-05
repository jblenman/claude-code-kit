"""Offline checks for the session-forensics scripts on synthetic transcripts (no real ~/.claude data is
read): find_session's reminder stripping, token_audit's columns and per-tool table (image tokens
from the header), thinking_retention's within-turn accounting, the record audit (notes / kb / memory
writes, asked vs unasked, wrap-ups, scripts written then run, subagent and headless exclusion,
configuration), announce's trailer count, turns.py, needles.py and prose.py.

    python3 plugins/session-forensics/tests/test_session_forensics.py      (Windows: py ...)

Stdlib only, Python 3.8+. Exit code 0 when every check passed.
"""
import base64
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
S = PLUGIN / "scripts"
PY = sys.executable
RESULTS = []
SID = "aaaa1111-2222-4333-8444-555566667777"
HEADLESS = "bbbb1111-2222-4333-8444-555566667777"


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("%s %s%s" % ("ok  " if ok else "FAIL", name, (" -- " + str(detail)[:600]) if (detail and not ok) else ""))
    return ok


def png_b64(w, h):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\x00" * (w * 3) for _ in range(h))
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return base64.b64encode(data).decode("ascii")


def user(ts, text, **kw):
    e = {"type": "user", "timestamp": ts, "version": "2.1.289", "entrypoint": kw.pop("entrypoint", "cli"),
         "message": {"role": "user", "content": text}}
    e.update(kw)
    return e


def asst(ts, content, ctx, out, entrypoint="cli"):
    return {"type": "assistant", "timestamp": ts, "version": "2.1.289", "entrypoint": entrypoint,
            "message": {"role": "assistant", "model": "claude-test-1", "content": content,
                        "usage": {"input_tokens": 10, "cache_read_input_tokens": ctx - 10, "cache_creation_input_tokens": 0,
                                  "output_tokens": out}}}


def tool_use(i, name, inp):
    return {"type": "tool_use", "id": "tu%d" % i, "name": name, "input": inp}


def result(ts, i, content):
    return {"type": "user", "timestamp": ts, "version": "2.1.289", "entrypoint": "cli",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu%d" % i, "content": content}]}}


def write_jsonl(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def build(root):
    proj = root / "-home-user-myproject"
    notes = "/home/user/myproject/session-context.md"
    ev = [
        {"customTitle": "parser-refactor", "sessionId": SID},
        user("2026-10-01T09:00:00Z", "<system-reminder>zebra is mentioned in memory</system-reminder>Refactor the parser module"),
        asst("2026-10-01T09:00:10Z", [{"type": "text", "text": "Looking."}, tool_use(1, "Read", {"file_path": "/home/user/myproject/parser.py"})], 1000, 50),
        result("2026-10-01T09:00:11Z", 1, "x" * 3700),
        asst("2026-10-01T09:00:20Z", [tool_use(2, "Write", {"file_path": notes, "content": "state"})], 2100, 300),
        result("2026-10-01T09:00:21Z", 2, "ok"),
        asst("2026-10-01T09:00:30Z", [tool_use(3, "Bash", {"command": "cat parser.py"})], 2300, 40),
        result("2026-10-01T09:00:31Z", 3, [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png_b64(750, 100)}}]),
        asst("2026-10-01T09:00:40Z", [{"type": "text", "text": "Done; notes updated.\n\nContext: session-context updated."}], 2500, 30),
        user("2026-10-01T12:00:00Z", "Now add tests for zebra parsing"),
        asst("2026-10-01T12:00:10Z", [tool_use(4, "Bash", {"command": "echo 'zebra' >> /home/user/kb/notes/parsing.md"})], 2600, 40),
        result("2026-10-01T12:00:11Z", 4, "ok"),
        asst("2026-10-01T12:00:20Z", [tool_use(5, "Write", {"file_path": "/tmp/upd.py", "content": "open('/home/user/myproject/session-context.md','a').write('x')"})], 2700, 40),
        result("2026-10-01T12:00:21Z", 5, "ok"),
        asst("2026-10-01T12:00:30Z", [tool_use(6, "Bash", {"command": "python3 /tmp/upd.py"})], 2800, 40),
        result("2026-10-01T12:00:31Z", 6, "ok"),
        asst("2026-10-01T12:00:40Z", [{"type": "text", "text": "Tests added."}], 2900, 20),
        user("2026-10-01T15:00:00Z", "Please update the session context and memory before we stop"),
        asst("2026-10-01T15:00:10Z", [tool_use(7, "Edit", {"file_path": "/home/user/.claude/projects/-home-user-myproject/memory/parser.md",
                                                           "old_string": "a", "new_string": "b"})], 3000, 40),
        result("2026-10-01T15:00:11Z", 7, "ok"),
        asst("2026-10-01T15:00:20Z", [{"type": "text", "text": "Memory updated."}], 3100, 20),
        {"type": "attachment", "timestamp": "2026-10-01T09:00:12Z",
         "attachment": {"type": "nested_memory", "path": "/home/user/myproject/sub/CLAUDE.md"}},
        {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-10-01T15:30:00Z"},
        dict(user("2026-10-01T15:30:01Z", "This session is being continued from a previous conversation."), isCompactSummary=True),
    ]
    write_jsonl(proj / (SID + ".jsonl"), ev)
    # a headless run (entrypoint sdk-cli) and a subagent transcript: left out of the record audit
    hv = [user("2026-10-01T10:00:00Z", "update the session context", entrypoint="sdk-cli")]
    for k in range(4):
        hv += [asst("2026-10-01T10:00:%02dZ" % (k * 10 + 5), [tool_use(10 + k, "Write", {"file_path": notes, "content": "h"})], 500, 10, entrypoint="sdk-cli"),
               user("2026-10-01T10:0%d:00Z" % (k + 1), "again %d" % k, entrypoint="sdk-cli")]
    write_jsonl(proj / (HEADLESS + ".jsonl"), hv)
    sub = [user("2026-10-01T11:00:0%dZ" % k, "subtask %d" % k) for k in range(4)]
    write_jsonl(proj / SID / "subagents" / "agent-x1.jsonl", sub)
    return proj


def run(script, *args):
    r = subprocess.run([PY, str(S / script)] + [str(a) for a in args], capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout, r.stderr


def main():
    tmp = Path(tempfile.mkdtemp(prefix="session-forensics-test-"))
    pj = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    check("plugin.json name/version/description/author", pj.get("name") == "session-forensics" and pj.get("version") == "0.1.0"
          and pj.get("description") and (pj.get("author") or {}).get("name"))
    skill = (PLUGIN / "skills" / "session-forensics" / "SKILL.md").read_text(encoding="utf-8")
    check("skill runs the scripts through ${CLAUDE_PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}/scripts/find_session.py" in skill
          and "${CLAUDE_PLUGIN_ROOT}/scripts/proactivity.py" in skill and "${CLAUDE_PLUGIN_ROOT}/scripts/token_audit.py" in skill)

    root = tmp / "projects"
    build(root)

    rc, out, err = run("find_session.py", "zebra", root)
    line = [l for l in out.splitlines() if SID[:8] in l]
    check("find_session: hits in real text only (the reminder copy is stripped), title and first prompt shown",
          rc == 0 and line and "hits(user/asst)=1/0" in line[0] and "title='parser-refactor'" in line[0]
          and "first='Refactor the parser module'" in line[0] and "user_turns=4" in line[0], out + err)
    check("find_session: no <== mark for one hit", line and not line[0].startswith("  <=="), line)
    rc, out, err = run("find_session.py", "nothing-here-xyz", tmp / "empty")
    check("find_session: an empty root says so", rc == 1 and "no transcripts" in err, err)

    rc, out, err = run("token_audit.py", "--root", root, "--list", "--min-turns", "1")
    row = [l for l in out.splitlines() if l.startswith(SID[:8])]
    cols = row[0].split() if row else []
    check("token_audit --list: turns, peak context, output, one image of 100 tokens, one compaction",
          rc == 0 and cols[3:12] == ["test-1", "10", "3", "1", "1", "0", "1", "0", "1"], (cols, out, err))
    rc, out, err = run("token_audit.py", "--root", root, "--session", SID[:6], "--min-turns", "1")
    check("token_audit --session: per-tool table with the image row and context quartiles",
          rc == 0 and "=== session %s parser-refactor" % SID[:8] in out and "Bash" in out and "Write" in out
          and "context at 25/50/75% of turns" in out and "compactions 1" in out, out + err)
    bash = [l for l in out.splitlines() if l.startswith("Bash ")]
    check("token_audit: image tokens = width x height / 750 (750x100 PNG -> 100)", bash and bash[0].split()[-1] == "100", bash)

    rc, out, err = run("thinking_retention.py", "--root", root, SID[:4])
    check("thinking_retention: within-turn steps and boundaries for the session",
          rc == 0 and out.startswith(SID[:8] + " test-1: within-turn steps") and "user-turn boundaries 2" in out, out + err)
    rc, out, err = run("thinking_retention.py", "--root", tmp / "missing")
    check("thinking_retention: a missing root is a clear error", rc == 2 and "not a directory" in err, err)

    sys.path.insert(0, str(S))
    import record_audit as ra
    cfg = ra.Config(kb_patterns=[r"/kb/"])
    check("config: default notes file and an ask pattern built from its name",
          cfg.notes_files == ["session-notes.md", "session-context.md"] and cfg.ask.search("please refresh the Session Context file") is not None
          and cfg.ask.search("update your session notes") is not None
          and cfg.ask.search("refactor the parser") is None)
    check("config: default memory patterns include the per-project auto-memory folder",
          cfg.t_mem.search("/home/u/.claude/projects/-x/memory/a.md") is not None)
    check("classify: Write path, read-only shell command ignored, writing shell command counted",
          ra.classify_write("Write", {"file_path": "/p/SESSION-CONTEXT.md"}, cfg) == {"notes"}
          and ra.classify_write("Bash", {"command": "cat session-context.md"}, cfg) == set()
          and ra.classify_write("Bash", {"command": "echo x >> /home/u/kb/a.md"}, cfg) == {"kb"})
    bodies = {}
    ra.script_writes("Write", {"file_path": "C:\\Temp\\claude\\s\\u.py", "content": "open('session-context.md','w').write('x')"}, bodies, cfg)
    check("script_writes: a script written with Write and then run counts its body",
          ra.script_writes("PowerShell", {"command": "py C:/Temp/claude/s/u.py"}, bodies, cfg) == {"notes"}
          and ra.script_writes("Bash", {"command": "py other.py"}, bodies, cfg) == set())
    check("version_band: lettered bands, exact version without bands",
          ra.version_band("2.1.230", ["2.1.228", "2.1.250"]) == "B 2.1.228..2.1.250"
          and ra.version_band("2.1.100", ["2.1.228"]) == "A <2.1.228" and ra.version_band("2.1.289", []) == "2.1.289")
    title, turns = ra.analyze(str(root / "-home-user-myproject" / (SID + ".jsonl")), cfg)
    check("analyze: three turns; notes unasked (turn 1, and turn 2 through the script), kb, then asked memory",
          title == "parser-refactor" and len(turns) == 3 and turns[0]["cats"] == {"notes"} and not turns[0]["asked"]
          and turns[1]["cats"] == {"notes", "kb"} and turns[2]["asked"] and turns[2]["cats"] == {"mem"}, turns)
    check("analyze: 3 h gaps make every turn a wrap-up (the last one is long idle)", [t["wrap"] for t in turns] == [True, True, True])
    check("analyze: headless runs and subagent transcripts are left out",
          ra.analyze(str(root / "-home-user-myproject" / (HEADLESS + ".jsonl")), cfg)[1] == []
          and ra.analyze(str(root / "-home-user-myproject" / SID / "subagents" / "agent-x1.jsonl"), cfg)[1] == [])

    rc, out, err = run("proactivity.py", root, "--kb", "/kb/")
    srow = [l for l in out.splitlines() if " %s " % SID[:8] in l]
    check("proactivity: per-session counts (notes 2 unasked, kb 1, mem asked 1, wrap 2 of 3 unasked)",
          rc == 0 and srow and "notes=2/0 kb=1/0 mem=0/1 asks=1 wrap=2/3" in srow[0], out + err)
    check("proactivity: the headless session is not listed; the asked prompt is listed",
          HEADLESS[:8] not in out and "Please update the session context and memory" in out, out)
    check("proactivity: the RECORD line shows the configuration", "RECORD: notes files: session-notes.md, session-context.md | kb: /kb/" in out, out[:300])
    cfgfile = tmp / "audit.json"
    cfgfile.write_text(json.dumps({"notes_files": ["NOTES.md"], "kb_patterns": ["/kb/"]}), encoding="utf-8")
    rc, out, err = run("proactivity.py", root, "--config", cfgfile)
    srow = [l for l in out.splitlines() if " %s " % SID[:8] in l]
    check("proactivity --config: another notes file name changes what counts",
          rc == 0 and srow and "notes=0/0 kb=1/0" in srow[0] and "notes files: NOTES.md" in out, out + err)

    rc, out, err = run("announce.py", root, "--trailer", "Context")
    check("announce: the unasked note write was announced, the trailer counted",
          rc == 0 and "wrote(any)=  2 said=  1" in out and "TRAILER 'Context:'" in out and "prompted=  3 trailer=  1" in out, out + err)
    rc, out, err = run("proact2.py", root, "--bands", "2.1.250")
    check("proact2: nested CLAUDE.md and compaction timeline, wrap-up splits", rc == 0 and " NESTED /home/user/myproject/sub/CLAUDE.md" in out
          and " COMPACT" in out and "compactions so far" in out and "B >=2.1.250" in out
          and any(l.startswith("nested CLAUDE.md live in context") and " yes " in l for l in out.splitlines()), out + err)
    rc, out, err = run("qshare.py", root)
    check("qshare: turn kinds", rc == 0 and "UNASKED TURNS BY KIND" in out
          and any(l.split()[:2] == ["directive", "n="] and l.split()[2] == "2" for l in out.splitlines()), out + err)
    rc, out, err = run("turns.py", root / "-home-user-myproject" / (SID + ".jsonl"), "2026-10-01T11", "--kb", "/kb/")
    check("turns.py: turns since the date with their writes", rc == 0 and "Now add tests" in out and "WRITE kb=" in out
          and "WRITE notes=python3 /tmp/upd.py" in out and "Refactor the parser" not in out, out + err)

    vers = tmp / "versions"
    vers.mkdir()
    (vers / "2.1.1").write_bytes(b"\x00junk Stop when the content stops.\x00")
    (vers / "2.1.2").write_bytes(b"\x00junk Stop when the content stops. Stop when the content stops. custom phrase here\x00")
    rc, out, err = run("needles.py", vers, "--needle", "mine=custom phrase here")
    row = [l for l in out.splitlines() if l.startswith("writing:stop when content stops")]
    mine = [l for l in out.splitlines() if l.startswith("mine")]
    check("needles: counts per build, custom needle added", rc == 0 and row and row[0].split()[-2:] == ["1", "2"]
          and mine and mine[0].split()[-2:] == ["0", "1"], out + err)
    binf = tmp / "bin.dat"
    binf.write_bytes(b"\x00\x01" + b"This is a long enough English sentence that the prose extractor should keep it." + b"\x00{var x = 1; return x;}\x00")
    rc, out, err = run("prose.py", binf)
    check("prose: keeps prose, drops code", rc == 0 and out.strip() == "This is a long enough English sentence that the prose extractor should keep it.", out)

    claude = shutil.which("claude")
    if claude:
        r = subprocess.run([claude, "plugin", "validate", str(PLUGIN), "--strict"], capture_output=True, text=True)
        check("claude plugin validate --strict", r.returncode == 0, r.stdout + r.stderr)

    shutil.rmtree(str(tmp), ignore_errors=True)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n%d checks, %d failed%s" % (len(RESULTS), len(failed), (": " + ", ".join(failed)) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
