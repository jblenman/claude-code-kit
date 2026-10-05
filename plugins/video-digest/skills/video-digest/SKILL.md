---
name: video-digest
description: Read, transcribe, quote, summarize or fact-check a YouTube or other online video. Use the moment a video link (youtube.com, youtu.be, vimeo…) appears in the conversation, or someone asks what a video says or "the part at minute X" — before any curl of the watch page, caption scraping or browser reading. Runs the video-digest plugin's script (yt-dlp + local Whisper transcript + caption cross-check + optional timestamped keyframes) and says how to read its report.
---

# video-digest — read a video through a local transcript

The pipeline beats any manual route: an independent local transcript (whisper.cpp), the platform's captions as a cross-check with numeric disagreements flagged, timestamps on every line, and keyframes with their times when on-screen text matters. Use it first; the manual routes at the end are fallbacks.

## Check the environment first (one Bash call, exactly this)

```
yt-dlp --version 2>/dev/null || echo "yt-dlp: NOT INSTALLED (macOS: brew install yt-dlp; elsewhere: pip install -U yt-dlp)"
command -v "${WHISPER_CLI:-whisper-cli}" >/dev/null && echo "whisper-cli: ok" || echo "whisper-cli: NOT INSTALLED (macOS: brew install whisper-cpp; see the plugin README)"
ls "${WHISPER_MODEL:-$HOME/Models/whisper/ggml-large-v3-turbo-q5_0.bin}" 2>/dev/null || echo "whisper model: MISSING (download line in the plugin README)"
command -v ffmpeg >/dev/null && echo "ffmpeg: ok" || echo "ffmpeg: NOT INSTALLED"
```

The first line prints yt-dlp's version as a date. If it is more than about two months old, upgrade before running: the video sites change their players often, and a stale yt-dlp fails with `HTTP Error 403: Forbidden` on the media download (seen with a three-month-old build; the upgrade fixed it). Anything marked NOT INSTALLED or MISSING: tell the user what to install; do not install system packages yourself without their OK.

## Run

1. Clean the URL: drop share-tracking parameters (`si=`, `feature=`, playlist `list=`); keep `https://www.youtube.com/watch?v=<id>` (keep `t=` only if the timestamp is the point).
2. Run it in the background (`run_in_background: true`) and keep working; a 20-minute video took 2–6 minutes on a 2021 M1 laptop (download plus Whisper at several times real time):

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/video_digest.py" "https://www.youtube.com/watch?v=<id>" --lang en --no-video
   ```

   - `--lang es` for Spanish, or leave it out for auto-detection; `--sub-langs "es-orig,es"` picks other caption tracks (default: the `--lang` tracks, else English).
   - Drop `--no-video` when slides or on-screen text matter: it adds a video download and `frames/*.jpg` scene-change keyframes, with their times in `frames/index.tsv` (`--max-frames`, `--scene`).
   - Output goes to `~/video-digests/<id>/` (`--outdir`, or `VIDEO_DIGEST_DIR`). Keep it out of cloud-synced folders: some sync clients mangle large files that are still being written.
   - Windows: `py` instead of `python3`.
3. Read `report.md` first: numeric disagreements, then flagged windows, then the keyframe list. Other files: `transcript.txt` (Whisper, primary, `[hh:mm:ss - hh:mm:ss] text`), `meta.json` (title, channel, date, views, description), `captions*.vtt`, `compare.json`, `whisper.json`, `frames/`.
4. When the result feeds a document, copy `transcript.txt` (and `report.md` when flags matter) next to it and name the digest folder.

## Reading the result

- Quote from `transcript.txt`; the captions say the same with worse proper nouns. Where Whisper's rendering of a name is doubtful, keep it and mark it: "Krakov [the narrator's rendering of Kraków]".
- "Numeric-critical" means the two transcripts give different numbers at the same place (`whisper 420 vs captions 240`). Formatting twins are not flagged (`16th`/`16`, `56.000`/`56,000`/`56 000`, `'96`/`1996`, `7.20`/`7.2`, `one-fifth`/`1/5`). A number only one transcript has (spoken as a word, a garbled caption word such as "midentth" for "mid-20th") is listed lower down under "Numbers in one transcript only". Adjudicate by reading both lines; a slide outranks any transcript (a re-decode once agreed with the wrong caption number; only the slide settled it).
- `--compare-only` redoes just the cross-check and `report.md` on an existing digest folder (`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/video_digest.py" ~/video-digests/<id> --compare-only`): no download, no Whisper, standard library only.
- Flagged windows with the same words at different offsets are alignment drift, not disagreement.
- Say what the video is: a machine-voiced content channel shows a boilerplate description, no sources, mangled names in the captions and a title the body does not deliver. Transcribing a claim does not verify it; keep the video's facts as the video's.
- Whisper mishears look-alike words (it once turned Spanish "corrosión" into "corrupción"). Never pass on a raw transcript unreviewed.
- Keyframes cost many tokens: hand `frames/` and `frames/index.tsv` to a subagent that returns text; never page through images in the main context. Take times from `index.tsv`, never from the frame file names.

## Discretion

- A digest sends about eight requests to the video site and its media servers (metadata, audio, captions, video). A request guard that reads commands sees only the one URL on the command line. Keep to the video asked about: no playlists, no channel crawls, one digest per video (a rerun skips files already downloaded).
- Personal research use of public videos; the platform's terms may restrict downloading. Nothing is posted and nothing is played through anyone's account.

## When it fails

| Symptom | Cause / fix |
|---|---|
| `HTTP Error 403: Forbidden` on the audio download | stale yt-dlp: upgrade it, rerun |
| no video formats with the default client (storyboards and audio only) | rerun with `--player-client android`; if that fails, `yt-dlp -F <url>` lists formats; download one by hand into the digest folder as `video.mp4` and rerun (existing files are skipped) |
| `missing tool(s): …` or `whisper model not found` | install what it names (plugin README, Setup) or pass `--model` / `--whisper-bin` |
| captions fetched with curl come back `200` with 0 bytes | expected: the site serves captions to its player; the digest gets them through yt-dlp |
| `NO CAPTION TRACK AVAILABLE` in the report | the Whisper transcript stands alone; say so when quoting |
| the whole download path fails after an upgrade | fallback: a read-only browser session opens the clean URL, pauses the video, expands "...more" → "Show transcript" and reads the transcript panel as text. That is the platform's own captions, no cross-check; label it so |

## Not this skill

- A local video or audio file → `ffmpeg -i <file> -ar 16000 -ac 1 x.wav`, then `whisper-cli -m <model> -f x.wav -otxt` (the same engine, no cross-check).
- A recorded call → the call-recorder plugin (speaker-labeled transcripts).
- A web page → WebFetch or a paced fetcher.
