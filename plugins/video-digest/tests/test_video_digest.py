"""Offline checks for video_digest.py beyond the number cross-check (that one is test_compare.py):
manifest and skill, caption parsing and choice, comparison levels, folder naming, and keyframe
timestamps on a synthetic local video (needs ffmpeg; skipped without it). No network.

    python3 plugins/video-digest/tests/test_video_digest.py      (Windows: py ...)

Stdlib only, Python 3.8+. Exit code 0 when every check passed.
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
SCRIPT = PLUGIN / "scripts" / "video_digest.py"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("%s %s%s" % ("ok  " if ok else "FAIL", name, (" -- " + str(detail)) if (detail and not ok) else ""))
    return ok


def load():
    spec = importlib.util.spec_from_file_location("video_digest", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    vd = load()
    tmp = Path(tempfile.mkdtemp(prefix="video-digest-test-"))

    pj = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    check("plugin.json name/version/description/author", pj.get("name") == "video-digest" and pj.get("version") == "0.1.0"
          and pj.get("description") and (pj.get("author") or {}).get("name"))
    skill = (PLUGIN / "skills" / "video-digest" / "SKILL.md").read_text(encoding="utf-8")
    check("skill runs the script through ${CLAUDE_PLUGIN_ROOT}", "${CLAUDE_PLUGIN_ROOT}/scripts/video_digest.py" in skill)

    # captions
    vtt = tmp / "captions.en.vtt"
    vtt.write_text("WEBVTT\nKind: captions\nLanguage: en\n\n"
                   "00:00:00.000 --> 00:00:02.000\nhello there\n\n"
                   "00:00:02.000 --> 00:00:04.000\nhello there\n<c>general</c> kenobi\n\n"
                   "00:00:04,000 --> 00:00:06,000\n[Music]\n", encoding="utf-8")
    segs = vd.parse_vtt(vtt)
    check("parse_vtt drops the rolling-caption repeat and inline tags",
          [s[2] for s in segs] == ["hello there", "general kenobi", "[Music]"] and segs[1][0] == 2.0 and segs[2][0] == 4.0, segs)
    d = tmp / "pick"
    d.mkdir()
    for n in ("captions.en.vtt", "captions.en-orig.vtt", "captions.es.vtt", "captions.es-orig.vtt"):
        (d / n).write_text("WEBVTT\n", encoding="utf-8")
    check("pick_captions prefers the --lang track's original", vd.pick_captions(d, "es")[0].name == "captions.es-orig.vtt")
    check("pick_captions with auto prefers an original-language track", vd.pick_captions(d, "auto")[0].name.endswith("-orig.vtt"))
    check("pick_captions without tracks", vd.pick_captions(tmp / "pick-none", "auto") == (None, None))

    # comparison levels
    w = [(0.0, 5.0, "In 1996, 96 people paid 56.000 dollars"), (15.0, 20.0, "the weather is nice today"),
         (30.0, 35.0, "growth was 7.2 percent")]
    c = [(0.5, 5.0, "in 1996 96 people paid 56,000 dollars"), (15.2, 20.0, "completely different words here"),
         (30.1, 35.0, "growth was 4.2 percent")]
    rows = {r["t"]: r for r in vd.compare(w, c, 15, 0.72)}
    check("compare: same numbers written differently agree", rows[0]["level"] == "OK" and rows[0]["num_mismatch"] == [], rows[0])
    check("compare: different words are flagged", rows[15]["level"] == "FLAG", rows[15])
    check("compare: a different number is critical", rows[30]["level"] == "CRITICAL-NUMBERS" and rows[30]["num_mismatch"] == ["7.2"], rows[30])
    rows = vd.compare(w, [], 15, 0.72)
    check("compare without captions marks every window NO-CAPTIONS", rows and all(r["level"] == "NO-CAPTIONS" for r in rows))

    # folder names
    check("digest_dirname: watch URL", vd.digest_dirname("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3") == "dQw4w9WgXcQ")
    check("digest_dirname: short link", vd.digest_dirname("https://youtu.be/dQw4w9WgXcQ?si=x") == "dQw4w9WgXcQ")
    check("digest_dirname: shorts", vd.digest_dirname("https://www.youtube.com/shorts/abcdefghijk") == "abcdefghijk")
    a, b = vd.digest_dirname("https://vimeo.com/123456"), vd.digest_dirname("https://vimeo.com/654321")
    check("digest_dirname: other sites get distinct hashed names", a.startswith("video-") and b.startswith("video-") and a != b, (a, b))

    # keyframes with real timestamps (ffmpeg only, no network)
    if shutil.which("ffmpeg"):
        vid = tmp / "vid"
        vid.mkdir()
        r = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=320x240:d=2:r=25",
                            "-f", "lavfi", "-i", "smptebars=s=320x240:d=2:r=25", "-f", "lavfi", "-i", "rgbtestsrc=s=320x240:d=2:r=25",
                            "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0", "-pix_fmt", "yuv420p", str(vid / "video.mp4")],
                           capture_output=True, text=True)
        if r.returncode == 0:
            frames = vd.keyframes(vid, 60, 0.3, 150)
            times = vd.frame_times(vid)
            check("keyframes: one frame per scene", len(frames) == 3, [f.name for f in frames])
            check("keyframes: index.tsv carries showinfo timestamps (0, 2, 4 s)",
                  sorted(times.values()) == ["00:00:00", "00:00:02", "00:00:04"], times)
            meta = {"title": "t", "channel": "c", "upload_date": "20260101", "duration": 6, "view_count": 0, "webpage_url": "u"}
            vd.report(vid, meta, vd.compare(w, c, 15, 0.72), frames, "captions.en.vtt")
            rep = (vid / "report.md").read_text(encoding="utf-8")
            check("report lists frames with their times and the critical window first",
                  "(00:00:02)" in rep and rep.index("NUMERIC") < rep.index("Flagged"), rep[:400])
        else:
            check("ffmpeg made the synthetic video", False, r.stderr[-300:])
    else:
        print("skip keyframes (ffmpeg not on PATH)")

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
