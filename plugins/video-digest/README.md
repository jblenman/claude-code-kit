# video-digest

Read an online video without watching it. One script pulls the audio and the platform's captions with yt-dlp, transcribes the audio locally with whisper.cpp, compares the two transcripts window by window and flags where they disagree (different numbers at the same place are critical), and, when asked, extracts scene-change keyframes with their timestamps so slides and on-screen text can be read. A skill tells Claude to use it the moment a video link shows up, and how to read the result.

| Component | What it does |
|---|---|
| `skills/video-digest/SKILL.md` | when to use the digest, the exact commands, how to read the report, failure table |
| `scripts/video_digest.py` | the pipeline (stdlib Python 3.8+; external tools below) |
| `tests/test_compare.py`, `tests/test_video_digest.py` | offline tests (no network) |

## Install

```
claude plugin marketplace add https://github.com/jblenman/claude-code-kit    # or the path of a local clone
claude plugin install video-digest@claude-code-kit
```

For one session without installing: `claude --plugin-dir <clone>/plugins/video-digest`. The skill appears as `video-digest:video-digest`; Claude picks it up from its description, or you type `/video-digest:video-digest`.

## Setup: the tools it runs

| Tool | macOS | Windows | Linux |
|---|---|---|---|
| yt-dlp | `brew install yt-dlp` | `winget install yt-dlp.yt-dlp` or `pip install -U yt-dlp` | `pip install -U yt-dlp` |
| ffmpeg + ffprobe | `brew install ffmpeg` | `winget install Gyan.FFmpeg` | the distribution's package |
| whisper.cpp (`whisper-cli`) | `brew install whisper-cpp` | a release build from the whisper.cpp project; pass `--whisper-bin` or set `WHISPER_CLI` to `whisper-cli.exe` | build from source (CMake) |
| a GGML Whisper model | see below | see below | see below |
| Pillow (optional, keyframe dedup) | `python3 -m pip install --user pillow` | `py -m pip install pillow` | same |

The model (about 570 MB; large-v3-turbo, 5-bit quantized, a good multilingual default):

```
mkdir -p ~/Models/whisper && curl -L -o ~/Models/whisper/ggml-large-v3-turbo-q5_0.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
```

Keep yt-dlp current: video sites change their players often, and a build a few months old fails with `HTTP Error 403: Forbidden` on the media download.

## Use

```
python3 "<plugin>/scripts/video_digest.py" "https://www.youtube.com/watch?v=<id>" --lang en --no-video
```

| Option | Meaning | Default |
|---|---|---|
| `--outdir DIR` | output root; one folder per video | `VIDEO_DIGEST_DIR`, else `~/video-digests` |
| `--model PATH` | GGML model file | `WHISPER_MODEL`, else `~/Models/whisper/ggml-large-v3-turbo-q5_0.bin` |
| `--whisper-bin PATH` | the whisper.cpp CLI | `WHISPER_CLI`, else `whisper-cli` on `PATH` |
| `--lang CODE` | spoken language for Whisper (`en`, `es`, …) | `auto` |
| `--sub-langs LIST` | caption tracks for yt-dlp | `<lang>-orig,<lang>` with `--lang`, else `en-orig,en` |
| `--no-video` | no video download, no keyframes | off |
| `--max-frames N`, `--scene X`, `--frame-interval S` | keyframe count cap, scene-change threshold (0–1), one frame at least every S seconds | 60, 0.30, 150 |
| `--window S`, `--flag-threshold X` | comparison window, similarity below which a window is flagged | 15, 0.72 |
| `--player-client NAME` | yt-dlp's YouTube player client | `default` |
| `--compare-only` | redo only the cross-check and report on an existing digest folder (give the folder or the URL); no tools needed | off |

A rerun skips every file already in the folder, so an interrupted digest resumes where it stopped.

Keep the output out of cloud-synced folders: some sync clients mangle large files while they are still being written.

## Output (`<outdir>/<video id>/`)

