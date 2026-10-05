"""request-guard: keeps this machine's automated web requests few and far enough apart.

Every request a Claude Code session or subagent sends to a public website or endpoint is counted
and spaced in time, through one ledger per machine shared by every session and subagent. This
script is the hook, the paced fetcher and the command-line view of the ledger.

  hook      PreToolUse (Bash, PowerShell, WebFetch, browser navigate). Reads the hook JSON on
            stdin, works out which public sites the call would contact and books the requests.
            Too soon after the last request to a site: the hook itself waits (up to
            max_hook_wait_s), then lets the call through. It refuses -- JSON
            {"hookSpecificOutput": {"permissionDecision": "deny", ...}} on stdout, exit 0 -- when
            a limit is reached, the site answered 403/429/503 a while ago, the site is on the
            do-not-contact list, the number of requests cannot be read (loop, xargs, recursive
            download, URL list, a script that fetches), the target cannot be read, or the request
            carries an invented identifier. No objection: prints nothing, so the normal
            permission flow applies. It never prints "allow".
  result    PostToolUse / PostToolUseFailure for WebFetch: a 403/429/503 answer starts a back-off.
  fetch     Paced fetcher for batches: waits for each site's turn for as long as it takes, books
            every request, stops at the limit, backs off when a site refuses.
  wait URL  Book one request and wait for its turn (shell scripts: `wait URL && curl URL`).
  check URL What the guard would do now, without booking anything.
  status    Limits, per-site counts, back-offs, log tail.
  grant / clear-backoff
            Raise one site's limit for a while / lift a back-off. The hook turns these, and any
            write to the guard's own files, into a permission prompt: only the user raises a limit.
  help      How to pace requests from a script.

As a module:   sys.path.insert(0, "<plugin>/scripts"); import request_guard
               request_guard.wait_turn(url)         # before each request; raises request_guard.Refusal
               request_guard.report(url, status)    # after it, so the guard can back off

Limits: defaults.json next to this file (shipped with the plugin), then the machine-local
~/.claude/request-guard/config.json (objects merge, lists add, values replace). Private and LAN
addresses are never counted. State: ~/.claude/request-guard/ledger.json and guard.log.

Fail-open by design: any internal error -> no output, exit 0. Never blocks via exit code 2 (the
hook command ends in `|| exit 1`, which would turn a 2 into a non-blocking 1).
Stdlib only, Python 3.8+.

Environment:
  CLAUDE_REQUEST_GUARD=off             disable (set it when starting claude; a tool call cannot set it)
  CLAUDE_REQUEST_GUARD_STATE=<dir>     state + log dir        (default ~/.claude/request-guard)
  CLAUDE_REQUEST_GUARD_CONFIG=<file>   machine-local config   (default <state dir>/config.json)
"""

import json
import os
import re
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent                      # scripts under the plugin itself are trusted
PY = "py" if os.name == "nt" else "python3"
SELF = '%s "%s"' % (PY, str(HERE / "request_guard.py").replace("\\", "/"))
HOUR = 3600.0
DAY = 86400.0
LOG_MAX = 1024 * 1024
SCRIPT_MAX = 512 * 1024
MAX_DEPTH = 4
SUBST_RE = re.compile(r"__RG_SUBST_(\d+)__")
HEREDOC_RE = re.compile(r"^__RG_HEREDOC_(\d+)__$")

BUILTIN = {      # used only when defaults.json cannot be read
    "enabled": True, "max_hook_wait_s": 25, "total_per_hour": 1500, "check_identifiers": True,
    "profiles": {"default": {"min_interval_s": 10, "max_per_hour": 20, "max_per_day": 50,
                             "max_per_command": 1, "backoff_codes": [403, 429, 503],
                             "backoff_min": 60, "miss_limit": 3}},
    "hosts": {}, "deny": [], "allowed_user_agents": ["Mozilla/*", "curl/*", "Wget/*"],
    "sensitive_terms": [], "never_send": [], "trusted_scripts": [], "debug": False,
}


# ---------------------------------------------------------------- state, log, config

def disabled():
    return os.environ.get("CLAUDE_REQUEST_GUARD", "").strip().lower() in ("off", "0", "false", "no")


def state_dir():
    p = os.environ.get("CLAUDE_REQUEST_GUARD_STATE")
    d = Path(p) if p else Path.home() / ".claude" / "request-guard"
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


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        elif isinstance(v, list) and isinstance(base.get(k), list):
            base[k] = base[k] + [x for x in v if x not in base[k]]
        else:
            base[k] = v
    return base


def local_config_path():
    p = os.environ.get("CLAUDE_REQUEST_GUARD_CONFIG")
    return Path(p) if p else state_dir() / "config.json"


def load_config():
    cfg = json.loads(json.dumps(BUILTIN))
    try:
        _merge(cfg, json.loads((HERE / "defaults.json").read_text(encoding="utf-8")))
    except Exception as ex:
        log("config: defaults.json unreadable (%r); built-in limits in use" % (ex,))
    local = local_config_path()
    if local.exists():
        try:
            _merge(cfg, json.loads(local.read_text(encoding="utf-8")))
        except Exception as ex:          # a broken local file must not switch the limits off
            log("config: %s unreadable (%r); shipped defaults in use" % (local, ex))
    return cfg


def profile(cfg, name):
    """A profile's values; anything it leaves out comes from the default profile."""
    p = dict(BUILTIN["profiles"]["default"])
    p.update((cfg.get("profiles") or {}).get("default") or {})
    if name != "default":
        p.update((cfg.get("profiles") or {}).get(name) or {})
    return p


# ---------------------------------------------------------------- ledger

class LockTimeout(Exception):
    pass


class Lock(object):
    """mkdir is atomic on macOS, Linux and Windows; a lock left behind by a killed process is taken over after `stale` seconds."""

    def __init__(self, timeout=5.0, stale=30.0):
        self.path = str(state_dir() / "ledger.lock")
        self.timeout = timeout
        self.stale = stale

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                os.mkdir(self.path)
                return self
            except OSError:
                try:
                    if time.time() - os.stat(self.path).st_mtime > self.stale:
                        os.rmdir(self.path)          # left behind by a killed process
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise LockTimeout("ledger lock busy")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            os.rmdir(self.path)
        except OSError:
            pass
        return False


def load_ledger():
    try:
        led = json.loads((state_dir() / "ledger.json").read_text(encoding="utf-8"))
        if isinstance(led, dict) and isinstance(led.get("sites"), dict):
            return led
    except Exception:
        pass
    return {"v": 1, "sites": {}}


def save_ledger(led):
    f = state_dir() / "ledger.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(led, sort_keys=True), encoding="utf-8")
    os.replace(str(tmp), str(f))


def prune(led, now):
    sites = led["sites"]
    for site in list(sites):
        e = sites[site]
        if not isinstance(e, dict):
            del sites[site]
            continue
        e["t"] = sorted(t for t in (e.get("t") or []) if isinstance(t, (int, float)) and t > now - DAY)
        if float(e.get("backoff_until") or 0) <= now:
            e.pop("backoff_until", None)
            e.pop("backoff_why", None)
        if float((e.get("grant") or {}).get("until") or 0) <= now:
            e.pop("grant", None)
        if now - float((e.get("miss") or {}).get("t") or 0) > HOUR:
            e.pop("miss", None)
        if not e["t"] and not any(k in e for k in ("backoff_until", "grant", "miss")):
            del sites[site]


# ---------------------------------------------------------------- which site, which limits

PRIVATE_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localhost", ".test", ".example",
                    ".invalid", ".home.arpa", ".localdomain")
TWO_LEVEL = frozenset((
    "co.uk org.uk ac.uk gov.uk com.au net.au org.au edu.au gov.au co.nz co.jp or.jp ne.jp ac.jp "
    "go.jp com.br com.mx com.ar co.in co.za com.cn com.tw com.hk com.sg co.kr com.tr co.il").split())


def registrable(host):
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in TWO_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _suffix(host, key):
    key = str(key).strip().lower().lstrip(".")
    return bool(key) and (host == key or host.endswith("." + key))


def host_of(url):
    """Lower-case host of a URL (scheme optional), or None when there is none to read."""
    from urllib.parse import urlsplit
    u = str(url).strip()
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", u):
        u = "http://" + u
    try:
        h = urlsplit(u).hostname
    except ValueError:
        return None
    h = (h or "").rstrip(".").lower()
    if not h or re.search(r"[^a-z0-9._:-]", h):
        return None
    return h


def classify(host, cfg):
    """-> (site, profile name). 'private' is never counted; 'deny' is never contacted.
    A host listed in the config keeps its listed profile even when its address is private."""
    import ipaddress
    for key in cfg.get("deny") or []:
        if _suffix(host, key):
            return str(key).strip().lower().lstrip("."), "deny"
    best = None
    for key, prof in (cfg.get("hosts") or {}).items():
        if key.startswith("_") or not _suffix(host, key):
            continue
        if best is None or len(key) > len(best[0]):
            best = (key.strip().lower().lstrip("."), str(prof))
    if best:
        return best
    try:
        if not ipaddress.ip_address(host).is_global:
            return host, "private"
        return host, "default"                     # a public address written as numbers
    except ValueError:
        pass
    if "." not in host or host.endswith(PRIVATE_SUFFIXES):
        return host, "private"
    return registrable(host), "default"


