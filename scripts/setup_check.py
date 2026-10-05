"""Check a machine's Claude Code configuration against a JSON file of expectations.

    python3 setup_check.py EXPECT.json                 # check ~/.claude/settings.json (or the file EXPECT.json names)
    python3 setup_check.py EXPECT.json --settings PATH # check another settings file
    python3 setup_check.py EXPECT.json --json          # machine-readable result
    (Windows: py setup_check.py ...)

The expectations file (see setup-check.example.json next to this script) can ask for:

    "values":           {"dotted.key": expected}   exact values in settings (e.g. "permissions.defaultMode": "auto")
    "allow":            ["Read", ...]              rules that must be in permissions.allow
    "allow_absent":     ["Bash(*)", ...]           rules that must NOT be in permissions.allow (warning)
    "enabled_plugins":  ["name@marketplace", ...]  enabledPlugins entries that must be true
    "marketplaces":     ["name", ...]              marketplaces that must be known (settings extraKnownMarketplaces
                                                   or ~/.claude/plugins/known_marketplaces.json)
    "hooks_absent":     [{"contains": "text", "why": "..."}]
                                                   no hook command in settings may contain the text: for hooks a
                                                   plugin now provides, because a hook present in both settings and
                                                   a plugin runs twice per event
    "env":              {"KEY": {"on": ["windows"], "why": "..."}}
                                                   keys that must exist in the settings env block, on the listed
                                                   platforms (windows, darwin, linux; default all)
    "files":            ["~/path", ...]            files that must exist
    "settings":         "~/.claude/settings.json"  the file to check (--settings overrides)

Nothing is changed. Exit 0 when every check passes (warnings allowed), 1 when a check fails, 2 when
the expectations or settings file cannot be read. Stdlib only, Python 3.8+.
"""
import argparse
import json
import os
import sys
from pathlib import Path

PLATFORM = "windows" if os.name == "nt" else ("darwin" if sys.platform == "darwin" else "linux")


def load_json(path, what):
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit("setup_check: %s not found: %s" % (what, path))
    except ValueError as ex:
        raise SystemExit("setup_check: %s is not valid JSON (%s): %s" % (what, ex, path))


def get_path(obj, dotted):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


def hook_commands(settings):
    """Every hook command in a settings file, with its event."""
    out = []
    for event, groups in (settings.get("hooks") or {}).items():
        for g in groups or []:
            for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
                if isinstance(h, dict) and h.get("command"):
                    out.append((event, str(h["command"])))
    return out


def known_marketplaces(settings):
    names = set((settings.get("extraKnownMarketplaces") or {}).keys())
    for base in (os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR"),
                 os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", ""), "plugins") if os.environ.get("CLAUDE_CONFIG_DIR") else None,
                 str(Path.home() / ".claude" / "plugins")):
        if not base:
            continue
        try:
            names |= set(json.loads((Path(base) / "known_marketplaces.json").read_text(encoding="utf-8")).keys())
        except Exception:
            pass
    return names


def run_checks(expect, settings, settings_path):
    results = []   # (status, name, detail)

    def add(status, name, detail=""):
        results.append((status, name, detail))

    for key, want in (expect.get("values") or {}).items():
        got, present = get_path(settings, key)
        if not present:
            add("FAIL", "settings %s" % key, "missing; expected %s" % json.dumps(want))
        elif got != want:
            add("FAIL", "settings %s" % key, "is %s; expected %s" % (json.dumps(got), json.dumps(want)))
        else:
            add("PASS", "settings %s = %s" % (key, json.dumps(want)))

    allow = ((settings.get("permissions") or {}).get("allow") or [])
    for rule in expect.get("allow") or []:
        add("PASS" if rule in allow else "FAIL", "permissions.allow has %s" % rule, "" if rule in allow else "absent")
    for rule in expect.get("allow_absent") or []:
        add("WARN" if rule in allow else "PASS", "permissions.allow without %s" % rule,
            "present: it bypasses the permission classifier" if rule in allow else "")

    enabled = settings.get("enabledPlugins") or {}
    for name in expect.get("enabled_plugins") or []:
        ok = enabled.get(name) is True
        add("PASS" if ok else "FAIL", "plugin enabled: %s" % name, "" if ok else "not in enabledPlugins (claude plugin install %s)" % name)

    if expect.get("marketplaces"):
        known = known_marketplaces(settings)
        for name in expect["marketplaces"]:
            add("PASS" if name in known else "FAIL", "marketplace known: %s" % name,
                "" if name in known else "not found (claude plugin marketplace add <path-or-url>)")

    cmds = hook_commands(settings)
    for item in expect.get("hooks_absent") or []:
        text = item.get("contains") if isinstance(item, dict) else str(item)
        why = (item.get("why") if isinstance(item, dict) else "") or "a hook present in both settings and a plugin runs twice per event"
        hits = [(e, c) for e, c in cmds if text and text in c]
        if hits:
            add("FAIL", "no settings hook containing %r" % text, "%s: %s" % ("; ".join("%s: %s" % (e, c[:80]) for e, c in hits), why))
        else:
            add("PASS", "no settings hook containing %r" % text)

    env = settings.get("env") or {}
    for key, spec in (expect.get("env") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        platforms = [p.lower() for p in (spec.get("on") or ["windows", "darwin", "linux"])]
        if PLATFORM not in platforms:
            add("SKIP", "env %s (not required on %s)" % (key, PLATFORM))
            continue
        if key in env and str(env[key]).strip():
            add("PASS", "env %s = %s" % (key, json.dumps(env[key])))
        else:
            add("FAIL", "env %s" % key, "missing from the settings env block; %s" % (spec.get("why") or ""))

    for f in expect.get("files") or []:
        p = Path(os.path.expandvars(f)).expanduser()
        add("PASS" if p.exists() else "FAIL", "file exists: %s" % f, "" if p.exists() else "missing (%s)" % p)

    return results


def main(argv=None):
    ap = argparse.ArgumentParser(prog="setup_check.py", description="Check Claude Code settings against a JSON file of expectations.")
    ap.add_argument("expect", help="the expectations JSON file")
    ap.add_argument("--settings", help="settings file to check (default: the expectations' \"settings\" key, else ~/.claude/settings.json)")
    ap.add_argument("--json", action="store_true", help="print the results as JSON")
    a = ap.parse_args(argv)
    try:
        expect = load_json(a.expect, "expectations file")
        settings_path = Path(a.settings or expect.get("settings") or "~/.claude/settings.json").expanduser()
        settings = load_json(settings_path, "settings file") if settings_path.exists() else {}
        if not settings_path.exists():
            print("WARN    %s does not exist; checking against an empty settings object" % settings_path)
    except SystemExit as ex:
        print(ex)
        return 2
    if not isinstance(settings, dict):
        print("setup_check: %s is not a JSON object" % settings_path)
        return 2
    results = run_checks(expect, settings, settings_path)
    if a.json:
        print(json.dumps({"settings": str(settings_path), "platform": PLATFORM,
                          "results": [{"status": s, "check": n, "detail": d} for s, n, d in results]}, indent=2))
    else:
        print("setup_check: %s (%s)" % (settings_path, PLATFORM))
        for s, n, d in results:
            print("%-5s %s%s" % (s, n, ("  -- " + d) if d else ""))
        counts = {k: sum(1 for s, _, _ in results if s == k) for k in ("PASS", "WARN", "FAIL", "SKIP")}
        print("%d pass, %d warn, %d fail, %d skipped" % (counts["PASS"], counts["WARN"], counts["FAIL"], counts["SKIP"]))
    return 1 if any(s == "FAIL" for s, _, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
