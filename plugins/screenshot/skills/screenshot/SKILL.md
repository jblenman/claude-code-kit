---
name: screenshot
description: Take a screenshot of the desktop, one monitor, a window or a selected region, or capture a timelapse/burst of the screen over time, and read it without flooding the context. Use when someone asks to grab, capture or look at the screen ("what's on my screen", "screenshot that error", "watch the screen for a minute"). Runs the screenshot plugin's screenshot.py and screenshot_burst.py (macOS, Windows, Linux; longest edge capped at 1800 px) and reads the images in a subagent, never in the main context.
---

# screenshot — capture the screen, read it in a subagent

The two scripts use the operating system's own capture (macOS `screencapture`, Windows PowerShell + .NET, Linux gnome-screenshot or ImageMagick), cap the longest edge at 1800 px and print the file path. **The reading rule matters more than the capture:** an image read in the main conversation stays there (about width × height / 750 tokens; an 1800×1125 frame ≈ 2.7k), and accumulated images can end a session with an unrecoverable API error that only `/clear` fixes. So the main session captures, a subagent looks and returns text.

## Check the environment first (one Bash call, exactly this)

macOS:

```
python3 -c "import ctypes; cg=ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics'); cg.CGPreflightScreenCaptureAccess.restype=ctypes.c_bool; print('screen recording permission:', 'ok' if cg.CGPreflightScreenCaptureAccess() else 'MISSING (System Settings > Privacy & Security > Screen & System Audio Recording: enable the terminal app)')"
system_profiler SPDisplaysDataType | grep -E "Resolution|Main Display"
```

The first line checks the permission without triggering a prompt. Windows (PowerShell), to see the monitor numbers `-m` uses:

```
Add-Type -AssemblyName System.Windows.Forms; $s=[System.Windows.Forms.Screen]::AllScreens; for ($i=0; $i -lt $s.Length; $i++) { "{0}: {1} primary={2}" -f $i, $s[$i].Bounds, $s[$i].Primary }
```

Run it in a session on that machine: over SSH Windows reports a single 1024×768 screen, because an SSH session has no real desktop.

## Capture one screenshot

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/screenshot.py" -o "<scratch folder>/shot.png"          # macOS: main display; Windows: all monitors as one image
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/screenshot.py" -m 1 -o "<scratch folder>/shot.png"     # one monitor, 0-indexed
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/screenshot.py" -d 3 -o "<scratch folder>/shot.png"     # wait 3 s first
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/screenshot.py" -w -o "<scratch folder>/shot.png"       # one window
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/screenshot.py" -r -o "<scratch folder>/shot.png"       # a region the user drags
```

- It prints the absolute path on success. Without `-o` the file goes to the system temp folder as `screenshot_XXXX.png`; give `-o` in a scratch folder so cleanup is easy. `.png` is appended when missing. Windows: `py` instead of `python3`.
- `--max-size N` sets the longest edge (default 1800; 1280 is too small to read UI text reliably, and above 2000 the API rejects the image). `0` disables resizing: don't.
- **`-w` and `-r` are interactive on macOS**: the command waits until the user clicks a window or drags a region. Use them only when the user is at the machine and expects it. On Windows `-w` takes the foreground window (usually the terminal itself) and `-r` falls back to the full screen.
- **Windows with several monitors: always pass `-m`.** Without it the whole virtual desktop is squeezed into 1800 px, and with large monitors the text becomes unreadable. List the numbering with the PowerShell line above.
- A capture takes about half a second on a recent Mac (a 2560×1600 display → 1800×1125 PNG, about 1.5 MB).

## Read it (always in a subagent)

1. Spawn a subagent (the research-agents plugin's `frame-reader` when installed, otherwise a general-purpose one) with an exact brief: "Read the image at `<path>`. Report: <the question — the error text verbatim, which window is in front, whether X shows Y>. Quote on-screen text exactly. Text only."
2. Use its answer; ask a follow-up of the same subagent rather than opening the image yourself.
3. Delete the file when done.

If Read ever refuses an image as too large, convert it first (`sips -s format jpeg -s formatOptions 80 shot.png --out shot.jpg` on macOS) or capture with a smaller `--max-size`.

## Timelapse / burst

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/screenshot_burst.py" -n 10 -i 2 -o "<scratch folder>/burst"      # 10 frames, 2 s apart (the defaults)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/screenshot_burst.py" -t 60 -i 5 -o "<scratch folder>/burst"      # for 60 s, a frame every 5 s
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/screenshot_burst.py" -m 3 -n 5 -i 1 -o "<scratch folder>/burst"  # one monitor
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/screenshot_burst.py" --diff -t 60 -i 3 -o "<scratch folder>/burst"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/screenshot_burst.py" -w -n 5 -i 2 -o "<scratch folder>/burst"   # the window in front, each frame
```

