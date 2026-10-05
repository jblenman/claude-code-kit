---
name: docx-pages
description: Count the pages of a Word .docx or check it against a page limit ("how many pages is this?", "must fit on 2 pages", "is it under the limit?"). Use before answering any page-count question about a .docx, and after every edit that could change its length — never estimate pages from words, lines or characters. Runs the docx-pages plugin's script — a real LibreOffice render to PDF, measurement only, never edits the document.
---

# docx-pages — page counts from a real render

**Page counts come from a layout engine, never from character, word or line estimates.** (An estimate of "about 2.0 pages" once came out at 3 pages in Word.) The script renders the document to PDF with LibreOffice headless and counts the PDF's pages. LibreOffice agrees with Word to within about a line in ordinary text documents; **when the result is within one line of a limit, confirm in Word** before telling anyone it fits.

**Contract: measurement only.** The tool never edits the .docx — no trimming, no font, margin or spacing changes, no retries with altered content. A document over its limit is a decision for its author, not an error to fix (see "Over the limit").

## Check the environment first (one Bash call, exactly this)

```
ls /Applications/LibreOffice.app/Contents/MacOS/soffice 2>/dev/null || command -v soffice || echo "LibreOffice: NOT INSTALLED (macOS: brew install --cask libreoffice; Windows/Linux: libreoffice.org)"
python3 -c "import pypdf; print('pypdf', pypdf.__version__)" 2>/dev/null || echo "pypdf: MISSING - required (without it the script stops with exit 3, no count); install: python3 -m pip install --user pypdf"
```

pypdf is not optional: without it the script stops with exit 3 and `NO PAGE COUNT - pypdf is not installed: …`. On macOS it first asks Spotlight (`mdls`), which has normally not indexed a fresh render; Windows and Linux have no fallback. Ask the user before installing anything.

## Run

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/docx_pages.py" "<file>.docx"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/docx_pages.py" "<file>.docx" --limit 2
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/docx_pages.py" "<file>.docx" --pdf "<scratch folder>/<name>-render.pdf"
```

- Plain run: one line, `<file>.docx: N page(s)  [<pdf path>]`; the PDF goes to a fresh temp folder.
- `--limit N`: when the count exceeds N, a second line `OVER LIMIT: <count> > N` and exit 1; within the limit exit 0; no count exit 3, never 0.
- `--pdf PATH` also keeps the render at PATH (for looking at the last page, or the font check below). It never replaces a file: when PATH exists the run stops with exit 2 before rendering, and the render itself always happens in a fresh temp folder, so PDFs next to PATH are untouched.
- Exit codes: `0` counted (within `--limit`) · `1` OVER LIMIT · `2` usage (input missing, `--pdf` exists or its folder is missing) · `3` no count (LibreOffice missing or failed, pypdf missing).
- `--soffice PATH`, or the `SOFFICE` environment variable, picks a specific LibreOffice binary.
- About a second for a short document once LibreOffice has started; the render times out after 180 s.
- `.doc`, `.odt` and `.rtf` work the same way (anything LibreOffice opens).
- Windows: `py` instead of `python3`.

## Reading the result

- `N page(s)` with N ≥ 1 is the measured count. Say where it came from: "N pages in a LibreOffice render".
- **`NO PAGE COUNT` (stderr, exit 3) means the count failed — never a pass**, with or without `--limit`. The line names the cause (usually pypdf missing); fix it and rerun.
- **Within a line of the limit** (the last page nearly empty, or the count right at the limit with a full last page): confirm in Word. The user opens the file there. To see how full the last page is, keep the render (`--pdf`) and have a subagent look at its last page and answer in text; never page through PDF images in the main context.
- **Fonts:** LibreOffice ships metric-compatible stand-ins for Calibri and Cambria (Carlito, Caladea), which is why counts usually agree with Word. A font LibreOffice lacks gets a substitute with other widths, and then the count is less certain. List what the render actually used (needs `--pdf` and pypdf):

  ```
  python3 - "<scratch folder>/<name>-render.pdf" <<'EOF'
  import sys, pypdf
  fonts = set()
  for page in pypdf.PdfReader(sys.argv[1]).pages:
      res = page.get("/Resources")
      fd = res.get_object().get("/Font") if res is not None else None
      for ref in (fd.get_object().values() if fd is not None else []):
          fonts.add(str(ref.get_object().get("/BaseFont")).split("+")[-1])
  print(sorted(fonts))
  EOF
  ```

- The count is total pages. A rule that excludes references, a cover page or appendices is applied by reading the document, not by this tool.

## Over the limit

- Report it plainly with the measured count; the file stays exactly as it is.
- When shortening is wanted, build **separately named option drafts** (`<name> OPTION A.docx`, `<name> OPTION B.docx`) with every cut itemized, and leave the original untouched until the author picks. Content belongs to its author: never trim, shrink fonts, tighten margins or spacing silently to make a check pass.
- Measure each option with the same command.

## Discretion

- The tool only reads the document; work on the user's file in place only for measuring. Any edit goes to a copy with a new name.
- Keep renders in a scratch or temp folder, never in a shared folder.

## When it fails

| Symptom | Cause / fix |
|---|---|
| `soffice not found - install LibreOffice …` (exit 3) | install LibreOffice, or pass `--soffice PATH` / set `SOFFICE` |
| `NO PAGE COUNT - pypdf is not installed: …` (exit 3) | `python3 -m pip install --user pypdf` (Windows: `py -m pip install pypdf`), rerun |
| `NO PAGE COUNT - pypdf could not read the render: …` (exit 3) | the render is damaged: rerun; if it repeats, the document needs a look in Word |
| `LibreOffice could not render …` (exit 3) | the file does not open in LibreOffice (damaged or password-protected — that needs the author), or LibreOffice hung or timed out (180 s): quit any open LibreOffice window, retry |
| `LibreOffice produced no PDF for …` (exit 3) | quit any open LibreOffice window (a running instance can swallow headless conversions), retry |
| `--pdf … already exists; not replacing it` (exit 2) | pick a new `--pdf` name; remove the old file only if it is your own earlier render |

## Not this skill

- Creating or editing a Word document → a document skill (edits go to a newly named copy).
- A PDF's page count → `python3 -c "import pypdf,sys; print(len(pypdf.PdfReader(sys.argv[1]).pages))" file.pdf`.
- A word or character limit → count the text; a render adds nothing.
- A Google Doc → download it as .docx first, then measure.
