"""prose.py - print the prose-like strings (>= 60 characters, >= 8 words) of a binary, one per line, unique.

Run it on two Claude Code CLI builds and compare the outputs (`comm -13 <(sort a.txt) <(sort b.txt)`)
to see which instructions a new version added.

Usage: python3 prose.py BINARY

Stdlib only, Python 3.8+.
"""
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CODE = re.compile(r"[{};]|=>|\bfunction\b|\bconst\b|\breturn\b|===|\bvar\b|\bimport\b|\bexport\b|\|\||&&|\\x|"
                  r"[A-Za-z]+\.[A-Za-z]+\(|__|\b0x")


def main(argv):
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0 if len(argv) == 2 else 2
    with open(argv[1], "rb") as fh:
        data = fh.read()
    seen = set()
    for m in re.finditer(rb"[\x20-\x7e]{60,}", data):
        s = m.group().decode("ascii")
        for piece in re.split(r"\\n|\\\"", s):
            piece = piece.strip()
            if len(piece) < 60 or len(piece.split()) < 8 or CODE.search(piece):
                continue
            letters = sum(ch.isalpha() or ch == " " for ch in piece)
            if letters / len(piece) < 0.85 or piece in seen:
                continue
            seen.add(piece)
            print(piece)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
