"""inbox-monitor: stream outside events into a Claude Code session, one line per event.

Claude Code starts this script as the plugin's `inbox` monitor (monitors/monitors.json) when an
interactive session starts, and turns every line it writes to stdout into a notification for the
model. While the session is idle a notification starts a turn by itself. So stdout carries exactly
one line per event and nothing else; diagnostics go to <state>/monitor.log.

Choose ONE source with environment variables of the `claude` process (a settings.json `env` block or
an export before `claude`):

  ntfy  INBOX_NTFY_URL   server, e.g. https://ntfy.example.org
        INBOX_TOPICS     comma-separated topics to subscribe to
        INBOX_NTFY_TOKEN optional access token for protected topics (sent as a Bearer header)
        INBOX_SECRET     optional HMAC-SHA256 key (or INBOX_SECRET_FILE = a file holding it):
                         verify signed messages (tags fsig=<hex>, fts=<unix-ts>; payload
                         "<ts>\\n<topic>\\n<message>")
        INBOX_REQUIRE_SIG on/off; default on when a key is set: deliver only validly signed messages
        INBOX_IGNORE_FROM comma-separated sender titles to skip (your own sends, for example)
        line:  [inbox] from=<title> topic=<topic> sig=<verdict> id=<id> :: <message>

  file  INBOX_FILE       a text file to follow: new lines only, survives rotation and truncation
        line:  [inbox] file=<name> :: <line>

  INBOX_LISTEN=off       this session does not listen: the monitor idles silently
  INBOX_STATE            state and log directory (default ~/.claude/inbox-monitor)

Lifetime: the ntfy stream reconnects with backoff (5 s doubling to 60 s) and resumes after the last
message it saw; the file follower polls once a second. A bug inside either loop restarts it with
the same backoff. The process exits when the `claude` process that started it (CLAUDE_PID) is gone
or its stdout pipe closes, so no listener outlives its session. One inbox per session: a second copy
for the same CLAUDE_CODE_SESSION_ID (a plugin reload can start monitors again) idles silently. A
missing or contradictory configuration is logged and the script exits 0 without printing; Claude
Code then shows "Monitor ... stream ended" once and the reason is the last START line in the log.

Subcommands:
  listen.py where                      print the resolved configuration and exit
  listen.py send TOPIC MESSAGE [--from NAME] [--url URL]
                                       publish one message to the ntfy server (signed when a key is set)

Stdlib only, Python 3.8+.
"""
import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_MAX = 2 * 1024 * 1024
MAX_LINE = 6000          # longer bodies are cut; ntfy's own message limit is 4096 bytes
MAX_BACKOFF = 60
WATCH_EVERY = 5.0
READ_TIMEOUT = 120       # ntfy sends a keepalive every 45 s; silence past this means a dead socket
SIG_WINDOW = 300         # seconds between the signature's timestamp and the server's receive time
FILE_POLL = 1.0
TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
OFF = ("off", "0", "no", "false")
ON = ("on", "1", "yes", "true")

_log_fh = None


def state_dir():
    v = os.environ.get("INBOX_STATE", "").strip()
    return Path(v).expanduser() if v else Path.home() / ".claude" / "inbox-monitor"


# ---------------------------------------------------------------- logging (never stdout)

def _open_log():
    global _log_fh
    if _log_fh is not None:
        return _log_fh
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / "monitor.log"
        try:
            if path.exists() and path.stat().st_size > LOG_MAX:
                os.replace(str(path), str(path.with_suffix(".log.1")))
        except OSError:
            pass
        _log_fh = open(str(path), "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        _log_fh = sys.__stderr__
    return _log_fh


def _rotate_if_needed():
    """The handle lives as long as the process (a whole session), so the size check at open time
    is not enough: check before each write and reopen past LOG_MAX (review finding, 2026-10-04)."""
    global _log_fh
    fh = _log_fh
    if fh is None or fh is sys.__stderr__:
        return
    try:
        if fh.tell() > LOG_MAX:
            fh.close()
            _log_fh = None
    except (OSError, ValueError):
        _log_fh = None


def log(msg):
    _rotate_if_needed()
    fh = _open_log()
    try:
        fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "inbox[%d] " % os.getpid() + msg.replace("\n", " | ") + "\n")
        fh.flush()
    except Exception:
        pass


# ---------------------------------------------------------------- configuration

class ConfigError(Exception):
    pass


def split_list(raw):
    out = []
    for part in (raw or "").replace(";", ",").split(","):
        p = part.strip()
        if p and p not in out:
            out.append(p)
    return out


