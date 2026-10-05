#!/usr/bin/env python3
"""transcribe-call.py - stereo call recording -> speaker-labeled transcript.

Expects the record-call.sh convention: LEFT channel = the local speaker (you), RIGHT channel = the
remote party. Splits the channels, transcribes each with whisper.cpp, then interleaves the segments
by start time into one Markdown transcript with speaker labels and [mm:ss] stamps.

Usage:
  transcribe-call.py recording.wav                      # labels: Me / Caller
  transcribe-call.py recording.wav --left Interviewer --right Guest --lang auto

Config: $CALL_RECORDER_CONF, else ${XDG_CONFIG_HOME:-~/.config}/call-recorder/call-recorder.conf,
overrides the DEFAULTS below (keys: WHISPER_BIN, WHISPER_MODEL, LEFT_NAME, RIGHT_NAME, LANGUAGE,
OUTPUT_DIR - empty OUTPUT_DIR writes next to the input file). Shell syntax, because record-call.sh
sources the same file: KEY="value", # comments, $HOME and ~ expanded. Start from
call-recorder.conf.example and keep people's names out of it (pass --left/--right instead).

FFmpeg 7 removed -map_channel; the channel split uses the pan filter (tested with FFmpeg 8.1).

The raw machine transcript is working material: verify quotes against the audio before anything
rides on them, and do not pass raw transcripts on (Whisper mishears names, numbers and look-alike
words).
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULTS = {
    "WHISPER_BIN": "",            # empty: whisper-cli on PATH, then the Homebrew prefixes below
    "WHISPER_MODEL": str(Path.home() / "Models/whisper/ggml-large-v3-turbo-q5_0.bin"),
    "LEFT_NAME": "Me",            # generic on purpose: pass --left/--right for real labels
    "RIGHT_NAME": "Caller",
    "LANGUAGE": "auto",           # 'auto' handles mixed-language calls
    "OUTPUT_DIR": "",
}
WHISPER_FALLBACKS = ["/opt/homebrew/bin/whisper-cli", "/usr/local/bin/whisper-cli"]


def conf_path():
    p = os.environ.get("CALL_RECORDER_CONF", "").strip()
    if p:
        return Path(p).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME", "").strip() or str(Path.home() / ".config")
    return Path(base).expanduser() / "call-recorder" / "call-recorder.conf"


def load_conf(path=None):
    conf = dict(DEFAULTS)
    p = conf_path() if path is None else Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                # Parse the value the way the shell does (quotes, trailing "# comment"),
                # then expand $HOME / ~ as `source` would.
                try:
                    v = " ".join(shlex.split(v, comments=True))
                except ValueError:
                    v = v.strip().strip('"')
                conf[k.strip()] = os.path.expanduser(os.path.expandvars(v))
    return conf


def which_whisper(conf):
    cands = ([conf["WHISPER_BIN"]] if conf.get("WHISPER_BIN") else []) + [shutil.which("whisper-cli") or ""] + WHISPER_FALLBACKS
    for c in cands:
        if c and Path(c).exists():
            return c
    sys.exit("whisper-cli not found (tried %s); install whisper.cpp (macOS: brew install whisper-cpp) "
             "or set WHISPER_BIN in %s" % ([c for c in cands if c], conf_path()))


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        sys.exit("command failed (%s): %s" % (cmd[0], r.stderr[-400:]))
    return r


def transcribe_channel(wav, channel, conf, whisper, tmp):
    """channel: 0 = left, 1 = right. Returns a list of (t0, t1, text)."""
    mono = Path(tmp) / ("ch%d.wav" % channel)
    # pan, not -map_channel: FFmpeg 7 removed -map_channel.
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
         "-af", "pan=mono|c0=c%d" % channel, "-ar", "16000", str(mono)])
    out = Path(tmp) / ("ch%d" % channel)
    cmd = [whisper, "-m", conf["WHISPER_MODEL"], "-f", str(mono), "-oj", "-of", str(out), "--no-prints"]
    if conf["LANGUAGE"] != "auto":
        cmd += ["-l", conf["LANGUAGE"]]
    run(cmd)
    data = json.loads(out.with_suffix(".json").read_text(encoding="utf-8", errors="replace"))
    segs = []
    for s in data.get("transcription", []):
        text = s.get("text", "").strip()
        if not text:
            continue
        segs.append((s["offsets"]["from"] / 1000.0, s["offsets"]["to"] / 1000.0, text))
    return segs


def mmss(t):
    return "%02d:%02d" % (int(t) // 60, int(t) % 60)


def render(name, left_name, right_name, left, right):
    """The transcript text: segments of both channels interleaved by start time."""
    merged = sorted([(t0, t1, left_name, tx) for t0, t1, tx in left]
                    + [(t0, t1, right_name, tx) for t0, t1, tx in right])
    lines = ["# Call transcript — %s" % name, "",
             "Speakers: LEFT = %s, RIGHT = %s. Machine transcript (whisper.cpp) — verify quotes against "
             "audio before use; working material, do not share raw." % (left_name, right_name), ""]
    prev = None
    for t0, _t1, who, text in merged:
        if who != prev:
            lines.append("\n**%s** [%s]" % (who, mmss(t0)))
            prev = who
        lines.append(text)
    return "\n".join(lines) + "\n", merged


def main():
    ap = argparse.ArgumentParser(description="Stereo call recording -> speaker-labeled transcript (whisper.cpp per channel).")
    ap.add_argument("wav", type=Path)
    ap.add_argument("--left", help="left-channel speaker label (default: Me)")
    ap.add_argument("--right", help="right-channel speaker label (default: Caller)")
    ap.add_argument("--lang", help="language code or 'auto'")
    args = ap.parse_args()

    if not args.wav.is_file():
        sys.exit("no such file: %s" % args.wav)
    conf = load_conf()
    if args.left:
        conf["LEFT_NAME"] = args.left
    if args.right:
        conf["RIGHT_NAME"] = args.right
    if args.lang:
        conf["LANGUAGE"] = args.lang
    whisper = which_whisper(conf)
    if not Path(conf["WHISPER_MODEL"]).is_file():
        sys.exit("whisper model not found: %s (set WHISPER_MODEL in %s)" % (conf["WHISPER_MODEL"], conf_path()))

    with tempfile.TemporaryDirectory() as tmp:
        left = transcribe_channel(args.wav, 0, conf, whisper, tmp)
        right = transcribe_channel(args.wav, 1, conf, whisper, tmp)

    text, merged = render(args.wav.name, conf["LEFT_NAME"], conf["RIGHT_NAME"], left, right)
    outdir = Path(conf["OUTPUT_DIR"]) if conf["OUTPUT_DIR"] else args.wav.parent
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / (args.wav.stem + ".transcript.md")
    out.write_text(text, encoding="utf-8")
    print("transcript: %s  (%d segments, %s span)" % (out, len(merged), mmss(merged[-1][0]) if merged else "00:00"))


if __name__ == "__main__":
    main()
