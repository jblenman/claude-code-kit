"""Tests for action-guard.

    python3 plugins/action-guard/tests/test_action_guard.py      (Windows: py ...)

Everything runs against a scratch state directory and the policy in TEST_CONFIG, never against
~/.claude. The hook tests start action_guard.py as a subprocess the way Claude Code does. The
Plugin tests check the manifest, hooks.json and run `claude plugin validate --strict` when the CLI
is on PATH.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent          # plugins/action-guard/tests
PLUGIN = HERE.parent
SCRIPTS = PLUGIN / "scripts"
SCRIPT = SCRIPTS / "action_guard.py"
STATE = tempfile.mkdtemp(prefix="action-guard-test-")
CONFIG = Path(STATE) / "test-config.json"
TEST_CONFIG = {
    "protected_hosts": ["app.example.com"],
    "allowed_methods": ["GET", "HEAD", "OPTIONS"],
    "denied_paths": ["/admin", "/api/v1/users"],
    "ask_paths": ["/api/v1/orders"],
    "write_ok_paths": ["/api/v1/comments"],
    "mcp_rules": [{"server": "myapp", "deny_tools": ["delete_item"], "ask_tools": ["update_item"]}],
}
CONFIG.write_text(json.dumps(TEST_CONFIG), encoding="utf-8")
os.environ["CLAUDE_ACTION_GUARD_STATE"] = STATE
os.environ["CLAUDE_ACTION_GUARD_CONFIG"] = str(CONFIG)
os.environ.pop("CLAUDE_ACTION_GUARD", None)
sys.path.insert(0, str(SCRIPTS))
import action_guard as ag  # noqa: E402

CFG = ag.load_config()
PY = sys.executable


def decide(command, tool="Bash", tabs=None):
    inp = {"tool_name": tool, "tool_input": {"command": command}, "cwd": STATE}
    if tool == "WebFetch":
        inp["tool_input"] = {"url": command}
    tabs = tabs if tabs is not None else {}
    f = ag.findings_for(inp, CFG, tabs)
    out = ag.policy(f, CFG, tabs, "test")
    return None if out is None else out["hookSpecificOutput"]["permissionDecision"], f


class Hosts(unittest.TestCase):
    def test_protected_by_suffix(self):
        self.assertTrue(ag.is_protected("app.example.com", CFG))
        self.assertTrue(ag.is_protected("api.app.example.com", CFG))
        self.assertFalse(ag.is_protected("example.com", CFG))
        self.assertFalse(ag.is_protected("notapp.example.com", CFG))
        self.assertFalse(ag.is_protected(None, CFG))

    def test_host_and_path(self):
        self.assertEqual(ag.host_of("https://App.Example.com:8443/x?y=1"), "app.example.com")
        self.assertEqual(ag.host_of("app.example.com/admin"), "app.example.com")
        self.assertEqual(ag.path_of("https://app.example.com"), "/")
        self.assertEqual(ag.path_of("https://app.example.com/api/v1/users?x=1"), "/api/v1/users")

    def test_config_merge(self):
        base = {"protected_hosts": ["a.example.com"], "allowed_methods": ["GET", "HEAD", "OPTIONS"], "log": True}
        ag._merge(base, {"protected_hosts": ["b.example.com"], "allowed_methods": ["GET"], "log": False})
        self.assertEqual(base["protected_hosts"], ["a.example.com", "b.example.com"])   # adds
        self.assertEqual(base["allowed_methods"], ["GET"])                             # replaces
        self.assertIs(base["log"], False)

    def test_path_patterns(self):
        self.assertEqual(ag.path_matches("/admin", ["/admin"]), "/admin")
        self.assertEqual(ag.path_matches("/admin/users", ["/admin"]), "/admin")
        self.assertIsNone(ag.path_matches("/administrator", ["/admin"]))
        self.assertEqual(ag.path_matches("/administrator", ["/admin*"]), "/admin*")
        self.assertIsNone(ag.path_matches("/api/v2/users", ["/api/v1/users"]))


class Reading(unittest.TestCase):
    """What the reader sees in a shell command: method and target."""

    def seen(self, command, ps=False):
        f = ag.Findings()
        ag.scan(command, f, STATE, ps=ps)
        return [(r.method, r.url) for r in f.requests], f

    def test_curl_methods(self):
        cases = [
            ("curl https://app.example.com/api/items", "GET"),
            ("curl -s -o out.json https://app.example.com/api/items", "GET"),
            ("curl -I https://app.example.com/", "HEAD"),
            ("curl --head https://app.example.com/", "HEAD"),
            ("curl -X POST https://app.example.com/api/items -d '{}'", "POST"),
            ("curl -XDELETE https://app.example.com/api/items/3", "DELETE"),
            ("curl --request PATCH https://app.example.com/api/items/3", "PATCH"),
            ("curl -d 'a=1' https://app.example.com/api/items", "POST"),
            ("curl --data-binary @file https://app.example.com/api/items", "POST"),
            ("curl -F file=@x.png https://app.example.com/upload", "POST"),
            ("curl --json '{\"a\":1}' https://app.example.com/api/items", "POST"),
            ("curl -T local.txt https://app.example.com/files/", "PUT"),
            ("curl -sS -H 'Accept: application/json' https://app.example.com/api/items", "GET"),
            ("curl -u user:pass -X PUT https://app.example.com/api/items/3", "PUT"),
        ]
        for cmd, method in cases:
            seen, _ = self.seen(cmd)
            self.assertEqual(len(seen), 1, cmd)
            self.assertEqual(seen[0][0], method, cmd)
            self.assertTrue(seen[0][1].startswith("https://app.example.com"), cmd)

    def test_bare_host_and_variables(self):
        seen, _ = self.seen("curl app.example.com/admin")
        self.assertEqual(seen, [("GET", "http://app.example.com/admin")])
        seen, _ = self.seen('BASE=https://app.example.com; curl -X POST "$BASE/api/items"')
        self.assertEqual(seen, [("POST", "https://app.example.com/api/items")])
        seen, _ = self.seen('M=DELETE; curl -X $M https://app.example.com/api/items/1')
        self.assertEqual(seen, [("DELETE", "https://app.example.com/api/items/1")])
        seen, f = self.seen('curl "$URL"')
        self.assertEqual(seen, [])
        self.assertTrue(f.unknown_targets)

    def test_wget_httpie_powershell(self):
        seen, _ = self.seen("wget -q https://app.example.com/report.csv")
        self.assertEqual(seen, [("GET", "https://app.example.com/report.csv")])
        seen, _ = self.seen("wget --post-data 'a=1' https://app.example.com/api/items")
        self.assertEqual(seen[0][0], "POST")
        seen, _ = self.seen("http POST https://app.example.com/api/items name=x")
        self.assertEqual(seen[0][0], "POST")
        seen, _ = self.seen("http https://app.example.com/api/items")
        self.assertEqual(seen[0][0], "GET")
        seen, _ = self.seen("Invoke-RestMethod -Uri https://app.example.com/api/items -Method Delete", ps=True)
        self.assertEqual(seen, [("DELETE", "https://app.example.com/api/items")])
        seen, _ = self.seen("iwr https://app.example.com/api/items -Body $b", ps=True)
        self.assertEqual(seen[0][0], "POST")
        seen, _ = self.seen("Invoke-WebRequest https://app.example.com/page -OutFile p.html", ps=True)
        self.assertEqual(seen, [("GET", "https://app.example.com/page")])

    def test_code_is_unreadable(self):
        seen, f = self.seen("python3 -c \"import requests; requests.post('https://app.example.com/api/items', json={})\"")
        self.assertEqual(seen, [("UNKNOWN", "https://app.example.com/api/items")])
        self.assertTrue(f.requests[0].unknown)
        seen, f = self.seen("node -e \"fetch('https://app.example.com/api/items', {method: 'DELETE'})\"")
        self.assertEqual(seen[0][0], "UNKNOWN")
        seen, f = self.seen("python3 - <<'EOF'\nimport urllib.request\nurllib.request.urlopen('https://app.example.com/x')\nEOF")
        self.assertEqual(seen, [("UNKNOWN", "https://app.example.com/x")])
        seen, f = self.seen("python3 -c \"import requests; requests.get(url)\"")
        self.assertEqual(seen, [])
        self.assertTrue(f.unknown_targets)

    def test_loops_and_wrappers(self):
        _, f = self.seen("for i in 1 2 3; do curl https://app.example.com/api/items/$i; done")
        self.assertEqual(len(f.requests), 1)
        self.assertTrue(f.uncounted)
        _, f = self.seen("cat ids.txt | xargs -I{} curl -X DELETE https://app.example.com/api/items/{}")
        self.assertTrue(f.uncounted)
        _, f = self.seen("cat urls.txt | xargs curl -s")
        self.assertTrue(f.unknown_targets)
        _, f = self.seen("bash -c 'curl -X POST https://app.example.com/api/items'")
        self.assertEqual(f.requests[0].method, "POST")
        _, f = self.seen("ssh box 'curl -X POST https://app.example.com/api/items'")
        self.assertEqual(f.requests[0].method, "POST")
        _, f = self.seen("x=$(curl -X POST https://app.example.com/api/items); echo $x")
        self.assertEqual(f.requests[0].method, "POST")
        _, f = self.seen("echo 'curl -X POST https://app.example.com/api/items'")
        self.assertEqual(f.requests, [])
        _, f = self.seen("# curl -X POST https://app.example.com/api/items\nls")
        self.assertEqual(f.requests, [])


class Policy(unittest.TestCase):
    def test_reads_pass(self):
        for cmd in ("curl https://app.example.com/api/items",
                    "curl -I https://app.example.com/",
                    "wget -q https://app.example.com/report.csv",
                    "git status",
                    "curl -X POST https://other.example.org/api/items -d '{}'"):
            self.assertIsNone(decide(cmd)[0], cmd)
        self.assertIsNone(decide("https://app.example.com/api/items", tool="WebFetch")[0])

    def test_writes_denied(self):
        for cmd in ("curl -X POST https://app.example.com/api/items -d '{}'",
                    "curl -X DELETE https://api.app.example.com/items/3",
                    "curl -d 'a=1' app.example.com/api/items",
                    "curl -T f.txt https://app.example.com/files/",
                    "python3 -c \"import requests; requests.post('https://app.example.com/api/items')\"",
                    "for i in 1 2; do curl https://app.example.com/api/items/$i; done"):
            self.assertEqual(decide(cmd)[0], "deny", cmd)

    def test_paths(self):
        self.assertEqual(decide("curl https://app.example.com/admin/settings")[0], "deny")
        self.assertEqual(decide("curl https://app.example.com/api/v1/users")[0], "deny")
        self.assertEqual(decide("curl https://app.example.com/api/v1/orders/7")[0], "ask")
        self.assertIsNone(decide("curl https://app.example.com/api/v1/products")[0])
        self.assertEqual(decide("https://app.example.com/admin", tool="WebFetch")[0], "deny")
        self.assertIsNone(decide("curl -X POST https://app.example.com/api/v1/comments -d 'text=hi'")[0])
        self.assertEqual(decide("curl -X POST https://app.example.com/api/v1/commentsX -d 'text=hi'")[0], "deny")

    def test_unknown_target_asks(self):
        self.assertEqual(decide('curl "$URL"')[0], "ask")
        self.assertEqual(decide("cat urls.txt | xargs curl -s")[0], "ask")

    def test_deny_beats_ask(self):
        out = decide("curl https://app.example.com/api/v1/orders/1 && curl -X POST https://app.example.com/api/v1/orders")
        self.assertEqual(out[0], "deny")

    def test_browser(self):
        tabs = {}
        nav = {"tool_name": "mcp__claude-in-chrome__navigate", "tool_input": {"url": "https://app.example.com/items", "tabId": 7}}
        f = ag.findings_for(nav, CFG, tabs)
        self.assertIsNone(ag.policy(f, CFG, tabs, "t"))
        self.assertEqual(tabs, {"7": "app.example.com"})
        nav_admin = {"tool_name": "mcp__claude-in-chrome__navigate", "tool_input": {"url": "https://app.example.com/admin", "tabId": 7}}
        self.assertEqual(ag.policy(ag.findings_for(nav_admin, CFG, tabs), CFG, tabs, "t")["hookSpecificOutput"]["permissionDecision"], "deny")
        form = {"tool_name": "mcp__claude-in-chrome__form_input", "tool_input": {"tabId": 7, "fields": []}}
        self.assertEqual(ag.policy(ag.findings_for(form, CFG, tabs), CFG, tabs, "t")["hookSpecificOutput"]["permissionDecision"], "deny")
        click = {"tool_name": "mcp__claude-in-chrome__computer", "tool_input": {"tabId": 7, "action": "left_click", "coordinate": [1, 1]}}
        self.assertEqual(ag.policy(ag.findings_for(click, CFG, tabs), CFG, tabs, "t")["hookSpecificOutput"]["permissionDecision"], "ask")
        shot = {"tool_name": "mcp__claude-in-chrome__computer", "tool_input": {"tabId": 7, "action": "screenshot"}}
        self.assertIsNone(ag.policy(ag.findings_for(shot, CFG, tabs), CFG, tabs, "t"))
        text = {"tool_name": "mcp__claude-in-chrome__get_page_text", "tool_input": {"tabId": 7}}
        self.assertIsNone(ag.policy(ag.findings_for(text, CFG, tabs), CFG, tabs, "t"))
        other_tab = {"tool_name": "mcp__claude-in-chrome__form_input", "tool_input": {"tabId": 8, "fields": []}}
        self.assertIsNone(ag.policy(ag.findings_for(other_tab, CFG, tabs), CFG, tabs, "t"))
        batch = {"tool_name": "mcp__claude-in-chrome__browser_batch", "tool_input": {"actions": [
            {"name": "navigate", "input": {"url": "https://app.example.com/x", "tabId": 9}},
            {"name": "form_input", "input": {"tabId": 9, "fields": []}}]}}
        self.assertEqual(ag.policy(ag.findings_for(batch, CFG, tabs), CFG, tabs, "t")["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_mcp_rules(self):
        d = {"tool_name": "mcp__myapp__delete_item", "tool_input": {"id": 3}}
        self.assertEqual(ag.policy(ag.findings_for(d, CFG, {}), CFG, {}, "t")["hookSpecificOutput"]["permissionDecision"], "deny")
        u = {"tool_name": "mcp__myapp__update_item", "tool_input": {"id": 3}}
        self.assertEqual(ag.policy(ag.findings_for(u, CFG, {}), CFG, {}, "t")["hookSpecificOutput"]["permissionDecision"], "ask")
        r = {"tool_name": "mcp__myapp__read_item", "tool_input": {"id": 3}}
        self.assertIsNone(ag.policy(ag.findings_for(r, CFG, {}), CFG, {}, "t"))
        o = {"tool_name": "mcp__otherserver__delete_item", "tool_input": {}}
        self.assertIsNone(ag.policy(ag.findings_for(o, CFG, {}), CFG, {}, "t"))


class Hook(unittest.TestCase):
    """The script as Claude Code runs it: JSON in on stdin, JSON or nothing out, exit 0."""

    def run_hook(self, inp, env_extra=None):
        env = dict(os.environ)
        env.update(env_extra or {})
        p = subprocess.run([PY, str(SCRIPT), "hook"], input=json.dumps(inp).encode(), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env, timeout=30)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

    def test_deny_json(self):
        code, out, err = self.run_hook({"session_id": "abc", "tool_name": "Bash",
                                        "tool_input": {"command": "curl -X POST https://app.example.com/api/items"}})
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("POST", data["hookSpecificOutput"]["permissionDecisionReason"])

    def test_silent_pass(self):
        code, out, err = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "curl https://app.example.com/api/items"}})
        self.assertEqual((code, out), (0, ""), err)
        code, out, err = self.run_hook({"tool_name": "Read", "tool_input": {"file_path": "/x"}})
        self.assertEqual((code, out), (0, ""))

    def test_disabled_and_broken_input(self):
        code, out, _ = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "curl -X POST https://app.example.com/a"}},
                                     {"CLAUDE_ACTION_GUARD": "off"})
        self.assertEqual((code, out), (0, ""))
        p = subprocess.run([PY, str(SCRIPT), "hook"], input=b"not json", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual((p.returncode, p.stdout), (0, b""))
        p = subprocess.run([PY, str(SCRIPT), "hook"], input=b"", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual((p.returncode, p.stdout), (0, b""))

    def test_tab_memory_persists_between_calls(self):
        self.run_hook({"tool_name": "mcp__claude-in-chrome__navigate", "tool_input": {"url": "https://app.example.com/", "tabId": 41}})
        code, out, _ = self.run_hook({"tool_name": "mcp__claude-in-chrome__form_input", "tool_input": {"tabId": 41, "fields": []}})
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_check_and_status(self):
        p = subprocess.run([PY, str(SCRIPT), "check", "curl -X POST https://app.example.com/api/items"], stdout=subprocess.PIPE, timeout=30)
        self.assertIn("deny", p.stdout.decode())
        p = subprocess.run([PY, str(SCRIPT), "status"], stdout=subprocess.PIPE, timeout=30)
        self.assertIn("app.example.com", p.stdout.decode())


class Plugin(unittest.TestCase):
    def test_manifest_and_policy_file(self):
        m = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(m["name"], "action-guard")
        for k in ("version", "description", "author"):
            self.assertIn(k, m)
        self.assertFalse((PLUGIN / "CLAUDE.md").exists())
        policy = json.loads((SCRIPTS / "action_guard.json").read_text(encoding="utf-8"))
        self.assertIn("protected_hosts", policy)                   # the shipped example protects app.example.com
        self.assertTrue(all(h.endswith(".example.com") or h.endswith(".example") for h in policy["protected_hosts"]))

    def test_hooks_json(self):
        hk = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(set(hk), {"PreToolUse"})
        g = hk["PreToolUse"]
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["matcher"], "Bash|PowerShell|WebFetch|mcp__.*")
        h = g[0]["hooks"][0]
        call = '"${CLAUDE_PLUGIN_ROOT}/scripts/action_guard.py" hook'
        self.assertEqual(h["command"], '"${KIT_PYTHON:-python3}" %s || python3 %s || py %s || exit 1' % (call, call, call))
        self.assertEqual(h["timeout"], 20)
        self.assertTrue(h.get("statusMessage"))
        self.assertNotIn("args", h)

    def test_chain_runs_once_under_sh(self):
        if os.name == "nt" or not shutil.which("sh"):
            self.skipTest("sh chain check runs on macOS/Linux; see request-guard's tests for the Git Bash variant")
        chain = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        env = dict(os.environ, CLAUDE_PLUGIN_ROOT=PLUGIN.as_posix())
        env.pop("KIT_PYTHON", None)
        inp = json.dumps({"session_id": "chain", "tool_name": "Bash", "tool_input": {"command": "curl -X POST https://app.example.com/api/items"}})
        log = Path(STATE) / "guard.log"
        before = log.read_text(encoding="utf-8").count("DENY method") if log.exists() else 0
        p = subprocess.run(["sh", "-c", chain], input=inp, capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(log.read_text(encoding="utf-8").count("DENY method") - before, 1)
        p = subprocess.run(["sh", "-c", chain], input=inp, capture_output=True, text=True,
                           env=dict(env, KIT_PYTHON=str(Path(STATE) / "no-such-python")), timeout=60)
        self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(log.read_text(encoding="utf-8").count("DENY method") - before, 2)

    def test_claude_plugin_validate(self):
        claude = shutil.which("claude")
        if not claude:
            self.skipTest("claude not on PATH")
        p = subprocess.run([claude, "plugin", "validate", str(PLUGIN), "--strict"], capture_output=True, text=True, timeout=120)
        self.assertTrue(p.returncode == 0 and "Validation passed" in p.stdout, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