def classify_url(url, cfg):
    h = host_of(url)
    return classify(h, cfg) if h else None


# ---------------------------------------------------------------- the limits

class Refusal(Exception):
    """The guard will not let this request go now. kind: hour, day, total, backoff, deny, busy."""

    def __init__(self, kind, site, message, short, retry_at=None):
        Exception.__init__(self, message)
        self.kind = kind
        self.site = site
        self.message = message
        self.short = short
        self.retry_at = retry_at


def _when(epoch):
    return time.strftime("%H:%M", time.localtime(epoch))


def _span(sec):
    sec = max(0.0, float(sec))
    if sec < 90:
        return "%d s" % round(sec)
    if sec < 5400:
        return "%d min" % round(sec / 60)
    return "%.1f h" % (sec / 3600)


NO_ROUTE = ("Do not retry early and do not route around this with another tool, another machine, "
            "a proxy or the browser.")
ASK_USER = ("If the work needs more than the limit allows, stop and tell the user how many requests "
            "and why; only the user raises a limit.")


def _cap_refusal(kind, site, window, used, cap, n, stamps, span, now):
    k = used + n - cap                       # bookings that must age out first
    retry_at = stamps[k - 1] + span if 0 < k <= len(stamps) else None
    nxt = (" The next request can go at %s (in %s)." % (_when(retry_at), _span(retry_at - now))
           if retry_at else "")
    who = "all public sites together" if site == "*" else site
    msg = ("request-guard: refused. Limit reached for %s: %d request%s booked in the last %s, limit %d.%s %s %s"
           % (who, used, "" if used == 1 else "s", window, cap, nxt, NO_ROUTE, ASK_USER))
    return Refusal(kind, site, msg, "%s limit for %s reached (%d of %d)" % (window, who, used, cap), retry_at)


def reserve(items, cfg, max_wait=None, now=None, commit=True):
    """Book the next free slot for each (site, profile name, count). Returns the seconds to wait
    before sending. Raises Refusal when a limit, a back-off or a queue longer than max_wait stands
    in the way; nothing is booked in that case."""
    now = time.time() if now is None else now
    with Lock():
        led = load_ledger()
        prune(led, now)
        sites = led["sites"]
        wait = 0.0
        plan = []
        for site, pname, n in items:
            if pname == "private" or n <= 0:
                continue
            if pname == "deny":
                raise Refusal("deny", site,
                              "request-guard: refused. %s is on this machine's do-not-contact list "
                              "(~/.claude/request-guard/config.json). Do not contact it by any other route "
                              "either. If the work depends on it, tell the user." % site,
                              "%s is on the do-not-contact list" % site)
            p = profile(cfg, pname)
            e = sites.get(site) or {"t": []}
            until = float(e.get("backoff_until") or 0)
            if until > now:
                raise Refusal("backoff", site,
                              "request-guard: refused. %s answered with %s; automated requests to it are paused "
                              "until %s (%s). A site that refuses requests must not be tried again through "
                              "another tool or address. If the work depends on it, tell the user."
                              % (site, e.get("backoff_why") or "a refusal", _when(until), _span(until - now)),
                              "%s is in back-off until %s" % (site, _when(until)), until)
            g = e.get("grant") or {}
            ts = e.get("t") or []
            hour = [t for t in ts if t > now - HOUR]
            cap_h = int(p["max_per_hour"]) + int(g.get("hour") or 0)
            cap_d = int(p["max_per_day"]) + int(g.get("day") or 0)
            if len(hour) + n > cap_h:
                raise _cap_refusal("hour", site, "hour", len(hour), cap_h, n, hour, HOUR, now)
            if len(ts) + n > cap_d:
                raise _cap_refusal("day", site, "24 hours", len(ts), cap_d, n, ts, DAY, now)
            last = ts[-1] if ts else 0.0
            wait = max(wait, last + float(p["min_interval_s"]) - now)
            plan.append((site, pname, n, p))
        wait = max(0.0, wait)
        if max_wait is not None and wait > max_wait:
            site = plan[0][0] if plan else "?"
            raise Refusal("busy", site,
                          "request-guard: not yet. Requests to %s are queued; the next free slot is in %s. "
                          "Do something else and try again after that, or use `%s fetch URL`, which waits "
                          "as long as it takes. %s" % (site, _span(wait), SELF, NO_ROUTE),
                          "%s is busy, next slot in %s" % (site, _span(wait)), now + wait)
        counted = sum(n for _s, pname, n, _p in plan if pname != "infra")
        if counted:
            stamps = sorted(t for s, e in sites.items() if e.get("p") != "infra"
                            for t in (e.get("t") or []) if t > now - HOUR)
            cap = int(cfg.get("total_per_hour") or 0)
            if cap and len(stamps) + counted > cap:
                raise _cap_refusal("total", "*", "hour", len(stamps), cap, counted, stamps, HOUR, now)
        if commit and plan:
            start = now + wait
            for site, pname, n, p in plan:
                e = sites.setdefault(site, {"t": []})
                e["p"] = pname
                e["t"] = sorted((e.get("t") or []) + [start + i * float(p["min_interval_s"]) for i in range(n)])
            save_ledger(led)
    return wait


def report(url, status, retry_after=None, cfg=None):
    """Tell the guard how a site answered. 403/429/503 start a back-off; several addresses in a
    row that do not exist do too. Returns a message when a back-off started, else None."""
    cfg = cfg or load_config()
    c = classify_url(url, cfg)
    if not c or c[1] in ("private", "deny"):
        return None
    site, pname = c
    p = profile(cfg, pname)
    try:
        status = int(status)
    except (TypeError, ValueError):
        return None
    now = time.time()
    msg = None
    with Lock():
        led = load_ledger()
        prune(led, now)
        e = led["sites"].setdefault(site, {"t": []})
        mins = float(p.get("backoff_min") or 0)
        if status in (p.get("backoff_codes") or []) and mins > 0:
            secs = mins * 60
            if retry_after:
                secs = min(max(secs, float(retry_after)), DAY)
            e["backoff_until"] = now + secs
            e["backoff_why"] = "HTTP %d" % status
        elif status in (404, 410) and int(p.get("miss_limit") or 0) > 0:
            m = e.get("miss") or {"n": 0}
            m = {"n": int(m.get("n") or 0) + 1, "t": now}
            e["miss"] = m
            if m["n"] >= int(p["miss_limit"]):
                e.pop("miss", None)
                e["backoff_until"] = now + 30 * 60
                e["backoff_why"] = "%d addresses in a row that do not exist (HTTP %d)" % (m["n"], status)
        elif 200 <= status < 400:
            e.pop("miss", None)
        if float(e.get("backoff_until") or 0) > now:
            msg = ("request-guard: %s answered with %s. Automated requests to it are paused until %s. Do not "
                   "try it again through another tool or address; if the work depends on it, tell the user."
                   % (site, e.get("backoff_why"), _when(e["backoff_until"])))
        e.setdefault("p", pname)
        save_ledger(led)
    if msg:
        log("BACKOFF site=%s why=%s until=%s" % (site, e.get("backoff_why"), _when(e["backoff_until"])))
    return msg


def wait_turn(url, max_wait=None, cfg=None):
    """Book one request to the URL's site and sleep until its turn. Raises Refusal at a limit."""
    if disabled():
        return 0.0
    cfg = cfg or load_config()
    if not cfg.get("enabled", True):
        return 0.0
    c = classify_url(url, cfg)
    if not c:
        raise Refusal("target", "?", "request-guard: cannot read a host in %r" % (url,), "no host in the URL")
    if c[1] == "private":
        return 0.0
    w = reserve([(c[0], c[1], 1)], cfg, max_wait=max_wait)
    log("PACED site=%s profile=%s wait=%.1fs by=%s" % (c[0], c[1], w, os.path.basename(sys.argv[0] or "?")))
    if w > 0.05:
        time.sleep(w)
    return w


# ---------------------------------------------------------------- reading a shell command

class Findings(object):
    def __init__(self):
        self.active = False     # something that sends web requests was found
        self.urls = []          # targets with a host the guard can read
        self.unknown = []       # requests whose target it cannot read
        self.uncounted = []     # reasons the number of requests cannot be read
        self.idents = []        # (header name, value) set explicitly
        self.payloads = []      # request bodies and credentials written in the command
        self.scanners = []      # (tool, site)
        self.manage = []        # attempts to change the guard's own limits or files
        self.paced = []         # parts that pace themselves through request_guard