def load_config(env=None):
    """The source and its options from the environment. Raises ConfigError with a reason."""
    env = os.environ if env is None else env
    url = env.get("INBOX_NTFY_URL", "").strip().rstrip("/")
    topics = split_list(env.get("INBOX_TOPICS", ""))
    path = env.get("INBOX_FILE", "").strip()
    if url and path:
        raise ConfigError("both INBOX_NTFY_URL and INBOX_FILE are set; choose one source")
    if not url and not path:
        if topics:
            raise ConfigError("INBOX_TOPICS is set but INBOX_NTFY_URL is empty")
        raise ConfigError("no source configured: set INBOX_NTFY_URL and INBOX_TOPICS, or INBOX_FILE")
    if path:
        return {"source": "file", "file": str(Path(path).expanduser())}
    if not url.startswith(("http://", "https://")):
        raise ConfigError("INBOX_NTFY_URL must start with http:// or https:// (got %r)" % url)
    if not topics:
        raise ConfigError("INBOX_TOPICS is empty: name at least one topic")
    bad = [t for t in topics if not TOPIC_RE.match(t)]
    if bad:
        raise ConfigError("invalid topic name(s) %s: letters, digits, - and _ only, at most 64 characters" % bad)
    secret, secret_from = env.get("INBOX_SECRET", "").strip(), "INBOX_SECRET"
    sfile = env.get("INBOX_SECRET_FILE", "").strip()
    if not secret and sfile:
        try:
            secret = Path(sfile).expanduser().read_text(encoding="utf-8").strip()
            secret_from = "INBOX_SECRET_FILE"
        except OSError as ex:
            raise ConfigError("INBOX_SECRET_FILE cannot be read: %s" % ex)
        if not secret:
            raise ConfigError("INBOX_SECRET_FILE is empty")
    req = env.get("INBOX_REQUIRE_SIG", "").strip().lower()
    if req and req not in ON + OFF:
        raise ConfigError("INBOX_REQUIRE_SIG must be on or off (got %r)" % req)
    require = (req in ON) if req else bool(secret)
    if require and not secret:
        raise ConfigError("INBOX_REQUIRE_SIG=on needs INBOX_SECRET or INBOX_SECRET_FILE")
    return {"source": "ntfy", "url": url, "topics": topics, "token": env.get("INBOX_NTFY_TOKEN", "").strip(),
            "secret": secret or None, "secret_from": secret_from if secret else None, "require_sig": require,
            "ignore_from": split_list(env.get("INBOX_IGNORE_FROM", ""))}


def listen_off(env=None):
    env = os.environ if env is None else env
    return env.get("INBOX_LISTEN", "").strip().lower() in OFF


# ---------------------------------------------------------------- signatures

def sign(secret, topic, message, ts=None):
    """-> (hex signature, ts). Payload: "<ts>\\n<topic>\\n<message>", HMAC-SHA256, UTF-8."""
    ts = int(time.time()) if ts is None else int(ts)
    payload = "%d\n%s\n%s" % (ts, topic, message)
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest(), ts


def sig_verdict(ev, secret, now=None):
    """'valid' (HMAC matches and the signature's timestamp is within SIG_WINDOW of the time the server
    received the message), 'valid-stale' (matches, outside the window: a re-published copy), 'bad',
    'unsigned', or 'no-secret' (signed, but no key here to check it)."""
    fsig = fts = None
    for t in (ev.get("tags") or []):
        if isinstance(t, str) and t.startswith("fsig="):
            fsig = t[5:]
        elif isinstance(t, str) and t.startswith("fts="):
            fts = t[4:]
    if not fsig or not fts:
        return "unsigned"
    if not secret:
        return "no-secret"
    try:
        ts = int(fts)
    except (TypeError, ValueError):
        return "bad"
    expected, _ = sign(secret, ev.get("topic", ""), ev.get("message", ""), ts)
    if not hmac.compare_digest(expected, fsig):
        return "bad"
    ref = ev.get("time")
    if not isinstance(ref, (int, float)):
        ref = time.time() if now is None else now
    return "valid" if abs(ref - ts) <= SIG_WINDOW else "valid-stale"


# ---------------------------------------------------------------- output to the model

