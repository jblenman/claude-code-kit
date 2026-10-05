#!/usr/bin/env python3
"""video_digest.py - transcribe an online video locally and cross-check it against the platform's captions.

Pulls a video's audio, its platform captions (auto-generated or uploaded) and, optionally, the
video itself with yt-dlp; makes an independent local transcript with whisper.cpp; aligns the two
transcripts on the time axis and flags disagreements (a NUMERIC disagreement - the two transcripts
give different numbers at the same place - is CRITICAL); and extracts scene-change keyframes,
deduplicated, with their timestamps, so slides and on-screen text can be read. Output: one folder
per video with every artifact and a review-ready report.md.

Requirements: yt-dlp, ffmpeg, whisper-cli (whisper.cpp) and a GGML model file. Pillow is optional
(keyframe deduplication). --compare-only needs none of them (stdlib only).

Usage:
  video_digest.py URL [--outdir DIR] [--model PATH] [--lang auto|en|es|...] [--sub-langs "en-orig,en"]
      [--no-video] [--max-frames 60] [--scene 0.30] [--frame-interval 150] [--window 15]
      [--flag-threshold 0.72] [--player-client default] [--whisper-bin PATH]
  video_digest.py DIGEST_DIR --compare-only        # redo the cross-check and report only
  video_digest.py URL --compare-only               # same, digest folder found from the URL

Defaults from the environment when set: VIDEO_DIGEST_DIR (output root, default ~/video-digests),
WHISPER_MODEL (default ~/Models/whisper/ggml-large-v3-turbo-q5_0.bin), WHISPER_CLI (default:
whisper-cli on PATH).

Design notes: both transcript sources carry timestamps, so alignment buckets text into fixed windows
and compares normalized text per window. Where both agree they can still share an error, so numbers
are re-checked separately, and the Whisper output is kept verbatim as the primary record (captions
are the cross-check, not the truth). Where neither reading is certain, a human ear on the
timestamped clip, or the slide on screen, is the final arbiter; the report keeps that list short.
Numbers: numerals are tokenized at their separators ("16, 2026" is two numbers, 20th -> 20) and
compared place by place after aligning the two token streams over the neighbouring windows.
Different numbers at the same place are CRITICAL; a number only one transcript has (spoken as a word
in the other, a garbled caption word) is listed separately, lower priority; a number at a window
edge that the other transcript has in the neighbouring window is not reported.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

FILLERS = {"eh", "em", "um", "uh", "mm", "ah", "aja", "ajá", "o", "y", "de", "la", "el",
           "que", "en", "a", "los", "las", "un", "una", "es", "no", "si", "sí"}
DEFAULT_MODEL = "~/Models/whisper/ggml-large-v3-turbo-q5_0.bin"
DEFAULT_OUTDIR = "~/video-digests"

try:                                    # Windows consoles default to a legacy code page
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def ytdlp_args(client):
    # YouTube: the "default" web client (with yt-dlp's JS challenge solving) has been the most reliable;
    # some videos expose video formats only to another client (try --player-client android).
    return ["--extractor-args", "youtube:player_client=%s" % client]


def run(cmd, **kw):
    print("  $", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec//3600:02d}:{sec%3600//60:02d}:{sec%60:02d}"


def digest_dirname(url: str) -> str:
    """The folder name for a URL: the YouTube id when there is one, else a short hash of the URL."""
    m = re.search(r"(?:[?&]v=|youtu\.be/|/live/|/shorts/|/embed/)([\w-]{6,})", url)
    if m:
        return m.group(1)
    return "video-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]


# ---------- fetch ----------

def fetch(url, out: Path, sub_langs: str, want_video: bool, client: str = "default"):
    yargs = ytdlp_args(client)
    meta_p = out / "meta.json"
    if not meta_p.exists():
        r = subprocess.run(["yt-dlp", "-J", "--no-playlist", *yargs, url],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
        full = json.loads(r.stdout)
        meta = {k: full.get(k) for k in ("id", "title", "channel", "uploader", "upload_date",
                                         "duration", "view_count", "description", "webpage_url")}
        meta_p.write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    if not (out / "audio.m4a").exists():
        run(["yt-dlp", "--no-playlist", *yargs, "-f", "ba[ext=m4a]/ba", "-o", out / "audio.%(ext)s", url])
        got = list(out.glob("audio.*"))
        if got and got[0].suffix != ".m4a":
            got[0].rename(out / "audio.m4a")
    if not list(out.glob("captions.*")):
        subprocess.run(["yt-dlp", "--no-playlist", *yargs, "--skip-download",
                        "--write-auto-subs", "--write-subs", "--sub-langs", sub_langs,
                        "--convert-subs", "vtt", "--sleep-subtitles", "3",
                        "-o", str(out / "captions"), url], check=False)
    if want_video and not (out / "video.mp4").exists():
        run(["yt-dlp", "--no-playlist", *yargs, "-f", "bv*[height<=720][ext=mp4]/bv*[height<=720]/bv*",
             "-o", out / "video.%(ext)s", url])
        got = [p for p in out.glob("video.*") if p.suffix in (".mp4", ".webm", ".mkv")]
        if got and got[0].suffix != ".mp4":
            got[0].rename(out / "video.mp4")
    return meta


# ---------- transcribe ----------

def load_whisper(out: Path):
    """Segments (start s, end s, text) from the digest's whisper.json."""
    data = json.loads((out / "whisper.json").read_text(encoding="utf-8", errors="replace"))
    segs = []
    for s in data.get("transcription", []):
        t0 = s["offsets"]["from"] / 1000.0
        t1 = s["offsets"]["to"] / 1000.0
        segs.append((t0, t1, s["text"].strip()))
    return segs


