# call-recorder (macOS)

Record a call on a Mac as one stereo file — **your microphone on the left, the other party on the right** — and turn it into a transcript that says who said what. The labels come from the wiring, not from AI speaker guessing: each channel is transcribed on its own with whisper.cpp and the segments are interleaved by time. Works for FaceTime, phone calls relayed to the Mac, and any call app whose audio plays through the Mac's output.

**Consent rule: tell the other party the call is being recorded.** Many places require every party's consent. The skill tells Claude never to start a recording on its own.

| Component | What it does |
|---|---|
| `skills/call-recorder/SKILL.md` | environment check, one-time setup, wiring test, record, transcribe, how to read the transcript, failure table |
| `scripts/record-call.sh` | records the aggregate input device to a stereo WAV with ffmpeg (bash 3.2 compatible) |
| `scripts/transcribe-call.py` | splits the channels, runs whisper.cpp per channel, writes `<name>.transcript.md` (stdlib Python 3.8+) |
| `call-recorder.conf.example` | every setting, commented |
| `tests/test_call_recorder.py` | offline checks with a stand-in ffmpeg and a stand-in whisper-cli |

## How it works

The call app's output goes to a **Multi-Output device** (so you still hear it) that also feeds the **BlackHole** virtual driver. An **Aggregate input device** combines BlackHole with your microphone. `record-call.sh` records that aggregate and pans it to stereo: left = microphone, right = BlackHole (the remote party). `transcribe-call.py` transcribes each side and merges them into one Markdown transcript with `**Name** [mm:ss]` headers.

## Install

```
claude plugin marketplace add https://github.com/jblenman/claude-code-kit    # or the path of a local clone
claude plugin install call-recorder@claude-code-kit
```

For one session without installing: `claude --plugin-dir <clone>/plugins/call-recorder`. The skill is `call-recorder:call-recorder`.

## One-time setup

1. `brew install blackhole-2ch ffmpeg whisper-cpp` (BlackHole asks for an admin password).
2. The Whisper model (about 570 MB):

   ```
   mkdir -p ~/Models/whisper && curl -L -o ~/Models/whisper/ggml-large-v3-turbo-q5_0.bin \
     https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin
   ```

3. **Audio MIDI Setup** (Applications → Utilities):
   - **+ → Create Aggregate Device**, name it `CallTap`. Tick **BlackHole 2ch first**, then your microphone (the order sets the channel numbers: the default map expects BlackHole on channels 1–2 and the microphone on 3). Tick **Drift Correction** on the microphone, with BlackHole as the clock source.
   - **+ → Create Multi-Output Device**, name it `CallOut`. Tick your speakers or headphones **and** BlackHole 2ch.
4. Before a call: System Settings → Sound → Output → `CallOut` (or the call app's own output setting). Your microphone stays the normal input.
5. Run the wiring test from the skill (a 10-second recording and a channel-level check) before the first real call. The first recording may raise the macOS microphone prompt for your terminal app.

## Use

```
"<plugin>/scripts/record-call.sh" -p interview -t 5400        # Ctrl-C (or the -t cap) stops it
python3 "<plugin>/scripts/transcribe-call.py" ~/Recordings/calls/interview-20261004-190000.wav --left Me --right Guest
```

| `record-call.sh` | Meaning |
|---|---|
| `-p LABEL` | file name `<LABEL>-YYYYMMDD-HHMMSS.wav` (default `call`) |
| `-t SECONDS` | stop after N seconds (default: until Ctrl-C) |
| `-d DEVICE` | avfoundation input to record (default `CallTap`) |
| `-o DIR` | output folder (default `~/Recordings/calls`) |
| `-l` | list the avfoundation audio devices and exit |

| `transcribe-call.py` | Meaning |
|---|---|
| `WAV` | the stereo recording (left = you, right = remote) |
| `--left NAME`, `--right NAME` | speaker labels (default `Me` / `Caller`) |
| `--lang CODE` | `auto` (default), `en`, `es` …, one language for both channels |

The transcript lands next to the WAV as `<name>.transcript.md`. Wear headphones: on speakers your microphone also picks up the other party, whose words then appear a second time under your label.

## Configuration (optional)

`$CALL_RECORDER_CONF`, else `~/.config/call-recorder/call-recorder.conf` (`$XDG_CONFIG_HOME` is honored). Both scripts read it: `record-call.sh` sources it, `transcribe-call.py` parses the same shell syntax (quotes, `#` comments, `$HOME` and `~`). Start from `call-recorder.conf.example`. Keys: `INPUT_DEVICE`, `PERSON`, `SAMPLE_RATE`, `CHANNEL_MAP`, `OUTPUT_DIR` (shared: recordings and transcripts; empty = transcripts next to the WAV), `WHISPER_BIN`, `WHISPER_MODEL`, `LEFT_NAME`, `RIGHT_NAME`, `LANGUAGE`. Flags override the file. The config lives outside the plugin folder on purpose: an installed plugin's folder is replaced on update. Keep people's names out of it; pass them per call with `--left` / `--right`.

If the microphone comes first in your aggregate device, the flipped channel map is in the example file's comment: `CHANNEL_MAP="pan=stereo|c0=c0|c1=0.5*c1+0.5*c2"`.

## Verify it works

```
python3 "<plugin>/tests/test_call_recorder.py"     # 21 checks, about 10 s
```

The tests run `record-call.sh` through its own `#!/bin/bash` (bash 3.2 on macOS) against a stand-in ffmpeg that lists devices and "records" an empty file: device lookup, `-l`, the `-t` cap, the empty-argument case that once broke recording without `-t`, a config that renames the device and channel map, and the missing-ffmpeg message. `transcribe-call.py` runs with the real ffmpeg on a generated stereo file and a stand-in whisper-cli: one call per channel, labels, interleaving, `[75:12]`-style stamps past an hour.

On a real Mac, the wiring test in the skill is the proof: left channel carries your voice, right channel the played audio. Verified in the original environment: device listing, transcription of a stereo test file (Spanish, with `--lang auto`), and that a SIGINT-stopped recording keeps a valid WAV header. A full call through a freshly built `CallTap` is the step every new setup should prove with the wiring test.

## What it cannot do

- **macOS only.** It records avfoundation devices; there is no Windows or Linux path.
- **No speaker separation inside a channel.** Several people on the remote side all land under the right-hand label.
- **Bleed without headphones** puts the other party's words under your label too.
- **Turn order is approximate:** Whisper folds silence into the following segment, so a remark that started at 0:04.5 can be stamped `[00:00]`.
- **Whisper mishears** names, numbers and look-alike words; verify quotes against the audio.
- A Multi-Output device has no volume control of its own: switch Output back after the call.
- Alternatives without BlackHole exist (Core Audio process taps on macOS 14.4+, commercial apps); this plugin uses the loopback route.

## Rollback

`claude plugin uninstall call-recorder@claude-code-kit`. Recordings and transcripts stay in your recordings folder; remove `CallTap` / `CallOut` in Audio MIDI Setup and BlackHole with `brew uninstall blackhole-2ch` if you no longer want them; delete `~/.config/call-recorder/` for the config.
