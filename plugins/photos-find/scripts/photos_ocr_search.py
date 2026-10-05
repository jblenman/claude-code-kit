#!/usr/bin/env python3
"""photos_ocr_search.py - find macOS Photos-library images by the words Live Text found in them.

Reads the library's own search index read-only (no export, no Photos.app):
  <library>/database/search/psi.sqlite   (groups.category 1203 = OCR word tokens)
  <library>/database/Photos.sqlite       (ZASSET: dates, dimensions, UUID)

Usage:
  photos_ocr_search.py [--library PATH] [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--min-hits N] REGEX [REGEX ...]
      list photos having an OCR word matching ANY regex (case-insensitive, whole-word match);
      --min-hits N requires N distinct regexes to hit the same photo.
  photos_ocr_search.py --tokens UUID      every OCR word + label of one photo (read a receipt without opening it)
  photos_ocr_search.py --derivative UUID  the largest local preview file (downscale before viewing)

The library defaults to $PHOTOS_LIBRARY, else ~/Pictures/Photos Library.photoslibrary.
Dates are local; --start is inclusive, --end exclusive. Output: local time | UUID | WxH | matched
words | preview path (or iCloud-only).
"""
import argparse
import datetime
import glob
import os
import re
import sqlite3
import struct
import sys
import uuid

DEFAULT_LIB = "~/Pictures/Photos Library.photoslibrary"
EPOCH = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc)


def open_ro(path):
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    return sqlite3.connect("file:%s?mode=ro" % path.replace("?", "%3F").replace("#", "%23"), uri=True)


def to_uuid(u0, u1):
    return str(uuid.UUID(bytes=struct.pack("<qq", u0, u1))).upper()


def to_ints(u):
    return struct.unpack("<qq", uuid.UUID(u).bytes)


def local(z):
    return (EPOCH + datetime.timedelta(seconds=z or 0)).astimezone()


def derivative(lib, u):
    fs = (glob.glob("%s/resources/derivatives/*/%s*" % (glob.escape(lib), u))
          + glob.glob("%s/resources/derivatives/masters/*/%s*" % (glob.escape(lib), u)))
    return max(fs, key=os.path.getsize) if fs else None


def parse_day(s, flag):
    try:
        return datetime.datetime.fromisoformat(s).astimezone()     # YYYY-MM-DD, or YYYY-MM-DDTHH:MM
    except ValueError:
        sys.exit("%s must be YYYY-MM-DD (got %r)" % (flag, s))


def parse_uuid(s):
    try:
        return str(uuid.UUID(s)).upper()
    except ValueError:
        sys.exit("not a photo UUID: %r - copy the full UUID from a result line" % s)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("regex", nargs="*")
    ap.add_argument("--library", default=os.environ.get("PHOTOS_LIBRARY") or DEFAULT_LIB,
                    help="the .photoslibrary folder (default: $PHOTOS_LIBRARY or %s)" % DEFAULT_LIB)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--min-hits", type=int, default=1)
    ap.add_argument("--tokens", metavar="UUID")
    ap.add_argument("--derivative", metavar="UUID")
    a = ap.parse_args()
    lib = os.path.expanduser(a.library).rstrip("/")
    if not a.regex and not a.tokens and not a.derivative:
        ap.error("give at least one REGEX, or --tokens/--derivative UUID")
    if a.derivative:
        print(derivative(lib, parse_uuid(a.derivative)) or "no local derivative (iCloud-only)")
        return
    try:
        psi = open_ro(os.path.join(lib, "database", "search", "psi.sqlite"))
        db = open_ro(os.path.join(lib, "database", "Photos.sqlite"))
    except (FileNotFoundError, sqlite3.OperationalError) as ex:
        sys.exit("cannot open the Photos library index at %s (%s). Wrong library path (--library / PHOTOS_LIBRARY), "
                 "not a Mac, or macOS blocks this terminal from the Pictures folder (System Settings > Privacy & "
                 "Security > Files and Folders / Full Disk Access)." % (lib, ex))
    if a.tokens:
        u0, u1 = to_ints(parse_uuid(a.tokens))
        rows = psi.execute("SELECT g.category, g.content_string FROM ga JOIN groups g ON g.rowid=ga.groupid "
                           "JOIN assets s ON s.rowid=ga.assetid WHERE s.uuid_0=? AND s.uuid_1=? ORDER BY g.category",
                           (u0, u1)).fetchall()
        words = [s.rstrip("\x00") for c, s in rows if c == 1203]
        print("%d words: %s" % (len(words), " ".join(words)))
        print("labels:", [(c, s.rstrip("\x00")) for c, s in rows if c != 1203])
        return
    try:
        pats = [re.compile("^(?:%s)$" % r, re.I) for r in a.regex]
    except re.error as ex:
        sys.exit("bad regex: %s" % ex)
    assets = {u: (d, w, h) for u, d, w, h in db.execute(
        "SELECT ZUUID, ZDATECREATED, ZWIDTH, ZHEIGHT FROM ZASSET WHERE ZTRASHEDSTATE=0")}
    lo = parse_day(a.start, "--start") if a.start else None
    hi = parse_day(a.end, "--end") if a.end else None
    hits = {}
    for rid, s in psi.execute("SELECT rowid, content_string FROM groups WHERE category=1203"):
        w = s.rstrip("\x00")
        idx = [i for i, p in enumerate(pats) if p.match(w)]
        if not idx:
            continue
        for u0, u1 in psi.execute("SELECT s.uuid_0, s.uuid_1 FROM ga JOIN assets s ON s.rowid=ga.assetid WHERE ga.groupid=?", (rid,)):
            u = to_uuid(u0, u1)
            if u not in assets:
                continue
            t = local(assets[u][0])
            if (lo and t < lo) or (hi and t >= hi):
                continue
            h = hits.setdefault(u, {"words": set(), "pats": set()})
            h["words"].add(w)
            h["pats"].update(idx)
    out = [(assets[u][0] or 0, u, h) for u, h in hits.items() if len(h["pats"]) >= a.min_hits]
    for d, u, h in sorted(out):
        _, w, hh = assets[u]
        print("%s | %s | %sx%s | %s | %s" % (local(d).strftime("%Y-%m-%d %H:%M"), u, w, hh, " ".join(sorted(h["words"])),
                                             derivative(lib, u) or "iCloud-only"))
    print("%d photo(s)" % len(out))


if __name__ == "__main__":
    main()