class Ctx(object):
    def __init__(self, cfg, cwd, ps=False, depth=0, where="command", shared=None):
        self.cfg = cfg
        self.cwd = cwd
        self.ps = ps            # PowerShell syntax
        self.depth = depth
        self.where = where
        self.piped = False      # the command gets its arguments from xargs / parallel
        self.many = []          # why the command now being read runs more than once (a loop, xargs ...)
        self.vars = shared["vars"] if shared else {}
        self.dirs = shared["dirs"] if shared else []
        self.subs = shared["subs"] if shared else []    # text of every $(...) met so far

    def child(self, ps=None, where=None):
        c = Ctx(self.cfg, self.cwd, self.ps if ps is None else ps, self.depth + 1,
                where or self.where, {"vars": self.vars, "dirs": self.dirs, "subs": self.subs})
        c.many = list(self.many)
        return c


def base(tok):
    b = re.split(r"[\\/]", str(tok))[-1].lower()
    return b[:-4] if b.endswith(".exe") else b


def extract_heredocs(text):
    """Cut each here-document body out of the text; its marker becomes a placeholder token."""
    bodies = []
    pat = re.compile(r"<<-?[ \t]*(?:(['\"])(\w+)\1|\\?(\w+))([^\n]*)\n(.*?)\n[ \t]*(?:\2|\3)[ \t]*(?=\n|$)", re.S)

    def repl(m):
        bodies.append(m.group(5))
        return " __RG_HEREDOC_%d__ %s" % (len(bodies) - 1, m.group(4) or "")
    return pat.sub(repl, text), bodies


def extract_substitutions(text, subs, backticks=True):
    """Replace each $(...) and `...` with a numbered placeholder; the inner text goes into subs and
    is read later at the place where the placeholder stands (inside a loop or not)."""
    def keep(inner):
        subs.append(inner)
        return "__RG_SUBST_%d__" % (len(subs) - 1)
    for _ in range(60):
        m = re.search(r"\$\(([^()]*)\)", text)
        if not m:
            break
        text = text[:m.start()] + keep(m.group(1)) + text[m.end():]
    if backticks:
        text = re.sub(r"`([^`\n]*)`", lambda m: keep(m.group(1)), text)
    return text


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
            if text[i + 1] != "\n":                 # backslash-newline joins lines
                buf.append(c)
                buf.append(text[i + 1])
            i += 2
            continue
        elif c == "`" and ps and i + 1 < n and text[i + 1] == "\n":
            i += 2                                   # PowerShell line continuation
            continue
        elif c in seps:
            if c == "&" and ((buf and buf[-1] == ">") or (i + 1 < n and text[i + 1] == ">")):
                buf.append(c)                        # 2>&1 and &> are redirections, not separators
            else:
                seg = "".join(buf).strip()
                if seg:
                    out.append(seg)
                buf = []
                if ps and c in "{}":
                    out.append(c)                    # block limits, needed to tell what a loop encloses
        elif c == "#" and (not buf or buf[-1].isspace()):
            j = text.find("\n", i)                   # comment to end of line
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
        name = m.group(1) or m.group(2)
        return variables.get(name.lower(), m.group(0))
    for _ in range(2):
        tok = re.sub(r"\$\{(\w+)\}|\$(?:env:)?(\w+)", repl, tok)
    return tok


def collect_assignments(toks, ctx):
    """NAME=value (shell) and $name = value (PowerShell), so "$BASE/path" can be read later."""
    i = 0
    if toks and toks[0] in ("export", "local", "declare", "readonly", "typeset", "set"):
        i = 1
    while i < len(toks):
        m = re.match(r"^\$?([A-Za-z_]\w*)=(.*)$", toks[i])
        if not m:
            break
        ctx.vars[m.group(1).lower()] = substitute(m.group(2), ctx.vars)
        i += 1
    if len(toks) >= 3 and re.match(r"^\$[A-Za-z_]\w*$", toks[0]) and toks[1] == "=":
        ctx.vars[toks[0][1:].lower()] = substitute(toks[2], ctx.vars)


PLAIN_PREFIX = frozenset(("sudo", "doas", "time", "nohup", "exec", "command", "builtin", "nice", "ionice",
                          "caffeinate", "stdbuf", "then", "do", "else", "elif", "if", "!", "{", "env",
                          "timeout", "gtimeout"))
MULTIPLIERS = {"xargs": "xargs", "parallel": "parallel", "watch": "watch (repeats the command)"}
RUNNERS = frozenset(("uv", "poetry", "pipenv", "pdm", "hatch", "conda", "mamba"))
PREFIX_VALUE_OPTS = {
    "sudo": r"^-[ughpCDRTU]$", "doas": r"^-[uC]$", "timeout": r"^(-[sk]|--signal|--kill-after)$",
    "gtimeout": r"^(-[sk]|--signal|--kill-after)$", "nice": r"^-n$", "env": r"^(-u|--unset|-C|--chdir)$",
    "caffeinate": r"^-[tw]$", "xargs": r"^-[nIPdaLsEJRS]$", "watch": r"^(-n|--interval)$",
    "parallel": r"^(-j|--jobs|-a|--arg-file)$",
}


def _skip_options(toks, i, word):
    pat = PREFIX_VALUE_OPTS.get(word)
    while i < len(toks) and toks[i].startswith("-") and len(toks[i]) > 1:
        opt = toks[i]
        i += 1
        if pat and re.match(pat, opt) and i < len(toks):
            i += 1
    return i


def strip_prefixes(toks, ps=False):
    """Drop what stands in front of the real command: assignments, sudo, time, do, xargs ...
    -> (the command and its arguments, reasons it runs more than once, fed by xargs/parallel,
        starts a loop)."""
    i, n = 0, len(toks)
    many, piped, loop = [], False, False
    while i < n:
        t = toks[i]
        low = base(t)
        if re.match(r"^\$?[A-Za-z_]\w*=", t):
            i += 1
        elif low in ("while", "until") or (ps and low == "do"):
            loop = True
            i += 1
        elif low in ("for", "foreach", "select"):
            return [], many, piped, True
        elif low in ("foreach-object", "%"):
            loop = True
            i += 1
        elif low in MULTIPLIERS:
            many.append(MULTIPLIERS[low])
            piped = piped or low != "watch"
            i = _skip_options(toks, i + 1, low)
        elif low in PLAIN_PREFIX:
            i = _skip_options(toks, i + 1, low)
            if low in ("timeout", "gtimeout") and i < n and re.match(r"^\d+(\.\d+)?[smhd]?$", toks[i]):
                i += 1
        elif low in RUNNERS and i + 1 < n and toks[i + 1] == "run":
            i += 2
            while i < n and toks[i].startswith("-"):
                opt = toks[i]
                i += 1
                if opt in ("--with", "-n", "--name", "-p", "--python", "--prefix", "--project") and i < n:
                    i += 1
        elif low in ("npx", "bunx"):
            i += 1
            while i < n and toks[i].startswith("-"):
                opt = toks[i]
                i += 1
                if opt in ("-p", "--package") and i < n:
                    i += 1
        else:
            break
    return toks[i:], many, piped, loop


REDIR_RE = re.compile(r"^(\d*(>>?|<)|&>>?)")


def drop_redirections(args):
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        m = REDIR_RE.match(a)
        if m:
            if m.end() == len(a):        # the operator alone: the file is the next token
                skip = True
            continue
        out.append(a)
    return out


URL_RE = re.compile(r"(?:https?|ftp)://[^\s\"'<>\\)`|;]+", re.I)
URL_START = re.compile(r"^(?:https?|ftp)://", re.I)
VAR_RE = re.compile(r"\$[\w{(]|__RG_SUBST_\d+__|`")
GLOB_RE = re.compile(r"\[[^\]\s]+-[^\]\s]+\]|\{[^}\s]*,[^}\s]*\}|\{\d+\.\.\d+\}")


def short(s, n=70):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def add_target(tok, f, ctx, bare_ok, globbing=True):
    t = substitute(tok, ctx.vars).strip()
    if not t or HEREDOC_RE.match(t) or t.lower().startswith("file:"):
        return
    has_scheme = bool(URL_START.match(t))
    if not has_scheme and not bare_ok:
        if VAR_RE.search(t):
            f.unknown.append(short(tok))
        return
    hostpart = re.match(r"^(?:[A-Za-z][\w+.-]*://)?([^/?#]*)", t).group(1)
    if VAR_RE.search(hostpart) or not hostpart:
        f.unknown.append(short(tok))
        return
    if globbing and GLOB_RE.search(t):
        f.uncounted.append("a URL range (%s)" % short(tok, 50))
    f.urls.append(t if has_scheme else "http://" + t)


CURL_LONG_VALUE = frozenset((
    "abstract-unix-socket aws-sigv4 cacert capath cert cert-type ciphers config connect-timeout connect-to "
    "continue-at cookie cookie-jar data data-ascii data-binary data-raw data-urlencode dns-servers "
    "dump-header expect100-timeout form form-string ftp-port header interface json keepalive-time key "
    "key-type limit-rate local-port max-filesize max-redirs max-time netrc-file oauth2-bearer output "
    "output-dir pinnedpubkey proxy proxy-user quote range rate referer request resolve retry retry-delay "
    "retry-max-time speed-limit speed-time time-cond tls-max tlsv1.3-ciphers unix-socket upload-file url "
    "url-query user user-agent variable write-out").split())
