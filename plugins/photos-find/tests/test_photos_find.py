"""Offline checks for photos_ocr_search.py against a synthetic Photos library: the two index databases are
built here with the tables and columns the script reads (psi.sqlite: groups, assets, ga; Photos.sqlite:
ZASSET), so no real library or photo is touched. Runs on any OS (the real library is macOS-only).

    python3 plugins/photos-find/tests/test_photos_find.py

Stdlib only, Python 3.8+. Exit code 0 when every check passed.
"""
import datetime
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
SCRIPT = PLUGIN / "scripts" / "photos_ocr_search.py"
PY = sys.executable
RESULTS = []
EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)
A = "0A1B2C3D-0000-4000-8000-00000000ABCD"   # receipt, local preview
B = "11111111-2222-4333-8444-555555555555"   # warranty label, iCloud-only
C = "99999999-8888-4777-8666-555555555555"   # trashed, must never appear


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("%s %s%s" % ("ok  " if ok else "FAIL", name, (" -- " + str(detail)) if (detail and not ok) else ""))
    return ok


def apple_time(y, mo, d, h=12):
    local = datetime.datetime(y, mo, d, h, 0).astimezone()
    return (local.astimezone(datetime.timezone.utc) - EPOCH).total_seconds()


def build_library(root):
    lib = root / "Test Library.photoslibrary"
    (lib / "database" / "search").mkdir(parents=True)
    db = sqlite3.connect(str(lib / "database" / "Photos.sqlite"))
    db.execute("CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, ZDATECREATED REAL, ZWIDTH INTEGER, "
               "ZHEIGHT INTEGER, ZTRASHEDSTATE INTEGER)")
    db.executemany("INSERT INTO ZASSET (ZUUID, ZDATECREATED, ZWIDTH, ZHEIGHT, ZTRASHEDSTATE) VALUES (?,?,?,?,?)", [
        (A, apple_time(2026, 9, 3), 4032, 3024, 0),
        (B, apple_time(2026, 8, 15), 3024, 4032, 0),
        (C, apple_time(2026, 9, 4), 1000, 1000, 1)])
    db.commit()
    db.close()
    psi = sqlite3.connect(str(lib / "database" / "search" / "psi.sqlite"))
    psi.execute("CREATE TABLE groups (rowid INTEGER PRIMARY KEY, category INTEGER, content_string TEXT)")
    psi.execute("CREATE TABLE assets (rowid INTEGER PRIMARY KEY, uuid_0 INTEGER, uuid_1 INTEGER)")
    psi.execute("CREATE TABLE ga (groupid INTEGER, assetid INTEGER)")
    for rid, u in ((1, A), (2, B), (3, C)):
        u0, u1 = struct.unpack("<qq", uuid.UUID(u).bytes)
        psi.execute("INSERT INTO assets VALUES (?,?,?)", (rid, u0, u1))
    groups = [(10, 1203, "invoice\x00"), (11, 1203, "total"), (12, 1203, "48213"), (13, 1500, "Receipt\x00"),
              (20, 1203, "warranty"), (21, 1203, "serial"), (22, 1203, "model"), (23, 1500, "Document")]
    psi.executemany("INSERT INTO groups VALUES (?,?,?)", groups)
    psi.executemany("INSERT INTO ga VALUES (?,?)", [(10, 1), (11, 1), (12, 1), (13, 1), (20, 2), (21, 2), (22, 2), (23, 2), (10, 3)])
    psi.commit()
    psi.close()
    prev = lib / "resources" / "derivatives" / "0"
    prev.mkdir(parents=True)
    (prev / (A + "_1_105_c.jpeg")).write_bytes(b"\xff\xd8 small preview")
    (prev / (A + "_4_5005_c.jpeg")).write_bytes(b"\xff\xd8 the larger preview, longer")
    return lib


