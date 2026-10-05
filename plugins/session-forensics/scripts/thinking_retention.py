"""thinking_retention.py - does earlier thinking stay in a session's context?

For consecutive assistant calls inside one user turn, the context should grow by the tool results in
between plus the visible text and tool arguments of the earlier call, plus that call's thinking if it
is kept. thinking(n) = output_tokens(n) - estimated visible tokens(n). The ratio of the unexplained
growth to the thinking produced ~ 1 means thinking is retained within a turn. The second line looks at
user-turn boundaries: a drop there means earlier thinking was discarded. Text tokens are estimated at
3.7 characters per token, so "unexplained" is an upper bound.

Usage: python3 thinking_retention.py [--root DIR] [IDPREFIX ...]     (default root: ~/.claude/projects)

Stdlib only, Python 3.8+.
"""
import argparse
import base64
import glob
import json
import os
import struct
import sys


def est(chars):
    return chars / 3.7


def img_tok(b64):
    try:
        raw = base64.b64decode(b64[:4000] + "=" * (-len(b64[:4000]) % 4), validate=False)
    except Exception:
        return 1200
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", raw[16:24])
        return int(w * h / 750)
    if raw[:2] == b"\xff\xd8":
        i = 2
        while i < len(raw) - 9:
            if raw[i] != 0xFF:
                i += 1
                continue
            m = raw[i + 1]
            if m in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                return int(w * h / 750)
            if m == 0xD8 or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            seg_len = struct.unpack(">H", raw[i + 2:i + 4])[0]
            i += 2 + seg_len
    return 1200


def events_of(path):
    """('asst', ctx, out, visible) / ('result', tokens) / ('human', tokens) / ('compact',) in order; and the model."""
    events, model = [], "?"
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            t, m = d.get("type"), d.get("message") or {}
            if d.get("isCompactSummary") or t == "summary":
                events.append(("compact",))
                continue
            if t == "assistant":
                u = m.get("usage") or {}
                if not u:
                    continue
                model = m.get("model") or model
                ctx = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                ta = 0
                for c in m.get("content") or []:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        ta += est(len(c.get("text") or ""))
                    elif c.get("type") == "tool_use":
                        ta += est(len(json.dumps(c.get("input") or {})))
                events.append(("asst", ctx, u.get("output_tokens", 0), ta))
            elif t == "user":
                content, tok, is_result = m.get("content"), 0, False
                if isinstance(content, str):
                    events.append(("human", est(len(content))))
                    continue
                for c in content or []:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_result":
                        is_result = True
                        cc = c.get("content")
                        if isinstance(cc, str):
                            tok += est(len(cc))
                        else:
                            for p in cc or []:
                                if isinstance(p, dict):
                                    if p.get("type") == "text":
                                        tok += est(len(p.get("text") or ""))
                                    elif p.get("type") == "image":
                                        tok += img_tok((p.get("source") or {}).get("data") or "")
                    elif c.get("type") == "text":
                        tok += est(len(c.get("text") or ""))
                    elif c.get("type") == "image":
                        tok += img_tok((c.get("source") or {}).get("data") or "")
                events.append(("result", tok) if is_result else ("human", tok))
    return events, model


def main():
    ap = argparse.ArgumentParser(description="Does earlier thinking stay in a session's context?")
    ap.add_argument("prefixes", nargs="*", help="session-id prefixes to keep (default: all sessions)")
    ap.add_argument("--root", default=os.path.join(os.path.expanduser("~"), ".claude", "projects"))
    a = ap.parse_args()
    root = os.path.expanduser(a.root)
    if not os.path.isdir(root):
        print("not a directory: %s" % root, file=sys.stderr)
        return 2
    for f in glob.glob(os.path.join(root, "*", "*.jsonl")):
        sid = os.path.basename(f)[:8]
        if a.prefixes and not any(sid.startswith(w) for w in a.prefixes):
            continue
        events, model = events_of(f)
        within = {"n": 0, "delta": 0, "results": 0, "textargs": 0, "thinking": 0}
        boundary = {"n": 0, "delta": 0, "neg": 0, "human": 0, "thinking_prev": 0}
        prev, between, saw_human, saw_compact, human_tok = None, 0, False, False, 0
        for e in events:
            if e[0] == "compact":
                saw_compact, prev = True, None
                continue
            if e[0] == "result":
                between += e[1]
                continue
            if e[0] == "human":
                saw_human = True
                human_tok += e[1]
                continue
            ctx, out, ta = e[1], e[2], e[3]
            if prev is not None and not saw_compact:
                pctx, pout, pta = prev
                delta, pthink = ctx - pctx, max(0, pout - pta)
                if saw_human:
                    boundary["n"] += 1
                    boundary["delta"] += delta
                    boundary["human"] += human_tok + between
                    boundary["thinking_prev"] += pthink
                    if delta < 0:
                        boundary["neg"] += 1
                else:
                    within["n"] += 1
                    within["delta"] += delta
                    within["results"] += between
                    within["textargs"] += pta
                    within["thinking"] += pthink
            prev, between, saw_human, saw_compact, human_tok = (ctx, out, ta), 0, False, False, 0
        if within["n"] == 0:
            continue
        implied = within["delta"] - within["results"] - within["textargs"]
        ratio = implied / within["thinking"] if within["thinking"] else float("nan")
        b_implied = boundary["delta"] - boundary["human"]
        print(f"{sid} {model.replace('claude-', '')}: within-turn steps {within['n']}: ctx grew {within['delta']:,}; explained by results "
              f"{within['results']:,.0f} + text/args {within['textargs']:,.0f}; unexplained {implied:,.0f} vs thinking produced "
              f"{within['thinking']:,.0f} => retained ratio {ratio:.2f}")
        print(f"         user-turn boundaries {boundary['n']}: ctx change {boundary['delta']:,} (negative in {boundary['neg']}); "
              f"human+results {boundary['human']:,.0f}; unexplained {b_implied:,.0f} vs prev-turn thinking {boundary['thinking_prev']:,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
