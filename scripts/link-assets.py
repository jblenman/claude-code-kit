"""Link a repository's shared Claude Code assets (skills, agents, rules) into ~/.claude on this machine.

    python3 link-assets.py [REPO]               # link everything from REPO (default: the current directory); idempotent
    python3 link-assets.py REPO --dry-run       # show what would change, write nothing
    python3 link-assets.py REPO --only skills   # or agents, rules (comma-separated or repeated)
    python3 link-assets.py REPO --machine-file machines/this-machine.md   # also link one file as ~/.claude/rules/machine.md
    python3 link-assets.py REPO --name team     # agents folder name under ~/.claude/agents (default: REPO's directory name)
    python3 link-assets.py REPO --uninstall     # remove only what points into REPO, plus the copies this script made
    (Windows: py link-assets.py ...)

What it links
    REPO/skills/<name>/     -> ~/.claude/skills/<name>        a directory link per skill folder that has a SKILL.md
    REPO/agents/            -> ~/.claude/agents/<name>        one directory link (Claude Code scans agents/ recursively)
    REPO/rules/*.md         -> ~/.claude/rules/<file>         a file link per rule (a README.md there is not a rule)
    --machine-file PATH     -> ~/.claude/rules/machine.md     one machine's notes, loaded as a user-level rule

Why links and not copies: a link follows `git pull`, so every machine that linked the repo gets a changed
skill at the next session (Claude Code re-reads a changed SKILL.md live).

Windows: a symlink needs Developer Mode or an elevated shell. A refused directory symlink becomes a
directory junction (no privilege needed, same effect). A refused file symlink becomes a copy, which
does NOT follow `git pull`: re-run this script after pulling (it refreshes copies nobody edited).

Safety: an existing real file or folder, or a link that points somewhere else, is never replaced; it
is reported as SKIP. Everything the script creates is recorded in ~/.claude/linked-assets.json with the
repo it came from, so --uninstall removes copies too and two linked repos never prune each other's
links; links that point into this repo are recognised even without a record. Links into this repo
whose target is gone (a skill renamed or deleted upstream) are pruned on the next run.

The target folder follows CLAUDE_CONFIG_DIR when it is set (scratch test profiles). The skills folder
honours CLAUDE_SKILLS_DIR.

Running sessions: a new skill appears live, but a ~/.claude/skills folder that did not exist when the
session started needs /reload-skills once; a new ~/.claude/agents folder needs a session restart;
rules load when a session starts. (Verified on CLI 2.1.283-2.1.289: a symlinked skill folder, a
symlinked agents folder and a symlinked rule file all load.)

Exit code 0 when everything is linked or already in place; 1 when something was skipped or failed.
Stdlib only, Python 3.8+.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

IS_WIN = os.name == "nt"
GROUPS = ("skills", "agents", "rules")
MANIFEST_NAME = "linked-assets.json"
REPO = None          # set in main()


# ---------------------------------------------------------------- paths and links

def claude_dir():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def _strip_prefix(s):
    for pre in ("\\\\?\\", "\\??\\"):
        if s.startswith(pre):
            return s[len(pre):]
    return s


def norm(p):
    """Comparable form of a path: links resolved, case folded on Windows."""
    return os.path.normcase(_strip_prefix(os.path.realpath(str(p))))


def same(a, b):
    return norm(a) == norm(b)


def inside_repo(p):
    root = norm(REPO)
    try:
        return os.path.commonpath([root, norm(p)]) == root
    except ValueError:  # another drive on Windows
        return False


def is_junction(p):
    test = getattr(os.path, "isjunction", None)  # Python 3.12+
    if test is not None:
        return test(str(p))
    if not IS_WIN:
        return False
    try:
        return getattr(os.lstat(str(p)), "st_reparse_tag", 0) == 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT
    except OSError:
        return False


def link_target(p):
    """Where the symlink or junction at p points (the target may be gone), or None if p is not a link."""
    s = str(p)
    if not (os.path.islink(s) or is_junction(p)):
        return None
    try:
        t = Path(_strip_prefix(os.readlink(s)))
    except OSError:
        return Path(os.path.realpath(s))
    return t if t.is_absolute() else p.parent / t


def make_link(src, dst, is_dir):
    """Create dst -> src. Returns 'symlink', 'junction' (Windows directory) or 'copy' (Windows file)."""
    try:
        os.symlink(str(src), str(dst), target_is_directory=is_dir)
        return "symlink"
    except OSError as ex:
        if not IS_WIN:
            raise
        if not is_dir:
            shutil.copy2(str(src), str(dst))
            return "copy"
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                           capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            raise OSError("symlink refused (%s) and junction failed: %s" % (ex, (r.stderr or r.stdout).strip()))
        return "junction"


def remove_link(p):
    """Remove a symlink or junction, never what it points to."""
    if IS_WIN:
        try:
            os.rmdir(str(p))  # a directory symlink or junction goes with rmdir; its target is untouched
            return
        except OSError:
            pass
    os.unlink(str(p))


def sha256(p):
    h = hashlib.sha256()
    with open(str(p), "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- manifest

def load_manifest(path):
    try:
        with open(str(path), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            return data
        print("WARN    %s has an unexpected shape; starting a new record" % path)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as ex:
        print("WARN    %s unreadable (%s); starting a new record" % (path, ex))
    return {"version": 2, "entries": {}}


def save_manifest(path, data):
    if not data["entries"]:
        if path.exists():
            path.unlink()
        return
    data["version"] = 2
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(str(tmp), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(str(tmp), str(path))


def ours(rec):
    """A manifest entry made from this repo (older records without a repo field count as ours)."""
    r = rec.get("repo") if rec else None
    return r is None or same(r, REPO)


# ---------------------------------------------------------------- what should exist

def dest_dirs(cdir):
    return {"skills": Path(os.environ.get("CLAUDE_SKILLS_DIR") or cdir / "skills"),
            "agents": cdir / "agents",
            "rules": cdir / "rules"}


def wanted(groups, dirs, agents_name, machine_file):
    """[(group, src, dst, is_dir)] for the selected groups. The machine rule has group 'machine'."""
    out = []
    if "skills" in groups:
        if (REPO / "skills").is_dir():
            for skill_md in sorted((REPO / "skills").glob("*/SKILL.md")):
                out.append(("skills", skill_md.parent, dirs["skills"] / skill_md.parent.name, True))
        else:
            print("absent  %s (no skills to link)" % (REPO / "skills"))
    if "agents" in groups:
        if (REPO / "agents").is_dir():
            out.append(("agents", REPO / "agents", dirs["agents"] / agents_name, True))
        else:
            print("absent  %s (no agents to link)" % (REPO / "agents"))
    if "rules" in groups:
        if (REPO / "rules").is_dir():
            for f in sorted((REPO / "rules").glob("*.md")):
                if f.name.lower() == "readme.md":
                    continue
                if f.name.lower() == "machine.md":
                    print("WARN    %s is skipped: machine.md is reserved for --machine-file" % f)
                    continue
                out.append(("rules", f, dirs["rules"] / f.name, False))
        else:
            print("absent  %s (no rules to link)" % (REPO / "rules"))
        if machine_file is not None:
            out.append(("machine", machine_file, dirs["rules"] / "machine.md", False))
    return out


def scan_repo_links(groups, dirs):
    """Links in the managed folders that point into this repo: {dst: (group, target)}."""
    found = {}
    for g in groups:
        d = dirs[g]
        if not d.is_dir():
            continue
        for p in d.iterdir():
            t = link_target(p)
            if t is not None and inside_repo(t):
                grp = "machine" if (g == "rules" and p.name == "machine.md") else g
                found[str(p)] = (grp, t)
    return found


# ---------------------------------------------------------------- actions

class Run(object):
    def __init__(self, dry, manifest):
        self.dry = dry
        self.manifest = manifest
        self.counts = {"linked": 0, "ok": 0, "removed": 0, "skipped": 0, "errors": 0}
        self.copies = 0
        self.changed = set()  # groups with a new, replaced or refreshed entry

    def say(self, verb, text):
        print("%-7s %s" % (verb, text))

    def record(self, dst, group, kind, src):
        if self.dry:
            return
        rec = {"group": group, "kind": kind, "source": str(src), "repo": str(REPO)}
        if kind == "copy":
            rec["sha256"] = sha256(dst)
        old = self.manifest["entries"].get(str(dst), {})
        rec["created"] = old.get("created") or time.strftime("%Y-%m-%dT%H:%M:%S")
        self.manifest["entries"][str(dst)] = rec

    def forget(self, dst):
        """Drop the record of a path that is no longer ours (replaced by hand since the script made it)."""
        if not self.dry:
            self.manifest["entries"].pop(str(dst), None)

    def link(self, group, src, dst, is_dir):
        key = str(dst)
        rec = self.manifest["entries"].get(key)
        tgt = link_target(dst)
        if tgt is not None:
            if same(tgt, src):
                self.say("ok", "%s -> %s" % (dst, src))
                self.counts["ok"] += 1
                kind = rec.get("kind") if rec and rec.get("kind") in ("symlink", "junction") else (
                    "junction" if is_junction(dst) else "symlink")
                self.record(dst, group, kind, src)
                return
            if inside_repo(tgt) or (rec and ours(rec) and same(tgt, rec.get("source", ""))):
                self.say("relink", "%s -> %s (was %s)" % (dst, src, tgt))
                if not self.dry:
                    remove_link(dst)
                    kind = make_link(src, dst, is_dir)
                    self.record(dst, group, kind, src)
                    self.copies += kind == "copy"
                self.counts["linked"] += 1
                self.changed.add(group)
                return
            self.say("SKIP", "%s already points to %s (remove it by hand to replace)" % (dst, tgt))
            self.counts["skipped"] += 1
            if rec and ours(rec):
                self.forget(dst)
            return
        if os.path.lexists(key):
            if rec and ours(rec) and rec.get("kind") == "copy" and not is_dir and os.path.isfile(key):
                current, fresh = sha256(dst), sha256(src)
                if current == fresh:
                    self.say("ok", "%s (copy of %s, current)" % (dst, src))
                    self.counts["ok"] += 1
                    self.copies += 1
                    return
                if current == rec.get("sha256"):
                    self.say("update", "%s (copy refreshed from %s)" % (dst, src))
                    if not self.dry:
                        shutil.copy2(str(src), key)
                        self.record(dst, group, "copy", src)
                    self.counts["linked"] += 1
                    self.copies += 1
                    self.changed.add(group)
                    return
                self.say("SKIP", "%s is a copy edited since it was made; not touched (delete it, then re-run)" % dst)
                self.counts["skipped"] += 1
                return
            what = "folder" if os.path.isdir(key) else "file"
            self.say("SKIP", "%s is a real %s not made by this script; not touched" % (dst, what))
            self.counts["skipped"] += 1
            if rec and ours(rec):
                self.forget(dst)
            return
        self.say("link", "%s -> %s" % (dst, src))
        if not self.dry:
            dst.parent.mkdir(parents=True, exist_ok=True)
            kind = make_link(src, dst, is_dir)
            if kind != "symlink":
                self.say("", "made a %s%s" % (kind, " (file symlink refused)" if kind == "copy" else ""))
            self.copies += kind == "copy"
            self.record(dst, group, kind, src)
        self.counts["linked"] += 1
        self.changed.add(group)

    def remove(self, dst, rec, verb="remove", why=""):
        """Remove dst if it is ours: a link into the repo (or to its recorded source), or an unedited copy."""
        key = str(dst)
        tgt = link_target(dst)
        if tgt is not None:
            if inside_repo(tgt) or (rec and ours(rec) and same(tgt, rec.get("source", ""))):
                self.say(verb, "%s -> %s%s" % (dst, tgt, why))
                if not self.dry:
                    remove_link(dst)
                    self.manifest["entries"].pop(key, None)
                self.counts["removed"] += 1
            else:
                self.say("keep", "%s (points to %s, not this repo)" % (dst, tgt))
                if rec and ours(rec):
                    self.forget(dst)
            return
        if not os.path.lexists(key):
            if rec is not None and ours(rec) and not self.dry:
                self.manifest["entries"].pop(key, None)
            return
        if rec and ours(rec) and rec.get("kind") == "copy" and os.path.isfile(key):
            src = rec.get("source", "")
            current = sha256(dst)
            if current == rec.get("sha256") or (os.path.isfile(src) and current == sha256(src)):
                self.say(verb, "%s (copy)%s" % (dst, why))
                if not self.dry:
                    os.unlink(key)
                    self.manifest["entries"].pop(key, None)
                self.counts["removed"] += 1
            else:
                self.say("keep", "%s (a copy edited since it was made)" % dst)
            return
        self.say("keep", "%s (not made by this script)" % dst)
        if rec and ours(rec):
            self.forget(dst)


def parse_groups(values):
    if not values:
        return list(GROUPS)
    groups = []
    for v in values:
        for g in v.split(","):
            g = g.strip().lower()
            if g not in GROUPS:
                raise SystemExit("link-assets: --only takes %s, not %r" % ("|".join(GROUPS), g))
            if g not in groups:
                groups.append(g)
    return groups


def main(argv=None):
    global REPO
    ap = argparse.ArgumentParser(prog="link-assets.py",
                                 description="Link a repository's skills, agents and rules into ~/.claude.")
    ap.add_argument("repo", nargs="?", default=".", help="the repository to link from (default: the current directory)")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    ap.add_argument("--uninstall", action="store_true", help="remove what this script linked or copied from REPO")
    ap.add_argument("--only", action="append", metavar="GROUP", help="skills, agents or rules (comma-separated or repeated)")
    ap.add_argument("--name", metavar="NAME", help="folder name for the agents link under ~/.claude/agents (default: REPO's directory name)")
    ap.add_argument("--machine-file", metavar="PATH", help="a Markdown file with this machine's notes, linked as ~/.claude/rules/machine.md")
    args = ap.parse_args(argv)

    REPO = Path(args.repo).expanduser().resolve()
    if not REPO.is_dir():
        print("link-assets: %s is not a directory" % REPO)
        return 1
    groups = parse_groups(args.only)
    cdir = claude_dir()
    dirs = dest_dirs(cdir)
    manifest_path = cdir / MANIFEST_NAME
    manifest = load_manifest(manifest_path)
    run = Run(args.dry_run, manifest)
    sel = set(groups) | ({"machine"} if "rules" in groups else set())
    agents_name = args.name or REPO.name

    machine_file = None
    if args.machine_file:
        mf = Path(args.machine_file).expanduser()
        if not mf.is_absolute():
            mf = (REPO / mf) if (REPO / mf).is_file() else mf.resolve()
        if not mf.is_file():
            print("link-assets: --machine-file %s is not a file" % mf)
            return 1
        machine_file = mf.resolve()
        if "rules" not in groups:
            groups.append("rules")
            sel |= {"rules", "machine"}
    print("link-assets: %s -> %s (%s)%s" % (REPO, cdir, ", ".join(groups), " [dry run]" if args.dry_run else ""))
    if machine_file is not None:
        print("machine rule: %s" % machine_file)

    want = wanted(groups, dirs, agents_name, machine_file)
    want_keys = set(str(w[2]) for w in want)
    fresh = dict((g, not dirs[g].exists()) for g in groups)

    if args.uninstall:
        targets = {}
        for g, src, dst, is_dir in want:
            targets[str(dst)] = dst
        for key, rec in manifest["entries"].items():
            if rec.get("group") in sel and ours(rec):
                targets[key] = Path(key)
        for key in scan_repo_links(groups, dirs):
            targets[key] = Path(key)
        for key in sorted(targets):
            try:
                run.remove(targets[key], manifest["entries"].get(key))
            except OSError as ex:
                run.say("ERROR", "%s: %s" % (key, ex))
                run.counts["errors"] += 1
    else:
        for g, src, dst, is_dir in want:
            try:
                run.link(g, src, dst, is_dir)
            except OSError as ex:
                run.say("ERROR", "%s: %s" % (dst, ex))
                run.counts["errors"] += 1
        # prune: this repo's recorded entries of these groups that are no longer wanted, and links into
        # the repo whose target is gone
        stale = {}
        for key, rec in manifest["entries"].items():
            if rec.get("group") in sel and ours(rec) and key not in want_keys:
                if rec.get("group") == "machine" and machine_file is None:
                    continue  # no --machine-file this time: keep the machine rule that is there
                stale[key] = rec
        for key, (grp, tgt) in scan_repo_links(groups, dirs).items():
            if key not in want_keys and key not in stale:
                if not os.path.exists(str(tgt)):
                    stale[key] = manifest["entries"].get(key) or {"group": grp, "source": str(tgt), "repo": str(REPO)}
                elif not (grp == "machine" and machine_file is None):
                    run.say("note", "%s points into the repo but is not one of its links; left alone" % key)
        for key in sorted(stale):
            try:
                run.remove(Path(key), manifest["entries"].get(key) or stale[key], verb="prune",
                           why=" (no longer in the repo)")
            except OSError as ex:
                run.say("ERROR", "%s: %s" % (key, ex))
                run.counts["errors"] += 1

    if not args.dry_run:
        try:
            save_manifest(manifest_path, manifest)
        except OSError as ex:
            run.say("ERROR", "could not write %s: %s" % (manifest_path, ex))
            run.counts["errors"] += 1

    c = run.counts
    if args.uninstall:
        print("done: %d removed, %d errors%s" % (c["removed"], c["errors"], " (dry run: nothing written)" if args.dry_run else ""))
    else:
        print("done: %d linked, %d ok, %d skipped, %d errors%s" % (
            c["linked"], c["ok"], c["skipped"], c["errors"], " (dry run: nothing written)" if args.dry_run else ""))
        if "skills" in run.changed and fresh.get("skills"):
            print("note: %s did not exist before; sessions already running need /reload-skills once" % dirs["skills"])
        if "agents" in run.changed and fresh.get("agents"):
            print("note: %s did not exist before; sessions already running need a restart to see the agents" % dirs["agents"])
        if run.changed & {"rules", "machine"}:
            print("note: rules load when a session starts; sessions already running see new rules after a restart")
        if run.copies:
            print("note: %d rule file(s) are copies because Windows refused a file symlink; re-run this script "
                  "after every git pull (it refreshes them)" % run.copies)
    return 1 if (c["skipped"] or c["errors"]) else 0


if __name__ == "__main__":
    sys.exit(main())
