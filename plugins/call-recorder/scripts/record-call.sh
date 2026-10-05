#!/bin/bash
# record-call.sh — capture a FaceTime (or any) call on macOS into a stereo WAV:
# LEFT = your microphone, RIGHT = the remote party (copied in through a BlackHole loopback).
#
# Setup (one time): see README.md — BlackHole 2ch, an Aggregate input device and a
# Multi-Output device, with the call's audio output pointed at the Multi-Output.
#
# Usage:
#   record-call.sh                  # record until Ctrl-C, label "call"
#   record-call.sh -p interview     # label for the file name (<label>-<stamp>.wav)
#   record-call.sh -t 3600          # stop after N seconds
#   record-call.sh -d "Other Input" # record another avfoundation input than CallTap
#   record-call.sh -o ~/somewhere   # output folder (default ~/Recordings/calls)
#   record-call.sh -l               # list the avfoundation audio devices and exit 0
#
# Config: $CALL_RECORDER_CONF, else ${XDG_CONFIG_HOME:-~/.config}/call-recorder/call-recorder.conf,
# is sourced when it exists and overrides the defaults below (start from
# call-recorder.conf.example). Command-line flags override the config. A relative -o is taken from
# the current directory.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ---- defaults (override in call-recorder.conf) -------------------------------
INPUT_DEVICE="CallTap"        # aggregate device name (BlackHole 2ch + mic)
OUTPUT_DIR="$HOME/Recordings/calls"
PERSON="call"                 # default file-name label
SAMPLE_RATE=48000
# Channel map: the aggregate's sub-device order decides the channel numbers.
# Default: BlackHole 2ch first (c0, c1 = remote L/R), the mic third (c2).
# Left out = mic, right out = remote mixdown.
CHANNEL_MAP="pan=stereo|c0=c2|c1=0.5*c0+0.5*c1"
FFMPEG="$(command -v ffmpeg || true)"
# ------------------------------------------------------------------------------
CONF="${CALL_RECORDER_CONF:-${XDG_CONFIG_HOME:-$HOME/.config}/call-recorder/call-recorder.conf}"
if [ -f "$CONF" ]; then source "$CONF"; fi

DURATION=""
LIST=""
while getopts "p:t:d:o:l" opt; do
  case $opt in
    p) PERSON="$OPTARG" ;;
    t) DURATION="$OPTARG" ;;
    d) INPUT_DEVICE="$OPTARG" ;;
    o) OUTPUT_DIR="$OPTARG" ;;
    l) LIST=1 ;;
    *) exit 2 ;;
  esac
done

[ -n "$FFMPEG" ] || { echo "ffmpeg not found (brew install ffmpeg)"; exit 1; }

if [ -n "$LIST" ]; then
  # The listing run has no input, so ffmpeg always exits non-zero (251) after
  # printing; "|| true" keeps pipefail + set -e from ending the script first.
  "$FFMPEG" -f avfoundation -list_devices true -i "" 2>&1 |
    grep -A20 "audio devices" | grep -v "Error opening input" || true
  exit 0
fi

mkdir -p "$OUTPUT_DIR"
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$OUTPUT_DIR/${PERSON}-${STAMP}.wav"

# Resolve the avfoundation index for the named input device. Same 251 exit as
# the -l listing: without "|| true" pipefail + set -e end the script right here.
IDX=$("$FFMPEG" -f avfoundation -list_devices true -i "" 2>&1 |
      sed -n "s/.*\[\([0-9]*\)\] ${INPUT_DEVICE}$/\1/p" | head -1 || true)
[ -n "$IDX" ] || { echo "input device '$INPUT_DEVICE' not found — run with -l to list, see README for Audio MIDI setup"; exit 1; }

echo "Recording from '$INPUT_DEVICE' (device $IDX) -> $OUT"
echo "LEFT = mic (you) | RIGHT = remote. Ctrl-C to stop."
DURARG=()
[ -n "$DURATION" ] && DURARG=(-t "$DURATION")

# ${DURARG[@]+...}: macOS /bin/bash is 3.2, where "${DURARG[@]}" on an empty
# array is an unbound-variable error under set -u (recording without -t failed).
"$FFMPEG" -hide_banner -loglevel warning \
  -f avfoundation -i ":${IDX}" \
  -af "$CHANNEL_MAP" -ar "$SAMPLE_RATE" -ac 2 \
  ${DURARG[@]+"${DURARG[@]}"} "$OUT"

echo "Saved: $OUT"
echo "Next: python3 \"$SCRIPT_DIR/transcribe-call.py\" \"$OUT\""