CURL_SHORT_VALUE = "AbcCdDeEFHKmoPQrtTuUwxXyYz"
CURL_SHORT_NAME = {"A": "user-agent", "H": "header", "e": "referer", "K": "config"}


CURL_PAYLOAD = frozenset(("data", "data-ascii", "data-binary", "data-raw", "data-urlencode", "form",
                          "form-string", "json", "user", "d", "F", "u", "url-query"))


def _curl_value(name, val, f, targets, ctx):
    if name in ("user-agent", "header", "referer") or name in CURL_PAYLOAD:
        val = substitute(val, ctx.vars)
    if name in CURL_PAYLOAD:
        f.payloads.append(val)
    elif name == "user-agent":
        f.idents.append(("User-Agent", val))
    elif name == "header":
        k, _, v = val.partition(":")
        if v.strip():
            f.idents.append((k.strip(), v.strip()))
    elif name == "referer":
        f.idents.append(("Referer", val))
    elif name == "url":
        targets.append(val)
    elif name == "config":
        f.uncounted.append("a curl config file that lists the URLs")
        f.unknown.append("URLs listed in %s" % short(val, 40))


def _read_options(args, long_value, short_value, short_name, on_value, on_flag=None):
    """Walk curl/wget style arguments. Returns the plain (non-option) arguments."""
    plain = []
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if a == "--":
            plain.extend(args[i + 1:])
            break
        if a.startswith("--") and len(a) > 2:
            name, eq, val = a[2:].partition("=")
            if name in long_value:
                if not eq:
                    i += 1
                    val = args[i] if i < n else ""
                on_value(name, val)
            elif on_flag:
                on_flag(name)
        elif a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                ch = a[j]
                if ch in short_value:
                    val = a[j + 1:]
                    if not val:
                        i += 1
                        val = args[i] if i < n else ""
                    on_value(short_name.get(ch, ch), val)
                    break
                if on_flag:
                    on_flag(ch)
                j += 1
        else:
            plain.append(a)
        i += 1
    return plain


def read_curl(args, f, ctx):
    f.active = True
    targets = []
    noglob = []

    def flag(name):
        if name in ("g", "globoff"):
            noglob.append(1)
    f.uncounted.extend(ctx.many)
    plain = _read_options(drop_redirections(args), CURL_LONG_VALUE, CURL_SHORT_VALUE, CURL_SHORT_NAME,
                          lambda k, v: _curl_value(k, v, f, targets, ctx), flag)
    for t in targets + plain:
        add_target(t, f, ctx, bare_ok=True, globbing=not noglob)
    if ctx.piped and not (targets + plain):
        f.unknown.append("URLs handed to curl by xargs or parallel")


WGET_LONG_VALUE = frozenset((
    "output-document output-file append-output input-file tries timeout wait waitretry quota "
    "directory-prefix user-agent execute header post-data post-file referer user password load-cookies "
    "save-cookies level accept reject domains exclude-domains include-directories exclude-directories "
    "limit-rate bind-address ca-certificate body-data body-file method max-redirect dns-timeout "
    "connect-timeout read-timeout progress cut-dirs restrict-file-names http-user http-password "
    "proxy-user proxy-password secure-protocol base accept-regex reject-regex").split())
WGET_SHORT_VALUE = "OoaitTwQPUeDXIlAR"
WGET_SHORT_NAME = {"U": "user-agent", "i": "input-file"}
WGET_MANY = {"r": "a recursive download", "recursive": "a recursive download", "m": "a mirror download",
             "mirror": "a mirror download", "p": "a download of every file a page refers to",
             "page-requisites": "a download of every file a page refers to"}


def read_wget(args, f, ctx):
    f.active = True
    f.uncounted.extend(ctx.many)

    def value(name, val):
        if name in ("user-agent", "header", "referer"):
            val = substitute(val, ctx.vars)
        if name == "user-agent":
            f.idents.append(("User-Agent", val))
        elif name == "header":
            k, _, v = val.partition(":")
            if v.strip():
                f.idents.append((k.strip(), v.strip()))
        elif name == "referer":
            f.idents.append(("Referer", val))
        elif name == "input-file":
            f.uncounted.append("a file that lists the URLs")
            f.unknown.append("URLs listed in %s" % short(val, 40))

    def flag(name):
        if name in WGET_MANY:
            f.uncounted.append(WGET_MANY[name])
    args = [a for a in drop_redirections(args) if not re.match(r"^-n[vcdHp]$", a)]   # -np is not -n -p
    plain = _read_options(args, WGET_LONG_VALUE, WGET_SHORT_VALUE, WGET_SHORT_NAME, value, flag)
    for t in plain:
        add_target(t, f, ctx, bare_ok=True, globbing=False)
    if ctx.piped and not plain:
        f.unknown.append("URLs handed to wget by xargs or parallel")


GENERIC_SKIP = frozenset(("-headers", "-body", "-proxy", "-outfile", "-o", "--output", "--dir", "--header",
                          "--proxy", "-infile", "-contenttype", "-method", "-credential", "-websession",
                          "-sessionvariable", "-form", "-certificate", "--all-proxy", "--referer",
                          "-destination", "--out", "-d", "--auth", "-a"))
GENERIC_IDENT = frozenset(("-useragent", "--user-agent", "-useragent:"))
GENERIC_LIST = frozenset(("-i", "--input-file", "-listfile", "-dump-list"))


def read_generic(name, args, f, ctx):
    """httpie, lynx, w3m, aria2c and the PowerShell web cmdlets: targets are the URLs with a scheme."""
    f.active = True
    f.uncounted.extend(ctx.many)
    args = drop_redirections(args)
    prev = ""
    found = 0
    for a in args:
        low = a.lower()
        if prev in GENERIC_IDENT:
            f.idents.append(("User-Agent", substitute(a, ctx.vars)))
        elif prev in GENERIC_SKIP:
            pass
        elif name == "aria2c" and prev in GENERIC_LIST:
            f.uncounted.append("a file that lists the URLs")
            f.unknown.append("URLs listed in %s" % short(a, 40))
        elif a.startswith("-") and not URL_START.match(a):
            pass
        else:
            seen = len(f.urls) + len(f.unknown)
            add_target(a, f, ctx, bare_ok=False)
            found += len(f.urls) + len(f.unknown) - seen
        prev = low
    if ctx.piped and not found:
        f.unknown.append("URLs handed to %s by xargs or parallel" % name)


CLIENTS = {"curl": read_curl, "wget": read_wget}
GENERIC_CLIENTS = frozenset(("http", "https", "xh", "httpie", "lynx", "w3m", "aria2c", "invoke-webrequest",
                             "invoke-restmethod", "iwr", "irm", "start-bitstransfer"))
SCANNERS = frozenset(("nmap", "masscan", "zmap", "nikto", "gobuster", "dirb", "ffuf", "wfuzz", "feroxbuster",
                      "sqlmap", "hydra", "medusa", "wpscan", "nuclei", "amass", "subfinder"))
FILE_EXT = frozenset(("txt log json xml html htm csv lst out conf cfg yaml yml md py sh nmap gnmap "
                      "pdf zip gz tar db sqlite bak tmp").split())


def read_scanner(name, args, f, ctx):
    for a in drop_redirections(args):
        if a.startswith("-"):
            continue
        t = substitute(a, ctx.vars)
        host = None
        if URL_START.match(t):
            host = host_of(t)
        else:
            m = re.match(r"^([A-Za-z0-9.-]+\.[A-Za-z0-9-]+)(?:/\d+)?(?::\d+)?$", t)
            if m and m.group(1).rsplit(".", 1)[-1].lower() not in FILE_EXT:
                host = m.group(1).lower()
        if not host:
            continue
        c = classify(host, ctx.cfg)
        if c[1] != "private":
            f.scanners.append((name, c[0]))


CODE_INTERP = re.compile(r"^(python|pypy|py|node|nodejs|deno|bun|ruby|perl|php|tsx|ts-node|rscript|julia)[\d.]*$")
SHELL_INTERP = re.compile(r"^(bash|sh|zsh|dash|ksh|fish|pwsh|powershell)$")
INLINE_FLAGS = {"python": ("-c",), "pypy": ("-c",), "py": ("-c",), "node": ("-e", "--eval", "-p", "--print"),
                "nodejs": ("-e", "--eval", "-p", "--print"), "bun": ("-e", "--eval"), "deno": (),
                "ruby": ("-e",), "perl": ("-e", "-E"), "php": ("-r",), "tsx": ("-e", "--eval"),
                "ts-node": ("-e", "--eval"), "rscript": ("-e",), "julia": ("-e", "--eval")}
INTERP_VALUE = {"python": ("-W", "-X", "-Q"), "pypy": ("-W", "-X"), "py": ("-W", "-X"),
                "node": ("-r", "--require", "--import", "--loader"),
                "nodejs": ("-r", "--require", "--import", "--loader"),
                "ruby": ("-I", "-r"), "perl": ("-I", "-M")}
