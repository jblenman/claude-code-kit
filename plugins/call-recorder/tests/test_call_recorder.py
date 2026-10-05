"""Offline checks for the call-recorder plugin. record-call.sh runs against a stand-in ffmpeg that prints
an avfoundation device list and "records" an empty file, so no microphone, BlackHole or call is
needed; transcribe-call.py runs with the real ffmpeg (channel split) and a stand-in whisper-cli
that answers per channel. Config parsing, label interleaving and the [mm:ss] stamps are checked
directly.

    python3 plugins/call-recorder/tests/test_call_recorder.py

Stdlib only, Python 3.8+, macOS or Linux (bash). Exit code 0 when every check passed.
"""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
RECORD = PLUGIN / "scripts" / "record-call.sh"
TRANSCRIBE = PLUGIN / "scripts" / "transcribe-call.py"
PY = sys.executable
RESULTS = []

FAKE_FFMPEG = r"""#!/bin/bash
if [[ " $* " == *" -list_devices true "* ]]; then
  {
    echo "[AVFoundation indev @ 0x1] AVFoundation video devices:"
    echo "[AVFoundation indev @ 0x1] [0] Built-in Camera"
    echo "[AVFoundation indev @ 0x1] AVFoundation audio devices:"
    echo "[AVFoundation indev @ 0x1] [0] Built-in Microphone"
    echo "[AVFoundation indev @ 0x1] [1] BlackHole 2ch"
    echo "[AVFoundation indev @ 0x1] [2] CallTap"
    echo "[AVFoundation indev @ 0x1] [3] Other Tap"
    echo "Error opening input file ."
  } >&2
  exit 251
fi
printf '%s\n' "$@" > "$FAKE_ARGS_LOG"
out="${@: -1}"
: > "$out"
exit 0
"""

FAKE_WHISPER = r'''#!{py}
import json, os, sys
a = sys.argv
out = a[a.index("-of") + 1]
src = a[a.index("-f") + 1]
segs = {{"ch0": [(0, 4500, " Hello, thanks for taking the call."), (9000, 12000, " Sure, go ahead.")],
        "ch1": [(4600, 8900, " Happy to. Can you hear me?"), (4512000, 4515000, " Bye for now.")]}}
key = "ch0" if src.endswith("ch0.wav") else "ch1"
data = {{"transcription": [{{"offsets": {{"from": f, "to": t}}, "text": x}} for f, t, x in segs[key]]}}
open(out + ".json", "w").write(json.dumps(data))
with open(os.environ["FAKE_WHISPER_LOG"], "a") as fh:
    fh.write(" ".join(a[1:]) + "\n")
'''


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("%s %s%s" % ("ok  " if ok else "FAIL", name, (" -- " + str(detail)) if (detail and not ok) else ""))
    return ok


