"""Checks for docx_pages.py: the exit-code contract (2 usage, 3 no count, 1 over the limit, 0 counted),
the never-replace rule for --pdf, and that the document is never modified. The render checks need
LibreOffice and are skipped without it; the counting checks need pypdf (without it the script must
stop with exit 3, and that is checked instead).

    python3 plugins/docx-pages/tests/test_docx_pages.py      (Windows: py ...)

Stdlib only, Python 3.8+. Exit code 0 when every check passed. A render takes a few seconds.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
SCRIPT = PLUGIN / "scripts" / "docx_pages.py"
PY = sys.executable
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("%s %s%s" % ("ok  " if ok else "FAIL", name, (" -- " + str(detail)) if (detail and not ok) else ""))
    return ok


def load():
    spec = importlib.util.spec_from_file_location("docx_pages", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_docx(path, pages):
    """A minimal valid .docx with `pages` pages (explicit page breaks)."""
    body = []
    for p in range(pages):
        body.append('<w:p><w:r><w:t>Page %d of a test document.</w:t></w:r></w:p>' % (p + 1))
        if p < pages - 1:
            body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + "".join(body) + '</w:body></w:document>')
    types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/word/document.xml" '
             'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>')
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)


def cli(*args):
    return subprocess.run([PY, str(SCRIPT)] + [str(a) for a in args], capture_output=True, text=True, timeout=300)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="docx-pages-test-"))
    pj = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    check("plugin.json name/version/description/author", pj.get("name") == "docx-pages" and pj.get("version") == "0.1.0"
          and pj.get("description") and (pj.get("author") or {}).get("name"))
    skill = (PLUGIN / "skills" / "docx-pages" / "SKILL.md").read_text(encoding="utf-8")
    check("skill runs the script through ${CLAUDE_PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}/scripts/docx_pages.py" in skill)

    # ---- usage errors: exit 2, before any render
    r = cli(tmp / "missing.docx")
    check("missing input: exit 2", r.returncode == 2 and "no such file" in r.stderr, r.stderr)
    doc = tmp / "two pages.docx"
    make_docx(doc, 2)
    taken = tmp / "taken.pdf"
    taken.write_bytes(b"keep me")
    r = cli(doc, "--pdf", taken)
    check("--pdf naming an existing file: exit 2, file untouched",
          r.returncode == 2 and "already exists" in r.stderr and taken.read_bytes() == b"keep me", r.stderr)
    r = cli(doc, "--pdf", tmp / "no-such-folder" / "x.pdf")
    check("--pdf in a missing folder: exit 2", r.returncode == 2 and "folder does not exist" in r.stderr, r.stderr)

    # ---- in-process: no LibreOffice -> exit 3; unreadable PDF -> no count
    mod = load()
    saved = (mod.CANDIDATES, mod.shutil.which, os.environ.pop("SOFFICE", None))
    try:
        mod.CANDIDATES = []
        mod.shutil.which = lambda name: None
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                mod.find_soffice()
            check("no LibreOffice: exit 3", False, "find_soffice returned")
        except SystemExit as ex:
            check("no LibreOffice: exit 3", ex.code == 3)
    finally:
        mod.CANDIDATES, mod.shutil.which = saved[0], saved[1]
        if saved[2] is not None:
            os.environ["SOFFICE"] = saved[2]
    junk = tmp / "junk.pdf"
    junk.write_bytes(b"not a pdf")
    with contextlib.redirect_stderr(io.StringIO()):      # pypdf warns about the junk on stderr
        n, why = mod.pdf_pages(junk)
    check("unreadable PDF: no count and a reason, never a guess", n == -1 and why, (n, why))

    # ---- real renders (LibreOffice)
    soffice = None
    for c in [os.environ.get("SOFFICE", "")] + mod.CANDIDATES:
        if c and Path(c).exists():
            soffice = c
            break
    soffice = soffice or shutil.which("soffice")
    if not soffice:
        print("skip render checks (LibreOffice not found)")
    else:
        has_pypdf = subprocess.run([PY, "-c", "import pypdf"], capture_output=True).returncode == 0
        before = sha(doc)
        r = cli(doc)
        if has_pypdf:
            check("render: 2-page document counts 2, exit 0", r.returncode == 0 and "two pages.docx: 2 page(s)" in r.stdout,
                  r.stdout + r.stderr)
            r1 = cli(doc, "--limit", "1")
            check("--limit 1: OVER LIMIT, exit 1", r1.returncode == 1 and "OVER LIMIT: 2 > 1" in r1.stdout, r1.stdout + r1.stderr)
            r2 = cli(doc, "--limit", "2")
            check("--limit 2: exit 0", r2.returncode == 0 and "OVER LIMIT" not in r2.stdout, r2.stdout + r2.stderr)
            keep = tmp / "kept render.pdf"
            r3 = cli(doc, "--pdf", keep)
            check("--pdf keeps the render at a new path", r3.returncode == 0 and keep.is_file()
                  and keep.read_bytes()[:4] == b"%PDF" and str(keep) in r3.stdout, r3.stdout + r3.stderr)
        else:
            counted = r.returncode == 0 and "2 page(s)" in r.stdout          # Spotlight answered (macOS)
            check("render without pypdf: exit 3 NO PAGE COUNT (or a Spotlight count of 2), never a guess",
                  counted or (r.returncode == 3 and "NO PAGE COUNT" in r.stderr and "pypdf" in r.stderr), r.stdout + r.stderr)
            r1 = cli(doc, "--limit", "5")
            check("without pypdf --limit never passes on an unknown count",
                  r1.returncode == 3 or (r1.returncode == 0 and "2 page(s)" in r1.stdout), r1.stdout + r1.stderr)
        check("the document is never modified", sha(doc) == before)

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