class ModelStdout(object):
    """stdout as the model sees it: UTF-8, flushed per line, and the process ends the moment the pipe
    to Claude Code is gone (a listener must never outlive its session)."""

    def __init__(self, raw):
        self.raw = raw

    def write(self, s):
        try:
            self.raw.write(s.encode("utf-8", "replace"))
        except (OSError, ValueError):
            log("stdout closed; exiting")
            os._exit(0)
        return len(s)

    def flush(self):
        try:
            self.raw.flush()
        except (OSError, ValueError):
            log("stdout closed; exiting")
            os._exit(0)


def compact(text):
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = " | ".join(part.strip() for part in text.split("\n") if part.strip())
    if len(text) > MAX_LINE:
        text = text[:MAX_LINE] + " ...[cut]"
    return text


def render_ntfy(ev, sig):
    body = ev.get("message") or ""
    att = ev.get("attachment") or {}
    if isinstance(att, dict) and att.get("url"):
        body = (body + " " if body else "") + "[attachment: %s %s]" % (att.get("name") or "file", att.get("url"))
    return "[inbox] from=%s topic=%s sig=%s id=%s :: %s" % (
        ev.get("title") or "?", ev.get("topic") or "?", sig, ev.get("id") or "?", compact(body))


def render_file(name, text):
    return "[inbox] file=%s :: %s" % (name, compact(text))


def emit(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------- sources

def ntfy_request(cfg, path, data=None, headers=None):
    h = dict(headers or {})
    if cfg.get("token"):
        h["Authorization"] = "Bearer " + cfg["token"]
    return urllib.request.Request(cfg["url"] + path, data=data, headers=h)


def handle_ntfy_event(ev, cfg, out=emit):
    """One ntfy message -> one line, or a logged skip. Returns the line or None."""
    sender = ev.get("title") or ""
    if sender and sender in cfg["ignore_from"]:
        log("SKIP id=%s from=%s (INBOX_IGNORE_FROM)" % (ev.get("id"), sender))
        return None
    sig = sig_verdict(ev, cfg["secret"])
    if cfg["require_sig"] and sig != "valid":
        log("DROP id=%s topic=%s from=%s sig=%s (INBOX_REQUIRE_SIG)" % (ev.get("id"), ev.get("topic"), sender, sig))
        return None
    line = render_ntfy(ev, sig)
    out(line)
    log("DELIVER id=%s topic=%s from=%s sig=%s" % (ev.get("id"), ev.get("topic"), sender, sig))
    return line


def follow_ntfy(cfg, state, sleep=time.sleep):
    """Stream the topics forever; resumes after the last message id across reconnects."""
    backoff = 5
    while True:
        since = state.get("since") or str(int(time.time()))
        path = "/%s/json?since=%s" % (",".join(cfg["topics"]), urllib.parse.quote(since))
        try:
            with urllib.request.urlopen(ntfy_request(cfg, path), timeout=READ_TIMEOUT) as stream:
                backoff = 5
                log("connected %s topics=%s since=%s" % (cfg["url"], ",".join(cfg["topics"]), since))
                for raw in stream:
                    try:
                        ev = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(ev, dict) or ev.get("event") != "message":
                        continue
                    state["since"] = ev.get("id") or state.get("since")
                    handle_ntfy_event(ev, cfg)
            log("stream closed by the server - reconnecting in %ds" % backoff)
        except urllib.error.HTTPError as ex:
            hint = " (check INBOX_NTFY_TOKEN and the topic's access rules)" if ex.code in (401, 403) else ""
            log("stream error (HTTP %s%s) - reconnecting in %ds" % (ex.code, hint, backoff))
        except Exception as ex:
            log("stream error (%s: %s) - reconnecting in %ds" % (type(ex).__name__, ex, backoff))
        sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)