def transcribe(out: Path, model: str, lang: str, whisper: str = "whisper-cli"):
    wav = out / "audio16k.wav"
    if not wav.exists():
        run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", out / "audio.m4a",
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav])
    wj = out / "whisper.json"
    if not wj.exists():
        run([whisper, "-m", model, "-l", lang, "-f", wav, "-oj", "-of", out / "whisper", "-pp"])
    segs = load_whisper(out)
    (out / "transcript.txt").write_text(
        "\n".join(f"[{hms(a)} - {hms(b)}] {tx}" for a, b, tx in segs), encoding="utf-8")
    return segs


# ---------- captions ----------

def parse_vtt(path: Path):
    txt = path.read_text(encoding="utf-8", errors="ignore")
    segs = []
    stamp = re.compile(r"(\d+):(\d+):(\d+)[.,](\d+)\s*-->\s*(\d+):(\d+):(\d+)[.,](\d+)")
    cur = None
    for line in txt.splitlines():
        m = stamp.search(line)
        if m:
            g = [int(x) for x in m.groups()]
            cur = [g[0]*3600 + g[1]*60 + g[2] + g[3]/1000.0,
                   g[4]*3600 + g[5]*60 + g[6] + g[7]/1000.0, []]
            segs.append(cur)
        elif cur is not None and line.strip() and "WEBVTT" not in line and not line.startswith(("Kind:", "Language:", "NOTE")):
            clean = re.sub(r"<[^>]+>", "", line).strip()
            if clean:
                cur[2].append(clean)
    # collapse rolling-caption duplication: drop the text already shown as the previous cue's tail
    out, tail = [], ""
    for t0, t1, lines in segs:
        text = " ".join(lines).strip()
        if not text:
            continue
        text2 = text[len(tail):].strip() if tail and text.startswith(tail) else text
        tail = " ".join(lines[-1:]).strip()
        if text2:
            out.append((t0, t1, text2))
    return out


def pick_captions(out: Path, lang: str = "auto"):
    """The caption track to compare with: the spoken language's track when --lang names it, then an
    original-language ('-orig') track, then the shortest name."""
    cands = sorted(out.glob("captions*.vtt"))
    if not cands:
        return None, None

    def score(p):
        parts = p.name.lower().split(".")          # captions.en-orig.vtt -> ['captions', 'en-orig', 'vtt']
        code = parts[1] if len(parts) >= 3 else ""
        return (0 if lang != "auto" and code.split("-")[0] == lang.lower() else 1,
                0 if code.endswith("-orig") else 1, len(p.name))
    cands.sort(key=score)
    return cands[0], parse_vtt(cands[0])


