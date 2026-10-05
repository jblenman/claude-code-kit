# docx-pages

Page counts for Word documents from a real layout engine. The script renders a `.docx` to PDF with LibreOffice in headless mode and counts the PDF's pages; `--limit N` turns that into a check with exit codes a script or a session can rely on. A skill tells Claude to measure before answering any page-count question, instead of estimating from words or lines (an estimate of "about 2.0 pages" once came out at 3 pages in Word).

**Measurement only.** The tool never edits the document, never retries with changed content, and an unknown count is never a pass.

| Component | What it does |
|---|---|
| `skills/docx-pages/SKILL.md` | when to measure, the commands, how to read the result, what to do over the limit |
| `scripts/docx_pages.py` | the measurement (stdlib Python 3.8+, plus LibreOffice and pypdf) |
| `tests/test_docx_pages.py` | exit-code contract, never-replace rule, document untouched; real renders when LibreOffice is present |

## Install

```
claude plugin marketplace add https://github.com/jblenman/claude-code-kit    # or the path of a local clone
claude plugin install docx-pages@claude-code-kit
```

For one session without installing: `claude --plugin-dir <clone>/plugins/docx-pages`. The skill is `docx-pages:docx-pages`.

Requirements:

- **LibreOffice** — macOS `brew install --cask libreoffice`; Windows and Linux from libreoffice.org or the distribution. Found automatically at the usual install paths or on `PATH`; otherwise set `SOFFICE` or pass `--soffice`.
- **pypdf** — `python3 -m pip install --user pypdf` (Windows: `py -m pip install pypdf`). Required: without it the script stops with exit 3. (macOS asks Spotlight first, which has normally not indexed a fresh render.)

## Use

```
python3 "<plugin>/scripts/docx_pages.py" "report.docx"                 # report.docx: 3 page(s)  [<temp>/report.pdf]
python3 "<plugin>/scripts/docx_pages.py" "report.docx" --limit 2       # + OVER LIMIT: 3 > 2, exit 1
python3 "<plugin>/scripts/docx_pages.py" "report.docx" --pdf ~/tmp/report-render.pdf
```

| Exit | Meaning |
|---|---|
| 0 | counted (and within `--limit`, if given) |
| 1 | OVER LIMIT (only with `--limit`) |
| 2 | usage: input missing, `--pdf` names an existing file, or its folder is missing (checked before rendering) |
| 3 | no count: LibreOffice missing or failed, or pypdf missing or unable to read the render |

- The render always happens in a fresh temp folder (LibreOffice names its output `<stem>.pdf`, which could replace a PDF of that name next to the document). `--pdf` copies the render to a path only when nothing exists there.
- `.doc`, `.odt` and `.rtf` work too: anything LibreOffice opens.
- The render times out after 180 s.

## How far to trust the count

- LibreOffice agrees with Word to within about a line in ordinary text documents. **Within one line of a limit, confirm in Word.**
- LibreOffice ships metric-compatible stand-ins for Calibri and Cambria (Carlito, Caladea); a font it lacks gets a substitute with other widths, and the count becomes less certain. The skill has a snippet that lists the fonts a render used.
- The count is total pages; rules that exclude a cover page or references are applied by reading the document.

## Verify it works

```
python3 "<plugin>/tests/test_docx_pages.py"
```

It builds a two-page `.docx`, checks the usage errors (exit 2, before any render), the missing-LibreOffice path (exit 3), and with LibreOffice present renders it: with pypdf, `2 page(s)`, `--limit 1` → exit 1, `--limit 2` → exit 0, `--pdf` keeps a copy; without pypdf, exit 3 `NO PAGE COUNT` (an unknown count never passes). Every run checks the document's hash is unchanged. Tested on macOS with LibreOffice, Python 3.13 and 3.9, with and without pypdf (13 and 11 checks).

By hand: `python3 "<plugin>/scripts/docx_pages.py" any.docx` prints `any.docx: N page(s)  [path]`.

## What it cannot do

- It cannot shorten a document, and it will not: over the limit is reported, and cutting is the author's decision in a separately named draft.
- It is not Word. Documents with unusual fonts, complex tables, tracked changes or fields that Word recomputes can lay out differently.
- It does not count words or characters (a render adds nothing there) and does not read Google Docs (download as .docx first).
- A running LibreOffice window can swallow headless conversions: quit it and retry when the script says it produced no PDF.

## Rollback

`claude plugin uninstall docx-pages@claude-code-kit`. The script writes only temp renders (and a `--pdf` copy where you ask for one).