def follow_file(path, state, out=emit, sleep=time.sleep, once=False):
    """Follow `path` like `tail -F -n 0`: new complete lines only (a file that appears after the start is
    read from its first line), rotation (new inode) and truncation start over at byte 0. The file is
    opened per read, so a writer can rotate it at any time (no handle is held between polls)."""
    name = os.path.basename(path)
    while True:
        try:
            st = os.stat(path)
        except OSError:
            if "pos" not in state:
                state.update(pos=0, ino=None, buf=b"")      # appears later: every line in it is new
                log("waiting for %s to appear" % path)
            if once:
                return
            sleep(FILE_POLL)
            continue
        if "pos" not in state:
            state.update(pos=st.st_size, ino=st.st_ino, buf=b"")
            log("following %s from byte %d" % (path, st.st_size))
        elif st.st_ino != state["ino"] or st.st_size < state["pos"]:
            log("%s was %s; reading it from the start" % (path, "replaced" if st.st_ino != state["ino"] else "truncated"))
            state.update(pos=0, ino=st.st_ino, buf=b"")
        if st.st_size > state["pos"]:
            try:
                with open(path, "rb") as fh:
                    fh.seek(state["pos"])
                    chunk = fh.read(min(st.st_size - state["pos"], 1 << 20))
            except OSError as ex:
                log("cannot read %s: %s" % (path, ex))
                chunk = b""
            if chunk:
                state["pos"] += len(chunk)
                parts = (state["buf"] + chunk).split(b"\n")
                state["buf"] = parts.pop()                  # a line without its newline waits for it
                if len(state["buf"]) > 4 * MAX_LINE:        # a runaway line without newlines: deliver it cut
                    parts.append(state["buf"])
                    state["buf"] = b""
                for raw in parts:
                    text = raw.decode("utf-8", "replace").strip()
                    if text:
                        out(render_file(name, text))
                        log("DELIVER file=%s bytes=%d" % (name, len(raw)))
                continue                                    # more may be waiting: read again at once
        if once:
            return
        sleep(FILE_POLL)


# ---------------------------------------------------------------- parent watchdog, one inbox per session

def _pid_alive(pid):
    if os.name == "nt":
        # os.kill(pid, 0) would TERMINATE the process on Windows; ask the kernel instead
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        except Exception:
            return True  # cannot tell: keep running rather than drop the inbox
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _stdout_gone():
    """POSIX: the pipe to Claude Code reports POLLERR/POLLHUP on its write end once the reader is gone,
    which a dead-but-unreaped `claude` (a zombie still answers os.kill(pid, 0)) does not hide."""
    if os.name == "nt":
        return False
    try:
        import select
        p = select.poll()
        p.register(sys.__stdout__.fileno(), select.POLLERR | select.POLLHUP)
        # POLLNVAL is ignored on purpose: macOS reports it for /dev/null and other devices it cannot poll
        return any(ev & (select.POLLERR | select.POLLHUP) for _, ev in p.poll(0))
    except Exception:
        return False


def watch_parent(pid):
    def run():
        while True:
            time.sleep(WATCH_EVERY)
            if pid and not _pid_alive(pid):
                log("claude process %d is gone; exiting" % pid)
                os._exit(0)
            if _stdout_gone():
                log("stdout pipe closed (session ended); exiting")
                os._exit(0)
    threading.Thread(target=run, name="parent-watchdog", daemon=True).start()


def claude_pid():
    try:
        pid = int(os.environ.get("CLAUDE_PID", ""))
        return pid if pid > 1 else None
    except ValueError:
        return None


def _is_listener(pid):
    """Does `pid` run this script? Guards the session lock against a recycled pid."""
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/fi", "PID eq %d" % pid, "/fo", "csv", "/nh"],
                                 capture_output=True, text=True, timeout=10).stdout.lower()
            return "python" in out or "py.exe" in out
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout
        return "listen.py" in out
    except Exception:
        return True


