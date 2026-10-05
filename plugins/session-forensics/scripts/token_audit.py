"""token_audit.py - where did a Claude Code session's context go?

Reads transcripts (~/.claude/projects/*/*.jsonl). The context size of each API call is exact, from its
usage record (input + cache read + cache creation tokens). Text sizes are characters; estimated tokens
= characters / 3.7 (JSON and code tokenize heavier). Image tokens are estimated as width x height / 750,
with the size read from the image header.

Usage:
  python3 token_audit.py --list [--min-turns N]        one row per session (default: at least 20 assistant calls)
  python3 token_audit.py --session IDPREFIX            one session broken down by tool
  python3 token_audit.py --grep WORD --list            only sessions whose raw transcript contains WORD
  python3 token_audit.py --root DIR ...                transcripts copied from elsewhere

--list columns: id, first, last (UTC dates), model (most used), turns (assistant API calls), maxctx(k)
(peak context), out(k) (output tokens), imgs, imgtok(k), tooltxt(k) (tool-result text), think(k)
(thinking text stored in the transcript - current models store little, so this is not the thinking
cost; see thinking_retention.py), cmp (compactions), title or working directory.

Stdlib only, Python 3.8+.
"""
import argparse
import base64
import collections
import glob
import json
import os
import struct
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CHARS_PER_TOKEN = 3.7
IMAGE_FALLBACK_TOKENS = 1200
USER_IMAGE_ROW = "(image in user msg: PDF pages via Read, or pasted)"


def img_dims(b64):
    """(width, height) from the first bytes of a base64 PNG or JPEG, or None."""
    try:
        raw = base64.b64decode(b64[:4000] + "=" * (-len(b64[:4000]) % 4), validate=False)
    except Exception:
        return None
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", raw[16:24])
        return w, h
    if raw[:2] == b"\xff\xd8":
        i = 2
        while i < len(raw) - 9:
            if raw[i] != 0xFF:
                i += 1
                continue
            m = raw[i + 1]
            if m in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                return w, h
            if m == 0xD8 or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            seg_len = struct.unpack(">H", raw[i + 2:i + 4])[0]
            i += 2 + seg_len
    return None


def img_tokens(source):
    dims = img_dims((source or {}).get("data") or "")
    return int(dims[0] * dims[1] / 750) if dims else IMAGE_FALLBACK_TOKENS


def new_tool():
    return {"calls": 0, "result_chars": 0, "images": 0, "img_tokens": 0, "input_chars": 0}


def audit(path):
    s = {"id": os.path.basename(path)[:8], "file": path, "title": None, "cwd": None, "models": collections.Counter(),
         "first": None, "last": None, "turns": 0, "out_tokens": 0, "max_ctx": 0, "ctx_series": [],
         "tools": collections.defaultdict(new_tool), "asst_text": 0, "asst_think": 0, "user_chars": 0,
         "user_msgs": 0, "compactions": 0, "tool_ids": {}}
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            if d.get("customTitle"):
                s["title"] = d["customTitle"]
            if d.get("cwd") and not s["cwd"]:
                s["cwd"] = d["cwd"]
            t, ts = d.get("type"), d.get("timestamp")
            if ts:
                s["first"] = min(s["first"], ts) if s["first"] else ts
                s["last"] = max(s["last"], ts) if s["last"] else ts
            if d.get("isCompactSummary") or t == "summary":
                s["compactions"] += 1
            m = d.get("message") or {}
            if t == "assistant":
                u = m.get("usage") or {}
                if u:
                    s["turns"] += 1
                    s["out_tokens"] += u.get("output_tokens", 0)
                    ctx = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                    s["max_ctx"] = max(s["max_ctx"], ctx)
                    s["ctx_series"].append(ctx)
                if m.get("model"):
                    s["models"][m["model"]] += 1
                for c in m.get("content") or []:
                    if not isinstance(c, dict):
                        continue
                    ty = c.get("type")
                    if ty == "text":
                        s["asst_text"] += len(c.get("text") or "")
                    elif ty == "thinking":
                        s["asst_think"] += len(c.get("thinking") or "")
                    elif ty == "tool_use":
                        name = c.get("name") or "?"
                        s["tools"][name]["calls"] += 1
                        s["tool_ids"][c.get("id")] = name
                        s["tools"][name]["input_chars"] += len(json.dumps(c.get("input") or {}))
            elif t == "user":
                content = m.get("content")
                if isinstance(content, str):
                    s["user_chars"] += len(content)
                    s["user_msgs"] += 1
                    continue
                for c in content or []:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_result":
                        rec = s["tools"][s["tool_ids"].get(c.get("tool_use_id"), "?")]
                        cc = c.get("content")
                        if isinstance(cc, str):
                            rec["result_chars"] += len(cc)
                        else:
                            for p in cc or []:
                                if not isinstance(p, dict):
                                    continue
                                if p.get("type") == "text":
                                    rec["result_chars"] += len(p.get("text") or "")
                                elif p.get("type") == "image":
                                    rec["images"] += 1
                                    rec["img_tokens"] += img_tokens(p.get("source"))
                    elif c.get("type") == "text":
                        s["user_chars"] += len(c.get("text") or "")
                        s["user_msgs"] += 1
                    elif c.get("type") == "image":
                        rec = s["tools"][USER_IMAGE_ROW]
                        rec["images"] += 1
                        rec["img_tokens"] += img_tokens(c.get("source"))
    return s