PWSH_VALUE = frozenset(("-executionpolicy", "-ep", "-ex", "-workingdirectory", "-wd", "-windowstyle", "-w",
                        "-configurationname", "-inputformat", "-outputformat", "-settingsfile",
                        "-custompipename", "-version"))


def _heredoc_body(args, bodies):
    for a in args:
        m = HEREDOC_RE.match(a)
        if m and int(m.group(1)) < len(bodies):
            return bodies[int(m.group(1))]
    return None


def read_code_interp(name, args, f, ctx, bodies):
    fam = re.sub(r"[\d.]+$", "", name)
    inline = INLINE_FLAGS.get(fam, ())
    values = INTERP_VALUE.get(fam, ())
    i = 0
    while i < len(args):
        a = args[i]
        if a in inline and i + 1 < len(args):
            read_code(args[i + 1], f, ctx, "inline %s code" % fam, args[i + 2:])
            return
        if fam in ("python", "pypy", "py") and a == "-m":
            return                               # a module (pip, venv, http.server ...), not a script
        if a in values:
            i += 2
            continue
        if fam in ("deno", "bun") and a == "run":
            i += 1
            continue
        if a == "-" or HEREDOC_RE.match(a):
            break
        if a.startswith("-"):
            i += 1
            continue
        read_script(a, args[i + 1:], f, ctx, shell=False)
        return
    body = _heredoc_body(args, bodies)
    if body is not None:
        read_code(body, f, ctx, "%s code on stdin" % fam, [])


def read_shell_interp(name, args, f, ctx, bodies):
    ps = name in ("pwsh", "powershell")
    i = 0
    while i < len(args):
        a = args[i]
        low = a.lower()
        if HEREDOC_RE.match(a):
            break
        if (ps and low in ("-c", "-command")) or (not ps and re.match(r"^-[A-Za-z]*c$", a)):
            if i + 1 < len(args):
                scan(" ".join(args[i + 1:]) if ps else args[i + 1], f, ctx.child(ps=ps))
            return
        if ps and low in ("-file", "-f"):
            if i + 1 < len(args):
                read_script(args[i + 1], args[i + 2:], f, ctx, shell=True, ps=True)
            return
        if a.startswith("-") or a.startswith("+"):
            i += 2 if (ps and low in PWSH_VALUE) or (not ps and a in ("-o", "+o", "-O", "+O", "--rcfile")) else 1
            continue
        read_script(a, args[i + 1:], f, ctx, shell=True, ps=ps)
        return
    body = _heredoc_body(args, bodies)
    if body is not None:
        scan(body, f, ctx.child(ps=ps, where="shell code on stdin"))


def read_ssh(args, f, ctx):
    """ssh [options] host command... : the command runs elsewhere but leaves from the same address."""
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("-"):
            break
        i += 2 if (len(a) == 2 and a[1] in "bcDEeFIiJLlmOopQRSWw") else 1
    rest = args[i + 1:]
    if rest:
        scan(" ".join(rest), f, ctx.child(where="command sent over ssh"))


# ---------------------------------------------------------------- reading code and scripts

HTTP_CODE = re.compile(r"""
    \brequests\s*\.\s*(?:get|post|put|patch|delete|head|request|Session)\b | \burllib\.request\b |
    \burlopen\s*\( | \burlretrieve\s*\( | \bhttp\.client\b | \bhttpx\b | \baiohttp\b | \burllib3\b |
    \bpycurl\b | \bmechanize\b | \bscrapy\b | \bselenium\b | \bplaywright\b | \bpuppeteer\b |
    \bfetch\s*\( | \baxios\b | \bnode-fetch\b | \bhttps?\.(?:get|request)\s*\( | \bgot\s*\( |
    \bNet::HTTP\b | \bLWP::\w+ | \bHTTP::Tiny\b | \bfile_get_contents\s*\(\s*['"]https?: | \bcurl_exec\b |
    \bWebClient\b | \bHttpClient\b | \bInvoke-(?:WebRequest|RestMethod)\b | \bdownload\.file\s*\( |
    \bDownloads\.download\b
    """, re.X)
LOOP_CODE = re.compile(r"""
    ^\s*for\b | \bfor\s*\( | \bfor\s+\w+(?:\s*,\s*\w+)*\s+in\b | \bwhile\b | \.map\s*\( | \.forEach\s*\( |
    \basyncio\.gather\b | \bThreadPool | \bProcessPool | \bconcurrent\.futures\b | \bPromise\.all |
    \.each\b | \bforeach\b | \bpmap\b | \bimap\b
    """, re.X | re.M | re.I)
IDENT_CODE = re.compile(r"""['"](User-Agent|Referer|From)['"]\s*[:=]\s*[fru]?['"]([^'"\n]{1,300})['"]""", re.I)
GUARD_REF = re.compile(r"request_guard|request-guard")


def strip_comments(text):
    return "\n".join(ln for ln in text.splitlines() if not re.match(r"^\s*(#|//|--\s|REM\s)", ln))


def read_code(text, f, ctx, where, args):
    """Python, JavaScript and the like: does it send requests, where to, and how many?"""
    if GUARD_REF.search(text):
        f.paced.append(where)
        return
    body = strip_comments(text)
    if not HTTP_CODE.search(body):
        return
    f.active = True
    f.uncounted.extend(ctx.many)
    urls = URL_RE.findall(body) + [a for a in args if URL_START.match(a)]
    for k, v in IDENT_CODE.findall(body):
        f.idents.append((k, v))
    if LOOP_CODE.search(body):
        f.uncounted.append("%s sends requests inside a loop" % where)
    if not urls:
        f.unknown.append("%s takes its target at run time" % where)
    for u in urls:
        add_target(u.rstrip(".,;:"), f, ctx, bare_ok=False, globbing=False)


def resolve_path(path, ctx):
    p = os.path.expandvars(os.path.expanduser(substitute(str(path), ctx.vars)))
    if VAR_RE.search(p):
        return None
    if os.name == "nt" and re.match(r"^/[A-Za-z]/", p):
        p = p[1] + ":" + p[2:]                       # Git Bash writes C:\Users as /c/Users
    cand = [Path(p)] if os.path.isabs(p) else [Path(d) / p for d in ([ctx.cwd] + ctx.dirs) if d]
    for c in cand:
        try:
            if c.is_file():
                return c.resolve()
        except OSError:
            pass
    return None


def trusted(path, cfg):
    import fnmatch
    s = str(path).replace("\\", "/")
    if s.startswith(str(PLUGIN).replace("\\", "/") + "/"):
        return True                                  # the plugin's own scripts (the fetcher paces itself)
    for pat in cfg.get("trusted_scripts") or []:
        pat = os.path.realpath(os.path.expanduser(str(pat))).replace("\\", "/")
        if fnmatch.fnmatch(s, pat):
            return True
    return False


def read_own_cli(args, f):
    sub = next((a for a in args if not a.startswith("-")), "")
    if sub in ("grant", "clear-backoff", "reset"):
        f.manage.append("request_guard.py %s" % sub)
    elif sub in ("fetch", "wait"):
        f.paced.append("request_guard.py %s" % sub)


def read_script(path, args, f, ctx, shell, ps=False):
    if str(path).lower().endswith("request_guard.py"):
        read_own_cli(args, f)
        return
    p = resolve_path(path, ctx)
    if p is None or ctx.depth >= MAX_DEPTH or trusted(p, ctx.cfg):
        return
    try:
        if p.stat().st_size > SCRIPT_MAX:
            return
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    where = "the script %s" % p.name
    if GUARD_REF.search(text):
        f.paced.append(where)
        return
    ext = p.suffix.lower()
    first = text.splitlines()[0] if text else ""
    if shell or ext in (".sh", ".bash", ".zsh", ".command", ".ps1", ".psm1") \
            or re.match(r"^#!.*\b(?:ba|z|da|k)?sh\b", first):
        sub = ctx.child(ps=ps or ext in (".ps1", ".psm1"), where=where)
        for k, a in enumerate(args, 1):              # $1, $2 ... as the script will see them
            sub.vars.setdefault(str(k), a)
        scan(text, f, sub)
    else:
        read_code(text, f, ctx, where, args)


def is_script_path(tok, ctx):
    if not re.search(r"[\\/]", tok) and not tok.startswith("."):
        return False
    if not re.search(r"\.(sh|bash|zsh|command|py|js|mjs|ts|rb|pl|php|ps1)$", tok, re.I):
        return False
    return resolve_path(tok, ctx) is not None


GUARD_FILES = re.compile(r"/request-guard/(?:scripts/)?(?:config|ledger|defaults)\.json")
GUARD_REDIR = re.compile(r">+\s*[\"']?[^\s\"'|;&]*/request-guard/(?:scripts/)?(?:config|ledger|defaults)\.json")
WRITE_CMDS = frozenset(("tee", "rm", "mv", "cp", "sed", "perl", "truncate", "dd", "install", "ln", "unlink",
                        "set-content", "out-file", "remove-item", "add-content", "copy-item", "move-item",
                        "clear-content", "del", "ri"))