def claim_session(session):
    """Claude Code may start a plugin's monitors again on a plugin reload without stopping the running
    one, which would deliver every event twice. The first copy for a session id records its pid; a
    later one that finds that pid alive and still a listener stands down. Returns the pid already
    serving the session, or None when this copy should serve."""
    if session in ("", "-"):
        return None
    try:
        d = state_dir() / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        f = d / (re.sub(r"[^-_A-Za-z0-9]", "_", session) + ".pid")
        try:
            other = int(f.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            other = None
        if other and other != os.getpid() and _pid_alive(other) and _is_listener(other):
            return other
        f.write_text("%d\n" % os.getpid(), encoding="utf-8")
        for old in d.glob("*.pid"):                   # locks of sessions that are gone
            if old == f:
                continue
            try:
                p = int(old.read_text(encoding="utf-8").strip() or "0")
                if not p or not _pid_alive(p):
                    old.unlink()
            except (OSError, ValueError):
                pass
    except Exception as ex:
        log("session lock skipped: %r" % (ex,))
    return None


def idle_forever():
    while True:
        time.sleep(3600)


# ---------------------------------------------------------------- subcommands

def cmd_where():
    print("launcher:  %s" % (HERE / "listen.py"))
    print("python:    %s (%s)" % (sys.executable, sys.version.split()[0]))
    print("listen:    %s" % ("off (INBOX_LISTEN)" if listen_off() else "on"))
    try:
        cfg = load_config()
    except ConfigError as ex:
        print("source:    none - %s" % ex)
    else:
        if cfg["source"] == "file":
            print("source:    file %s (%s)" % (cfg["file"], "exists" if os.path.exists(cfg["file"]) else "not there yet"))
        else:
            print("source:    ntfy %s topics=%s" % (cfg["url"], ",".join(cfg["topics"])))
            print("token:     %s" % ("set" if cfg["token"] else "none"))
            print("signature: %s; deliver only valid: %s" % (
                ("key from " + cfg["secret_from"]) if cfg["secret"] else "no key", "on" if cfg["require_sig"] else "off"))
            print("ignore:    %s" % (", ".join(cfg["ignore_from"]) or "-"))
    print("log:       %s" % (state_dir() / "monitor.log"))
    return 0


def cmd_send(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="listen.py send", description="Publish one message to the ntfy server.")
    ap.add_argument("topic")
    ap.add_argument("message")
    ap.add_argument("--from", dest="sender", default="cli", help="sender title shown as from= (default: cli)")
    ap.add_argument("--url", help="server (default: INBOX_NTFY_URL)")
    a = ap.parse_args(argv)
    env = dict(os.environ)
    if a.url:
        env["INBOX_NTFY_URL"] = a.url
    env.pop("INBOX_FILE", None)
    env["INBOX_TOPICS"] = a.topic
    try:
        cfg = load_config(env)
    except ConfigError as ex:
        print("cannot send: %s" % ex, file=sys.stderr)
        return 2
    headers = {"Title": a.sender}
    if cfg["secret"]:
        sig, ts = sign(cfg["secret"], a.topic, a.message)
        headers["Tags"] = "fsig=%s,fts=%d" % (sig, ts)
    req = ntfy_request(cfg, "/" + a.topic, data=a.message.encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            ans = json.loads(r.read().decode("utf-8") or "{}")
    except Exception as ex:
        print("send failed: %s: %s" % (type(ex).__name__, ex), file=sys.stderr)
        return 1
    print("sent id=%s topic=%s signed=%s" % (ans.get("id", "?"), a.topic, "yes" if cfg["secret"] else "no"))
    return 0


# ---------------------------------------------------------------- main

def serve(cfg):
    """Run the configured source forever; a bug inside it restarts it with backoff."""
    state = {}
    backoff = 5
    while True:
        began = time.time()
        try:
            if cfg["source"] == "file":
                follow_file(cfg["file"], state)
            else:
                follow_ntfy(cfg, state)
            log("source loop returned; restarting in %ds" % backoff)
        except KeyboardInterrupt:
            log("interrupted; exiting")
            return 0
        except BaseException as ex:  # a bug must not end the inbox
            log("source loop raised %s: %s; restarting in %ds" % (type(ex).__name__, ex, backoff))
        if time.time() - began > 60:
            backoff = 5
        time.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)


def main(argv):
    if len(argv) > 1 and argv[1] == "where":
        return cmd_where()
    if len(argv) > 1 and argv[1] == "send":
        return cmd_send(argv[2:])
    if len(argv) > 1:
        print(__doc__)
        return 0 if argv[1] in ("-h", "--help", "help") else 2

    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "") or "-"
    pid = claude_pid()
    watch_parent(pid)
    if listen_off():
        log("START session=%s: INBOX_LISTEN=off, not listening; idling until the session ends" % session)
        idle_forever()
    other = claim_session(session)
    if other:
        log("START session=%s: pid %d already serves this session's inbox; this copy idles" % (session, other))
        idle_forever()
    try:
        cfg = load_config()
    except ConfigError as ex:
        log("START session=%s: %s; exiting 0 (the monitor ends)" % (session, ex))
        return 0
    if cfg["source"] == "file":
        log("START session=%s claude_pid=%s source=file %s" % (session, pid, cfg["file"]))
    else:
        log("START session=%s claude_pid=%s source=ntfy %s topics=%s key=%s require_sig=%s" % (
            session, pid, cfg["url"], ",".join(cfg["topics"]), "yes" if cfg["secret"] else "no",
            "on" if cfg["require_sig"] else "off"))
    sys.stdout = ModelStdout(sys.__stdout__.buffer)
    sys.stderr = _open_log()            # anything stray lands in the log, never in the model's stream
    return serve(cfg)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except BaseException as ex:          # never exit non-zero: the monitor's `||` chain would start a second copy
        log("fatal %s: %s; exiting 0" % (type(ex).__name__, ex))
        sys.exit(0)
