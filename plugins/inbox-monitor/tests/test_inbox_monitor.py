"""Offline checks for the inbox-monitor plugin: manifests, the stdout contract (one line per event,
nothing else), configuration errors, signature verdicts, the file follower, reconnect with backoff,
one inbox per session, and exit with the claude process or a closed stdout. The ntfy checks run
against a small stand-in server on 127.0.0.1; nothing leaves the machine and nothing under
~/.claude is touched (every run uses a temporary INBOX_STATE).

    python3 plugins/inbox-monitor/tests/test_inbox_monitor.py      (Windows: py ...)

Stdlib only, Python 3.8+. Exit code 0 when every check passed. Takes about 45 seconds.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
LAUNCHER = PLUGIN / "scripts" / "listen.py"
PY = sys.executable
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("%s %s%s" % ("ok  " if ok else "FAIL", name, (" -- " + str(detail)) if (detail and not ok) else ""))
    return ok


def clean_env(**extra):
    """The launcher reads INBOX_* and CLAUDE_* from its environment; never inherit a live session's."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "INBOX_"))}
    env.update(extra)
    return env


def run_for(seconds, env, args=()):
    p = subprocess.Popen([PY, str(LAUNCHER)] + list(args), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = p.communicate(timeout=seconds)
        return p.returncode, out or b"", err or b""
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        return None, out or b"", err or b""   # None = still running when the time was up


def load_module():
    spec = importlib.util.spec_from_file_location("inbox_listen", str(LAUNCHER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class StubNtfy(object):
    """A stand-in ntfy server: a subscription gets the queued events, then the connection stays open
    for `hold` seconds; a POST is recorded and answered like ntfy does."""

    def __init__(self, events=(), hold=20):
        self.events, self.hold, self.posts, self.gets = list(events), hold, [], []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                stub.gets.append((self.path, dict(self.headers)))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                try:
                    self.wfile.write(b'{"event":"open"}\n')
                    for ev in stub.events:
                        self.wfile.write((json.dumps(ev) + "\n").encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(stub.hold)
                except OSError:
                    pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
                stub.posts.append((self.path, dict(self.headers), body))
                ans = json.dumps({"id": "post1", "time": int(time.time()), "event": "message",
                                  "topic": self.path.strip("/"), "message": body}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(ans)))
                self.end_headers()
                self.wfile.write(ans)

        self.server = _ThreadedServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def main():
    state = Path(tempfile.mkdtemp(prefix="inbox-monitor-test-"))
    log_path = state / "monitor.log"

    def log_text():
        try:
            return log_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    # ---- manifests
    pj = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    check("plugin.json name/version/description/author", pj.get("name") == "inbox-monitor" and pj.get("version") == "0.1.0"
          and pj.get("description") and (pj.get("author") or {}).get("name"))
    mons = json.loads((PLUGIN / "monitors" / "monitors.json").read_text(encoding="utf-8"))
    check("monitors.json is a one-entry array", isinstance(mons, list) and len(mons) == 1)
    m = mons[0] if mons else {}
    check("monitor entry has only documented keys", set(m) <= {"name", "command", "description", "when"}
          and {"name", "command", "description"} <= set(m), sorted(m))
    check("monitor is named inbox with a short description", m.get("name") == "inbox" and 0 < len(m.get("description", "")) <= 40)
    cmd = m.get("command", "")
    check("command quotes ${CLAUDE_PLUGIN_ROOT}/scripts/listen.py in all three branches",
          cmd.count('"${CLAUDE_PLUGIN_ROOT}/scripts/listen.py"') == 3)
    check("command is the KIT_PYTHON || python3 || py chain", cmd.startswith('"${KIT_PYTHON:-python3}" "')
          and ' || python3 "' in cmd and ' || py "' in cmd and "exit" not in cmd)
    check("launcher exists", LAUNCHER.is_file())

    # ---- pure functions
    os.environ["INBOX_STATE"] = str(state)
    mod = load_module()

    # ---- log rotation while the handle stays open for the whole session
    mod.LOG_MAX = 2000
    for i in range(60):
        mod.log("rotation probe line %02d " % i + "x" * 80)
    rotated = (state / "monitor.log.1").exists()
    check("monitor.log rotates during a long-lived process (not only at open)",
          rotated and log_path.stat().st_size < 2400, "rotated=%s" % rotated)
    try:
        mod._log_fh.close()
    except Exception:
        pass
    mod._log_fh = None
    mod.LOG_MAX = 2 * 1024 * 1024
    for f in (log_path, state / "monitor.log.1"):
        try:
            f.unlink()
        except OSError:
            pass
    ev = {"id": "abc123", "topic": "alerts", "title": "ci/build", "message": "line one\r\n  line two  \n\nthree"}
    line = mod.render_ntfy(ev, "valid")
    check("render_ntfy format", line == "[inbox] from=ci/build topic=alerts sig=valid id=abc123 :: line one | line two | three", line)
    check("render_ntfy has no newline", "\n" not in line)
    long = mod.render_ntfy({"id": "x", "topic": "t", "title": "a", "message": "y" * (mod.MAX_LINE + 50)}, "unsigned")
    check("long bodies are cut", long.endswith(" ...[cut]") and len(long) < mod.MAX_LINE + 80)
    att = mod.render_ntfy({"id": "x", "topic": "t", "message": "", "attachment": {"name": "r.txt", "url": "http://h/r.txt"}}, "unsigned")
    check("attachment rendered", att.endswith(":: [attachment: r.txt http://h/r.txt]") and "from=? " in att, att)
    check("render_file format", mod.render_file("app.log", "  ERROR x\n") == "[inbox] file=app.log :: ERROR x")
    check("split_list splits and dedupes", mod.split_list(" a, b;a ,") == ["a", "b"])

    def cfg_err(env):
        try:
            mod.load_config(env)
            return ""
        except mod.ConfigError as ex:
            return str(ex)

    check("config: nothing set is an error", "no source configured" in cfg_err({}))
    check("config: both sources is an error", "choose one source" in cfg_err({"INBOX_NTFY_URL": "http://h", "INBOX_TOPICS": "a", "INBOX_FILE": "/x"}))
    check("config: topics without a server is an error", "INBOX_NTFY_URL is empty" in cfg_err({"INBOX_TOPICS": "a"}))
    check("config: a server without topics is an error", "INBOX_TOPICS is empty" in cfg_err({"INBOX_NTFY_URL": "http://h"}))
    check("config: non-http URL is an error", "must start with http" in cfg_err({"INBOX_NTFY_URL": "ntfy.sh", "INBOX_TOPICS": "a"}))
    check("config: bad topic name is an error", "invalid topic" in cfg_err({"INBOX_NTFY_URL": "http://h", "INBOX_TOPICS": "a b"}))
    check("config: INBOX_REQUIRE_SIG=on without a key is an error",
          "needs INBOX_SECRET" in cfg_err({"INBOX_NTFY_URL": "http://h", "INBOX_TOPICS": "a", "INBOX_REQUIRE_SIG": "on"}))
    check("config: unreadable INBOX_SECRET_FILE is an error",
          "cannot be read" in cfg_err({"INBOX_NTFY_URL": "http://h", "INBOX_TOPICS": "a", "INBOX_SECRET_FILE": str(state / "nope")}))
    kf = state / "key"
    kf.write_text("k3y\n", encoding="utf-8")
    c = mod.load_config({"INBOX_NTFY_URL": "http://h/", "INBOX_TOPICS": "a,b", "INBOX_SECRET_FILE": str(kf)})
    check("config: key file read, require_sig defaults on, URL trimmed",
          c["secret"] == "k3y" and c["require_sig"] and c["url"] == "http://h" and c["topics"] == ["a", "b"], c)
    c = mod.load_config({"INBOX_NTFY_URL": "http://h", "INBOX_TOPICS": "a", "INBOX_SECRET": "k", "INBOX_REQUIRE_SIG": "off"})
    check("config: INBOX_REQUIRE_SIG=off keeps the key but delivers everything", c["secret"] == "k" and not c["require_sig"])
    check("config: no key means require_sig off", not mod.load_config({"INBOX_NTFY_URL": "http://h", "INBOX_TOPICS": "a"})["require_sig"])
    check("config: file source", mod.load_config({"INBOX_FILE": "~/x.log"})["file"] == str(Path("~/x.log").expanduser()))

    now = int(time.time())
    sig, ts = mod.sign("k", "alerts", "hello", now)
    signed = {"id": "s1", "topic": "alerts", "title": "ci", "message": "hello", "time": now + 3,
              "tags": ["fsig=" + sig, "fts=%d" % ts]}
    check("sig_verdict valid", mod.sig_verdict(signed, "k") == "valid")
    check("sig_verdict valid inside the window even when read much later (catch-up after an outage)",
          mod.sig_verdict(signed, "k", now=now + 3600) == "valid")
    replay = dict(signed, time=now + mod.SIG_WINDOW + 60)
    check("sig_verdict valid-stale when the server received it outside the window (a re-published copy)",
          mod.sig_verdict(replay, "k") == "valid-stale")
    check("sig_verdict bad with another key", mod.sig_verdict(signed, "other") == "bad")
    check("sig_verdict bad when the text changed", mod.sig_verdict(dict(signed, message="hello!"), "k") == "bad")
    check("sig_verdict unsigned", mod.sig_verdict({"topic": "alerts", "message": "x"}, "k") == "unsigned")
    check("sig_verdict no-secret", mod.sig_verdict(signed, None) == "no-secret")

    got = []
    cfg = {"secret": "k", "require_sig": True, "ignore_from": ["me/cli"]}
    mod.handle_ntfy_event(dict(signed), cfg, out=got.append)
    mod.handle_ntfy_event({"id": "u1", "topic": "alerts", "title": "x", "message": "unsigned"}, cfg, out=got.append)
    mod.handle_ntfy_event(dict(signed, title="me/cli"), cfg, out=got.append)
    check("require_sig delivers the valid message only; INBOX_IGNORE_FROM skips own sends",
          len(got) == 1 and got[0].startswith("[inbox] from=ci topic=alerts sig=valid id=s1 :: hello"), got)

    # file follower, driven step by step
    f = state / "events.log"
    f.write_text("old line\n", encoding="utf-8")
    st, got = {}, []
    mod.follow_file(str(f), st, out=got.append, once=True)
    check("file: existing content is skipped (new lines only)", got == [], got)
    with open(str(f), "a", encoding="utf-8") as fh:
        fh.write("first\nsecond\r\npart")
    mod.follow_file(str(f), st, out=got.append, once=True)
    check("file: appended lines delivered, a line without newline waits",
          got == ["[inbox] file=events.log :: first", "[inbox] file=events.log :: second"], got)
    with open(str(f), "a", encoding="utf-8") as fh:
        fh.write("ial\n")
    mod.follow_file(str(f), st, out=got.append, once=True)
    check("file: the waiting line completes", got[-1:] == ["[inbox] file=events.log :: partial"], got)
    f.write_text("after truncation\n", encoding="utf-8")
    mod.follow_file(str(f), st, out=got.append, once=True)
    check("file: truncation starts over at byte 0", got[-1:] == ["[inbox] file=events.log :: after truncation"], got)
    f2 = state / "events.log.new"
    f2.write_text("rotated in, longer than before\n", encoding="utf-8")
    os.replace(str(f2), str(f))
    mod.follow_file(str(f), st, out=got.append, once=True)
    check("file: a replaced file is read from the start", got[-1:] == ["[inbox] file=events.log :: rotated in, longer than before"], got)
    g = state / "later.log"
    st2, got2 = {}, []
    mod.follow_file(str(g), st2, out=got2.append, once=True)
    g.write_text("born with a line\n", encoding="utf-8")
    mod.follow_file(str(g), st2, out=got2.append, once=True)
    check("file: a file that appears after the start is read from its first line",
          got2 == ["[inbox] file=later.log :: born with a line"], got2)
    os.environ.pop("INBOX_STATE", None)

    # ---- `where`
    r = subprocess.run([PY, str(LAUNCHER), "where"], capture_output=True, text=True,
                       env=clean_env(INBOX_STATE=str(state), INBOX_NTFY_URL="http://127.0.0.1:9", INBOX_TOPICS="a,b"))
    check("where prints the source", r.returncode == 0 and "source:    ntfy http://127.0.0.1:9 topics=a,b" in r.stdout, r.stdout + r.stderr)
    r = subprocess.run([PY, str(LAUNCHER), "where"], capture_output=True, text=True, env=clean_env(INBOX_STATE=str(state)))
    check("where explains a missing source", r.returncode == 0 and "source:    none - no source configured" in r.stdout, r.stdout)

    # ---- launcher contract
    base = dict(INBOX_STATE=str(state))
    rc, out, err = run_for(10, clean_env(**base))
    check("unconfigured: exit 0, nothing on stdout", rc == 0 and out == b"", "rc=%r out=%r" % (rc, out[:80]))
    check("unconfigured: reason logged", "no source configured" in log_text() and "exiting 0" in log_text())
    rc, out, err = run_for(10, clean_env(INBOX_NTFY_URL="http://127.0.0.1:9", INBOX_TOPICS="a", INBOX_FILE="/x", **base))
    check("both sources: exit 0, nothing on stdout, logged", rc == 0 and out == b"" and "choose one source" in log_text())

    rc, out, err = run_for(3, clean_env(INBOX_LISTEN="off", INBOX_NTFY_URL="http://127.0.0.1:9", INBOX_TOPICS="a", **base))
    check("INBOX_LISTEN=off: idles (still running after 3 s), nothing on stdout", rc is None and out == b"")
    check("INBOX_LISTEN=off: logged", "not listening" in log_text())

    rc, out, err = run_for(12, clean_env(INBOX_NTFY_URL="http://127.0.0.1:9", INBOX_TOPICS="a", **base))
    lt = log_text()
    check("server unreachable: keeps running, nothing on stdout or stderr", rc is None and out == b"" and err == b"",
          "rc=%r out=%r err=%r" % (rc, out[:80], err[:120]))
    check("server unreachable: reconnects with backoff 5 s then 10 s", "reconnecting in 5s" in lt and "reconnecting in 10s" in lt)

    sig2, ts2 = mod.sign("k", "alerts", "deploy finished", int(time.time()))
    stub = StubNtfy(events=[
        {"id": "m1", "time": int(time.time()), "event": "message", "topic": "alerts", "title": "ci",
         "message": "deploy finished", "tags": ["fsig=" + sig2, "fts=%d" % ts2]},
        {"id": "m2", "time": int(time.time()), "event": "keepalive", "topic": "alerts"},
        {"id": "m3", "time": int(time.time()), "event": "message", "topic": "alerts", "title": "stranger",
         "message": "unsigned text"},
    ])
    try:
        rc, out, err = run_for(4, clean_env(INBOX_NTFY_URL=stub.url, INBOX_TOPICS="alerts", INBOX_SECRET="k", **base))
        lines = out.decode("utf-8", "replace").splitlines()
        check("ntfy: exactly one line, the validly signed message", lines == ["[inbox] from=ci topic=alerts sig=valid id=m1 :: deploy finished"],
              repr(lines))
        check("ntfy: nothing on stderr", err == b"", err[:200])
        check("ntfy: the unsigned message was dropped and logged", "DROP id=m3" in log_text() and "sig=unsigned" in log_text())
        check("ntfy: subscribed with since=<now> on the right path",
              stub.gets and stub.gets[0][0].startswith("/alerts/json?since="), stub.gets[:1])

        r = subprocess.run([PY, str(LAUNCHER), "send", "alerts", "hello there", "--from", "tester"], capture_output=True, text=True,
                           env=clean_env(INBOX_NTFY_URL=stub.url, INBOX_SECRET="k", INBOX_NTFY_TOKEN="tk_x", **base), timeout=30)
        p = stub.posts[-1] if stub.posts else ("", {}, "")
        hdr = {k.lower(): v for k, v in p[1].items()}
        tags = dict(t.split("=", 1) for t in hdr.get("tags", "").split(",") if "=" in t)
        ev_back = {"topic": "alerts", "message": p[2], "time": int(time.time()), "tags": ["fsig=" + tags.get("fsig", ""), "fts=" + tags.get("fts", "")]}
        check("send: posts title, token and a signature that verifies",
              r.returncode == 0 and "sent id=post1 topic=alerts signed=yes" in r.stdout and p[0] == "/alerts"
              and hdr.get("title") == "tester" and hdr.get("authorization") == "Bearer tk_x"
              and mod.sig_verdict(ev_back, "k") == "valid", (r.stdout, r.stderr, p))
    finally:
        stub.close()

    tf = state / "watch.log"
    tf.write_text("before start\n", encoding="utf-8")
    p = subprocess.Popen([PY, str(LAUNCHER)], env=clean_env(INBOX_FILE=str(tf), **base), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(2.5)
    with open(str(tf), "a", encoding="utf-8") as fh:
        fh.write("ERROR one\nERROR two\n")
    time.sleep(2.5)
    p.kill()
    out, err = p.communicate()
    check("file source end to end: two new lines -> two notifications",
          out.decode("utf-8").splitlines() == ["[inbox] file=watch.log :: ERROR one", "[inbox] file=watch.log :: ERROR two"], out)

    sid = "test-session-%d" % os.getpid()
    env1 = clean_env(INBOX_NTFY_URL="http://127.0.0.1:9", INBOX_TOPICS="a", CLAUDE_CODE_SESSION_ID=sid, **base)
    p1 = subprocess.Popen([PY, str(LAUNCHER)], env=env1, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(2)
    rc2, out2, _ = run_for(3, env1)
    lock = state / "sessions" / (sid + ".pid")
    check("one inbox per session: the lock holds the first pid", lock.is_file() and lock.read_text().strip() == str(p1.pid))
    check("one inbox per session: a second copy idles silently", rc2 is None and out2 == b"" and "already serves" in log_text())
    p1.kill()
    p1.communicate()

    fake = subprocess.Popen([PY, "-c", "import time; time.sleep(2)"])   # stands in for the claude process
    p = subprocess.Popen([PY, str(LAUNCHER)], env=clean_env(INBOX_NTFY_URL="http://127.0.0.1:9", INBOX_TOPICS="a",
                                                            CLAUDE_PID=str(fake.pid), **base),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    fake.wait()               # reaped at once, as a shell reaps claude (a zombie still answers kill -0)
    t0 = time.time()
    try:
        out, err = p.communicate(timeout=15)
        check("exits once its claude pid is gone (within 15 s)", p.returncode == 0 and out == b"" and "is gone; exiting" in log_text(),
              "rc=%r after %.0fs" % (p.returncode, time.time() - t0))
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        check("exits once its claude pid is gone (within 15 s)", False, "still running")

    if os.name != "nt":
        p = subprocess.Popen([PY, str(LAUNCHER)], env=clean_env(INBOX_NTFY_URL="http://127.0.0.1:9", INBOX_TOPICS="a", **base),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(1)
        p.stdout.close()          # the reader goes away, as when the session ends
        t0 = time.time()
        try:
            p.wait(timeout=15)
            check("exits once stdout is closed (within 15 s)", p.returncode == 0 and "stdout pipe closed" in log_text(),
                  "rc=%r after %.0fs" % (p.returncode, time.time() - t0))
        except subprocess.TimeoutExpired:
            p.kill()
            check("exits once stdout is closed (within 15 s)", False, "still running")

    claude = shutil.which("claude")
    if claude:
        r = subprocess.run([claude, "plugin", "validate", str(PLUGIN), "--strict"], capture_output=True, text=True)
        check("claude plugin validate --strict", r.returncode == 0 and "passed" in (r.stdout + r.stderr).lower(), r.stdout + r.stderr)
    else:
        print("skip claude plugin validate (claude not on PATH)")

    shutil.rmtree(str(state), ignore_errors=True)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n%d checks, %d failed%s" % (len(RESULTS), len(failed), (": " + ", ".join(failed)) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
