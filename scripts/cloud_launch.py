"""Start a Claude Code cloud session (`claude --cloud`) from a context that has no terminal.

    python3 cloud_launch.py --cwd DIR --log FILE [--settle 90] [--max 420] [--env KEY=VALUE ...] [--no-trust] "task text" [-- extra claude args]

`claude --cloud "<task>"` is an interactive command: it draws a terminal UI, may ask whether the
working directory is trusted, prints the session URL once the cloud VM is provisioned, and keeps a
local client attached. Run from a tool call, a cron job or another agent, there is no TTY, so the UI
never renders and the command hangs or exits at once. This script gives it a pseudo-terminal
(Python's pty module, so macOS and Linux only), reads the screen, answers the folder-trust dialog,
waits until a session URL appears plus --settle seconds (so the queued task is sent once the VM is
up), then ends the local client. The cloud session keeps running. The URL is printed on stdout.

The folder-trust dialog, as drawn by CLI 2.1.289:

    Quick safety check: Is this a project you created or one you trust? ...
    > No, exit
      Yes, I trust this folder
    Enter to confirm . Esc to cancel

"No, exit" is selected by default, so a bare Enter would end the session. The script sends Down
(ESC [ B) then Enter when the screen contains "I trust this folder". It answers only that dialog;
pass --no-trust to leave it unanswered (the run then stops at --max seconds without a URL and exits
2, which is the safe outcome for a directory you did not mean to trust).

Exit codes: 0 with the URL found; 2 when no URL appeared within --max seconds (the raw screen is in
--log, ANSI escapes included; the last visible lines are printed). Stdlib only, Python 3.8+.

Environment for the launched client: the current environment plus TERM/COLUMNS/LINES for the UI,
plus any --env KEY=VALUE. Use --env to switch off hooks that would hold up a headless turn (for
example a Stop hook that insists on a notes update: session-guard reads CLAUDE_SESSION_GUARD=off).
"""
import argparse
import os
import re
import select
import signal
import subprocess
import sys
import time

URL_RE = re.compile(rb"https://claude\.ai/code/(session_[A-Za-z0-9]+|cse_[A-Za-z0-9]+)")
ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Start `claude --cloud` from a non-TTY context.")
    ap.add_argument("task", help="the task text handed to `claude --cloud`")
    ap.add_argument("--cwd", required=True, help="working directory for the session (the repository to work in)")
    ap.add_argument("--log", required=True, help="file that receives the raw terminal output (appended)")
    ap.add_argument("--settle", type=int, default=90, help="seconds to keep the client attached after the URL appears (default 90)")
    ap.add_argument("--max", type=int, default=420, help="give up after this many seconds without a URL (default 420)")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="extra environment for the client (repeatable)")
    ap.add_argument("--no-trust", action="store_true", help="do not answer the folder-trust dialog")
    ap.add_argument("extra", nargs="*", help="extra arguments for claude, after --")
    a = ap.parse_args(argv)

    try:
        import pty
    except ImportError:
        print("cloud_launch: a pseudo-terminal is needed; the pty module exists on macOS and Linux only", file=sys.stderr)
        return 2
    env = dict(os.environ, TERM="xterm-256color", COLUMNS="140", LINES="40")
    for kv in a.env:
        k, eq, v = kv.partition("=")
        if not eq or not k:
            ap.error("--env takes KEY=VALUE, not %r" % kv)
        env[k] = v

    master, slave = pty.openpty()
    p = subprocess.Popen(["claude", "--cloud", a.task] + list(a.extra), cwd=a.cwd, stdin=slave, stdout=slave,
                         stderr=slave, env=env, preexec_fn=os.setsid, close_fds=True)
    os.close(slave)
    buf = b""
    url = None
    seen_at = None
    trusted = False
    start = time.time()
    log = open(a.log, "ab")
    try:
        while True:
            if time.time() - start > a.max:
                break
            r, _, _ = select.select([master], [], [], 1.0)
            if r:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                log.write(chunk)
                log.flush()
                plain = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]|\s", b"", buf[-6000:])
                if b"Itrustthisfolder" in plain and not trusted and not a.no_trust:
                    time.sleep(1.0)
                    os.write(master, b"\x1b[B")      # Down: "Yes, I trust this folder"
                    time.sleep(0.5)
                    os.write(master, b"\r")
                    trusted = True
                if url is None:
                    m = URL_RE.search(buf)
                    if m:
                        url = m.group(0).decode()
                        seen_at = time.time()
            if url and time.time() - seen_at > a.settle:
                break
            if p.poll() is not None:
                break
    finally:
        if p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                time.sleep(2)
                if p.poll() is None:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass
        log.close()
    clean = ANSI_RE.sub(b"", buf).decode("utf-8", "replace")
    tail = [l for l in clean.replace("\r", "\n").splitlines() if l.strip()][-12:]
    print("URL:", url or "NOT FOUND")
    print("--- last output lines:")
    print("\n".join(tail))
    return 0 if url else 2


if __name__ == "__main__":
    sys.exit(main())