def scan(text, f, ctx):
    """Read shell text: find every command that sends web requests and what it would contact."""
    if ctx.depth > MAX_DEPTH:
        return
    if GUARD_REDIR.search(text.replace("\\", "/")):
        f.manage.append("a write to the guard's own files")
    bodies = []
    if not ctx.ps:
        text, bodies = extract_heredocs(text)
    text = extract_substitutions(text, ctx.subs, backticks=not ctx.ps)
    segments = [tokenize(seg, ctx.ps) for seg in split_commands(text, ctx.ps)]
    for toks in segments:
        collect_assignments(toks, ctx)
    outer = list(ctx.many)      # what already multiplies this whole text (a loop around `bash -c` ...)
    loops = 0                   # shell: depth of for/while ... done
    blocks = []                 # PowerShell: one entry per open { }, True when it is a loop body
    pending = False             # PowerShell: a loop keyword was read, its { is still to come
    for toks in segments:
        if ctx.ps and toks in (["{"], ["}"]):
            if toks == ["{"]:
                blocks.append(pending)
                pending = False
            elif blocks:
                blocks.pop()
            continue
        if not ctx.ps and toks and toks[0] == "done":
            loops = max(0, loops - 1)
        whole = toks
        toks, many, piped, loop = strip_prefixes(toks, ctx.ps)
        if loop and ctx.ps:
            pending = True
        elif loop:
            loops += 1
        inside = loops > 0 or any(blocks)
        ctx.many = outer + (["a loop"] if inside else []) + many
        ctx.piped = piped
        for tok in whole:                                # $(...) runs where it stands, also in X=$(...)
            for k in SUBST_RE.findall(tok):
                if int(k) < len(ctx.subs):
                    scan(ctx.subs[int(k)], f, ctx.child())
        if not toks:
            continue
        cmd, args = toks[0], toks[1:]
        name = base(cmd)
        if name in WRITE_CMDS and any(GUARD_FILES.search(a.replace("\\", "/")) for a in args):
            f.manage.append("a write to the guard's own files")
        elif name == "cd" and args:
            d = os.path.expandvars(os.path.expanduser(substitute(args[0], ctx.vars)))
            ctx.dirs.append(d if os.path.isabs(d) else os.path.join(ctx.cwd or "", d))
        elif name in CLIENTS and not (ctx.ps and not cmd.lower().endswith(".exe")):
            CLIENTS[name](args, f, ctx)
        elif name in GENERIC_CLIENTS or name in CLIENTS:
            read_generic(name, args, f, ctx)
        elif name in SCANNERS:
            read_scanner(name, args, f, ctx)
        elif CODE_INTERP.match(name):
            read_code_interp(name, args, f, ctx, bodies)
        elif SHELL_INTERP.match(name):
            read_shell_interp(name, args, f, ctx, bodies)
        elif name == "ssh":
            read_ssh(args, f, ctx)
        elif name == "eval":
            scan(" ".join(args), f, ctx.child())
        elif name == "find" and ("-exec" in args or "-execdir" in args):
            k = args.index("-exec") if "-exec" in args else args.index("-execdir")
            sub = ctx.child()
            sub.many.append("find -exec")
            scan(" ".join(a for a in args[k + 1:] if a not in (";", "+", "{}")), f, sub)
        elif is_script_path(cmd, ctx):
            read_script(cmd, args, f, ctx, shell=False)
    ctx.many = outer


# ---------------------------------------------------------------- the decision

def bad_identifier(idents, cfg, urls=(), payloads=()):
    """-> (what, value, why) for the first identifier the guard does not accept, else None.
    never_send terms are looked for in headers, target URLs and request bodies; sensitive_terms in
    headers only (a research subject's name belongs in a search query, not in a header)."""
    import fnmatch
    if not cfg.get("check_identifiers", True):
        return None
    never = [str(t).lower() for t in (cfg.get("never_send") or []) if str(t).strip()]
    for what, values in (("URL", urls), ("request body", payloads)):
        for v in values:
            for t in never:
                if t in str(v).lower():
                    return what, short(v, 90), "it contains '%s', which never leaves this machine in a request" % t
    allowed = cfg.get("allowed_user_agents") or []
    terms = [str(t).lower() for t in (cfg.get("sensitive_terms") or []) if str(t).strip()]
    for name, value in idents:
        v = str(value).strip()
        for t in never + terms:
            if t in v.lower():
                return name, v, "it contains '%s', which this machine keeps out of requests" % t
        if name.lower() != "user-agent":
            continue
        if VAR_RE.search(v):
            return name, v, "the guard cannot read it; write it out in the command"
        if not any(fnmatch.fnmatchcase(v, pat) for pat in allowed):
            return name, v, "it is not a stock client or browser identifier"
    return None


def _deny(reason, short_text):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason},
            "systemMessage": "request-guard: held back a call -- %s." % short_text}


def _ask(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                   "permissionDecisionReason": reason}}


HOW = ("Do one of these: (1) send the requests one per tool call, each URL written out; (2) give the "
       "list to the paced fetcher, which waits between requests and stops at the limit: "
       "`%s fetch --out-dir DIR URL1 URL2 ...` or `--list urls.txt`; (3) in your own script, call "
       "request_guard.wait_turn(url) before each request (`%s help` shows how). " % (SELF, SELF))


def decide(f, cfg, tag, max_wait):
    """-> hook output (dict) or None. Books the requests and waits for their turn when allowed."""
    if f.manage:
        log("%s ASK manage: %s" % (tag, "; ".join(f.manage)))
        return _ask("request-guard: this would change a request limit, lift a back-off or write to the guard's "
                    "own files (%s). Only the user decides that." % "; ".join(sorted(set(f.manage))))
    if f.scanners:
        tool, site = f.scanners[0]
        log("%s DENY(scan) tool=%s site=%s" % (tag, tool, site))
        return _deny("request-guard: refused. %s against a public address (%s) reads as an attack to the "
                     "receiving side. Scanning tools run against private addresses only, unless the user "
                     "names a public target and says to go ahead." % (tool, site),
                     "%s against the public address %s" % (tool, site))
    if not f.active:
        return None
    if f.paced and not f.urls and not f.unknown:
        log("%s pass: paced by %s" % (tag, "; ".join(f.paced)))
        return None
    if any(p.startswith("request_guard.py wait") for p in f.paced):
        log("%s pass: booked by request_guard.py wait" % tag)     # `wait URL && curl URL`
        return None
    groups, order = {}, []
    for u in f.urls:
        c = classify_url(u, cfg)
        if not c:
            f.unknown.append(short(u))
            continue
        if c[1] == "private":
            continue
        if c[0] not in groups:
            groups[c[0]] = [c[1], 0]
            order.append(c[0])
        groups[c[0]][1] += 1
    public = bool(groups)
    if f.uncounted and (public or f.unknown):
        why = "; ".join(sorted(set(f.uncounted)))
        log("%s DENY(count) why=%s sites=%s" % (tag, why, ",".join(order) or "?"))
        return _deny("request-guard: refused. This call would send web requests whose number the guard cannot "
                     "read (%s). Every request to a public site is counted and spaced. %s%s"
                     % (why, HOW, NO_ROUTE), "request count cannot be read (%s)" % short(why, 60))
    if f.unknown:
        what = "; ".join(sorted(set(f.unknown))[:3])
        log("%s DENY(target) what=%s" % (tag, what))
        return _deny("request-guard: refused. The guard cannot read where this request goes (%s). Write the "
                     "URL out in the command, or fetch it with `%s fetch URL`. %s" % (what, SELF, NO_ROUTE),
                     "request target cannot be read")
    if not public:
        return None
    bad = bad_identifier(f.idents, cfg, f.urls, f.payloads)
    if bad:
        log("%s DENY(ident) in=%s" % (tag, bad[0]))
        return _deny("request-guard: refused. The request carries an identifier the guard does not accept -- "
                     "%s: %s (%s). Invented identifiers have carried project and person names to the very "
                     "sites being researched. Leave the User-Agent at the client's default or use a stock "
                     "browser string, and never put a project, person, folder or session name, or the "
                     "user's own name or address, in a header, a URL or a request body."
                     % (bad[0], short(bad[1], 90), bad[2]), "identifier not accepted (%s)" % bad[0])
    items = []
    for site in order:
        pname, n = groups[site]
        p = profile(cfg, pname)
        if pname != "deny" and n > int(p["max_per_command"]):
            log("%s DENY(burst) site=%s n=%d max=%d" % (tag, site, n, int(p["max_per_command"])))
            return _deny("request-guard: refused. %d requests to %s in one call; the limit is %d per call, %s "
                         "apart. Send them one per call, or use `%s fetch ...`, which spaces them. %s"
                         % (n, site, int(p["max_per_command"]), _span(p["min_interval_s"]), SELF, NO_ROUTE),
                         "%d requests to %s in one call" % (n, site))
        items.append((site, pname, n))
    try:
        wait = reserve(items, cfg, max_wait=max_wait)
    except Refusal as r:
        log("%s DENY(%s) site=%s" % (tag, r.kind, r.site))
        return _deny(r.message, r.short)
    log("%s ALLOW %s wait=%.1fs" % (tag, " ".join("%s(%s)x%d" % it for it in items), wait))
    if wait > 0.05:
        time.sleep(wait)
    return None


