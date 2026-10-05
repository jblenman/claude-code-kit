#!/usr/bin/env python3
"""docx_pages.py - render a .docx to PDF with LibreOffice (headless) and report the page count.

Why: page counts must come from a real layout engine, never from line, word or character
estimates (an estimate of "about 2.0 pages" once came out at 3 pages in Word). LibreOffice's
renderer agrees with Word to within about a line in ordinary text documents; when the count is
within one line of a limit, confirm in Word.

Usage:
  docx_pages.py FILE.docx [--pdf OUT.pdf] [--soffice PATH]
  docx_pages.py FILE.docx --limit 2        # exit 1 if pages > limit

Exit codes:
  0  counted (and within --limit, if given)
  1  OVER LIMIT (only with --limit)
  2  usage error: input missing, or --pdf names an existing file or a missing folder
  3  no count: LibreOffice missing or failed, or the page count could not be read
     (pypdf missing: python3 -m pip install --user pypdf). An unknown count is
     never a pass, with or without --limit.

Output: LibreOffice always renders into a fresh temp folder (it names its output <stem>.pdf, so
rendering next to other files could replace a PDF of that name). Without --pdf the render stays
there and its path is printed; --pdf copies it to OUT.pdf only when nothing exists at that path -
an existing file is never replaced.

Page count: pypdf. Without pypdf, macOS falls back to Spotlight (mdls), which has normally not
indexed a fresh render yet; other systems have no fallback. So pypdf is effectively required.

Config: env SOFFICE (path to the soffice binary) overrides autodetection.

CONTRACT: measurement only. This tool never edits the .docx - no trimming, no font, margin or
spacing changes - and never retries with altered content. --limit reports OVER LIMIT and exits 1;
what to cut (if anything) is a human decision made in a separately named draft.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CANDIDATES = [
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/opt/homebrew/bin/soffice", "/usr/local/bin/soffice", "/usr/bin/soffice",
    r"C:\Program Files\LibreOffice\program\soffice.exe",
]
PYPDF_FIX = "python3 -m pip install --user pypdf  (Windows: py -m pip install pypdf)"


def fail(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


def find_soffice(explicit=None):
    for c in ([explicit] if explicit else []) + [os.environ.get("SOFFICE", "")] + CANDIDATES:
        if c and Path(c).exists():
            return c
    w = shutil.which("soffice")
    if w:
        return w
    fail(3, "soffice not found - install LibreOffice (macOS: brew install --cask libreoffice) or set SOFFICE")


def pdf_pages(pdf):
    """-> (count, None), or (-1, reason) when the count cannot be read. Never a guess."""
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None
    if PdfReader is not None:
        try:
            return len(PdfReader(str(pdf)).pages), None
        except Exception as e:
            return -1, "pypdf could not read the render: %s" % e
    if sys.platform == "darwin" and shutil.which("mdls"):   # Spotlight: macOS only
        out = subprocess.run(["mdls", "-name", "kMDItemNumberOfPages", "-raw", str(pdf)],
                             capture_output=True, text=True).stdout.strip()
        if out.isdigit() and int(out) > 0:
            return int(out), None
    return -1, "pypdf is not installed: " + PYPDF_FIX


def render(docx, soffice):
    """Render into a fresh temp folder; return the PDF's path there."""
    outdir = Path(tempfile.mkdtemp(prefix="docx-pages-"))
    try:
        subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(docx)],
                       check=True, capture_output=True, encoding="utf-8", errors="replace", timeout=180)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        fail(3, "LibreOffice could not render %s: %s - a damaged or password-protected file, "
                "or a hung LibreOffice (quit any open LibreOffice window, retry)" % (docx.name, e))
    produced = outdir / (docx.stem + ".pdf")
    if not produced.exists():
        fail(3, "LibreOffice produced no PDF for %s - quit any open LibreOffice window "
                "(a running instance can swallow headless conversions), retry" % docx.name)
    return produced


def keep_copy(src, dest):
    """Copy src to dest only when dest does not exist (exclusive create, so never a replace)."""
    with open(src, "rb") as fi, open(dest, "xb") as fo:
        shutil.copyfileobj(fi, fo)


def main():
    ap = argparse.ArgumentParser(
        description="Page count of a .docx from a LibreOffice render. Measurement only; never edits the document.",
        epilog="exit codes: 0 counted (within --limit) | 1 OVER LIMIT | 2 usage: missing input, --pdf exists "
               "or its folder is missing | 3 no count (LibreOffice missing/failed, or pypdf missing: %s) - "
               "an unknown count is never a pass" % PYPDF_FIX)
    ap.add_argument("docx", type=Path)
    ap.add_argument("--pdf", type=Path, help="also keep the render here (never replaces an existing file; default: temp only)")
    ap.add_argument("--soffice", help="path to soffice")
    ap.add_argument("--limit", type=int, help="exit 1 if pages exceed this (an unknown count exits 3, never 0)")
    a = ap.parse_args()
    if not a.docx.is_file():
        fail(2, "no such file: %s" % a.docx)
    if a.pdf is not None:
        if os.path.lexists(a.pdf):
            fail(2, "--pdf %s already exists; not replacing it - pick a new name or remove it first" % a.pdf)
        if not a.pdf.resolve().parent.is_dir():
            fail(2, "--pdf folder does not exist: %s" % a.pdf.resolve().parent)
    pdf = render(a.docx, find_soffice(a.soffice))
    if a.pdf is not None:
        try:
            keep_copy(pdf, a.pdf)
        except FileExistsError:
            fail(2, "--pdf %s appeared during the render; not replaced (render left at %s)" % (a.pdf, pdf))
        shutil.rmtree(str(pdf.parent), ignore_errors=True)
        pdf = a.pdf
    n, why = pdf_pages(pdf)
    if n < 1:
        fail(3, "%s: NO PAGE COUNT - %s  [%s]  An unknown count is not a pass; rerun once fixed."
                % (a.docx.name, why or "the render has no pages", pdf))
    print("%s: %d page(s)  [%s]" % (a.docx.name, n, pdf))
    if a.limit is not None and n > a.limit:
        print("OVER LIMIT: %d > %d" % (n, a.limit))
        sys.exit(1)


if __name__ == "__main__":
    main()
