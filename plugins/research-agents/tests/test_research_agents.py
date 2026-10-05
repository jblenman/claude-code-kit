"""Offline checks for the research-agents plugin: every agent file has front matter Claude Code accepts for
a plugin agent (name, description; no permissionMode, hooks, mcpServers or initialPrompt, which plugin
agents ignore), the tool lists and the read-only limits of browser-reader, the researcher's
cross-plugin skill reference, and `claude plugin validate --strict` when claude is on PATH.

    python3 plugins/research-agents/tests/test_research_agents.py

Stdlib only, Python 3.8+. Exit code 0 when every check passed.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
RESULTS = []
SUPPORTED = {"name", "description", "model", "effort", "maxTurns", "tools", "disallowedTools", "skills", "memory",
             "background", "omitClaudeMd", "isolation", "color", "experimental"}
IGNORED = {"permissionMode", "hooks", "mcpServers", "initialPrompt"}


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("%s %s%s" % ("ok  " if ok else "FAIL", name, (" -- " + str(detail)) if (detail and not ok) else ""))
    return ok


def front_matter(path):
    """A tiny reader for the flat YAML these files use: key: value, and '- item' lists under a key."""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None, text
    fm, key = {}, None
    for line in m.group(1).splitlines():
        if re.match(r"^\s+-\s+", line) and key:
            fm.setdefault(key, [])
            if not isinstance(fm[key], list):
                fm[key] = []
            fm[key].append(line.split("-", 1)[1].strip())
        elif ":" in line and not line.startswith(" "):
            key, val = line.split(":", 1)
            key, val = key.strip(), val.strip()
            fm[key] = val if val else []
    return fm, text[m.end():]


def main():
    pj = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    check("plugin.json name/version/description/author", pj.get("name") == "research-agents" and pj.get("version") == "0.1.0"
          and pj.get("description") and (pj.get("author") or {}).get("name"))
    agents = {p.stem: front_matter(p) for p in sorted((PLUGIN / "agents").glob("*.md"))}
    check("four agents", sorted(agents) == ["browser-reader", "frame-reader", "researcher", "verifier"], sorted(agents))
    for name, (fm, body) in agents.items():
        check("%s: front matter parses, name matches the file" % name, fm is not None and fm.get("name") == name, fm)
        if not fm:
            continue
        check("%s: has a description that says when to use it" % name, len(fm.get("description", "")) > 80)
        check("%s: only fields plugin agents support" % name, set(fm) <= SUPPORTED and not (set(fm) & IGNORED), sorted(set(fm) - SUPPORTED))
        check("%s: no machine paths or private names in the body" % name,
              not re.search(r"~/[a-z-]+-knowledge|/Users/|C:\\\\Users", body), re.findall(r"/Users/\S+", body)[:3])
    br = agents["browser-reader"][0] or {}
    dis = br.get("disallowedTools", "")
    check("browser-reader withholds batch, form, script, upload and shortcut tools",
          all(t in dis for t in ("browser_batch", "form_input", "javascript_tool", "file_upload", "shortcuts_execute", "upload_image")))
    check("browser-reader's tools are read-oriented Chrome tools plus Read/Write",
          "form_input" not in br.get("tools", "") and "get_page_text" in br.get("tools", ""))
    rs = agents["researcher"][0] or {}
    check("researcher preloads the request-guard plugin's skill by its <plugin>:<skill> name",
          rs.get("skills") == ["request-guard:fetch-paced"], rs.get("skills"))
    vf = agents["verifier"][0] or {}
    check("verifier keeps its own user-scope memory", vf.get("memory") == "user")
    fr = agents["frame-reader"][0] or {}
    check("frame-reader runs on sonnet without CLAUDE.md, no network tools",
          fr.get("model") == "sonnet" and fr.get("omitClaudeMd") == "true" and "WebFetch" not in fr.get("tools", ""))

    claude = shutil.which("claude")
    if claude:
        r = subprocess.run([claude, "plugin", "validate", str(PLUGIN), "--strict"], capture_output=True, text=True)
        check("claude plugin validate --strict", r.returncode == 0, r.stdout + r.stderr)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n%d checks, %d failed%s" % (len(RESULTS), len(failed), (": " + ", ".join(failed)) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