def findings_for(inp, cfg):
    tool = str(inp.get("tool_name") or "")
    ti = inp.get("tool_input") or {}
    if not isinstance(ti, dict):
        return None
    f = Findings()
    ctx = Ctx(cfg, str(inp.get("cwd") or os.getcwd()), ps=(tool == "PowerShell"))
    if tool == "WebFetch":
        if ti.get("url"):
            f.active = True
            add_target(str(ti["url"]), f, ctx, bare_ok=True, globbing=False)
    elif tool in ("Bash", "PowerShell"):
        cmd = ti.get("command")
        if isinstance(cmd, str) and cmd.strip():
            scan(cmd, f, ctx)
    elif tool.startswith("mcp__claude-in-chrome__"):
        short_name = tool.rsplit("__", 1)[-1]
        calls = [(short_name, ti)]
        if short_name == "browser_batch":
            calls = [(str(a.get("name") or ""), a.get("input") or {})
                     for a in (ti.get("actions") or []) if isinstance(a, dict)]
        for name, args in calls:
            url = str((args or {}).get("url") or "") if isinstance(args, dict) else ""
            if name.rsplit("__", 1)[-1] == "navigate" and url and url.lower() not in ("back", "forward"):
                f.active = True
                add_target(url, f, ctx, bare_ok=True, globbing=False)
    else:
        return None
    return f


def _tag(inp):
    sid = "".join(ch for ch in str(inp.get("session_id") or "?") if ch.isalnum() or ch in "-_")[:8]
    who = str(inp.get("agent_type") or "main")
    return "hook sid=%s who=%s tool=%s" % (sid, who, str(inp.get("tool_name") or "?").rsplit("__", 1)[-1])


def do_hook(inp):
    if disabled():
        return None
    cfg = load_config()
    if not cfg.get("enabled", True):
        return None
    f = findings_for(inp, cfg)
    if f is None:
        return None
    return decide(f, cfg, _tag(inp), float(cfg.get("max_hook_wait_s") or 0))


def _status_in(obj):
    if isinstance(obj, dict):
        for k in ("code", "status", "statusCode", "status_code"):
            v = obj.get(k)
            if isinstance(v, int) and 100 <= v <= 599:
                return v
        for v in obj.values():
            s = _status_in(v) if isinstance(v, (dict, list)) else None
            if s:
                return s
    elif isinstance(obj, list):
        for v in obj:
            s = _status_in(v)
            if s:
                return s
    elif isinstance(obj, str):
        m = re.search(r"(?:status(?: code)?|HTTP(?:/[\d.]+)?)\D{0,4}([1-5]\d\d)\b", obj, re.I)
        if m:
            return int(m.group(1))
    return None


def do_result(inp):
    """PostToolUse / PostToolUseFailure for WebFetch: learn from how the site answered."""
    if disabled():
        return None
    cfg = load_config()
    if not cfg.get("enabled", True) or str(inp.get("tool_name") or "") != "WebFetch":
        return None
    url = (inp.get("tool_input") or {}).get("url")
    if cfg.get("debug"):
        log("result raw=%s" % json.dumps({k: inp.get(k) for k in ("hook_event_name", "tool_response", "error")},
                                         default=str)[:700])
    status = _status_in(inp.get("tool_response")) or _status_in(str(inp.get("error") or ""))
    if not url or not status:
        return None
    msg = report(url, status, cfg=cfg)
    if not msg:
        return None
    return {"hookSpecificOutput": {"hookEventName": str(inp.get("hook_event_name") or "PostToolUse"),
                                   "additionalContext": msg},
            "systemMessage": "request-guard: a site refused a request; requests to it are paused."}


# ---------------------------------------------------------------- the paced fetcher

CONTENT_EXT = {"text/html": ".html", "application/pdf": ".pdf", "application/json": ".json",
               "text/plain": ".txt", "application/xml": ".xml", "text/xml": ".xml", "text/csv": ".csv"}


def _http(url, out_path, headers, agent, max_time, head):
    """One request. -> (status, bytes, content type, retry-after seconds or None, error text)."""
    import shutil
    import subprocess
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if curl:
        hdr = out_path + ".hdr"
        cmd = [curl, "-sS", "-L", "--max-redirs", "5", "--max-time", str(int(max_time)), "-o", out_path,
               "-D", hdr, "-w", "%{http_code}\t%{size_download}\t%{content_type}"]
        if head:
            cmd.append("-I")
        if agent:
            cmd += ["-A", agent]
        for h in headers:
            cmd += ["-H", h]
        cmd.append(url)
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=max_time + 20)
        except subprocess.TimeoutExpired:
            return 0, 0, "", None, "timed out"
        parts = r.stdout.decode("utf-8", "replace").strip().split("\t")
        status = int(parts[0]) if parts and parts[0].isdigit() else 0
        size = int(float(parts[1])) if len(parts) > 1 and parts[1].replace(".", "").isdigit() else 0
        ctype = parts[2] if len(parts) > 2 else ""
        retry = None
        try:
            for ln in open(hdr, encoding="latin-1"):
                if ln.lower().startswith("retry-after:") and ln.split(":", 1)[1].strip().isdigit():
                    retry = int(ln.split(":", 1)[1].strip())
            os.remove(hdr)
        except OSError:
            pass
        err = r.stderr.decode("utf-8", "replace").strip() if r.returncode else ""
        return status, size, ctype, retry, err
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, method="HEAD" if head else "GET")
    if agent:
        req.add_header("User-Agent", agent)
    for h in headers:
        k, _, v = h.partition(":")
        req.add_header(k.strip(), v.strip())
    try:
        with urllib.request.urlopen(req, timeout=max_time) as resp:
            data = resp.read()
            with open(out_path, "wb") as fh:
                fh.write(data)
            return resp.status, len(data), resp.headers.get("Content-Type") or "", None, ""
    except urllib.error.HTTPError as ex:
        ra = ex.headers.get("Retry-After") if ex.headers else None
        return ex.code, 0, "", int(ra) if ra and ra.isdigit() else None, ""
    except Exception as ex:
        return 0, 0, "", None, repr(ex)


def _out_name(k, url, ctype):
    from urllib.parse import urlsplit
    sp = urlsplit(url)
    leaf = [s for s in sp.path.split("/") if s]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", leaf[-1] if leaf else "index")[:60] or "index"
    ext = CONTENT_EXT.get((ctype or "").split(";")[0].strip().lower(), "")
    if ext and not stem.lower().endswith(ext):
        stem += ext
    return "%03d_%s_%s" % (k, re.sub(r"[^A-Za-z0-9.-]+", "_", sp.hostname or "host"), stem)


