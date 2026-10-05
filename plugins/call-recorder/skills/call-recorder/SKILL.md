---
name: call-recorder
description: Record or transcribe a FaceTime, phone or other call on the Mac with speaker labels (you vs. the other party). Use when someone wants to record a call, capture both sides of a conversation, or turn a call recording (.wav) into a who-said-what transcript — before reaching for a phone app or a one-microphone capture. Runs the call-recorder plugin's scripts (BlackHole loopback + ffmpeg stereo capture, whisper.cpp per channel). macOS only; the other party must be told the call is being recorded.
---

# call-recorder — two-sided call capture with exact speaker labels (macOS)

`record-call.sh` writes one stereo WAV: **LEFT = your microphone, RIGHT = the remote party** (the call's audio output is copied into a BlackHole loopback). `transcribe-call.py` runs whisper.cpp on each channel separately and interleaves the segments by time, so the speaker labels come from the wiring, not from guessing. Works for FaceTime, a phone call relayed to the Mac, or any call app whose audio plays through the Mac's output.

**Consent first: tell the other party the call is being recorded**, at the start, so it is on the recording too. Many places require every party's consent. Record only calls the user takes part in and has asked to record.

## Check the environment first (one Bash call, exactly this)

```
sw_vers -productVersion
command -v ffmpeg >/dev/null && ffmpeg -hide_banner -version | head -1 || echo "ffmpeg: NOT INSTALLED (brew install ffmpeg)"
command -v whisper-cli >/dev/null && echo "whisper-cli: ok" || echo "whisper-cli: NOT INSTALLED (brew install whisper-cpp)"
ls ~/Models/whisper/ggml-large-v3-turbo-q5_0.bin 2>/dev/null || echo "whisper model: MISSING (download line in the plugin README)"
ls -d /Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver 2>/dev/null || echo "BlackHole 2ch: NOT INSTALLED (the user runs: brew install blackhole-2ch — needs an admin password)"
ffmpeg -hide_banner -f avfoundation -list_devices true -i "" 2>&1 | grep -E "\] CallTap$" || echo "CallTap (aggregate input): NOT CREATED — one-time setup step 2"
system_profiler SPAudioDataType 2>/dev/null | grep -q "CallOut:" && echo "CallOut: ok" || echo "CallOut (multi-output): NOT CREATED — one-time setup step 2"
```

Everything marked NOT must be fixed before the call, not during it. BlackHole and the two devices need the user (an admin password, clicks in Audio MIDI Setup); a session guides. A config file (`CALL_RECORDER_CONF`, default `~/.config/call-recorder/call-recorder.conf`) can rename the devices and the model path; the check above assumes the defaults.

## One-time setup

1. `brew install blackhole-2ch` — the user runs it (admin password; in a Claude session: `! brew install blackhole-2ch`).
2. **Audio MIDI Setup** (Applications → Utilities):
   - **+ → Create Aggregate Device**, name it `CallTap`: tick **BlackHole 2ch first**, then the microphone. The order sets the channel numbers; the default channel map expects BlackHole = channels 1–2, mic = 3. Tick **Drift Correction** on the microphone, with BlackHole as the clock source.
   - **+ → Create Multi-Output Device**, name it `CallOut`: tick the speakers or headphones **and** BlackHole 2ch.
3. `brew install ffmpeg whisper-cpp` if missing; model at `~/Models/whisper/ggml-large-v3-turbo-q5_0.bin` (or `WHISPER_MODEL` in the config).
4. Wiring test: set Output to `CallOut`, play any audio, have the user talk for ten seconds, then

   ```
   "${CLAUDE_PLUGIN_ROOT}/scripts/record-call.sh" -p wiring-test -t 10
   ffmpeg -hide_banner -nostats -i "$(ls -t ~/Recordings/calls/wiring-test-*.wav | head -1)" -af astats -f null - 2>&1 | grep -E "Channel: |RMS level dB"
   ```

   Channel 1 (left) must carry the voice, channel 2 (right) the played audio; an `RMS level dB: -inf` channel is silent, which means the device order in `CallTap` does not match the channel map (failure table). The first recording may raise macOS's microphone prompt for the terminal app; the user approves it. Delete the test files afterwards. Do this test before the first real call: it is the first time the recording path meets real devices on that Mac.

## Record

Run it in the background (`run_in_background: true`) with a cap a little longer than the call should last:

```
"${CLAUDE_PLUGIN_ROOT}/scripts/record-call.sh" -p <label> -t 5400
```

- `-p <label>` names the file `<label>-YYYYMMDD-HHMMSS.wav`; `-t <seconds>` stops it on its own (without `-t` it runs until Ctrl-C). `-d "<device>"` records another input than `CallTap`, `-o <dir>` another folder (default `~/Recordings/calls`), `-l` lists the input devices and exits.
- Before the call: System Settings → Sound → Output → **CallOut** (or the call app's own output setting). The microphone stays the normal input.
- **Wear headphones.** On speakers the raw microphone also hears the remote voice, and their words can turn up a second time under your label.
- Stop early with `pkill -INT -f "ffmpeg.*avfoundation"` — the same as Ctrl-C; ffmpeg closes the file properly (a SIGINT-stopped WAV keeps a valid header). The script's `Saved:` line may not print; the file is complete.
- After the call, switch Output back to the normal device (a Multi-Output device has no volume control of its own).
- Size: 48 kHz stereo 16-bit is 11.5 MB a minute, about 690 MB an hour. Keep recordings out of cloud-synced folders (some sync clients mangle large files that are still being written).

## Transcribe

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/transcribe-call.py" ~/Recordings/calls/<label>-<stamp>.wav --left "<you>" --right "<them>"
```

- Pass `--left` and `--right` for real labels; without them the transcript says `Me` (left) and `Caller` (right), or `LEFT_NAME` / `RIGHT_NAME` from the config.
- `--lang` takes `auto` (default), `en`, `es` …, one language for both channels. `auto` leaves out whisper's `-l`; if a call in another language comes back translated into English, rerun with its code.
- Run it in the background: each channel is transcribed on its own, so an hour-long call is two hours of audio for Whisper (tens of minutes on an M1-class Mac).
- Output: `<same name>.transcript.md` next to the WAV (or in `OUTPUT_DIR` from the config), and one line `transcript: <path>  (N segments, mm:ss span)` — the "span" is the start of the last segment, not the call length.

## Reading the result

```
# Call transcript — <label>-<stamp>.wav

Speakers: LEFT = <you>, RIGHT = <them>. Machine transcript (whisper.cpp) — verify quotes against audio before use; working material, do not share raw.

**<them>** [00:00]
<text>

**<you>** [00:41]
<text>
```

- A new `**Name** [mm:ss]` header starts whenever the speaker changes; minutes keep counting past 59 (`[75:10]`).
- Turn order comes from Whisper's segment start times, and silence before a remark is folded into its segment: a remark that began at 0:04.5 can be stamped `[00:00]`. Where the order of two remarks matters, check the audio.
- Whisper mishears names, numbers and look-alike words. Verify every quote against the audio before anything rides on it; the raw transcript is working material and is not passed on.
- The remote party's words under your label mean speaker bleed (no headphones), not a wiring fault.

## Discretion

- Consent, every time. Never start a recording on your own initiative.
- Recordings and transcripts are private: they stay in the recordings folder on the Mac. Never upload, attach or paste them anywhere without the user's say-so; quote only verified passages.
- Prefer the flags (`-p`, `--left`, `--right`) to the config file, and keep people's names out of the config.

## When it fails

| Symptom | Cause / fix |
|---|---|
| `input device 'CallTap' not found — run with -l …` | aggregate not created, or named differently: `record-call.sh -l`, setup step 2, or `-d "<name>"` |
| `ffmpeg not found (brew install ffmpeg)` | `brew install ffmpeg` |
| your voice on the right, or one channel `-inf` in the wiring test | device order in `CallTap` differs from the default channel map: copy `call-recorder.conf.example` from the plugin folder to `~/.config/call-recorder/call-recorder.conf` and use the flipped `CHANNEL_MAP` (mic first) from its comment, rerun the wiring test |
| `whisper-cli not found (tried …)` | `brew install whisper-cpp`, or set `WHISPER_BIN` in the config |
| `whisper model not found: …` | download the model (plugin README) or set `WHISPER_MODEL` in the config |
| no call audio in your ears | Output is not `CallOut`, or `CallOut` lacks the speakers/headphones |
| the channels drift apart on a long call | Drift Correction on the microphone in `CallTap`, BlackHole as clock source |

## Not this skill

- A video link → the video-digest plugin.
- One mixed recording (a voice memo, a meeting saved as one track) → `ffmpeg -i <file> -ar 16000 -ac 1 x.wav`, then `whisper-cli -m ~/Models/whisper/ggml-large-v3-turbo-q5_0.bin -f x.wav -l auto -otxt` (no speaker labels).
- Windows or Linux: nothing to run there; this setup is macOS-only.