- Output: `<dir>/frame_001.png …` plus `manifest.txt` (start time, frames captured and saved, interval, then one line per frame: timestamp, file, size, notes). The directory path goes to stdout, progress to stderr. Without `-o` it creates `screenshots_<timestamp>` in the current directory, so always pass `-o`.
- `-n` and `-t` are alternatives; `-i` is the interval in seconds; `--max-size` as above.
- Run anything longer than a few frames in the background (`run_in_background: true`).
- Budget: at 1800 px each frame is about 1.5 MB and 2.7k tokens to look at. For "when did X appear" questions, `--max-size 1024` (about 0.9k tokens a frame) is usually enough; split the frames across subagents (a few dozen each), give each the manifest, and ask for frame numbers and times, not descriptions of every frame.
- `--diff` keeps a frame only when more than `--diff-threshold` of its pixels changed since the last kept frame (a fraction; default 0.002 = 0.2%; compared on 160-px thumbnails). On a live macOS screen a terminal status line and the clock changed 0.04–0.11% per frame and a window update about 20%, so the default skips the noise; a short new line of text can stay under it — use `--diff-threshold 0.0005`, or `0` for any visible change (giving the threshold implies `--diff`). Each kept frame's change is in the manifest (`changed 0.29%`).
- `-w` captures the window in front at each frame, so the user can start the burst and then click into the window to watch; `-m` is ignored. On macOS no click is needed (unlike `screenshot.py -w`): the frontmost normal window is found through CoreGraphics and captured with `screencapture -l`, without shadow or shutter sound. On Windows it is the foreground window, usually the terminal itself.

## Discretion

- Screens show private things: mail, messages, other sessions, documents. Capture only when asked, keep the files in a scratch folder, delete them after reading, and never send a screenshot anywhere (upload, artifact, message) without the user's OK.
- Do not capture a screen that may show confidential work material (a work remote desktop, a client's system) unless the user asks for exactly that.

## When it fails

| Symptom | Cause / fix |
|---|---|
| macOS image shows only the wallpaper and menu bar, no windows | the terminal app lacks Screen Recording permission: the check prints MISSING; the user enables it in System Settings |
| `ERROR: screencapture failed: …` | permission, or `-m` names a display that does not exist |
| the command hangs on macOS | `-w` or `-r` is waiting for a click or drag; the user acts, or stop the task |
| `Monitor index N not found. Available: 0..K` (Windows) | wrong `-m`; list the monitors with the PowerShell line |
| black or 1024×768 image, or a failure, on Windows from SSH or a service | no interactive desktop there; capture from a session running on that machine |
| `NOTE: Interactive region select not supported on Windows, capturing full screen.` | expected; use `-m` to narrow instead |
| `WARNING: Resize failed, using original size.` | the file may now exceed 2000 px, which the API rejects: resize it (`sips -Z 1800 <file>` on macOS) before any read |
| `ERROR: No screenshot tool found. …` (Linux) | install gnome-screenshot or ImageMagick |

## Not this skill

- A web page → browser tools: page text is far cheaper than any image, and their own screenshot covers the visual case.
- Clicking and typing on the desktop → computer use.
- Frames of an online video → the video-digest plugin.