def do_fetch(argv):
    import argparse
    import tempfile
    ap = argparse.ArgumentParser(prog="request_guard.py fetch", description="Fetch URLs at the guard's pace.")
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--list", help="file with one URL per line (# starts a comment)")
    ap.add_argument("-o", "--output", help="file for the body (one URL only)")
    ap.add_argument("--out-dir", help="directory for the bodies (several URLs)")
    ap.add_argument("-H", "--header", action="append", default=[])
    ap.add_argument("-A", "--user-agent", default="")
    ap.add_argument("--head", action="store_true", help="headers only")
    ap.add_argument("--max-time", type=int, default=60)
    a = ap.parse_args(argv)
    urls = list(a.urls)
    if a.list:
        for ln in open(a.list, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                urls.append(ln)
    if not urls:
        ap.error("no URLs")
    if len(urls) > 1 and not a.out_dir:
        ap.error("several URLs need --out-dir")
    cfg = load_config()
    idents = [("User-Agent", a.user_agent)] if a.user_agent else []
    for h in a.header:
        k, _, v = h.partition(":")
        idents.append((k.strip(), v.strip()))
    bad = bad_identifier(idents, cfg, urls)
    if bad:
        sys.stderr.write("request-guard: refused. Identifier not accepted -- %s: %s (%s).\n" % bad)
        return 3
    if a.out_dir:
        os.makedirs(a.out_dir, exist_ok=True)
    stopped = {}                      # site -> why no more requests go to it in this run
    skipped = errors = 0
    for k, url in enumerate(urls, 1):
        c = classify_url(url, cfg)
        if not c:
            sys.stderr.write("skip\t-\t-\t%s\t(no host in the URL)\n" % url)
            skipped += 1
            continue
        site = c[0]
        if site in stopped:
            sys.stderr.write("skip\t-\t-\t%s\t(%s)\n" % (url, stopped[site]))
            skipped += 1
            continue
        try:
            wait_turn(url, cfg=cfg)
        except Refusal as r:
            stopped[site] = r.short
            sys.stderr.write("skip\t-\t-\t%s\t(%s)\n" % (url, r.short))
            skipped += 1
            continue
        if a.output:
            dest = a.output
        elif a.out_dir:
            dest = os.path.join(a.out_dir, "%03d.part" % k)
        else:
            fd, dest = tempfile.mkstemp(prefix="request-guard-")
            os.close(fd)
        status, size, ctype, retry, err = _http(url, dest, a.header, a.user_agent, a.max_time, a.head)
        msg = report(url, status, retry, cfg) if status else None
        if msg:
            stopped[site] = "%s answered HTTP %d; paused" % (site, status)
        if a.out_dir and os.path.exists(dest):
            final = os.path.join(a.out_dir, _out_name(k, url, ctype))
            os.replace(dest, final)
            dest = final
        if not (200 <= status < 400):
            errors += 1
        if not a.output and not a.out_dir:
            try:
                with open(dest, "rb") as fh:
                    sys.stdout.buffer.write(fh.read())
                os.remove(dest)
            except OSError:
                pass
            dest = "-"
        sys.stderr.write("%s\t%d\t%s\t%s%s\n" % (status or "ERR", size, dest, url, "\t(%s)" % err if err else ""))
    if skipped:
        sys.stderr.write("request-guard: %d of %d not fetched. %s %s\n" % (skipped, len(urls), NO_ROUTE, ASK_USER))
    return 3 if skipped else (4 if errors else 0)


# ---------------------------------------------------------------- command line

def _site_arg(arg, cfg):
    c = classify_url(arg, cfg)
    return c[0] if c else None


def do_status(args):
    cfg = load_config()
    now = time.time()
    led = load_ledger()
    prune(led, now)
    print("request-guard  enabled=%s  disabled-by-env=%s  state=%s" % (cfg.get("enabled", True), disabled(), state_dir()))
    print("limits     all public sites together: %s/hour; the hook waits at most %s s"
          % (cfg.get("total_per_hour"), cfg.get("max_hook_wait_s")))
    for name in sorted(cfg.get("profiles") or {}):
        p = profile(cfg, name)
        print("  %-8s %5s s apart  %4s/hour  %5s/day  %2s per call  back-off %s min on %s"
              % (name, p["min_interval_s"], p["max_per_hour"], p["max_per_day"], p["max_per_command"],
                 p.get("backoff_min"), p.get("backoff_codes") or "-"))
    local = local_config_path()
    print("config     %s%s" % (local, "" if local.exists() else "  (none; shipped defaults only)"))
    if cfg.get("deny"):
        print("never      %s" % ", ".join(cfg["deny"]))
    rows = sorted(led["sites"].items(), key=lambda kv: -len(kv[1].get("t") or []))
    print("sites with requests in the last 24 hours: %d" % len(rows))
    for site, e in rows[: int(args[0]) if args and args[0].isdigit() else 25]:
        ts = e.get("t") or []
        extra = ""
        if e.get("backoff_until"):
            extra += "  BACK-OFF until %s (%s)" % (_when(e["backoff_until"]), e.get("backoff_why"))
        if e.get("grant"):
            extra += "  grant +%s/hour +%s/day until %s" % (e["grant"].get("hour"), e["grant"].get("day"),
                                                             _when(e["grant"]["until"]))
        print("  %-32s %-8s hour %3d  day %4d  last %s%s"
              % (site, e.get("p") or "?", len([t for t in ts if t > now - HOUR]), len(ts),
                 _when(ts[-1]) if ts else "-", extra))
    lg = state_dir() / "guard.log"
    if lg.exists():
        print("log tail:")
        for ln in lg.read_text(encoding="utf-8", errors="replace").splitlines()[-10:]:
            print("  " + ln)
    return 0


def do_check(args):
    cfg = load_config()
    for url in args:
        c = classify_url(url, cfg)
        if not c:
            print("%s\tno host to read" % url)
        elif c[1] == "private":
            print("%s\tprivate address, not counted" % url)
        else:
            try:
                w = reserve([(c[0], c[1], 1)], cfg, commit=False)
                print("%s\tsite %s, profile %s: can go %s" % (url, c[0], c[1], "now" if w < 0.05 else "in " + _span(w)))
            except Refusal as r:
                print("%s\tsite %s, profile %s: %s" % (url, c[0], c[1], r.short))
    return 0


def do_wait(args):
    if not args:
        sys.stderr.write("usage: request_guard.py wait URL\n")
        return 2
    try:
        wait_turn(args[0])
    except Refusal as r:
        sys.stderr.write(r.message + "\n")
        return 3
    return 0


def do_grant(args):
    import argparse
    ap = argparse.ArgumentParser(prog="request_guard.py grant")
    ap.add_argument("site")
    ap.add_argument("--hour", type=int, default=0, help="extra requests per hour")
    ap.add_argument("--day", type=int, default=0, help="extra requests per day")
    ap.add_argument("--for-hours", type=float, default=2.0)
    a = ap.parse_args(args)
    cfg = load_config()
    site = _site_arg(a.site, cfg)
    if not site:
        sys.stderr.write("no host in %r\n" % a.site)
        return 2
    now = time.time()
    with Lock():
        led = load_ledger()
        prune(led, now)
        led["sites"].setdefault(site, {"t": []})["grant"] = {
            "hour": a.hour, "day": a.day, "until": now + a.for_hours * HOUR}
        save_ledger(led)
    log("GRANT site=%s hour=+%d day=+%d for=%.1fh" % (site, a.hour, a.day, a.for_hours))
    print("request-guard: %s may take %d more per hour and %d more per day until %s"
          % (site, a.hour, a.day, _when(now + a.for_hours * HOUR)))
    return 0


def do_clear(args):
    cfg = load_config()
    site = _site_arg(args[0], cfg) if args else None
    if not site:
        sys.stderr.write("usage: request_guard.py clear-backoff SITE\n")
        return 2
    with Lock():
        led = load_ledger()
        e = led["sites"].get(site) or {}
        had = e.pop("backoff_until", None)
        e.pop("backoff_why", None)
        e.pop("miss", None)
        save_ledger(led)
    log("CLEAR-BACKOFF site=%s had=%s" % (site, bool(had)))
    print("request-guard: back-off for %s %s" % (site, "lifted" if had else "was not set"))
    return 0


HELP = """request-guard: pacing web requests

One request:           write the URL out and send it; the guard books it and waits if needed.
Several requests:      %(self)s fetch --out-dir DIR URL1 URL2 ...
                       %(self)s fetch --list urls.txt --out-dir DIR
                       (long lists take time: run the command in the background or raise its timeout)
In a shell script:     %(self)s wait "$url" && curl -sS "$url" -o out.html
In a Python script:    import sys; sys.path.insert(0, %(here)r)
                       import request_guard
                       for url in urls:
                           request_guard.wait_turn(url)        # raises request_guard.Refusal at a limit
                           r = requests.get(url, timeout=60)
                           request_guard.report(url, r.status_code)  # lets the guard back off on 403/429/503
What is booked:        %(self)s status
What would happen:     %(self)s check URL

Identifiers: leave the User-Agent at the client's default or use a stock browser string. Never put
a project, person, folder or session name into a header.
At a limit: stop and tell the user. Only the user raises a limit.
""" % {"self": SELF, "here": str(HERE)}


def main(argv):
    cmd = (argv[1] if len(argv) > 1 else "").strip().lower()
    rest = argv[2:]
    if cmd == "status":
        return do_status(rest)
    if cmd == "check":
        return do_check(rest)
    if cmd == "fetch":
        return do_fetch(rest)
    if cmd == "wait":
        return do_wait(rest)
    if cmd == "grant":
        return do_grant(rest)
    if cmd == "clear-backoff":
        return do_clear(rest)
    if cmd in ("help", "-h", "--help"):
        print(HELP)
        return 0
    if cmd not in ("hook", "result"):
        sys.stderr.write("usage: request_guard.py hook|result  (hook JSON on stdin)\n"
                         "       request_guard.py fetch|wait|check|status|grant|clear-backoff|help\n")
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
    if not isinstance(inp, dict):
        inp = {}
    out = None
    try:
        out = do_hook(inp) if cmd == "hook" else do_result(inp)
    except Exception as ex:          # fail open, never lock a session
        log("%s ERROR %r" % (cmd, ex))
        out = None
    if out:
        sys.stdout.write(json.dumps(out))
        sys.stdout.flush()
    return 0


def _hook_safe_main(argv):
    """Hook subcommands exit 0 whatever happens: the hook command is an interpreter chain joined with
    `||`, and a non-zero exit from the first interpreter would make the next one run the guard again
    (a second booking for the same call). Other subcommands keep their real exit codes."""
    is_hook = len(argv) > 1 and argv[1].strip().lower() in ("hook", "result")
    try:
        return main(argv)
    except SystemExit as ex:
        if not is_hook:
            raise
        return 0
    except BaseException:
        if not is_hook:
            raise
        try:
            import traceback
            log("%s CRASH %s" % (argv[1], traceback.format_exc().rstrip().replace("\n", " | ")))
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(_hook_safe_main(sys.argv))