| File | Content |
|---|---|
| `report.md` | read first: numeric disagreements, then flagged low-agreement windows, then numbers found in one transcript only, then the keyframe list with times |
| `transcript.txt` | the Whisper transcript, `[hh:mm:ss - hh:mm:ss] text`: the primary record to quote from |
| `meta.json` | title, channel, upload date, duration, views, description, URL |
| `captions*.vtt` | the platform's caption tracks (the cross-check) |
| `compare.json` | every window: similarity, level, numbers on each side, conflicts, one-sided numbers |
| `whisper.json`, `audio.m4a`, `audio16k.wav` | raw Whisper output and the audio |
| `frames/f_*.jpg`, `frames/index.tsv` | keyframes (960 px wide) and their timestamps |

## How the cross-check works

- Whisper's text is the primary record; the captions are a second, independent reading. Both carry timestamps, so the text is bucketed into fixed windows (15 s) and each Whisper window is compared with the caption windows around it (captions lag or lead by up to a window).
- Numbers are checked separately, place by place: each window's words and numbers are aligned with the captions of that window and its neighbours. **Different numbers at the same place are critical.** A number only one transcript has (spoken as a word in the other, a garbled caption word, a dropped phrase) is listed separately, lower priority. Formatting twins are not flagged: `16th`/`16`, `56.000`/`56,000`/`56 000`, `'96`/`1996`, `7.20`/`7.2`, `one-fifth`/`1/5`. Numerals are split at their separators, so `September 16, 2026` is two numbers and `1996 1996` stays two (an earlier version joined them and raised false alarms).
- Two transcripts that agree can still share an error. Where a number matters and a slide shows it, the slide wins: in one case a re-decode agreed with the wrong caption number and only the slide settled it.
- Keyframe times come from ffmpeg's own frame timestamps (`showinfo`), written to `frames/index.tsv`. Never compute times from the frame file names: their numbers depend on the video's frame rate (a 25 fps upload read as 30 fps drifts by about 20 minutes over two hours).

## Verify it works

Offline (no network, no Whisper needed for the first; ffmpeg for the keyframe checks of the second):

```
python3 "<plugin>/tests/test_compare.py"        # 24 tests (one skipped unless VIDEO_DIGEST_REPLAY is set)
python3 "<plugin>/tests/test_video_digest.py"   # 18 checks, incl. keyframe timestamps on a synthetic video
```

Live, with a short public video of your choice:

```
python3 "<plugin>/scripts/video_digest.py" "https://www.youtube.com/watch?v=<id>" --no-video --lang en
```

It prints `== fetch ==`, `== transcribe ==`, `== captions == captions.en….vtt (N cues)` and `== done == report: …/report.md (agree …, flagged …, numeric-critical …)`. With a digest of your own you can also run the mutation check: `VIDEO_DIGEST_REPLAY=~/video-digests/<id> python3 "<plugin>/tests/test_compare.py"` changes every number in turn and expects at least 95 % to be caught.

## What it cannot do

- **It does not verify the video.** It tells you what the video says, with timestamps; whether that is true is a separate question.
- **Requests a command-level guard cannot see.** A digest sends about eight requests to the video site and its media servers (metadata, audio, captions, video), all made by yt-dlp. A request guard that reads commands sees one URL. Keep to the video asked about: no playlists or channel crawls (the script passes `--no-playlist`).
- **No captions, no cross-check.** Without a caption track the report says so and the Whisper transcript stands alone.
- **Whisper mishears** names, numbers and look-alike words (it once turned Spanish "corrosión" into "corrupción"). Review before passing a transcript on.
- **Frames cost tokens.** In Claude Code, hand `frames/` to a subagent that answers in text (the research-agents plugin's `frame-reader`), never page through images in the main session.
- Some videos expose video formats only to another player client: rerun with `--player-client android`, or download one format by hand into the folder as `video.mp4` and rerun.
- The platform's terms may restrict downloading; this is meant for personal research on public videos. Nothing is posted and nothing is played through an account.
- Tested on macOS (Apple Silicon, Python 3.13 and 3.9). The Windows and Linux rows above are the documented install paths of each tool, not tested runs.

## Rollback

`claude plugin uninstall video-digest@claude-code-kit`. The digests stay in `~/video-digests/` (or your `--outdir`) until you delete them; the tools and the model are yours to keep or remove.