def run(*args, env=None):
    e = dict(os.environ)
    e.pop("PHOTOS_LIBRARY", None)
    e.update(env or {})
    return subprocess.run([PY, str(SCRIPT)] + list(args), capture_output=True, text=True, timeout=60, env=e)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="photos-find-test-"))
    pj = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    check("plugin.json name/version/description/author", pj.get("name") == "photos-find" and pj.get("version") == "0.1.0"
          and pj.get("description") and (pj.get("author") or {}).get("name"))
    skill = (PLUGIN / "skills" / "photos-find" / "SKILL.md").read_text(encoding="utf-8")
    check("skill runs the script through ${CLAUDE_PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}/scripts/photos_ocr_search.py" in skill)

    lib = build_library(tmp)
    L = ["--library", str(lib)]
    r = run(*L, "invoice")
    lines = r.stdout.splitlines()
    check("search: one hit, the trashed photo left out", r.returncode == 0 and lines[-1] == "1 photo(s)" and len(lines) == 2
          and (" | %s | 4032x3024 | invoice | " % A) in lines[0] and C not in r.stdout, r.stdout + r.stderr)
    check("search: the largest local preview is printed", lines and lines[0].endswith(A + "_4_5005_c.jpeg"), lines[:1])
    check("search: local date first", lines and lines[0].startswith("2026-09-03 12:00 | "), lines[:1])
    r = run(*L, "invoic")
    check("patterns match whole words only", r.stdout.strip() == "0 photo(s)", r.stdout)
    r = run(*L, "--min-hits", "2", "warranty", "serial", "nothing")
    check("--min-hits 2: two of three patterns on one photo; iCloud-only shown",
          (" | %s | 3024x4032 | serial warranty | iCloud-only" % B) in r.stdout and r.stdout.strip().endswith("1 photo(s)"), r.stdout)
    r = run(*L, "--min-hits", "3", "warranty", "serial", "nothing")
    check("--min-hits 3 with only two matching: nothing", r.stdout.strip() == "0 photo(s)", r.stdout)
    r = run(*L, "--start", "2026-09-01", "invoice|warranty")
    check("--start is inclusive and filters by local date", A in r.stdout and B not in r.stdout, r.stdout)
    r = run(*L, "--end", "2026-09-01", "invoice|warranty")
    check("--end is exclusive", B in r.stdout and A not in r.stdout, r.stdout)
    r = run(*L, "--tokens", A.lower())
    check("--tokens lists the words and the labels", r.returncode == 0 and r.stdout.startswith("3 words: ")
          and "48213" in r.stdout and "(1500, 'Receipt')" in r.stdout, r.stdout + r.stderr)
    r = run(*L, "--derivative", B)
    check("--derivative of an iCloud-only photo says so", r.stdout.strip() == "no local derivative (iCloud-only)", r.stdout)
    r = run("invoice", env={"PHOTOS_LIBRARY": str(lib)})
    check("PHOTOS_LIBRARY sets the library", r.stdout.strip().endswith("1 photo(s)"), r.stdout + r.stderr)
    r = run("--library", str(tmp / "none.photoslibrary"), "invoice")
    check("missing library: a clear error, no traceback", r.returncode != 0 and "cannot open the Photos library index" in r.stderr
          and "Traceback" not in r.stderr, r.stderr)
    r = run(*L, "--tokens", "not-a-uuid")
    check("bad UUID: a clear error", r.returncode != 0 and "not a photo UUID" in r.stderr and "Traceback" not in r.stderr, r.stderr)
    r = run(*L, "--start", "Sept 1", "invoice")
    check("bad date: a clear error", r.returncode != 0 and "--start must be YYYY-MM-DD" in r.stderr, r.stderr)
    r = run(*L)
    check("no pattern: usage error", r.returncode == 2 and "give at least one REGEX" in r.stderr, r.stderr)
    before = sorted((p.name, p.stat().st_size) for p in (lib / "database").rglob("*"))
    run(*L, "total")
    check("the index files are opened read-only (nothing added or changed)",
          sorted((p.name, p.stat().st_size) for p in (lib / "database").rglob("*")) == before)

    claude = shutil.which("claude")
    if claude:
        r = subprocess.run([claude, "plugin", "validate", str(PLUGIN), "--strict"], capture_output=True, text=True)
        check("claude plugin validate --strict", r.returncode == 0, r.stdout + r.stderr)

    shutil.rmtree(str(tmp), ignore_errors=True)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n%d checks, %d failed%s" % (len(RESULTS), len(failed), (": " + ", ".join(failed)) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
