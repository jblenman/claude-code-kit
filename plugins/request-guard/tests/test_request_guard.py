"""Tests for request-guard. No request leaves the machine: the hook is fed made-up tool calls, the
limits are exercised against a scratch ledger, and the fetcher talks to a server on 127.0.0.1.
Nothing under ~/.claude is read or written.

    python3 plugins/request-guard/tests/test_request_guard.py      (Windows: py ...)

Also checks the plugin shape (manifest, hooks.json, the interpreter chain under sh / Git Bash) and
runs `claude plugin validate --strict` when the claude CLI is on PATH.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent          # plugins/request-guard/tests
PLUGIN = HERE.parent                            # plugins/request-guard
SCRIPTS = PLUGIN / "scripts"
SCRIPT = SCRIPTS / "request_guard.py"
CLI = SCRIPT.as_posix()          # as written in a shell command (forward slashes on every platform)
SCRATCH = Path(tempfile.mkdtemp(prefix="rgtest-"))
os.environ["CLAUDE_REQUEST_GUARD_STATE"] = str(SCRATCH / "state")
os.environ["CLAUDE_REQUEST_GUARD_CONFIG"] = str(SCRATCH / "config.json")
os.environ.pop("CLAUDE_REQUEST_GUARD", None)
sys.path.insert(0, str(SCRIPTS))
import request_guard as rg  # noqa: E402

TEST_CONFIG = {
    "max_hook_wait_s": 3,
    "total_per_hour": 12,
    "profiles": {
        "default": {"min_interval_s": 2, "max_per_hour": 3, "max_per_day": 5, "max_per_command": 1},
        "bulk": {"min_interval_s": 1, "max_per_hour": 6, "max_per_day": 8, "max_per_command": 3},
        "local": {"min_interval_s": 1, "max_per_hour": 4, "max_per_day": 9, "max_per_command": 1,
                  "backoff_codes": [403, 429, 503], "backoff_min": 60, "miss_limit": 3},
    },
    "hosts": {"127.0.0.1": "local"},
    "deny": ["never.example.org"],
    "sensitive_terms": ["secretproject"],
    "never_send": ["owner.name"],
}
LAN = "http://10.0.0.5:8080"      # a private address: never counted


def fresh(extra=None):
    """Empty ledger, test config (plus extra keys)."""
    state = Path(os.environ["CLAUDE_REQUEST_GUARD_STATE"])
    state.mkdir(parents=True, exist_ok=True)
    for f in state.iterdir():
        if f.is_dir():
            f.rmdir()
        else:
            f.unlink()
    cfg = json.loads(json.dumps(TEST_CONFIG))
    cfg.update(extra or {})
    Path(os.environ["CLAUDE_REQUEST_GUARD_CONFIG"]).write_text(json.dumps(cfg), encoding="utf-8")
    return rg.load_config()


def read(cmd, tool="Bash", cwd=None):
    cfg = rg.load_config()
    return rg.findings_for({"tool_name": tool, "tool_input": {"command": cmd}, "cwd": cwd or str(SCRATCH)}, cfg)


def sites(f):
    cfg = rg.load_config()
    out = []
    for u in f.urls:
        c = rg.classify_url(u, cfg)
        out.append(c[0] if c and c[1] != "private" else None)
    return [s for s in out if s]


def verdict(cmd, tool="Bash", cwd=None):
    """deny kind / 'ask' / 'pass' for a command, against an empty ledger, without sleeping."""
    cfg = fresh()
    f = read(cmd, tool, cwd)
    real_sleep = rg.time.sleep
    rg.time.sleep = lambda s: None
    try:
        out = rg.decide(f, cfg, "test", 3.0)
    finally:
        rg.time.sleep = real_sleep
    if out is None:
        return "pass"
    d = out["hookSpecificOutput"]["permissionDecision"]
    if d == "ask":
        return "ask"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    for kind, mark in (("count", "whose number the guard cannot read"), ("target", "cannot read where"),
                       ("burst", "in one call"), ("ident", "identifier the guard does not accept"),
                       ("scan", "reads as an attack"), ("deny", "do-not-contact"), ("cap", "Limit reached"),
                       ("backoff", "are paused"), ("busy", "not yet")):
        if mark in reason:
            return kind
    return "deny:?"


def run_hook(payload, sub="hook", env=None):
    e = dict(os.environ)
    e.update(env or {})
    t0 = time.time()
    r = subprocess.run([sys.executable, str(SCRIPT), sub], input=json.dumps(payload).encode("utf-8"),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e, timeout=60)
    out = r.stdout.decode("utf-8").strip()
    return r.returncode, (json.loads(out) if out else None), time.time() - t0


class Classify(unittest.TestCase):
    def setUp(self):
        self.cfg = fresh()

    def test_private_addresses_are_never_counted(self):
        for h in ("localhost", "10.0.0.5", "172.16.4.2", "100.64.1.2", "nas", "printer.local",
                  "::1", "169.254.1.1", "fd00::1", "box.internal", "dev.test"):
            self.assertEqual(rg.classify(h, self.cfg)[1], "private", h)

    def test_public_sites_group_by_registrable_domain(self):
        self.assertEqual(rg.classify("www.example.com", self.cfg), ("example.com", "default"))
        self.assertEqual(rg.classify("a.b.example.co.uk", self.cfg), ("example.co.uk", "default"))
        self.assertEqual(rg.classify("8.8.8.8", self.cfg), ("8.8.8.8", "default"))

    def test_listed_hosts_take_their_profile(self):
        self.assertEqual(rg.classify("www.osti.gov", self.cfg), ("osti.gov", "bulk"))
        self.assertEqual(rg.classify("api.github.com", self.cfg), ("github.com", "infra"))
        self.assertEqual(rg.classify("www.google.com", self.cfg), ("www.google.com", "careful"))
        self.assertEqual(rg.classify("drive.google.com", self.cfg), ("google.com", "default"))
        self.assertEqual(rg.classify("127.0.0.1", self.cfg), ("127.0.0.1", "local"))

    def test_deny_list(self):
        self.assertEqual(rg.classify("www.never.example.org", self.cfg)[1], "deny")

    def test_host_of(self):
        self.assertEqual(rg.host_of("https://User:pw@WWW.Example.com:8443/a?b#c"), "www.example.com")
        self.assertEqual(rg.host_of("example.com/path"), "example.com")
        self.assertIsNone(rg.host_of("https://{host}/x"))


class Limits(unittest.TestCase):
    def setUp(self):
        self.cfg = fresh()

    def test_spacing(self):
        t = 1000000.0
        self.assertEqual(rg.reserve([("a.com", "default", 1)], self.cfg, now=t), 0.0)
        self.assertAlmostEqual(rg.reserve([("a.com", "default", 1)], self.cfg, now=t + 0.5), 1.5, places=3)
        self.assertEqual(rg.reserve([("b.com", "default", 1)], self.cfg, now=t + 0.5), 0.0)   # other site
        self.assertEqual(rg.reserve([("a.com", "default", 1)], self.cfg, now=t + 60), 0.0)

    def test_hour_cap_and_when_it_reopens(self):
        t = 1000000.0
        for k in range(3):
            rg.reserve([("a.com", "default", 1)], self.cfg, now=t + 100 * k)
        with self.assertRaises(rg.Refusal) as r:
            rg.reserve([("a.com", "default", 1)], self.cfg, now=t + 300)
        self.assertEqual(r.exception.kind, "hour")
        self.assertAlmostEqual(r.exception.retry_at, t + 3600, places=3)
        self.assertIn("only the user raises a limit", r.exception.message)
        self.assertEqual(rg.reserve([("a.com", "default", 1)], self.cfg, now=t + 3601), 0.0)

    def test_day_cap(self):
        t = 1000000.0
        for k in range(5):
            rg.reserve([("a.com", "default", 1)], self.cfg, now=t + 4000 * k)
        with self.assertRaises(rg.Refusal) as r:
            rg.reserve([("a.com", "default", 1)], self.cfg, now=t + 20000)
        self.assertEqual(r.exception.kind, "day")

    def test_total_cap_leaves_infra_out(self):
        t = 1000000.0
        for k in range(12):
            rg.reserve([("s%d.com" % k, "default", 1)], self.cfg, now=t + k)
        with self.assertRaises(rg.Refusal) as r:
            rg.reserve([("other.com", "default", 1)], self.cfg, now=t + 20)
        self.assertEqual(r.exception.kind, "total")
        self.assertEqual(rg.reserve([("github.com", "infra", 5)], self.cfg, now=t + 20), 0.0)

    def test_busy_books_nothing(self):
        t = 1000000.0
        rg.reserve([("a.com", "default", 1)], self.cfg, now=t)
        rg.reserve([("a.com", "default", 1)], self.cfg, now=t)          # slot at t+2
        with self.assertRaises(rg.Refusal) as r:
            rg.reserve([("a.com", "default", 1)], self.cfg, max_wait=3, now=t)   # would be t+4
        self.assertEqual(r.exception.kind, "busy")
        self.assertEqual(len(rg.load_ledger()["sites"]["a.com"]["t"]), 2)

    def test_several_in_one_call_are_booked_apart(self):
        t = 1000000.0
        rg.reserve([("osti.gov", "bulk", 3)], self.cfg, now=t)
        self.assertEqual(rg.load_ledger()["sites"]["osti.gov"]["t"], [t, t + 1, t + 2])
        self.assertAlmostEqual(rg.reserve([("osti.gov", "bulk", 1)], self.cfg, now=t), 3.0, places=3)

    def test_refusal_starts_a_back_off(self):
        self.assertIsNotNone(rg.report("https://www.a.com/x", 403, cfg=self.cfg))
        with self.assertRaises(rg.Refusal) as r:
            rg.reserve([("a.com", "default", 1)], self.cfg)
        self.assertEqual(r.exception.kind, "backoff")
        self.assertIsNone(rg.report("https://www.osti.gov/x", 403, cfg=self.cfg))      # bulk: 429 only
        self.assertIsNotNone(rg.report("https://www.osti.gov/x", 429, cfg=self.cfg))
        self.assertIsNone(rg.report("https://api.github.com/x", 403, cfg=self.cfg))    # infra: never
        self.assertIsNone(rg.report(LAN + "/x", 403, cfg=self.cfg))                      # private

    def test_three_missing_addresses_in_a_row(self):
        self.assertIsNone(rg.report("https://a.com/1", 404, cfg=self.cfg))
        self.assertIsNone(rg.report("https://a.com/2", 404, cfg=self.cfg))
        self.assertIsNone(rg.report("https://a.com/ok", 200, cfg=self.cfg))            # resets the count
        self.assertIsNone(rg.report("https://a.com/3", 404, cfg=self.cfg))
        self.assertIsNone(rg.report("https://a.com/4", 404, cfg=self.cfg))
        self.assertIn("do not exist", rg.report("https://a.com/5", 404, cfg=self.cfg))

    def test_grant_and_clear(self):
        t = time.time()
        for k in range(3):
            rg.reserve([("a.com", "default", 1)], self.cfg, now=t - 600 + k * 10)
        with self.assertRaises(rg.Refusal):
            rg.reserve([("a.com", "default", 1)], self.cfg, now=t)
        self.assertEqual(rg.main(["x", "grant", "a.com", "--hour", "2", "--day", "2"]), 0)
        self.assertEqual(rg.reserve([("a.com", "default", 1)], self.cfg, now=t), 0.0)
        rg.report("https://a.com/x", 503, cfg=self.cfg)
        self.assertEqual(rg.main(["x", "clear-backoff", "a.com"]), 0)
        self.assertGreaterEqual(rg.reserve([("a.com", "default", 1)], self.cfg, now=t + 5), 0.0)

    def test_old_bookings_are_pruned(self):
        t = 1000000.0
        rg.reserve([("a.com", "default", 1)], self.cfg, now=t)
        rg.reserve([("b.com", "default", 1)], self.cfg, now=t + 90000)
        self.assertEqual(list(rg.load_ledger()["sites"]), ["b.com"])

    def test_parallel_callers_each_get_their_own_slot(self):
        waits = []

        def one():
            waits.append(rg.reserve([("a.com", "bulk", 1)], self.cfg))
        threads = [threading.Thread(target=one) for _ in range(5)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        stamps = rg.load_ledger()["sites"]["a.com"]["t"]
        self.assertEqual(len(stamps), 5)
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(all(g >= 0.999 for g in gaps), gaps)


PASS = [
    "ls -la",
    "which curl",
    "curl --version",
    "echo 'curl https://example.com/a'",
    "grep -rn curl .",
    'git commit -m "use curl https://example.com/x in a loop: for u in a; do curl; done"',
    "git clone https://github.com/octocat/Hello-World.git",
    "python3 -c \"import json; print(json.dumps({'a': 1}))\"",
    "python3 -m pip install requests",
    "python3 -m http.server 8000",
    "curl -s https://example.com/a",
    'curl -sSL -o out.html "https://example.com/a?x=1&y=2"',
    "curl -s https://example.com/a > out.html 2>&1",
    "curl -s https://example.com/a &> /dev/null",
    "curl -fsSLo out.bin https://example.com/file.bin",
    'curl -s -H "Accept: application/json" -H "Authorization: Bearer $TOKEN" https://example.com/api',
    'curl -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)" https://example.com/',
    "curl -s -X POST -d '{\"u\": \"https://other.org/x\"}' https://example.com/api",
    'curl -s -e "https://www.google.com/" https://example.com/',
    'UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)"; curl -sL -A "$UA" https://example.com/',
    'curl -s "https://www.osti.gov/api/v1/records?author=secretproject" -o "/Users/x/Sync-owner.name@mail.example/out.json"',
    "URL=https://example.com/a; curl -s \"$URL\"",
    'BASE="https://example.com"; curl -s "${BASE}/a/b"',
    "RESULT=$(curl -s https://example.com/api); echo \"$RESULT\"",
    "for f in *.html; do grep -c nitride \"$f\"; done; curl -s https://example.com/a",
    "for f in a b; do n=$(grep -c x $f); echo $n; done; code=$(curl -s -o o.html -w '%{http_code}' https://example.com/a)",
    "ls | while read f; do wc -w \"$f\"; done && wget -q https://example.com/x",
    "cd /tmp && curl -s https://example.com/ | head -5",
    "timeout 30 curl -s https://example.com/",
    "sudo -u www curl -s https://example.com/",
    "time curl -s https://example.com/",
    "wget -q -O - https://example.com/x",
    "wget -np -nd -nv https://example.com/x",
    "curl -g -s 'https://example.com/item/[1-10]'",
    "curl -s %s/topics/json?poll=1" % LAN,
    "for t in a b; do curl -s %s/$t/json?poll=1; done" % LAN,
    "while true; do curl -s http://localhost:11434/api/tags; sleep 1; done",
    "curl -s https://api.github.com/repos/a/b && curl -s https://api.github.com/repos/a/c",
    "curl -s https://www.osti.gov/api/v1/records/1 https://www.osti.gov/api/v1/records/2",
    "python3 -c \"import requests; print(requests.get('https://example.com/a').status_code)\"",
    "ssh user@10.0.0.7 \"curl.exe -s https://example.com/a\"",
    "ssh nas 'uptime; df -h'",
    "nmap -sn 10.0.0.0/24",
    "nmap -p 22 -oN out.txt 10.0.0.5",
    "cat ~/.claude/request-guard/ledger.json",
    "jq . ~/.claude/request-guard/config.json > /tmp/copy.json",
    "cat > /tmp/f.sh <<'EOF'\nfor u in $(cat urls.txt); do curl -s \"$u\"; done\nEOF",
    "python3 %s fetch --out-dir /tmp/x https://example.com/a https://example.com/b" % CLI,
    "python3 %s wait https://example.com/a && curl -s https://example.com/a" % CLI,
    "python3 %s status" % CLI,
    "pwsh /Users/x/tools/notify.ps1 -From host/main -Message 'see https://example.com'",
    "open https://example.com/",
]
DENY = [
    ("count", "for u in a b c; do curl -s \"https://example.com/$u\"; done"),
    ("count", "for i in $(seq 1 60); do curl -s https://example.com/page/$i -o p$i.html; done"),
    ("count", "while read u; do curl -s \"$u\"; done < urls.txt"),
    ("count", "cat urls.txt | xargs -n1 curl -s"),
    ("count", "cat urls.txt | xargs -n 1 -P 4 curl -sO"),
    ("count", "parallel -j 8 curl -s {} :::: urls.txt"),
    ("count", "watch -n 5 curl -s https://example.com/status"),
    ("count", "wget -r -np https://example.com/docs/"),
    ("count", "wget --mirror https://example.com/"),
    ("count", "wget -p https://example.com/page"),
    ("count", "wget -q -i urls.txt"),
    ("count", "curl -K urls.cfg https://example.com/"),
    ("count", "curl -s 'https://example.com/item/[1-10]'"),
    ("count", "curl -s 'https://example.com/{a,b,c}.html'"),
    ("count", "curl -s https://example.com/p/{1..20}"),
    ("count", "find . -name '*.url' -exec curl -s https://example.com/{} \\;"),
    ("count", "for p in a b; do code=$(curl -sS -o $p.pdf -w '%{http_code}' \"https://example.com/$p.pdf\"); sleep 5; done"),
    ("count", "for p in a b; do set -- $p; url=\"https://example.com/$1\"; code=$(curl -s \"$url\"); done"),
    ("count", "for i in 1 2 3; do code=`curl -s https://example.com/x`; [ \"$code\" = 200 ] && break; done"),
    ("count", "ssh nas \"for i in 1 2 3; do curl -s https://example.com/\\$i; done\""),
    ("count", "bash -c 'for i in 1 2 3; do curl -s https://example.com/$i; done'"),
    ("count", "python3 - <<'EOF'\nimport requests\nfor i in range(10):\n    requests.get(f\"https://example.com/p/{i}\")\nEOF"),
    ("count", "python3 -c \"import requests\nfor i in range(9): requests.get('https://example.com/%d' % i)\""),
    ("count", "node -e \"urls.forEach(u => fetch('https://example.com/' + u))\""),
    ("target", "curl -s \"$URL\""),
    ("target", "curl -s \"$(cat url.txt)\""),
    ("target", "curl -s https://$HOST/path"),
    ("target", "python3 -c \"import sys, requests; print(requests.get(sys.argv[1]).text)\""),
    ("burst", "curl -s https://example.com/a https://example.com/b"),
    ("burst", "curl -s https://example.com/a && curl -s https://www.example.com/b"),
    ("burst", "curl -s https://example.com/a; wget -q https://example.com/b"),
    ("ident", 'curl -s -A "SecretProject-document-verification/1.0 (personal research)" https://example.com/'),
    ("ident", 'curl -s -H "User-Agent: research-bot/2.0" https://example.com/'),
    ("ident", 'curl -s --user-agent=my-crawler https://example.com/'),
    ("ident", 'wget -q -U "inquiry-fetcher" https://example.com/x'),
    ("ident", 'curl -s -e "https://secretproject.example/ref" https://example.com/'),
    ("ident", 'curl -s -H "X-Project: SecretProject" https://example.com/'),
    ("ident", "python3 -c \"import requests; requests.get('https://example.com/', headers={'User-Agent': 'my-bot/1.0'})\""),
    ("ident", 'curl -s -A "$UA" https://example.com/'),
    ("ident", 'UA="Personal research owner.name@mail.example"; curl -s -A "$UA" https://www.sec.gov/x'),
    ("ident", 'curl -s "https://api.crossref.org/works?query=x&mailto=Owner.Name@mail.example"'),
    ("ident", "curl -s -d 'email=owner.name@mail.example' https://example.com/form"),
    ("ident", 'curl -s -u owner.name:pw https://example.com/'),
    ("scan", "nmap -sV scanme.nmap.org"),
    ("scan", "nikto -h https://example.com"),
    ("scan", "gobuster dir -u https://example.com -w words.txt"),
    ("deny", "curl -s https://www.never.example.org/page"),
    ("ask", "python3 %s grant example.com --hour 50" % CLI),
    ("ask", "python3 %s clear-backoff example.com" % CLI),
    ("ask", "echo '{\"enabled\": false}' > ~/.claude/request-guard/config.json"),
    ("ask", "rm ~/.claude/request-guard/ledger.json"),
    ("ask", "sed -i '' 's/20/2000/' /Users/x/kit/plugins/request-guard/scripts/defaults.json"),
    ("ask", "echo x | tee -a ~/.claude/request-guard/config.json"),
]
POWERSHELL_PASS = [
    "Get-ChildItem C:\\Users",
    "Invoke-WebRequest -Uri https://example.com/a -OutFile out.html",
    "Invoke-RestMethod -Uri %s/topics -Method Post -Body 'see https://other.org'" % LAN,
    "$u = 'https://example.com/a'; Invoke-WebRequest -Uri $u",
    "Get-ChildItem | ForEach-Object { $_.Name }; Invoke-WebRequest -Uri https://example.com/a",
    "if ($x) { Invoke-WebRequest -Uri https://example.com/a -Headers @{ Accept = 'text/html' } }",
    "curl.exe -s https://example.com/a",
    "py C:/Users/user/claude-code-kit/plugins/request-guard/scripts/request_guard.py status",
]
POWERSHELL_DENY = [
    ("count", "$urls | ForEach-Object { Invoke-WebRequest $_ }"),
    ("count", "1..20 | % { iwr \"https://example.com/p/$_\" }"),
    ("count", "while ($true) { Invoke-RestMethod -Uri https://example.com/status; Start-Sleep 5 }"),
    ("count", "foreach ($u in $urls) { Invoke-RestMethod -Uri $u }"),
    ("target", "Invoke-WebRequest -Uri $target"),
    ("ident", "Invoke-WebRequest -Uri https://example.com/ -UserAgent 'my-project-agent'"),
    ("burst", "iwr https://example.com/a; iwr https://example.com/b"),
]


class Reading(unittest.TestCase):
    def test_commands_that_pass(self):
        for cmd in PASS:
            self.assertEqual(verdict(cmd), "pass", cmd)

    def test_commands_that_are_refused(self):
        for kind, cmd in DENY:
            self.assertEqual(verdict(cmd), kind, cmd)

    def test_powershell(self):
        for cmd in POWERSHELL_PASS:
            self.assertEqual(verdict(cmd, tool="PowerShell"), "pass", cmd)
        for kind, cmd in POWERSHELL_DENY:
            self.assertEqual(verdict(cmd, tool="PowerShell"), kind, cmd)

    def test_targets_found(self):
        fresh()
        cases = {
            "curl -s https://example.com/a": ["example.com"],
            "curl -s -H 'Referer: https://ref.org/' -d 'x=https://body.org' https://example.com/a": ["example.com"],
            "curl example.com/path": ["example.com"],
            "curl --url https://example.com/a -o https.html": ["example.com"],
            "curl -s https://example.com/a >out.html 2>err.log": ["example.com"],
            "wget -O out.html https://a.example.com/x": ["example.com"],
            "curl -s https://www.osti.gov/a https://example.com/b": ["osti.gov", "example.com"],
            "curl -s %s/x https://example.com/b" % LAN: ["example.com"],
            "A=https://one.org; B=$A/x; curl -s $B": ["one.org"],
        }
        for cmd, want in cases.items():
            self.assertEqual(sites(read(cmd)), want, cmd)

    def test_nothing_found_in_plain_commands(self):
        fresh()
        for cmd in ("ls", "git status", "echo curl", "make test", "cat notes.md | wc -w"):
            self.assertFalse(read(cmd).active, cmd)

    def test_scripts(self):
        fresh()
        d = SCRATCH / "scripts"
        d.mkdir(exist_ok=True)
        (d / "loop.py").write_text("import requests\nfor u in open('u.txt'):\n    requests.get(u)\n")
        (d / "one.py").write_text("import urllib.request\nprint(urllib.request.urlopen('https://example.com/a').read())\n")
        (d / "paced.py").write_text("import requests, request_guard\nfor u in urls:\n    request_guard.wait_turn(u)\n    requests.get(u)\n")
        (d / "lan.py").write_text("import requests\nwhile True:\n    requests.get('%s/api/tags')\n" % LAN)
        (d / "plain.py").write_text("# see https://example.com/docs\nprint('no requests here')\n")
        (d / "argv.py").write_text("import sys, requests\nprint(requests.get(sys.argv[1]).status_code)\n")
        (d / "loop.sh").write_text("#!/bin/bash\nfor i in 1 2 3; do\n  curl -s https://example.com/$i\ndone\n")
        (d / "one.sh").write_text("#!/bin/bash\ncurl -s \"$1\" -o out.html\n")
        (d / "many.ps1").write_text("$urls | ForEach-Object { Invoke-WebRequest $_ }\n")
        cwd = str(d)
        self.assertEqual(verdict("python3 loop.py", cwd=cwd), "count")
        self.assertEqual(verdict("python3 -u loop.py > log.txt", cwd=cwd), "count")
        self.assertEqual(verdict(".venv/bin/python %s" % (d / "loop.py").as_posix(), cwd=cwd), "count")
        self.assertEqual(verdict("cd %s && python3 loop.py" % d.as_posix(), cwd="/"), "count")
        self.assertEqual(verdict('python3 "%s"' % (d / "loop.py"), cwd="/"), "count")
        self.assertEqual(verdict("python3 one.py", cwd=cwd), "pass")
        self.assertEqual(sites(read("python3 one.py", cwd=cwd)), ["example.com"])
        self.assertEqual(verdict("python3 paced.py", cwd=cwd), "pass")
        self.assertEqual(sites(read("python3 paced.py", cwd=cwd)), [])
        self.assertEqual(verdict("python3 lan.py", cwd=cwd), "pass")
        self.assertEqual(verdict("python3 plain.py", cwd=cwd), "pass")
        self.assertFalse(read("python3 plain.py", cwd=cwd).active)
        self.assertEqual(verdict("python3 argv.py", cwd=cwd), "target")
        self.assertEqual(sites(read("python3 argv.py https://example.com/a", cwd=cwd)), ["example.com"])
        self.assertEqual(verdict("bash loop.sh", cwd=cwd), "count")
        self.assertEqual(verdict("./loop.sh", cwd=cwd), "count")
        self.assertEqual(sites(read("bash one.sh https://example.com/a", cwd=cwd)), ["example.com"])
        self.assertEqual(verdict("bash one.sh", cwd=cwd), "target")
        self.assertEqual(verdict("pwsh -NoProfile -File many.ps1", cwd=cwd), "count")
        self.assertEqual(verdict("python3 missing.py", cwd=cwd), "pass")
        trusted = fresh({"trusted_scripts": [(d / "*.py").as_posix()]})
        f = rg.findings_for({"tool_name": "Bash", "tool_input": {"command": "python3 loop.py"}, "cwd": cwd}, trusted)
        self.assertFalse(f.active)

    def test_plugin_scripts_are_trusted(self):
        """Scripts inside the plugin pace themselves (the fetcher is one), so the guard does not read them."""
        cfg = fresh()
        self.assertTrue(rg.trusted(SCRIPT, cfg))
        self.assertTrue(rg.trusted(PLUGIN / "skills" / "fetch-paced" / "helper.py", cfg))
        self.assertFalse(rg.trusted(SCRATCH / "elsewhere.py", cfg))
        self.assertFalse(rg.trusted(PLUGIN.parent / "other-plugin" / "x.py", cfg))

    def test_browser_and_webfetch(self):
        cfg = fresh()
        nav = rg.findings_for({"tool_name": "mcp__claude-in-chrome__navigate",
                               "tool_input": {"url": "www.linkedin.com/in/someone", "tabId": 5}}, cfg)
        self.assertEqual(sites(nav), ["linkedin.com"])
        back = rg.findings_for({"tool_name": "mcp__claude-in-chrome__navigate",
                                "tool_input": {"url": "back", "tabId": 5}}, cfg)
        self.assertFalse(back.active)
        batch = rg.findings_for({"tool_name": "mcp__claude-in-chrome__browser_batch", "tool_input": {"actions": [
            {"name": "navigate", "input": {"url": "https://example.com/a", "tabId": 1}},
            {"name": "computer", "input": {"action": "screenshot", "tabId": 1}},
            {"name": "navigate", "input": {"url": "https://example.com/b", "tabId": 1}}]}}, cfg)
        self.assertEqual(sites(batch), ["example.com", "example.com"])
        self.assertEqual(rg.decide(batch, cfg, "test", 3.0)["hookSpecificOutput"]["permissionDecision"], "deny")
        fetch = rg.findings_for({"tool_name": "WebFetch", "tool_input": {"url": "https://example.com/a", "prompt": "x"}}, cfg)
        self.assertEqual(sites(fetch), ["example.com"])
        self.assertIsNone(rg.findings_for({"tool_name": "Read", "tool_input": {"file_path": "/x"}}, cfg))


class Hook(unittest.TestCase):
    def setUp(self):
        fresh()

    def fetch(self, url, **kw):
        return run_hook({"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "WebFetch",
                         "tool_input": {"url": url, "prompt": "p"}, "cwd": str(SCRATCH)}, **kw)

    def test_first_passes_silently_second_waits_then_passes(self):
        code, out, took = self.fetch("https://example.com/a")
        self.assertEqual((code, out), (0, None))
        self.assertLess(took, 1.5)
        code, out, took = self.fetch("https://example.com/b")
        self.assertEqual((code, out), (0, None))
        self.assertGreater(took, 1.0)                      # waited for the 2 s spacing
        self.assertEqual(len(rg.load_ledger()["sites"]["example.com"]["t"]), 2)

    def test_limit_is_refused_with_a_reason(self):
        t = time.time()
        for k in range(3):
            rg.reserve([("example.com", "default", 1)], rg.load_config(), now=t - 900 + k * 60)
        code, out, _ = self.fetch("https://example.com/c")
        self.assertEqual(code, 0)
        h = out["hookSpecificOutput"]
        self.assertEqual((h["hookEventName"], h["permissionDecision"]), ("PreToolUse", "deny"))
        self.assertIn("Limit reached for example.com", h["permissionDecisionReason"])
        self.assertIn("systemMessage", out)

    def test_never_prints_allow(self):
        for url in ("https://example.com/a", LAN + "/x", "https://api.github.com/x"):
            self.assertIsNone(self.fetch(url)[1])

    def test_switch_off_by_environment_and_by_config(self):
        t = time.time()
        for k in range(3):
            rg.reserve([("example.com", "default", 1)], rg.load_config(), now=t - 900 + k * 60)
        self.assertIsNone(self.fetch("https://example.com/c", env={"CLAUDE_REQUEST_GUARD": "off"})[1])
        fresh({"enabled": False})
        for k in range(3):
            rg.reserve([("example.com", "default", 1)], rg.load_config(), now=t - 900 + k * 60)
        self.assertIsNone(self.fetch("https://example.com/c")[1])

    def test_fails_open(self):
        e = dict(os.environ)
        for raw in (b"", b"not json", b"[1, 2]", b'{"tool_name": "Bash"}', b'{"tool_name": "Bash", "tool_input": 5}',
                    b'{"tool_name": "Bash", "tool_input": {"command": "curl \'unterminated https://example.com"}}'):
            r = subprocess.run([sys.executable, str(SCRIPT), "hook"], input=raw, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=e, timeout=30)
            self.assertEqual(r.returncode, 0, raw)
        blocked = SCRATCH / "a-file-not-a-dir"
        blocked.write_text("x")
        code, out, _ = self.fetch("https://example.com/a", env={"CLAUDE_REQUEST_GUARD_STATE": str(blocked)})
        self.assertEqual((code, out), (0, None))
        Path(os.environ["CLAUDE_REQUEST_GUARD_CONFIG"]).write_text("{ broken", encoding="utf-8")
        code, out, _ = self.fetch("https://example.com/a")           # shipped defaults still apply
        self.assertEqual((code, out), (0, None))

    def test_a_refused_request_pauses_the_site(self):
        payload = {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "WebFetch",
                   "tool_input": {"url": "https://example.com/a"},
                   "tool_response": {"code": 403, "codeText": "Forbidden", "bytes": 0, "result": ""}}
        code, out, _ = run_hook(payload, sub="result")
        self.assertEqual(code, 0)
        self.assertIn("paused", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.fetch("https://example.com/b")[1]["hookSpecificOutput"]["permissionDecision"], "deny")
        ok = dict(payload, tool_response={"code": 200, "result": "fine"}, tool_input={"url": "https://other.org/"})
        self.assertIsNone(run_hook(ok, sub="result")[1])
        failed = {"hook_event_name": "PostToolUseFailure", "tool_name": "WebFetch",
                  "tool_input": {"url": "https://third.org/a"}, "error": "Request failed with status code 429"}
        self.assertIsNotNone(run_hook(failed, sub="result")[1])

    def test_bash_loop_is_refused_through_the_hook(self):
        code, out, _ = run_hook({"session_id": "s2", "agent_type": "general-purpose", "tool_name": "Bash",
                                 "cwd": str(SCRATCH), "tool_input": {
                                     "command": "for i in $(seq 1 60); do curl -s https://example.com/p/$i; done"}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("request_guard.py", out["hookSpecificOutput"]["permissionDecisionReason"])
        log = (Path(os.environ["CLAUDE_REQUEST_GUARD_STATE"]) / "guard.log").read_text()
        self.assertIn("who=general-purpose", log)
        self.assertIn("DENY(count)", log)

    def test_plain_bash_is_fast_and_silent(self):
        code, out, took = run_hook({"tool_name": "Bash", "tool_input": {"command": "ls -la && git status"}})
        self.assertEqual((code, out), (0, None))
        self.assertLess(took, 1.0)


class Server(object):
    """A web server on 127.0.0.1 that records when each request arrived."""

    def __enter__(self):
        import http.server
        hits = self.hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append((time.time(), self.path, self.headers.get("User-Agent")))
                code = 403 if self.path.startswith("/forbidden") else 404 if self.path.startswith("/missing") else 200
                body = b"<html>ok</html>"
                self.send_response(code)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


def run_cli(*args):
    r = subprocess.run([sys.executable, str(SCRIPT)] + list(args), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, env=dict(os.environ), timeout=120)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


class Fetcher(unittest.TestCase):
    def setUp(self):
        fresh()

    def test_paces_and_stops_at_the_limit(self):
        out = SCRATCH / "out"
        with Server() as s:
            urls = ["%s/page/%d" % (s.base, k) for k in range(6)]
            code, _, err = run_cli("fetch", "--out-dir", str(out), *urls)
            self.assertEqual(code, 3, err)                       # limit is 4 an hour for this host
            self.assertEqual(len(s.hits), 4)
            gaps = [b[0] - a[0] for a, b in zip(s.hits, s.hits[1:])]
            self.assertTrue(all(g >= 0.95 for g in gaps), gaps)  # 1 s apart
            self.assertEqual(err.count("hour limit for 127.0.0.1 reached"), 2)
            self.assertEqual(len(list(out.glob("*.html"))), 4)

    def test_stops_when_the_site_refuses(self):
        with Server() as s:
            code, _, err = run_cli("fetch", "--out-dir", str(SCRATCH / "out2"),
                                   s.base + "/ok", s.base + "/forbidden", s.base + "/never-asked")
            self.assertEqual(code, 3, err)
            self.assertEqual([h[1] for h in s.hits], ["/ok", "/forbidden"])
        self.assertEqual(verdict_keep("curl -s http://127.0.0.1:1/x"), "backoff")

    def test_single_url_goes_to_stdout_and_identifiers_are_checked(self):
        with Server() as s:
            code, out, err = run_cli("fetch", s.base + "/ok")
            self.assertEqual((code, out), (0, "<html>ok</html>"), err)
            self.assertTrue(s.hits[0][2].startswith("curl/") or s.hits[0][2].startswith("Python-urllib/"), s.hits)
            code, _, err = run_cli("fetch", "-A", "my-project-bot/1.0", s.base + "/ok")
            self.assertEqual(code, 3)
            self.assertIn("Identifier not accepted", err)
            self.assertEqual(len(s.hits), 1)

    def test_wait_and_check(self):
        self.assertEqual(run_cli("wait", "https://example.com/a")[0], 0)
        self.assertIn("can go in", run_cli("check", "https://example.com/b")[1])
        self.assertIn("private address", run_cli("check", LAN + "/")[1])
        self.assertEqual(len(rg.load_ledger()["sites"]["example.com"]["t"]), 1)       # check books nothing
        self.assertEqual(run_cli("status")[0], 0)
        self.assertIn("wait_turn", run_cli("help")[1])


def verdict_keep(cmd):
    """Like verdict(), but against the ledger as it stands."""
    cfg = rg.load_config()
    f = read(cmd)
    real_sleep = rg.time.sleep
    rg.time.sleep = lambda s: None
    try:
        out = rg.decide(f, cfg, "test", 3.0)
    finally:
        rg.time.sleep = real_sleep
    if out is None:
        return "pass"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    return "backoff" if "are paused" in reason else "cap" if "Limit reached" in reason else "deny:?"


# ---------------------------------------------------------------- the plugin around the script

EVENTS = {                                             # event -> (matcher or None, subcommand, timeout)
    "PreToolUse": ("Bash|PowerShell|WebFetch|mcp__claude-in-chrome__navigate|mcp__claude-in-chrome__browser_batch", "hook", 60),
    "PostToolUse": ("WebFetch", "result", 10),
    "PostToolUseFailure": ("WebFetch", "result", 10),
}


def git_bash():
    """Git for Windows, which Claude Code runs hook commands with: (bash.exe or usr/bin/sh.exe, usr/bin dir)
    or None. Never C:/Windows/System32/bash.exe, which is WSL."""
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


def sh_wrapper(path, body):
    """A #!/bin/sh script (LF endings, also under Git Bash) that ends by exec'ing this interpreter."""
    path.write_bytes(("#!/bin/sh\n%sexec %s \"$@\"\n" % (body, shlex.quote(Path(sys.executable).as_posix()))).encode("utf-8"))
    os.chmod(str(path), 0o755)


