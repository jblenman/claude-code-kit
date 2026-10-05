"""action-guard: a Claude Code PreToolUse hook that refuses, or asks about, actions against a web
application the agent works with. Keep the skeleton, change the policy.

How Claude Code talks to it (hooks reference, code.claude.com/docs/en/hooks):
  - It runs the hook command before every tool call whose name matches the matcher
    (Bash|PowerShell|WebFetch|mcp__.*), with one JSON object on stdin: tool_name, tool_input, cwd,
    session_id, tool_use_id, and agent_type when a subagent made the call.
  - Print nothing and exit 0: no objection; the normal permission flow still applies.
    (Never print "allow": that skips the permission prompt.)
  - Print {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
    "permissionDecisionReason": "..."}} and exit 0: the call is cancelled and the reason goes to
    the model. "ask" instead of "deny" shows the user the permission prompt.
  - Exit code 2 also blocks, but the hook command ends in `|| exit 1`, which would turn a 2 into a
    non-blocking 1; so decisions always travel as JSON, and any internal error ends in "no output,
    exit 0" (fail open). A guard that can wedge a session is worse than none.

Sub-commands:
  hook          the hook itself (JSON on stdin)
  check "<cmd>" what the hook would decide for a Bash command, without a session
  status        config in force, tab table, log tail

Config: action_guard.json next to this file (the example policy shipped with the plugin), overridden
by ~/.claude/action-guard/config.json (hosts, paths and MCP rules add up across files; every other
value replaces). State and log: ~/.claude/action-guard/.

Environment:
  CLAUDE_ACTION_GUARD=off            disable for one claude process
  CLAUDE_ACTION_GUARD_STATE=<dir>    state + log dir       (default ~/.claude/action-guard)
  CLAUDE_ACTION_GUARD_CONFIG=<file>  machine-local policy  (default <state dir>/config.json)
Stdlib only, Python 3.8+.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_MAX = 512 * 1024
SUBST = "__AG_SUBST__"
HEREDOC_RE = re.compile(r"^__AG_HEREDOC_(\d+)__$")

BUILTIN = {
    "protected_hosts": [],
    "allowed_methods": ["GET", "HEAD", "OPTIONS"],
    "denied_paths": [],
    "ask_paths": [],
    "write_ok_paths": [],
    "deny_browser_tools_on_protected_hosts": ["form_input", "javascript_tool", "file_upload", "shortcuts_execute"],
    "ask_browser_actions_on_protected_hosts": ["left_click", "right_click", "double_click", "triple_click", "type", "key", "scroll"],
    "mcp_rules": [],
    "on_unreadable_method": "deny",
    "on_unknown_target": "ask",
    "log": True,
}


# ---------------------------------------------------------------- state, log, config

def disabled():
    return os.environ.get("CLAUDE_ACTION_GUARD", "").strip().lower() in ("off", "0", "false", "no")


def state_dir():
    d = Path(os.environ.get("CLAUDE_ACTION_GUARD_STATE") or Path.home() / ".claude" / "action-guard")
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


ADDITIVE = ("protected_hosts", "denied_paths", "ask_paths", "write_ok_paths", "mcp_rules")


def _merge(base, over):
    """Later config over earlier: objects merge; the ADDITIVE lists add; every other value replaces
    (so a local file can narrow allowed_methods or the browser lists, not only widen them)."""
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        elif isinstance(v, list) and isinstance(base.get(k), list) and k in ADDITIVE:
            base[k] = base[k] + [x for x in v if x not in base[k]]
        else:
            base[k] = v
    return base


def load_config():
    cfg = json.loads(json.dumps(BUILTIN))
    for path in (HERE / "action_guard.json",
                 Path(os.environ.get("CLAUDE_ACTION_GUARD_CONFIG") or state_dir() / "config.json")):
        if path.exists():
            try:
                _merge(cfg, json.loads(path.read_text(encoding="utf-8")))
            except Exception as ex:           # a broken file must not switch the guard off
                log("config: %s unreadable (%r)" % (path, ex))
    return cfg


class Lock(object):
    """mkdir is atomic on macOS, Linux and Windows; a stale lock is taken over after 30 s."""

    def __init__(self):
        self.path = str(state_dir() / "state.lock")

    def __enter__(self):
        deadline = time.time() + 5
        while True:
            try:
                os.mkdir(self.path)
                return self
            except OSError:
                try:
                    if time.time() - os.stat(self.path).st_mtime > 30:
                        os.rmdir(self.path)
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise RuntimeError("state lock busy")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            os.rmdir(self.path)
        except OSError:
            pass
        return False


def load_tabs():
    """tab id -> host of the page it was last navigated to (browser tools carry a tabId)."""
    try:
        d = json.loads((state_dir() / "tabs.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_tabs(tabs):
    f = state_dir() / "tabs.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(tabs), encoding="utf-8")
    os.replace(str(tmp), str(f))


# ---------------------------------------------------------------- hosts and paths

def host_of(url):
    from urllib.parse import urlsplit
    u = str(url).strip()
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", u):
        u = "http://" + u
    try:
        h = urlsplit(u).hostname
    except ValueError:
        return None
    return (h or "").rstrip(".").lower() or None


def path_of(url):
    from urllib.parse import urlsplit
    u = str(url).strip()
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", u):
        u = "http://" + u
    try:
        return urlsplit(u).path or "/"
    except ValueError:
        return "/"


def is_protected(host, cfg):
    host = (host or "").lower()
    for key in cfg.get("protected_hosts") or []:
        key = str(key).lower().lstrip(".")
        if key and (host == key or host.endswith("." + key)):
            return True
    return False


def path_matches(path, patterns):
    """'/admin' matches /admin and everything under /admin/; '/adm*' matches any path starting /adm."""
    for pat in patterns or []:
        pat = str(pat)
        if pat.endswith("*"):
            if path.startswith(pat[:-1]):
                return pat
        elif path == pat or path.startswith(pat.rstrip("/") + "/"):
            return pat
    return None


# ---------------------------------------------------------------- what a call would do

class Request(object):
    def __init__(self, url, method, via, unknown=False):
        self.url = url
        self.host = host_of(url) if url else None
        self.path = path_of(url) if url else "/"
        self.method = (method or "GET").upper()
        self.via = via              # "curl", "WebFetch", "navigate" ...
        self.unknown = unknown      # the target or the count could not be read


class Findings(object):
    def __init__(self):
        self.requests = []          # Request objects
        self.uncounted = []         # reasons a request count cannot be read (loop, xargs ...)
        self.unknown_targets = []   # descriptions of requests whose target cannot be read
        self.code_calls = []        # inline code or scripts that talk HTTP (method unreadable)
        self.browser = []           # (short tool name, action, tabId) for Claude in Chrome calls
        self.mcp = None             # (server, tool) for an MCP tool of another server


# --- a small shell reader: enough for curl, wget, httpie and the PowerShell web cmdlets ---

def extract_heredocs(text):
    bodies = []
    pat = re.compile(r"<<-?[ \t]*(?:(['\"])(\w+)\1|\\?(\w+))([^\n]*)\n(.*?)\n[ \t]*(?:\2|\3)[ \t]*(?=\n|$)", re.S)

    def repl(m):
        bodies.append(m.group(5))
        return " __AG_HEREDOC_%d__ %s" % (len(bodies) - 1, m.group(4) or "")
    return pat.sub(repl, text), bodies


def extract_substitutions(text, inner):
    for _ in range(40):
        m = re.search(r"\$\(([^()]*)\)", text)
        if not m:
            break
        inner.append(m.group(1))
        text = text[:m.start()] + SUBST + text[m.end():]
    return re.sub(r"`([^`\n]*)`", lambda m: (inner.append(m.group(1)), SUBST)[1], text)


def split_commands(text, ps=False):
    """Simple commands, split at unquoted ; | & newline ( ) -- and { } in PowerShell."""
    seps = ";|&\n(){}" if ps else ";|&\n()"
    out, buf, q = [], [], None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if q:
            buf.append(c)
            if c == "\\" and q == '"' and not ps and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if c == q:
                q = None
        elif c in "'\"":
            q = c
            buf.append(c)
        elif c == "\\" and not ps and i + 1 < n:
            if text[i + 1] != "\n":
                buf.append(c)
                buf.append(text[i + 1])
            i += 2
            continue
        elif c in seps:
            if c == "&" and ((buf and buf[-1] == ">") or (i + 1 < n and text[i + 1] == ">")):
                buf.append(c)
            else:
                seg = "".join(buf).strip()
                if seg:
                    out.append(seg)
                buf = []
                if ps and c in "{}":
                    out.append(c)
        elif c == "#" and (not buf or buf[-1].isspace()):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        else:
            buf.append(c)
        i += 1
    seg = "".join(buf).strip()
    if seg:
        out.append(seg)
    return out


def tokenize(seg, ps=False):
    toks, buf, q, have = [], [], None, False
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if q:
            if c == q:
                q = None
            elif not ps and q == '"' and c == "\\" and i + 1 < n and seg[i + 1] in '"\\$`':
                buf.append(seg[i + 1])
                i += 1
            else:
                buf.append(c)
        elif c in "'\"":
            q = c
            have = True
        elif not ps and c == "\\" and i + 1 < n:
            buf.append(seg[i + 1])
            i += 1
            have = True
        elif c.isspace():
            if buf or have:
                toks.append("".join(buf))
            buf, have = [], False
        else:
            buf.append(c)
        i += 1
    if buf or have:
        toks.append("".join(buf))
    return toks


def substitute(tok, variables):
    if "$" not in tok or not variables:
        return tok

    def repl(m):
        return variables.get((m.group(1) or m.group(2)).lower(), m.group(0))
    for _ in range(2):
        tok = re.sub(r"\$\{(\w+)\}|\$(?:env:)?(\w+)", repl, tok)
    return tok


PREFIXES = frozenset(("sudo", "doas", "time", "nohup", "exec", "command", "env", "nice", "timeout",
                      "then", "do", "else", "elif", "if", "!", "{"))
MULTIPLIERS = frozenset(("xargs", "parallel", "watch"))
LOOPS = frozenset(("for", "while", "until", "foreach", "foreach-object", "%", "select"))
VAR_RE = re.compile(r"\$[\w{(]|__AG_SUBST__|`")
URL_RE = re.compile(r"^(?:https?|ftp)://", re.I)
REDIR_RE = re.compile(r"^(\d*(>>?|<)|&>>?)")


def base(tok):
    b = re.split(r"[\\/]", str(tok))[-1].lower()
    return b[:-4] if b.endswith(".exe") else b


def strip_prefixes(toks):
    """-> (command tokens, reasons the command runs more than once, it starts a loop)."""
    i, many, loop = 0, [], False
    while i < len(toks):
        low = base(toks[i])
        if re.match(r"^\$?[A-Za-z_]\w*=", toks[i]) or low in PREFIXES:
            i += 1
            while i < len(toks) and toks[i].startswith("-"):
                i += 1
            if low == "timeout" and i < len(toks) and re.match(r"^\d+(\.\d+)?[smhd]?$", toks[i]):
                i += 1
        elif low in LOOPS:
            loop = True
            if low in ("for", "foreach", "select"):
                return [], many, True
            i += 1
        elif low in MULTIPLIERS:
            many.append(low)
            i += 1
            while i < len(toks) and toks[i].startswith("-"):
                i += 1
        else:
            break
    return toks[i:], many, loop


def drop_redirections(args):
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        m = REDIR_RE.match(a)
        if m:
            skip = m.end() == len(a)
            continue
        out.append(a)
    return out


CURL_VALUE_LONG = frozenset((
    "cacert capath cert ciphers config connect-timeout cookie cookie-jar data data-ascii data-binary data-raw "
    "data-urlencode dump-header form form-string header interface json key limit-rate max-filesize max-redirs "
    "max-time output output-dir proxy proxy-user range referer request resolve retry retry-delay upload-file "
    "url user user-agent write-out").split())
CURL_SHORT_VALUE = "AbcCdDeEFHKmoPQrtTuUwxXyYz"
CURL_METHOD_BY_BODY = {"d": "POST", "data": "POST", "data-ascii": "POST", "data-binary": "POST", "data-raw": "POST",
                       "data-urlencode": "POST", "F": "POST", "form": "POST", "form-string": "POST", "json": "POST",
                       "T": "PUT", "upload-file": "PUT"}


def read_curl(args, f, variables):
    method, targets, unknown = None, [], False
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if a == "--":
            targets += args[i + 1:]
            break
        if a.startswith("--") and len(a) > 2:
            name, eq, val = a[2:].partition("=")
            if name in CURL_VALUE_LONG:
                if not eq:
                    i += 1
                    val = args[i] if i < n else ""
                if name == "request":
                    method = substitute(val, variables)
                elif name == "url":
                    targets.append(val)
                elif name == "config":
                    unknown = True
                elif name in CURL_METHOD_BY_BODY and not method:
                    method = CURL_METHOD_BY_BODY[name]
            elif name == "head":
                method = method or "HEAD"
        elif a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                ch = a[j]
                if ch in CURL_SHORT_VALUE:
                    val = a[j + 1:]
                    if not val:
                        i += 1
                        val = args[i] if i < n else ""
                    if ch == "X":
                        method = substitute(val, variables)
                    elif ch == "K":
                        unknown = True
                    elif ch in CURL_METHOD_BY_BODY and not method:
                        method = CURL_METHOD_BY_BODY[ch]
                    break
                if ch == "I":
                    method = method or "HEAD"
                j += 1
        else:
            targets.append(a)
        i += 1
    if unknown:
        f.unknown_targets.append("curl reads its URLs from a config file")
    _add_targets(targets, method or "GET", "curl", f, variables, bare_ok=True)


def read_wget(args, f, variables):
    method, targets = "GET", []
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if a in ("-r", "--recursive", "-m", "--mirror", "-p", "--page-requisites", "-i", "--input-file"):
            f.uncounted.append("wget %s" % a)
            if a in ("-i", "--input-file"):
                i += 1
        elif a.startswith("--post-data") or a.startswith("--post-file"):
            method = "POST"
            if "=" not in a:
                i += 1
        elif a.startswith("--method"):
            method = a.partition("=")[2] if "=" in a else (args[i + 1] if i + 1 < n else "GET")
            i += 0 if "=" in a else 1
        elif a.startswith("--") and "=" not in a and a[2:] in ("output-document", "output-file", "tries", "timeout", "wait", "user-agent", "header", "referer", "user", "password", "directory-prefix", "load-cookies", "level"):
            i += 1
        elif re.match(r"^-[OoatTwUeDXIlAR]$", a):
            i += 1
        elif a.startswith("-"):
            pass
        else:
            targets.append(a)
        i += 1
    _add_targets(targets, method, "wget", f, variables, bare_ok=True)


HTTP_VERBS = frozenset(("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"))
GENERIC_SKIP = frozenset(("-headers", "-body", "-proxy", "-outfile", "-o", "--output", "--header", "--proxy",
                          "-infile", "-contenttype", "-credential", "-websession", "-sessionvariable", "-form",
                          "-useragent", "--user-agent", "-d", "--auth", "-a"))


def read_generic(name, args, f, variables):
    """httpie (`http POST url`), the PowerShell web cmdlets (`-Method`, `-Body`), lynx, w3m."""
    method, targets, prev, body = None, [], "", False
    for a in args:
        low = a.lower()
        if prev == "-method":
            method = a
        elif prev in GENERIC_SKIP:
            if prev == "-body":
                body = True
        elif a.upper() in HTTP_VERBS and not method and name in ("http", "https", "xh"):
            method = a.upper()
        elif a.startswith("-") and not URL_RE.match(a):
            pass
        else:
            targets.append(a)
        prev = low
    if body and not method:
        method = "POST"
    _add_targets(targets, method or "GET", name, f, variables, bare_ok=False)


def _add_targets(tokens, method, via, f, variables, bare_ok):
    method = substitute(str(method), variables).upper()
    if not tokens:
        f.unknown_targets.append("%s without a written URL (stdin, xargs or a config file)" % via)
    for t in tokens:
        t = substitute(t, variables).strip()
        if not t or HEREDOC_RE.match(t) or t.lower().startswith("file:"):
            continue
        if not URL_RE.match(t) and not bare_ok:
            if VAR_RE.search(t):
                f.unknown_targets.append(t[:60])
            continue
        hostpart = re.match(r"^(?:[A-Za-z][\w+.-]*://)?([^/?#]*)", t).group(1)
        if VAR_RE.search(hostpart) or not hostpart:
            f.unknown_targets.append(t[:60])
            continue
        f.requests.append(Request(t if URL_RE.match(t) else "http://" + t, method, via, unknown=VAR_RE.search(method) is not None))


HTTP_CODE = re.compile(r"\brequests\s*\.\s*(?:get|post|put|patch|delete|head|request|Session)\b|\burllib\.request\b|"
                       r"\burlopen\s*\(|\bhttpx\b|\baiohttp\b|\bfetch\s*\(|\baxios\b|\bhttps?\.(?:get|request)\s*\(|"
                       r"\bInvoke-(?:WebRequest|RestMethod)\b|\bWebClient\b|\bHttpClient\b")
CODE_INTERP = re.compile(r"^(python|pypy|py|node|nodejs|deno|bun|ruby|perl|php)[\d.]*$")
SHELL_INTERP = re.compile(r"^(bash|sh|zsh|dash|ksh|pwsh|powershell)$")
CLIENTS = {"curl": read_curl, "wget": read_wget}
GENERIC_CLIENTS = frozenset(("http", "https", "xh", "lynx", "w3m", "invoke-webrequest", "invoke-restmethod", "iwr", "irm"))


def read_code(text, f, where):
    """Inline code or a script: the method cannot be read reliably, so record every URL it holds."""
    if not HTTP_CODE.search(text):
        return
    urls = re.findall(r"(?:https?)://[^\s\"'<>\\)`|;]+", text)
    f.code_calls.append(where)
    for u in urls:
        f.requests.append(Request(u.rstrip(".,;:"), "UNKNOWN", where, unknown=True))
    if not urls:
        f.unknown_targets.append("%s takes its target at run time" % where)


def scan(text, f, cwd, ps=False, depth=0, variables=None):
    if depth > 4:
        return
    variables = variables if variables is not None else {}
    inner = []
    bodies = []
    if not ps:
        text, bodies = extract_heredocs(text)
    text = extract_substitutions(text, inner)
    for sub in inner:
        scan(sub, f, cwd, ps, depth + 1, variables)
    segments = [tokenize(s, ps) for s in split_commands(text, ps)]
    for toks in segments:                                  # NAME=value assignments, for "$NAME/path"
        for t in toks:
            m = re.match(r"^\$?([A-Za-z_]\w*)=(.*)$", t)
            if m:
                variables[m.group(1).lower()] = substitute(m.group(2), variables)
            else:
                break
    loops = 0
    for toks in segments:
        if toks and toks[0] == "done":
            loops = max(0, loops - 1)
        toks, many, loop = strip_prefixes(toks)
        if loop:
            loops += 1
        if not toks:
            continue
        name, args = base(toks[0]), drop_redirections(toks[1:])
        before = len(f.requests)
        if name in CLIENTS and not ps:
            CLIENTS[name](args, f, variables)
        elif name in GENERIC_CLIENTS or name in CLIENTS:
            read_generic(name, args, f, variables)
        elif CODE_INTERP.match(name):
            code = None
            for k, a in enumerate(args):
                if a in ("-c", "-e", "--eval", "-r") and k + 1 < len(args):
                    code = args[k + 1]
                    break
                if HEREDOC_RE.match(a):
                    code = bodies[int(HEREDOC_RE.match(a).group(1))]
                    break
                if not a.startswith("-"):
                    p = Path(a) if os.path.isabs(a) else Path(cwd or ".") / a
                    if p.is_file() and p.stat().st_size < 512 * 1024:
                        code = p.read_text(encoding="utf-8", errors="replace")
                    break
            if code:
                read_code(code, f, "%s code" % name)
        elif SHELL_INTERP.match(name):
            for k, a in enumerate(args):
                if (re.match(r"^-[A-Za-z]*c$", a) or a.lower() == "-command") and k + 1 < len(args):
                    sub_ps = name in ("pwsh", "powershell")
                    scan(" ".join(args[k + 1:]) if sub_ps else args[k + 1], f, cwd, sub_ps, depth + 1, variables)
                    break
        elif name == "ssh" and len(args) > 1:
            rest = [a for a in args if not a.startswith("-")]
            if len(rest) > 1:
                scan(" ".join(rest[1:]), f, cwd, ps, depth + 1, variables)
        if len(f.requests) > before and (loops > 0 or many):
            f.uncounted.append("a loop" if loops else ", ".join(many))


# ---------------------------------------------------------------- reading a tool call

def findings_for(inp, cfg, tabs):
    """-> Findings: the requests a call would send, browser events on known tabs, or an MCP tool."""
    tool = str(inp.get("tool_name") or "")
    ti = inp.get("tool_input") or {}
    f = Findings()
    if not isinstance(ti, dict):
        return f
    if tool == "WebFetch":
        if ti.get("url"):
            f.requests.append(Request(str(ti["url"]), "GET", "WebFetch"))
    elif tool in ("Bash", "PowerShell"):
        cmd = ti.get("command")
        if isinstance(cmd, str) and cmd.strip():
            scan(cmd, f, str(inp.get("cwd") or ""), ps=(tool == "PowerShell"))
    elif tool.startswith("mcp__claude-in-chrome__"):
        short = tool.rsplit("__", 1)[-1]
        calls = [(short, ti)]
        if short == "browser_batch":
            calls = [(str(a.get("name") or "").rsplit("__", 1)[-1], a.get("input") or {})
                     for a in (ti.get("actions") or []) if isinstance(a, dict)]
        for name, args in calls:
            args = args if isinstance(args, dict) else {}
            tab = str(args.get("tabId") or "")
            if name == "navigate":
                url = str(args.get("url") or "")
                if url and url.lower() not in ("back", "forward"):
                    r = Request(url if URL_RE.match(url) else "https://" + url, "GET", "navigate")
                    f.requests.append(r)
                    if tab and r.host:
                        tabs[tab] = r.host
            else:
                f.browser.append((name, str(args.get("action") or ""), tab))
    elif tool.startswith("mcp__"):
        parts = tool.split("__", 2)
        if len(parts) == 3:
            f.mcp = (parts[1], parts[2])
    return f


# ---------------------------------------------------------------- the policy (change this part)

def _deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "action-guard: refused. " + reason},
            "systemMessage": "action-guard: held back a call -- " + reason[:120]}


def _ask(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                   "permissionDecisionReason": "action-guard: " + reason}}


def policy(f, cfg, tabs, tag):
    """Decide. Return None (no opinion), or the JSON to print. Order: deny beats ask beats silence."""
    allowed = set(m.upper() for m in cfg.get("allowed_methods") or [])
    protected = [r for r in f.requests if is_protected(r.host, cfg)]
    ask = None

    # 1. Requests to the protected application, read from the call
    for r in protected:
        hit = path_matches(r.path, cfg.get("denied_paths"))
        if hit:
            log("%s DENY path %s %s" % (tag, r.method, r.url))
            return _deny("%s matches the protected path pattern %r on %s. That part of the application is "
                         "off limits to this session. Do not try another address or another tool for it; "
                         "tell the user what you needed." % (r.path, hit, r.host))
        if r.unknown:
            if cfg.get("on_unreadable_method") == "ask":
                ask = ask or _ask("a request to %s whose method the guard cannot read (made from code); "
                                  "the user decides." % r.host)
                continue
            log("%s DENY unreadable %s" % (tag, r.url))
            return _deny("a request to %s made from code, whose method the guard cannot read. Against this "
                         "application use curl (or WebFetch) with the URL written out, one request per call, "
                         "so the guard can see the method." % r.host)
        if r.method not in allowed and not path_matches(r.path, cfg.get("write_ok_paths")):
            log("%s DENY method %s %s" % (tag, r.method, r.url))
            article = "an" if r.method[:1] in "AEIOU" else "a"
            return _deny("%s %s request to %s. Only %s requests are allowed against this application from a "
                         "session; anything that changes its state is for the user to do. Say what change you "
                         "wanted." % (article, r.method, r.host, ", ".join(sorted(allowed))))
        hit = path_matches(r.path, cfg.get("ask_paths"))
        if hit:
            ask = ask or _ask("%s %s on %s matches the pattern %r, which needs the user's approval."
                              % (r.method, r.path, r.host, hit))
    if protected and f.uncounted:
        log("%s DENY uncounted" % tag)
        return _deny("this call would send an unreadable number of requests to %s (%s). Send them one per "
                     "call, each URL written out." % (protected[0].host, "; ".join(f.uncounted)[:160]))
    if f.unknown_targets:
        # A request whose target cannot be read might be aimed at the protected application.
        what = "; ".join(f.unknown_targets)[:160]
        if cfg.get("on_unknown_target") == "deny":
            log("%s DENY unknown target" % tag)
            return _deny("a request whose target the guard cannot read (%s). Write the URL out in the call." % what)
        if cfg.get("on_unknown_target") == "ask":
            ask = ask or _ask("a request whose target the guard cannot read (%s); the user decides." % what)

    # 2. Browser actions on a tab that last navigated to the protected application
    for name, action, tab in getattr(f, "browser", []):
        host = tabs.get(tab)
        if not is_protected(host, cfg):
            continue
        if name in (cfg.get("deny_browser_tools_on_protected_hosts") or []):
            log("%s DENY browser %s tab=%s host=%s" % (tag, name, tab, host))
            return _deny("%s on a page of %s. Changing that application through the browser is for the user; "
                         "reading it (screenshots, page text, find) is fine." % (name, host))
        if name == "computer" and action in (cfg.get("ask_browser_actions_on_protected_hosts") or []):
            ask = ask or _ask("a %s in the browser on a page of %s; the user decides whether that action is allowed." % (action, host))

    # 3. MCP tools of the application's own server
    if f.mcp:
        server, tool = f.mcp
        for rule in cfg.get("mcp_rules") or []:
            if str(rule.get("server", "")).lower() != server.lower():
                continue
            if tool in (rule.get("deny_tools") or []):
                log("%s DENY mcp %s/%s" % (tag, server, tool))
                return _deny("the tool %s of server %s is off limits to this session." % (tool, server))
            if tool in (rule.get("ask_tools") or []):
                ask = ask or _ask("the tool %s of server %s needs the user's approval." % (tool, server))
    if ask:
        log("%s ASK %s" % (tag, ask["hookSpecificOutput"]["permissionDecisionReason"][:100]))
    return ask


# ---------------------------------------------------------------- entry points

def do_hook(inp):
    if disabled():
        return None
    cfg = load_config()
    with Lock():
        tabs = load_tabs()
        f = findings_for(inp, cfg, tabs)
        save_tabs(tabs)
    sid = "".join(ch for ch in str(inp.get("session_id") or "?") if ch.isalnum())[:8]
    tag = "hook sid=%s who=%s tool=%s" % (sid, inp.get("agent_type") or "main", str(inp.get("tool_name") or "?").rsplit("__", 1)[-1])
    return policy(f, cfg, tabs, tag)


def do_check(args):
    cfg = load_config()
    for cmd in args:
        f = findings_for({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": os.getcwd()}, cfg, {})
        out = policy(f, cfg, {}, "check")
        for r in f.requests:
            print("  %-7s %s  (%s)%s" % (r.method, r.url, r.via, "  protected" if is_protected(r.host, cfg) else ""))
        print("%s -> %s" % (cmd[:70], "no opinion" if out is None else out["hookSpecificOutput"]["permissionDecision"] + ": " + out["hookSpecificOutput"]["permissionDecisionReason"]))
    return 0


def do_status():
    cfg = load_config()
    print("action-guard  disabled-by-env=%s  state=%s" % (disabled(), state_dir()))
    print("protected hosts: %s" % (", ".join(cfg.get("protected_hosts") or []) or "(none: the guard does nothing)"))
    print("allowed methods: %s | denied paths: %s | ask paths: %s | writes allowed under: %s"
          % (cfg.get("allowed_methods"), cfg.get("denied_paths"), cfg.get("ask_paths"), cfg.get("write_ok_paths")))
    print("browser tools denied on protected pages: %s" % cfg.get("deny_browser_tools_on_protected_hosts"))
    print("tabs known: %s" % (load_tabs() or "{}"))
    lg = state_dir() / "guard.log"
    if lg.exists():
        for ln in lg.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]:
            print("  " + ln)
    return 0


def main(argv):
    cmd = (argv[1] if len(argv) > 1 else "").strip().lower()
    if cmd == "check":
        return do_check(argv[2:])
    if cmd == "status":
        return do_status()
    if cmd != "hook":
        sys.stderr.write("usage: action_guard.py hook  (hook JSON on stdin) | check '<command>' | status\n")
        return 0
    raw = ""
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    except Exception:
        raw = ""
    try:
        inp = json.loads(raw) if raw.strip() else {}
    except ValueError:
        inp = {}
    out = None
    try:
        out = do_hook(inp if isinstance(inp, dict) else {})
    except Exception as ex:                    # fail open, never lock a session
        log("hook ERROR %r" % (ex,))
        out = None
    if out:
        sys.stdout.write(json.dumps(out))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