def print_list(sessions):
    print(f"{'id':<9}{'first':<11}{'last':<11}{'model':<17}{'turns':>6}{'maxctx(k)':>10}{'out(k)':>8}{'imgs':>6}"
          f"{'imgtok(k)':>10}{'tooltxt(k)':>11}{'think(k)':>9}{'cmp':>4}  title/cwd")
    for s in sessions:
        model = (s["models"].most_common(1)[0][0] if s["models"] else "?").replace("claude-", "")
        imgs = sum(v["images"] for v in s["tools"].values())
        imgtok = sum(v["img_tokens"] for v in s["tools"].values())
        tooltxt = sum(v["result_chars"] for v in s["tools"].values())
        print(f"{s['id']:<9}{(s['first'] or '')[:10]:<11}{(s['last'] or '')[:10]:<11}{model:<17}{s['turns']:>6}"
              f"{s['max_ctx'] / 1000:>10.0f}{s['out_tokens'] / 1000:>8.0f}{imgs:>6}{imgtok / 1000:>10.0f}"
              f"{tooltxt / 3700:>11.0f}{s['asst_think'] / 3700:>9.0f}{s['compactions']:>4}  "
              f"{(s['title'] or os.path.basename(s['cwd'] or ''))[:40]}")


def print_session(s):
    print(f"\n=== session {s['id']} {s['title'] or ''} {(s['first'] or '')[:16]} .. {(s['last'] or '')[:16]} models={dict(s['models'])}")
    print(f"assistant turns {s['turns']} | output tokens {s['out_tokens']:,} | max context {s['max_ctx']:,} | compactions {s['compactions']}")
    print(f"assistant text {s['asst_text'] / 3700:,.0f}k est tok | thinking {s['asst_think'] / 3700:,.0f}k est tok | "
          f"user typed {s['user_chars'] / 3700:,.0f}k est tok in {s['user_msgs']} msgs")
    rows = sorted(s["tools"].items(), key=lambda kv: -(kv[1]["result_chars"] / CHARS_PER_TOKEN + kv[1]["img_tokens"]
                                                       + kv[1]["input_chars"] / CHARS_PER_TOKEN))
    print(f"{'tool':<40}{'calls':>6}{'args est tok':>13}{'result est tok':>15}{'images':>7}{'img est tok':>12}")
    tot = targs = 0
    for name, v in rows:
        est = v["result_chars"] / CHARS_PER_TOKEN
        tot += est + v["img_tokens"]
        targs += v["input_chars"] / CHARS_PER_TOKEN
        print(f"{name[:39]:<40}{v['calls']:>6}{v['input_chars'] / CHARS_PER_TOKEN:>13,.0f}{est:>15,.0f}{v['images']:>7}{v['img_tokens']:>12,}")
    print(f"{'TOTAL':<40}{'':>6}{targs:>13,.0f}{tot:>15,.0f}")
    cs = s["ctx_series"]
    if cs:
        q = [cs[int(len(cs) * p)] for p in (0.25, 0.5, 0.75)]
        print(f"context at 25/50/75% of turns: {q[0]:,} / {q[1]:,} / {q[2]:,}; final {cs[-1]:,}")


def main():
    ap = argparse.ArgumentParser(description="Where did a Claude Code session's context go?")
    ap.add_argument("--root", default=os.path.join(os.path.expanduser("~"), ".claude", "projects"))
    ap.add_argument("--list", action="store_true", help="one row per session")
    ap.add_argument("--grep", help="only sessions whose raw transcript contains WORD (case-insensitive)")
    ap.add_argument("--session", help="break down the session(s) whose id starts with this")
    ap.add_argument("--min-turns", type=int, default=20, help="leave out sessions with fewer assistant calls (default 20)")
    a = ap.parse_args()
    sessions = []
    for f in glob.glob(os.path.join(os.path.expanduser(a.root), "*", "*.jsonl")):
        try:
            s = audit(f)
        except Exception:
            continue
        if s["turns"] >= a.min_turns:
            sessions.append(s)
    sessions.sort(key=lambda s: s["first"] or "")
    if a.grep:
        hits = []
        for s in sessions:
            try:
                with open(s["file"], encoding="utf-8", errors="ignore") as fh:
                    if a.grep.lower() in fh.read().lower():
                        hits.append(s)
            except Exception:
                pass
        sessions = hits
    if a.list or a.grep:
        print_list(sessions)
    if a.session:
        for s in sessions:
            if s["id"].startswith(a.session):
                print_session(s)
    if not (a.list or a.grep or a.session):
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