class Plugin(unittest.TestCase):
    def test_manifest(self):
        m = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(m["name"], "request-guard")
        for k in ("version", "description", "author"):
            self.assertIn(k, m)
        self.assertFalse((PLUGIN / "CLAUDE.md").exists())           # not loaded; validate warns
        self.assertTrue((PLUGIN / "skills" / "fetch-paced" / "SKILL.md").is_file())

    def test_hooks_json_shape_and_interpreter_chain(self):
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        hk = hooks["hooks"]
        self.assertEqual(set(hk), set(EVENTS))
        for event, (matcher, sub, timeout) in EVENTS.items():
            groups = hk[event]
            self.assertEqual(len(groups), 1, event)
            self.assertEqual(groups[0].get("matcher"), matcher, event)
            self.assertEqual(len(groups[0]["hooks"]), 1, event)
            h = groups[0]["hooks"][0]
            self.assertEqual(h["type"], "command")
            self.assertEqual(h["timeout"], timeout, event)
            self.assertNotIn("args", h)                              # shell form: || and ${VAR:-default} need a shell
            call = '"${CLAUDE_PLUGIN_ROOT}/scripts/request_guard.py" %s' % sub
            self.assertEqual(h["command"], '"${KIT_PYTHON:-python3}" %s || python3 %s || py %s || exit 1' % (call, call, call), event)
        self.assertTrue(hk["PreToolUse"][0]["hooks"][0].get("statusMessage"))

    def test_chain_runs_the_guard_exactly_once(self):
        """The shipped PreToolUse command under the shell Claude Code uses (sh; Git Bash on Windows):
        one guard.log line per run. KIT_PYTHON set, unset, and pointing nowhere -> exactly one run each."""
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        chain = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        if os.name == "nt":
            gb = git_bash()
            if not gb:
                self.skipTest("Git Bash not found (Claude Code runs hooks with it on Windows)")
            shell, shell_bin, sim_shell = str(gb[0]), str(gb[1]), str(gb[1] / "sh.exe")
        else:
            if not shutil.which("sh"):
                self.skipTest("no sh")
            shell, shell_bin, sim_shell = "sh", "/bin", "sh"
        d = Path(tempfile.mkdtemp(prefix="rgchain-"))
        try:
            env = {"CLAUDE_REQUEST_GUARD_STATE": str(d / "state"), "CLAUDE_REQUEST_GUARD_CONFIG": str(d / "config.json")}
            glog = d / "state" / "guard.log"
            loop = json.dumps({"session_id": "t", "hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(d),
                               "tool_input": {"command": "for u in a b; do curl https://example.com/$u; done"}})
            base = dict(os.environ, CLAUDE_PLUGIN_ROOT=PLUGIN.as_posix(), **env)   # forward slashes, as Claude Code substitutes it
            for k in ("CLAUDE_REQUEST_GUARD", "KIT_PYTHON"):
                base.pop(k, None)

            def chain_run(e, sh):
                before = glog.read_text(encoding="utf-8").count("\n") if glog.exists() else 0
                p = subprocess.run([sh, "-c", chain], input=loop, capture_output=True, text=True, env=e, timeout=60)
                out = json.loads(p.stdout) if p.stdout.strip() else {}
                runs = (glog.read_text(encoding="utf-8").count("\n") if glog.exists() else 0) - before
                ok = p.returncode == 0 and out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
                return ok, runs, "runs=%d rc=%d %s" % (runs, p.returncode, p.stdout + p.stderr)

            if os.name == "nt":          # give the chain a python3 that is this interpreter (Store aliases may be denied)
                pybin = d / "pybin"
                pybin.mkdir()
                sh_wrapper(pybin / "python3", "")
                base = dict(base, PATH=str(pybin) + os.pathsep + shell_bin + os.pathsep + base.get("PATH", ""))
            ok, runs, detail = chain_run(base, shell)
            self.assertTrue(ok and runs == 1, "KIT_PYTHON unset: " + detail)
            bindir = d / "bin"
            bindir.mkdir()
            marker = d / "kit-python.log"
            sh_wrapper(bindir / "kitpy", "echo run >> %s\n" % shlex.quote(marker.as_posix()))
            ok, runs, detail = chain_run(dict(base, KIT_PYTHON=(bindir / "kitpy").as_posix()), shell)
            used = marker.read_text(encoding="utf-8").count("run") if marker.exists() else 0
            self.assertTrue(ok and runs == 1 and used == 1, "KIT_PYTHON set: %s used=%d" % (detail, used))
            ok, runs, detail = chain_run(dict(base, KIT_PYTHON=(d / "no-such-python").as_posix()), shell)
            self.assertTrue(ok and runs == 1, "KIT_PYTHON pointing nowhere: " + detail)
            sh_wrapper(bindir / "py", "")
            path2 = str(bindir) + os.pathsep + shell_bin                    # sh + coreutils, no python3
            if shutil.which("python3", path=path2) is None and shutil.which(sim_shell, path=path2):
                ok, runs, detail = chain_run(dict(base, PATH=path2), sim_shell)
                self.assertTrue(ok and runs == 1, "python3 absent, py present: " + detail)
        finally:
            shutil.rmtree(str(d), ignore_errors=True)

    def test_claude_plugin_validate(self):
        claude = shutil.which("claude")
        if not claude:
            self.skipTest("claude not on PATH")
        p = subprocess.run([claude, "plugin", "validate", str(PLUGIN), "--strict"], capture_output=True, text=True, timeout=120)
        self.assertTrue(p.returncode == 0 and "Validation passed" in p.stdout, p.stdout + p.stderr)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=1)
    finally:
        shutil.rmtree(str(SCRATCH), ignore_errors=True)
