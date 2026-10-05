# screenshot

Screenshots and timed bursts of the screen for Claude Code, without letting the images fill the session. Two stdlib scripts capture with the operating system's own tools and cap the longest edge at 1800 px; a skill tells Claude when to use them and to read the images in a subagent that answers in text.

| Component | What it does |
|---|---|
| `skills/screenshot/SKILL.md` | when to capture, the commands, the read-in-a-subagent rule, failure table, discretion |
| `scripts/screenshot.py` | one capture: full screen, one monitor (`-m`), a window (`-w`), a region (`-r`), a delay (`-d`) |
| `scripts/screenshot_burst.py` | a series: `-n` frames or `-t` seconds every `-i` seconds; `--diff` keeps only frames that changed; `-w` follows the front window |
| `tests/test_screenshot.py` | offline checks of the image logic (no capture) |

Why the subagent rule: an image read in the main conversation stays in its context for the rest of the session (about width × height / 750 tokens; an 1800×1125 frame is about 2,700), and enough of them can end the session with an API error that only `/clear` clears. A subagent's images leave with the subagent.

## Install

```
claude plugin marketplace add https://github.com/jblenman/claude-code-kit    # or the path of a local clone
claude plugin install screenshot@claude-code-kit
```

For one session without installing: `claude --plugin-dir <clone>/plugins/screenshot`. The skill is `screenshot:screenshot`; pair it with the research-agents plugin's `frame-reader` agent for the reading step.

Requirements: Python 3.8+. macOS: nothing else (`screencapture`, `sips`), but the terminal app needs **Screen Recording** permission (System Settings → Privacy & Security → Screen & System Audio Recording). Windows: Windows PowerShell (built in). Linux: gnome-screenshot or ImageMagick (`import`, `convert`).

## Use

```
python3 "<plugin>/scripts/screenshot.py" -o /tmp/shot.png             # prints the absolute path
python3 "<plugin>/scripts/screenshot.py" -m 1 -o /tmp/shot.png        # monitor 1 (0-indexed)
python3 "<plugin>/scripts/screenshot_burst.py" -t 60 -i 5 --diff -o /tmp/burst
```

| Option | screenshot.py | screenshot_burst.py |
|---|---|---|
| `-o` | output file (`.png` appended when missing; default: a temp file) | output folder (default `screenshots_<timestamp>` in the current folder) |
| `-m N` | one monitor | one monitor |
| `-w` | a window: macOS waits for a click; Windows the foreground window; Linux the active window | the window in front at each frame (macOS: no click, through CoreGraphics) |
| `-r` | a region the user drags (macOS, Linux) | — |
| `-d S` | wait S seconds | — |
| `--max-size N` | longest edge, default 1800 (`0` = no resize) | same |
| `-n`, `-t`, `-i` | — | frame count, or duration; interval (defaults 10 frames, 2 s) |
| `--diff`, `--diff-threshold F` | — | keep a frame only when more than F of its pixels changed (default 0.002; compared on 160-px thumbnails; a channel must move by more than 4 of 255) |

A burst writes `frame_001.png …` and `manifest.txt` (one line per frame: time, file, size, change). On a live macOS screen a terminal status line and the clock changed 0.04–0.11 % of pixels between frames and a window update about 20 %, which is where the default threshold comes from.

## Verify it works

```
python3 "<plugin>/tests/test_screenshot.py"      # 18 checks on macOS (the resize checks need sips)
```

The tests never capture the screen: they encode PNGs with every filter type and check the stdlib decoder, the change measure behind `--diff`, the byte-comparison fallback, argument validation, and on macOS the resize through `sips`.

A real capture, when you are at the machine: `python3 "<plugin>/scripts/screenshot.py" -o /tmp/shot.png` prints `/tmp/shot.png`; open it. In Claude Code, ask "take a screenshot and tell me which window is in front": the session should capture, hand the file to a subagent and answer from its text.

## What it cannot do

- **macOS without Screen Recording permission** captures only the wallpaper and menu bar, without an error. The skill's first check catches it.
- **No desktop, no capture:** over SSH, from a service or a scheduled task there is no interactive desktop (Windows reports a single 1024×768 screen).
- **Interactive modes block:** `screenshot.py -w` and `-r` on macOS wait for a click or drag.
- **Windows:** no region selection (falls back to the full screen); `-w` usually captures the terminal itself.
- **Change detection is pixel-based:** a ticking clock or a blinking cursor counts as change at threshold 0; when the thumbnail cannot be decoded the burst falls back to comparing bytes, where any change counts.
- It cannot see what the screen shows: that is the subagent's job, and only when the user asked for it.
- The capture paths come from the operating systems' own tools and were exercised on macOS 26 (Python 3.13 and 3.9); the plugin's automated tests cover the image logic only.

## Rollback

`claude plugin uninstall screenshot@claude-code-kit`. The scripts keep nothing except the files you ask them to write.