# ---------- numbers ----------
#
# A numeral run is digits joined only by a '.' or ',' sitting directly between digits: "16, 2026" is
# two runs, "1,500" one, "1996 1996" two. (An earlier version let whitespace join runs too, so
# "16, 2026" became 16.2026 and "1996 [y en el] 1996" became 19961996: false numeric-critical
# flags.) Letters glued to the end of a run are dropped: 20th, 1800s, 1º, 4K -> 20, 1800, 1, 4.

_RUN_OR_WORD = re.compile(r"(\d+(?:[.,]\d+)*)[^\W\d_]*|[^\W\d_]+")
_GROUPED = re.compile(r"\d{1,3}([.,])\d{3}(?:\1\d{3})*")
_GROUPED_DEC = re.compile(r"\d{1,3}([.,])\d{3}(?:\1\d{3})*([.,])\d+")
_DECIMAL = re.compile(r"(\d+)[.,](\d+)")
_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)")         # [música], (applause)
EDGE_TOKENS = 8   # "at a window edge" = within this many tokens of the window's start or end
EDGE_SLACK = 2    # extra caption tokens searched beyond an edge stretch's own length
# Single number words (English, Spanish). They only ever clear a one-sided number ("one-fifth"
# against "1/5"); they never raise a flag. Compounds stay one-sided.
NUMBER_WORDS = dict(
    [(w, str(i)) for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen "
        "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())]
    + [(w, str(i)) for i, w in enumerate(
        "cero uno dos tres cuatro cinco seis siete ocho nueve diez once doce trece "
        "catorce quince dieciséis diecisiete dieciocho diecinueve veinte".split())]
    + [(w, str(10 * i)) for i, w in enumerate(
        "thirty forty fifty sixty seventy eighty ninety".split(), start=3)]
    + [(w, str(10 * i)) for i, w in enumerate(
        "treinta cuarenta cincuenta sesenta setenta ochenta noventa".split(), start=3)]
    + [(w, str(i)) for i, w in enumerate(
        "first second third fourth fifth sixth seventh eighth ninth tenth".split(), start=1)]
    + [(w, str(i)) for i, w in enumerate(
        "primero segundo tercero cuarto quinto sexto séptimo octavo noveno décimo".split(), start=1)]
    + [(w, str(i)) for i, w in enumerate(
        "primera segunda tercera cuarta quinta sexta séptima octava novena décima".split(), start=1)]
    + [("primer", "1"), ("tercer", "3"), ("half", "2"), ("quarter", "4"), ("medio", "2"),
       ("twentieth", "20"), ("hundred", "100"), ("thousand", "1000"), ("million", "1000000"),
       ("cien", "100"), ("ciento", "100"), ("mil", "1000"), ("millón", "1000000"),
       ("millon", "1000000"), ("billion", "1000000000")])


def _int_canon(digits):
    return digits.lstrip("0") or "0"


def _dec_canon(ip, fp):
    fp = fp.rstrip("0")
    return _int_canon(ip) + ("." + fp if fp else "")


def parse_numeral(run):
    """One numeral run -> [(canonical value, raw digits)], usually one item.
    56.000 / 56,000 / 1,500,000 -> 56000 / 1500000 (groups of exactly three digits are thousands);
    1.234,5 / 1,234.5 -> 1234.5; 7.2 / 7,2 / 16.2026 -> decimals (one separator, not followed by
    exactly three digits); anything else (16.09.2026, 10.0.0.12) -> one number per part. Leading
    zeros and trailing decimal zeros are dropped."""
    if _GROUPED.fullmatch(run):
        digits = re.sub(r"[.,]", "", run)
        return [(_int_canon(digits), digits)]
    m = _GROUPED_DEC.fullmatch(run)
    if m and m.group(1) != m.group(2):
        cut = run.rfind(m.group(2))
        ip, fp = re.sub(r"[.,]", "", run[:cut]), run[cut + 1:]
        return [(_dec_canon(ip, fp), ip + fp)]
    m = _DECIMAL.fullmatch(run)
    if m:
        return [(_dec_canon(m.group(1), m.group(2)), m.group(1) + m.group(2))]
    return [(_int_canon(p), p) for p in re.split(r"[.,]", run)]


def tokens(text):
    """Comparison tokens of one transcript window, in order: numbers as ('#' + canonical, canonical,
    raw digits, start, end), words lower-cased as (word, None, None, start, end). Fillers are
    dropped; spans index into text."""
    blank = _BRACKETS.sub(lambda m: " " * len(m.group(0)), text)
    out = []
    for m in _RUN_OR_WORD.finditer(blank):
        if m.group(1):
            for canon, raw in parse_numeral(m.group(1)):
                out.append(("#" + canon, canon, raw, m.start(), m.end()))
        else:
            word = m.group(0).lower()
            if word not in FILLERS:
                out.append((word, None, None, m.start(), m.end()))
    return out


def canon_nums(text):
    """The numbers in a text, canonical and in order (see parse_numeral)."""
    return [t[1] for t in tokens(text) if t[1] is not None]


def _ctx(text, toks, a, b, pad=5):
    """The original text around tokens a..b-1, pad tokens either side."""
    lo, hi = max(0, a - pad), min(len(toks), b + pad)
    if lo >= hi:
        return ""
    return " ".join(text[toks[lo][3]:toks[hi - 1][4]].split())


def _same_year(x, y):
    """'96 / año 96 against 1996: two digits against the 19xx/20xx year they end."""
    for short, year in ((x[2], y[1]), (y[2], x[1])):
        if (len(short) == 2 and len(year) == 4 and year.isdigit()
                and year[:2] in ("19", "20") and year[2:] == short):
            return True
    return False


def _space_grouped_run(side, toks, whole):
    """Adjacent number tokens written as '56 000' / '1 500 000' whose digits joined give `whole`
    (every part after the first has three digits)."""
    for a in range(len(side)):
        for b in range(a + 2, len(side) + 1):
            run = side[a:b]
            if run[-1] - run[0] != len(run) - 1:         # adjacent tokens only
                break
            parts = [toks[i][2] for i in run]
            if (len(parts[0]) <= 3 and all(len(p) == 3 for p in parts[1:])
                    and "".join(parts) == whole):
                return run
    return None


def _reconcile(wi, ci, wt, ct):
    """Leftover number tokens (indices into wt / ct) of one unaligned stretch after dropping values
    both sides have and formatting twins of each other."""
    w = [i for i in wi if wt[i][1] is not None]
    c = [j for j in ci if ct[j][1] is not None]
    for same in (lambda x, y: x[1] == y[1], _same_year):
        for i in list(w):
            j = next((j for j in c if same(wt[i], ct[j])), None)
            if j is not None:
                w.remove(i)
                c.remove(j)
    for side, toks, other, otoks in ((w, wt, c, ct), (c, ct, w, wt)):
        for j in list(other):
            if j in other and otoks[j][1].isdigit():
                run = _space_grouped_run(side, toks, otoks[j][2])
                if run:
                    for i in run:
                        side.remove(i)
                    other.remove(j)
    return w, c


def check_numbers(w_text, cap_texts, w_prev="", w_next=""):
    """Number cross-check of one Whisper window against the caption windows k-1, k, k+1 (cap_texts,
    a 3-tuple); w_prev / w_next are Whisper's windows k-1 and k+1. The two token streams are aligned
    (difflib) and every stretch where they differ is compared number by number:
      conflict  - both transcripts have a number there and the values differ (420 vs 240): CRITICAL;
      one-sided - only one transcript has a number there (spoken as a word in the other, a garbled
                  caption word, a dropped phrase).
    Not reported: values both sides have, '96 vs 1996, '56 000' vs 56.000, a number the other
    transcript spells as one word there ("one-fifth" / "1/5"); a one-sided number at a window edge
    that the other transcript has in the neighbouring window; a conflict whose numbers both
    transcripts contain nearby (alignment drift)."""
    cap_prev, _, cap_next = cap_texts
    joined = " ".join(cap_texts)
    wt, ct = tokens(w_text), tokens(joined)
    res = {"whisper": [t[1] for t in wt if t[1] is not None], "captions": [],
           "conflicts": [], "one_sided": []}
    if not ct or (not res["whisper"] and all(t[1] is None for t in ct)):
        return res                      # no caption text nearby, or no numbers at all
    cap_all = {t[1] for t in ct if t[1] is not None}
    cap_prev_v, cap_next_v = set(canon_nums(cap_prev)), set(canon_nums(cap_next))
    w_prev_v, w_next_v = set(canon_nums(w_prev)), set(canon_nums(w_next))
    w_all = w_prev_v | w_next_v | set(res["whisper"])
    ops = difflib.SequenceMatcher(None, [t[0] for t in wt], [t[0] for t in ct],
                                  autojunk=False).get_opcodes()
    equal = [op for op in ops if op[0] == "equal"]
    anchors = [op for op in equal if op[2] - op[1] >= 2] or equal
    if not anchors:                     # nothing lines up: no place-by-place check
        for i, t in enumerate(wt):
            if t[1] is not None and t[1] not in cap_all:
                res["one_sided"].append(dict(source="whisper", n=t[1],
                                             context=_ctx(w_text, wt, i, i + 1), other=""))
        return res
    lo_i, lo_j, hi_i, hi_j = anchors[0][1], anchors[0][3], anchors[-1][2], anchors[-1][4]
    tail = len(wt) - hi_i
    # Whisper tokens before the first / after the last anchor are compared with as many caption
    # tokens (plus slack) right before / after it: the window edges.
    cap_lo = max(0, lo_j - lo_i - EDGE_SLACK) if lo_i else lo_j
    cap_hi = min(len(ct), hi_j + tail + EDGE_SLACK) if tail else hi_j
    res["captions"] = [ct[j][1] for j in range(cap_lo, cap_hi) if ct[j][1] is not None]
    stretches = [(0, lo_i, cap_lo, lo_j)] if lo_i else []
    stretches += [(i1, i2, j1, j2) for tag, i1, i2, j1, j2 in ops
                  if tag != "equal" and lo_i <= i1 and i2 <= hi_i and lo_j <= j1 and j2 <= hi_j]
    if tail:
        stretches.append((hi_i, len(wt), hi_j, cap_hi))
    for i1, i2, j1, j2 in stretches:
        wl, cl = _reconcile(range(i1, i2), range(j1, j2), wt, ct)
        if wl and cl:
            if all(wt[i][1] in cap_all for i in wl) and all(ct[j][1] in w_all for j in cl):
                continue                # both transcripts have both numbers nearby
            res["conflicts"].append(dict(
                whisper=[wt[i][1] for i in wl], captions=[ct[j][1] for j in cl],
                whisper_context=_ctx(w_text, wt, i1, i2),
                captions_context=_ctx(joined, ct, j1, j2)))
            continue
        c_words = {NUMBER_WORDS.get(ct[j][0]) for j in range(j1, j2)}
        w_words = {NUMBER_WORDS.get(wt[i][0]) for i in range(i1, i2)}
        for i in wl:
            v = wt[i][1]
            if (v in c_words or (i < EDGE_TOKENS and v in cap_prev_v)
                    or (i >= len(wt) - EDGE_TOKENS and v in cap_next_v)):
                continue
            res["one_sided"].append(dict(source="whisper", n=v, context=_ctx(w_text, wt, i, i + 1),
                                         other=_ctx(joined, ct, j1, j2)))
        for j in cl:
            v = ct[j][1]
            if (v in w_words or (j < cap_lo + EDGE_TOKENS and v in w_prev_v)
                    or (j >= cap_hi - EDGE_TOKENS and v in w_next_v)):
                continue
            res["one_sided"].append(dict(source="captions", n=v, context=_ctx(joined, ct, j, j + 1),
                                         other=_ctx(w_text, wt, i1, i2)))
    return res


# ---------- align + diff ----------

def norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", s)         # [música], (applause)
    s = re.sub(r"[^\wáéíóúüñ.,% ]", " ", s)
    toks = [t for t in re.split(r"\s+", s) if t and t not in FILLERS]
    return " ".join(toks)


def bucket(segs, window):
    b = {}
    for t0, t1, tx in segs:
        b.setdefault(int(t0 // window), []).append(tx)
    return {k: " ".join(v) for k, v in b.items()}


def compare(whisper_segs, cap_segs, window, thresh):
    wb, cb = bucket(whisper_segs, window), bucket(cap_segs, window)
    rows = []
    for k in sorted(set(wb) | set(cb)):
        w = wb.get(k, "")
        nw = norm(w)
        if not cb:                      # no caption track at all
            rows.append(dict(t=k * window, ratio=None, level="NO-CAPTIONS", whisper=w.strip(), captions="",
                             nums_whisper=canon_nums(w), nums_captions=[], num_mismatch=[],
                             num_conflicts=[], num_one_sided=[]))
            continue
        # caption timing lags or leads Whisper by up to a window: best of k-1..k+1 and their join
        best_c, best_ratio = "", 0.0
        near = (cb.get(k - 1, ""), cb.get(k, ""), cb.get(k + 1, ""))
        joined = " ".join(near)
        for cand in near + (joined,):
            nc = norm(cand)
            if not nc:
                continue
            r = difflib.SequenceMatcher(None, nw, nc).ratio()
            if nw and nw in nc:         # a window split: the Whisper text is contained in the candidate
                r = max(r, 0.99)
            if r > best_ratio:
                best_ratio, best_c = r, cand
        c, ratio = best_c, best_ratio
        nc = norm(c)
        if not nw and not nc:
            continue
        nums = check_numbers(w, near, wb.get(k - 1, ""), wb.get(k + 1, ""))
        level = "OK"
        if nums["conflicts"]:
            level = "CRITICAL-NUMBERS"
        elif ratio < thresh:
            level = "FLAG"
        rows.append(dict(t=k * window, ratio=round(ratio, 2), level=level, whisper=w.strip(), captions=c.strip(),
                         nums_whisper=nums["whisper"], nums_captions=nums["captions"],
                         num_mismatch=sorted({n for cf in nums["conflicts"] for n in cf["whisper"]}),
                         num_conflicts=nums["conflicts"], num_one_sided=nums["one_sided"]))
    return rows


# ---------- keyframes ----------

def keyframes(out: Path, max_frames: int, scene: float, interval: int):
    """Scene-change frames (plus one at least every `interval` s) as frames/f_*.jpg, deduplicated by
    dHash when Pillow is present, thinned to max_frames. Each kept frame's timestamp comes from
    ffmpeg's showinfo filter (pts_time) and is written to frames/index.tsv: never derive times from
    the file names, whose numbers depend on the video's frame rate."""
    vid = out / "video.mp4"
    if not vid.exists():
        return []
    fdir = out / "frames"
    if fdir.exists() and list(fdir.glob("*.jpg")):
        return sorted(fdir.glob("*.jpg"))
    fdir.mkdir(exist_ok=True)
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-i", str(vid), "-vf",
           f"select='gt(scene,{scene})+eq(n,0)+gte(t-prev_selected_t,{interval})',scale=960:-2,showinfo",
           "-vsync", "vfr", "-frame_pts", "1", "-qscale:v", "3", str(fdir / "f_%08d.jpg")]
    print("  $", " ".join(cmd), flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(r.stderr[-1500:], file=sys.stderr)
        raise subprocess.CalledProcessError(r.returncode, cmd)
    frames = sorted(fdir.glob("*.jpg"))
    times = [float(x) for x in re.findall(r"\bpts_time:\s*([-\d.]+)", r.stderr)]
    when = dict(zip(frames, times)) if len(times) == len(frames) else {}
    if not when and frames:
        print("  note: %d showinfo timestamps for %d frames; frames/index.tsv left out" % (len(times), len(frames)),
              file=sys.stderr)
    try:
        from PIL import Image

        def dhash(p, size=9):
            im = Image.open(p).convert("L").resize((size, size - 1))
            px = list(im.getdata())
            bits = [1 if px[r*size + c] > px[r*size + c + 1] else 0 for r in range(size - 1) for c in range(size - 1)]
            return int("".join(map(str, bits)), 2)
        kept, hashes = [], []
        for f in frames:
            h = dhash(f)
            if any(bin(h ^ h2).count("1") < 10 for h2 in hashes):
                f.unlink()
                continue
            hashes.append(h)
            kept.append(f)
        frames = kept
    except ImportError:
        pass
    while len(frames) > max_frames:      # thin evenly
        frames = [f for i, f in enumerate(frames) if i % 2 == 0]
    keep = set(frames)
    for f in fdir.glob("*.jpg"):
        if f not in keep:
            f.unlink()
    frames = sorted(fdir.glob("*.jpg"))
    if when:
        (fdir / "index.tsv").write_text("file\tseconds\ttime\n" + "".join(
            "%s\t%.3f\t%s\n" % (f.name, when[f], hms(when[f])) for f in frames if f in when), encoding="utf-8")
    return frames


def frame_times(out: Path):
    """{file name: hh:mm:ss} from frames/index.tsv, or {} when there is none."""
    p = out / "frames" / "index.tsv"
    if not p.exists():
        return {}
    rows = [ln.split("\t") for ln in p.read_text(encoding="utf-8").splitlines()[1:] if ln.count("\t") == 2]
    return {name: t for name, _s, t in rows}


# ---------- report ----------

def report(out: Path, meta, rows, frames, cap_name):
    times = frame_times(out)
    frame_list = " ".join("%s (%s)" % (f.name, times[f.name]) if f.name in times else f.name for f in frames)
    if rows and all(r["level"] == "NO-CAPTIONS" for r in rows):
        text = (f"# Video digest - {meta.get('title')}\n\nNO CAPTION TRACK AVAILABLE - the Whisper transcript "
                "stands alone (no cross-check). See transcript.txt.\n")
        if frames:
            text += "\n## Keyframes\n\n" + frame_list + "\n"
        (out / "report.md").write_text(text, encoding="utf-8")
        (out / "compare.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
        return 0, 0, 0
    crit = [r for r in rows if r["level"] == "CRITICAL-NUMBERS"]
    flag = [r for r in rows if r["level"] == "FLAG"]
    ok = sum(1 for r in rows if r["level"] == "OK")
    one = [(r, e) for r in rows for e in r.get("num_one_sided", [])]
    L = [f"# Video digest - {meta.get('title')}",
         f"\nChannel: **{meta.get('channel') or meta.get('uploader')}** | uploaded {meta.get('upload_date')} | "
         f"duration {hms(meta.get('duration') or 0)} | views {meta.get('view_count')}\nURL: {meta.get('webpage_url')}\n",
         f"Transcription: local Whisper (primary, verbatim in `transcript.txt`). Cross-check: platform captions "
         f"(`{cap_name}`). Windows compared: {len(rows)} | agree: {ok} | flagged: {len(flag)} | "
         f"numeric-critical: {len(crit)} | numbers in one transcript only: {len(one)}. "
         f"Keyframes: {len(frames)} in `frames/`.\n"]
    if crit:
        L.append("## NUMERIC DISAGREEMENTS (check these against the audio or the slides)\n")
        L.append("The two transcripts give different numbers at the same place.\n")
        for r in crit:
            for cf in r["num_conflicts"]:
                L.append(f"- **{hms(r['t'])}** (sim {r['ratio']}): whisper "
                         f"{', '.join(cf['whisper'])} vs captions {', '.join(cf['captions'])}\n"
                         f"  - whisper: …{cf['whisper_context']}…\n"
                         f"  - captions: …{cf['captions_context']}…")
        L.append("")
    if flag:
        L.append("## Flagged low-agreement windows\n")
        for r in flag:
            L.append(f"- **{hms(r['t'])}** (sim {r['ratio']})\n"
                     f"  - whisper: {r['whisper'][:300]}\n  - captions: {r['captions'][:300]}")
        L.append("")
    if one:
        L.append("## Numbers in one transcript only (lower priority)\n")
        L.append("The other transcript has no number at that place: usually a number spoken as a word "
                 "(\"one-fifth\" / \"1/5\"), a garbled caption word (\"midentth\" for \"mid-20th\") or a "
                 "dropped phrase. Check only where the number matters.\n")
        for r, e in one:
            other = "captions" if e["source"] == "whisper" else "whisper"
            L.append(f"- **{hms(r['t'])}** {e['source']} only: {e['n']} - "
                     f"{e['source']}: …{e['context']}… | {other}: …{e['other']}…")
        L.append("")
    if frames:
        L.append("## Keyframes\n")
        L.append(frame_list)
    (out / "report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    (out / "compare.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    return len(crit), len(flag), ok


def main():
    ap = argparse.ArgumentParser(description="Transcribe an online video locally and cross-check it against its captions.")
    ap.add_argument("url", help="video URL; with --compare-only also an existing digest folder")
    ap.add_argument("--outdir", default=os.environ.get("VIDEO_DIGEST_DIR") or DEFAULT_OUTDIR,
                    help="output root; one folder per video inside (default: $VIDEO_DIGEST_DIR or %s)" % DEFAULT_OUTDIR)
    ap.add_argument("--model", default=os.environ.get("WHISPER_MODEL") or DEFAULT_MODEL,
                    help="GGML model file (default: $WHISPER_MODEL or %s)" % DEFAULT_MODEL)
    ap.add_argument("--whisper-bin", default=os.environ.get("WHISPER_CLI") or "whisper-cli",
                    help="whisper.cpp CLI (default: $WHISPER_CLI or whisper-cli on PATH)")
    ap.add_argument("--compare-only", action="store_true",
                    help="redo only the caption cross-check, report.md and compare.json from the whisper.json "
                         "and captions already in the digest folder (no download, no transcription, no tools)")
    ap.add_argument("--lang", default="auto", help="spoken language for Whisper: auto (default), en, es, ...")
    ap.add_argument("--sub-langs", default=None,
                    help="yt-dlp caption languages (default: <lang>-orig,<lang> with --lang, else en-orig,en)")
    ap.add_argument("--no-video", action="store_true", help="skip the video download and keyframes")
    ap.add_argument("--max-frames", type=int, default=60)
    ap.add_argument("--scene", type=float, default=0.30, help="scene-change threshold for keyframes (0-1)")
    ap.add_argument("--frame-interval", type=int, default=150, help="also take a frame at least every N seconds")
    ap.add_argument("--window", type=int, default=15, help="comparison window in seconds")
    ap.add_argument("--flag-threshold", type=float, default=0.72, help="flag windows whose similarity is below this")
    ap.add_argument("--player-client", default="default", help="yt-dlp YouTube player client (default: default)")
    a = ap.parse_args()

    if a.compare_only and Path(a.url).expanduser().is_dir():
        out = Path(a.url).expanduser().resolve()
    else:
        out = Path(a.outdir).expanduser() / digest_dirname(a.url)

    if a.compare_only:
        if not (out / "whisper.json").exists():
            sys.exit(f"no whisper.json in {out}: run the full digest first")
        meta_p = out / "meta.json"
        meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {"title": out.name}
        print(f"== compare only == {out}", flush=True)
        wsegs = load_whisper(out)
        print(f"   {len(wsegs)} whisper segments", flush=True)
    else:
        whisper = shutil.which(a.whisper_bin) or (a.whisper_bin if Path(a.whisper_bin).is_file() else None)
        missing = [t for t in ("yt-dlp", "ffmpeg") if not shutil.which(t)] + ([] if whisper else [a.whisper_bin])
        if missing:
            sys.exit("missing tool(s): %s - see the README's Setup section" % ", ".join(missing))
        model = str(Path(a.model).expanduser())
        if not Path(model).is_file():
            sys.exit("whisper model not found: %s - download one (README, Setup) or pass --model / set WHISPER_MODEL" % model)
        sub_langs = a.sub_langs or ("%s-orig,%s" % (a.lang, a.lang) if a.lang != "auto" else "en-orig,en")
        out.mkdir(parents=True, exist_ok=True)
        print("== fetch ==", flush=True)
        meta = fetch(a.url, out, sub_langs, not a.no_video, a.player_client)
        print(f"   {meta.get('title')!r} | {hms(meta.get('duration') or 0)}", flush=True)
        print("== transcribe (local Whisper) ==", flush=True)
        wsegs = transcribe(out, model, a.lang, whisper)
        print(f"   {len(wsegs)} whisper segments", flush=True)
    cap_path, csegs = pick_captions(out, a.lang)
    print(f"== captions == {cap_path.name if cap_path else 'NONE'} ({len(csegs) if csegs else 0} cues)", flush=True)
    rows = compare(wsegs, csegs or [], a.window, a.flag_threshold)
    if a.compare_only:
        frames = sorted((out / "frames").glob("*.jpg"))
    else:
        print("== keyframes ==", flush=True)
        frames = keyframes(out, a.max_frames, a.scene, a.frame_interval)
        print(f"   {len(frames)} kept", flush=True)
    crit, flag, ok = report(out, meta, rows, frames, cap_path.name if cap_path else "none")
    one = sum(len(r.get("num_one_sided", [])) for r in rows)
    print(f"== done == report: {out / 'report.md'}  (agree {ok}, flagged {flag}, numeric-critical {crit}, "
          f"numbers in one transcript only {one})")


if __name__ == "__main__":
    main()
