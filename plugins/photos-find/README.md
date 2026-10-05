# photos-find (macOS)

Find a photo by the words in it. Photos runs Live Text on every picture and stores the recognized words in its own search index; this plugin's script searches that index read-only and prints matching photos with date, size, UUID and the path of a local preview. You can find "that photo of the receipt" or read a photographed label's serial number without exporting anything, opening Photos.app or looking at a single image. A skill tells Claude to search the words first and look at images last, in a subagent.

| Component | What it does |
|---|---|
| `skills/photos-find/SKILL.md` | search patterns, reading a hit's words, when and how to look at the image, discretion, failure table |
| `scripts/photos_ocr_search.py` | the search (stdlib Python 3.8+: sqlite3, read-only) |
| `tests/test_photos_find.py` | offline checks against a synthetic library |

## Install

```
claude plugin marketplace add https://github.com/jblenman/claude-code-kit    # or the path of a local clone
claude plugin install photos-find@claude-code-kit
```

For one session without installing: `claude --plugin-dir <clone>/plugins/photos-find`. The skill is `photos-find:photos-find`.

Requirements: macOS with a Photos library (Live Text runs on macOS 12 and later), Python 3.8+. The terminal app may need access to the Pictures folder (System Settings → Privacy & Security → Files and Folders, or Full Disk Access) on systems that restrict it.

## Use

```
python3 "<plugin>/scripts/photos_ocr_search.py" 'invoice' 'receipt'                          # ANY of the words
python3 "<plugin>/scripts/photos_ocr_search.py" --start 2026-08-01 --end 2026-09-01 'warrant.*'
python3 "<plugin>/scripts/photos_ocr_search.py" --min-hits 2 'warranty' 'serial' 'model'      # at least two on one photo
python3 "<plugin>/scripts/photos_ocr_search.py" --tokens <UUID>                               # all words + labels of one photo
python3 "<plugin>/scripts/photos_ocr_search.py" --derivative <UUID>                           # its largest local preview
```

| Option | Meaning | Default |
|---|---|---|
| `REGEX …` | patterns, each matched against one whole word, case-insensitive | — |
| `--library PATH` | the `.photoslibrary` folder | `PHOTOS_LIBRARY`, else `~/Pictures/Photos Library.photoslibrary` |
| `--start`, `--end` | local dates, `YYYY-MM-DD` (start inclusive, end exclusive) | none |
| `--min-hits N` | how many different patterns must hit the same photo | 1 |
| `--tokens UUID` | every OCR word and the labels (1500 scene labels, 2100 file name, 2300 camera …) | — |
| `--derivative UUID` | the largest local preview, or `no local derivative (iCloud-only)` | — |

Output, oldest first: `local time | UUID | width×height | matched words | preview path or iCloud-only`, then `N photo(s)`.

## What it reads

- `<library>/database/search/psi.sqlite`: tables `groups` (category 1203 = one OCR word per row), `assets` (UUID as two 64-bit integers), `ga` (which word belongs to which photo).
- `<library>/database/Photos.sqlite`: table `ZASSET` (UUID, creation date in seconds since 2001-01-01 UTC, size, trashed state; trashed photos are left out).
- `<library>/resources/derivatives/`: preview files, located by UUID.

Both databases are opened with SQLite's `mode=ro`; nothing in the library is written. These are Photos' private, undocumented tables: an operating-system update can rename them, and then the script fails with an SQL error rather than returning wrong results.

## Verify it works

```
python3 "<plugin>/tests/test_photos_find.py"      # 19 checks; builds a synthetic library, needs no Mac
```

It checks whole-word matching, `--min-hits`, the date bounds, trashed photos left out, the largest preview chosen, `iCloud-only`, `--tokens` labels, `PHOTOS_LIBRARY`, clear errors for a missing library, a bad UUID and a bad date, and that the index files are unchanged afterwards.

On your Mac: `python3 "<plugin>/scripts/photos_ocr_search.py" 'the'` should list photos containing the word "the" (most libraries have some) and end with `N photo(s)`.

## What it cannot do

- **Photos without text** are invisible to it (people, places, objects): use the date-based recipe in the skill.
- **Words, not sentences:** `--tokens` gives an unordered bag of words, enough to confirm a match or pull a number, not to quote a paragraph.
- **New photos** appear only after Photos has run Live Text on them in the background.
- **iCloud-optimized libraries** keep only reduced previews locally (about 1024 px); the original comes through Photos.app.
- How Live Text splits prices, dates and dashes into tokens is not documented: search for the obvious pieces and widen.
- macOS only; tested against a synthetic library here and against a real library (macOS 26) in the original environment.

## Rollback

`claude plugin uninstall photos-find@claude-code-kit`. The script keeps no state.