def load():
    spec = importlib.util.spec_from_file_location("transcribe_call", str(TRANSCRIBE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def executable(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="call-recorder-test-"))
    pj = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    check("plugin.json name/version/description/author", pj.get("name") == "call-recorder" and pj.get("version") == "0.1.0"
          and pj.get("description") and (pj.get("author") or {}).get("name"))
    skill = (PLUGIN / "skills" / "call-recorder" / "SKILL.md").read_text(encoding="utf-8")
    check("skill runs both scripts through ${CLAUDE_PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}/scripts/record-call.sh" in skill
          and "${CLAUDE_PLUGIN_ROOT}/scripts/transcribe-call.py" in skill)
    check("scripts are executable", os.access(str(RECORD), os.X_OK) and os.access(str(TRANSCRIBE), os.X_OK))
    check("the example config exists", (PLUGIN / "call-recorder.conf.example").is_file())

    # ---- config
    mod = load()
    env_saved = {k: os.environ.get(k) for k in ("CALL_RECORDER_CONF", "XDG_CONFIG_HOME")}
    try:
        os.environ.pop("CALL_RECORDER_CONF", None)
        os.environ["XDG_CONFIG_HOME"] = str(tmp / "xdg")
        check("conf_path follows XDG_CONFIG_HOME", mod.conf_path() == tmp / "xdg" / "call-recorder" / "call-recorder.conf")
        os.environ.pop("XDG_CONFIG_HOME", None)
        check("conf_path defaults to ~/.config", mod.conf_path() == Path.home() / ".config" / "call-recorder" / "call-recorder.conf")
        conf = tmp / "my.conf"
        conf.write_text('# comment\nLEFT_NAME="Host"   # trailing comment\nRIGHT_NAME=Guest\n'
                        'OUTPUT_DIR="$HOME/rec dir"\nWHISPER_MODEL=~/m.bin\nLANGUAGE=\'es\'\n', encoding="utf-8")
        os.environ["CALL_RECORDER_CONF"] = str(conf)
        check("CALL_RECORDER_CONF wins", mod.conf_path() == conf)
        c = mod.load_conf()
        check("config: shell quoting, comments, $HOME and ~ expanded",
              c["LEFT_NAME"] == "Host" and c["RIGHT_NAME"] == "Guest" and c["LANGUAGE"] == "es"
              and c["OUTPUT_DIR"] == str(Path.home() / "rec dir") and c["WHISPER_MODEL"] == str(Path.home() / "m.bin"), c)
        check("config: missing file keeps the defaults", mod.load_conf(tmp / "none.conf") == mod.DEFAULTS)
    finally:
        for k, v in env_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    text, merged = mod.render("call-1.wav", "Host", "Guest",
                              [(0.0, 4.5, "Hello."), (9.0, 12.0, "Go ahead.")],
                              [(4.6, 8.9, "Hi."), (4512.0, 4515.0, "Bye.")])
    check("render interleaves by start time with a header per speaker change",
          text.splitlines()[4:] == ["", "**Host** [00:00]", "Hello.", "", "**Guest** [00:04]", "Hi.", "",
                                    "**Host** [00:09]", "Go ahead.", "", "**Guest** [75:12]", "Bye."], text)
    check("render: minutes keep counting past 59", "[75:12]" in text and len(merged) == 4)

    # ---- record-call.sh against a stand-in ffmpeg
    fake_bin = tmp / "bin"
    fake_bin.mkdir()
    executable(fake_bin / "ffmpeg", FAKE_FFMPEG)
    args_log = tmp / "ffmpeg-args.txt"
    rec_out = tmp / "recordings"
    base_env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                    FAKE_ARGS_LOG=str(args_log), CALL_RECORDER_CONF=str(tmp / "no-such.conf"))

    def record(*args, env=None):
        return subprocess.run([str(RECORD)] + list(args), capture_output=True, text=True, timeout=60, env=env or base_env)

    r = record("-l")
    check("-l lists the audio devices, exit 0", r.returncode == 0 and "[2] CallTap" in r.stdout and "Error opening" not in r.stdout,
          r.stdout + r.stderr)
    r = record("-d", "Nope", "-o", str(rec_out))
    check("unknown device: exit 1 with a hint", r.returncode == 1 and "input device 'Nope' not found" in r.stdout, r.stdout + r.stderr)
    r = record("-p", "wiring-test", "-t", "10", "-o", str(rec_out))
    wavs = sorted(rec_out.glob("wiring-test-*.wav"))
    logged = args_log.read_text().splitlines() if args_log.exists() else []
    check("records from the CallTap index with the default channel map and the -t cap",
          r.returncode == 0 and "Saved: " in r.stdout and len(wavs) == 1
          and ":2" in logged and "pan=stereo|c0=c2|c1=0.5*c0+0.5*c1" in logged and logged[logged.index("-t") + 1] == "10",
          (r.stdout, r.stderr, logged))
    r = record("-p", "no-timer", "-o", str(rec_out))
    check("without -t (empty argument array under bash 3.2's set -u) it still records",
          r.returncode == 0 and "Saved: " in r.stdout and "-t" not in args_log.read_text().splitlines(), r.stdout + r.stderr)
    conf = tmp / "rec.conf"
    conf.write_text('INPUT_DEVICE="Other Tap"\nCHANNEL_MAP="pan=stereo|c0=c0|c1=0.5*c1+0.5*c2"\nPERSON="cfg"\n'
                    'OUTPUT_DIR="%s"\n' % (tmp / "from-conf"), encoding="utf-8")
    r = record(env=dict(base_env, CALL_RECORDER_CONF=str(conf)))
    logged = args_log.read_text().splitlines()
    check("the config file sets device, channel map, label and folder",
          r.returncode == 0 and ":3" in logged and "pan=stereo|c0=c0|c1=0.5*c1+0.5*c2" in logged
          and len(list((tmp / "from-conf").glob("cfg-*.wav"))) == 1, (r.stdout, r.stderr, logged))
    r = record(env=dict(base_env, PATH="/usr/bin:/bin"))
    check("no ffmpeg: exit 1 with a hint", r.returncode == 1 and "ffmpeg not found" in r.stdout, r.stdout + r.stderr)

    # ---- transcribe-call.py: real ffmpeg for the split, stand-in whisper-cli
    real_ffmpeg = shutil.which("ffmpeg")
    if real_ffmpeg:
        wav = tmp / "call-20260101-120000.wav"
        subprocess.run([real_ffmpeg, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-filter_complex", "[0][1]amerge=inputs=2",
                        "-t", "2", "-ar", "48000", "-ac", "2", str(wav)], check=True)
        executable(tmp / "whisper-fake", FAKE_WHISPER.format(py=PY))
        model = tmp / "model.bin"
        model.write_bytes(b"stand-in")
        tconf = tmp / "t.conf"
        tconf.write_text('WHISPER_BIN="%s"\nWHISPER_MODEL="%s"\nLANGUAGE="es"\n' % (tmp / "whisper-fake", model), encoding="utf-8")
        wlog = tmp / "whisper-args.txt"
        env = dict(os.environ, CALL_RECORDER_CONF=str(tconf), FAKE_WHISPER_LOG=str(wlog))
        r = subprocess.run([PY, str(TRANSCRIBE), str(wav), "--left", "Host", "--right", "Guest"], capture_output=True, text=True,
                           timeout=120, env=env)
        out = tmp / "call-20260101-120000.transcript.md"
        body = out.read_text(encoding="utf-8") if out.exists() else ""
        check("transcribe: one transcript next to the WAV, labels from the flags",
              r.returncode == 0 and "transcript: %s  (4 segments, 75:12 span)" % out in r.stdout
              and "**Host** [00:00]" in body and "**Guest** [00:04]" in body and "Bye for now." in body, r.stdout + r.stderr + body)
        calls = wlog.read_text(encoding="utf-8").splitlines() if wlog.exists() else []
        check("transcribe: whisper ran once per channel with the config's model and -l es",
              len(calls) == 2 and all(("-m %s" % model) in c and c.endswith("-l es") for c in calls)
              and calls[0].split(" -f ")[1].split()[0].endswith("ch0.wav") and calls[1].split(" -f ")[1].split()[0].endswith("ch1.wav"),
              calls)
    else:
        print("skip transcribe run (ffmpeg not on PATH)")
    r = subprocess.run([PY, str(TRANSCRIBE), str(tmp / "missing.wav")], capture_output=True, text=True, timeout=60)
    check("transcribe: missing WAV is a clear error", r.returncode != 0 and "no such file" in r.stderr, r.stderr)

    claude = shutil.which("claude")
    if claude:
        r = subprocess.run([claude, "plugin", "validate", str(PLUGIN), "--strict"], capture_output=True, text=True)
        check("claude plugin validate --strict", r.returncode == 0, r.stdout + r.stderr)

    shutil.rmtree(str(tmp), ignore_errors=True)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n%d checks, %d failed%s" % (len(RESULTS), len(failed), (": " + ", ".join(failed)) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
