---
name: photos-find
description: Find a photo in the Mac's Photos library by the words in it — a receipt, label, sign, serial or ticket number, screenshot or photographed document — optionally within a date range, and read what it says without opening it. Use when someone asks to find or dig up "that photo of the receipt/label/…", or what a photographed document says, before exporting anything or scanning images by eye. Runs the photos-find plugin's script over Photos' own Live Text index (read-only); macOS only.
---

# photos-find — find photos by the text in them

Photos runs Live Text on every picture and keeps the words in its own search index (`database/search/psi.sqlite`, category 1203 = one lowercase word per token). The script searches that index read-only, joins each hit to the asset table for date, size and UUID, and prints the local preview's path. No export, no Photos.app. A receipt can be found in a few queries without viewing a single image: looking at images is the expensive last step, not the first.

## Check the environment first (one Bash call, exactly this)

```
[ "$(uname)" = Darwin ] || echo "not a Mac: the Photos library is macOS-only"
L="${PHOTOS_LIBRARY:-$HOME/Pictures/Photos Library.photoslibrary}"; ls "$L/database/search/psi.sqlite" >/dev/null 2>&1 && echo "Live Text index: ok ($L)" || echo "Live Text index: NOT FOUND at $L (another library? pass --library or set PHOTOS_LIBRARY)"
```

## Search

```
P="${CLAUDE_PLUGIN_ROOT}/scripts/photos_ocr_search.py"
python3 "$P" 'invoice' 'receipt'                                  # photos with ANY of these words
python3 "$P" --start 2026-08-01 --end 2026-09-01 'warrant.*'      # within dates
python3 "$P" --min-hits 2 'warranty' 'serial' 'model'             # at least 2 of the patterns on one photo
python3 "$P" --tokens <UUID>                                      # every OCR word + labels of one photo
python3 "$P" --derivative <UUID>                                  # the largest local preview file
python3 "$P" --library "<path>.photoslibrary" 'invoice'           # another library
```

- Each pattern must match **one whole word** (`^(?:pattern)$`, case-insensitive). Word forms need a regex: `warrant.*`, `invoic(e|es)`. A pattern with a space never matches, because tokens are single words; a phrase is several patterns plus `--min-hits`.
- Numbers: try the digits the way Live Text likely split them (`48213`, `4821\d+`); how it breaks prices, dashes and slashes is not documented, so try the obvious pieces and widen.
- Dates are local; `--start` is inclusive, `--end` exclusive (`--end 2026-09-01` stops at August 31).
- Start narrow and widen on a miss (drop the date range, loosen the regex, try a distinctive number) rather than listing hundreds of hits.

Output, oldest first, then a count:

```
2026-09-03 14:12 | 0A1B2C3D-0000-4000-8000-00000000ABCD | 4032x3024 | invoice total | ~/Pictures/Photos Library.photoslibrary/resources/derivatives/0/0A1B2C3D-0000-4000-8000-00000000ABCD_1_105_c.jpeg
3 photo(s)
```

Fields: local time | UUID | original's width×height | the words that matched | local preview path, or `iCloud-only`.

## Read a hit without opening it

`--tokens <UUID>` prints `N words: …` (every OCR word on the photo) and `labels: [(category, value), …]` (1500 = scene labels such as Receipt or Document, 1205 = text found, 2100 = original file name, 2300 = camera). The words come as an unordered bag: enough to confirm a match or pull a total, a date or a number, not to quote sentences.

## When the image itself is needed

1. `--derivative <UUID>` gives the preview path. In an iCloud-optimized library the Mac keeps reduced previews (about 1024 px), and the full original only through Photos.app.
2. Hand the preview to a **subagent** with an exact question ("does this receipt show a total and a date? give both, verbatim") and take its text answer; never page through photos in the main context.
3. Several candidates: downscale them into a scratch folder first and batch them to subagents with a strict per-file answer format (`NNNN: MATCH/NO — reason`):

   ```
   sips -Z 640 -s format jpeg -s formatOptions 75 "<preview path>" --out "<scratch folder>/0001_<date>.jpg"
   ```

4. `iCloud-only` (no local file): say so, and ask the user to open the photo in Photos (which downloads it) or settle for the word list. Never skip such a photo silently.

## Discretion

- These are personal photos: search for what was asked, nothing more. Do not browse, catalogue or describe unrelated pictures, and quote only the values needed, not a document's full word list, into notes.
- Read-only: the script opens both databases with `mode=ro`. Never write to anything inside the library.
- Scratch copies are deleted when the task is done; a photo leaves the machine only when the user says so.

## When it fails

| Symptom | Cause / fix |
|---|---|
| `0 photo(s)` | word form or tokenization differs: loosen the regex, drop the dates, try another distinctive word; a very new photo may not have been through Live Text yet (Photos indexes in the background) |
| `give at least one REGEX, or --tokens/--derivative UUID` | no pattern given |
| `not a photo UUID: …` | copy the full UUID from a result line |
| `--start must be YYYY-MM-DD` | fix the date |
| `no local derivative (iCloud-only)` | see step 4 above |
| `cannot open the Photos library index at …` | not a Mac, the library is elsewhere (`--library` or `PHOTOS_LIBRARY`), or macOS blocks the terminal from the Pictures folder (the user grants access in System Settings → Privacy & Security) |

## Not this skill

- Photos without text (people, places, objects) → query candidates by date in `Photos.sqlite` (`sqlite3 -readonly`, table `ZASSET`, `ZDATECREATED` = seconds since 2001-01-01 UTC), downscale to 640 px, and scan them in subagents a small batch at a time, newest first, widening on a miss.
- What is on the screen right now → the screenshot plugin.
- Files in Finder or Downloads → a file search (`mdfind <word>`), not this index.
