"""turns.py - one session turn by turn since a date: time, CLI version, the prompt, every record write
(category and target) and the end of the last assistant text. For reading what a specific session did.

Usage: python3 turns.py TRANSCRIPT.jsonl SINCE [record options, see proactivity.py]
       SINCE is compared as text with the UTC timestamps: 2026-10-04 or 2026-10-04T21 both work.

Stdlib only, Python 3.8+.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import record_audit as ra  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("transcript")
    ap.add_argument("since")
    ra.add_args(ap)
    a = ap.parse_args()
    cfg = ra.config_from(a)
    cur, rows, bodies = None, [], {}
    with open(os.path.expanduser(a.transcript), encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if not isinstance(e, dict):
                continue
            ty = e.get("type")
            if ty == "user" and not e.get("isMeta") and not e.get("isCompactSummary"):
                msg = e.get("message", {}) or {}
                if ra.has_tool_result(msg):
                    continue
                txt = ra.strip_reminders(ra.user_text(msg)).strip()
                if not txt or txt.startswith("<local-command"):
                    continue
                cur = dict(ts=e.get("timestamp", ""), ver=e.get("version"), prompt=txt[:120].replace("\n", " "), writes=[], last="")
                rows.append(cur)
            elif ty == "assistant" and cur is not None:
                for b in (e.get("message", {}) or {}).get("content", []) or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        cats = ra.classify_write(b.get("name"), inp, cfg) | ra.script_writes(b.get("name"), inp, bodies, cfg)
                        if cats:
                            tgt = inp.get("file_path") or inp.get("notebook_path") or (inp.get("command") or "")[:90].replace("\n", " ")
                            cur["writes"].append("%s=%s" % ("+".join(sorted(cats)), ra.tilde(tgt)[-70:]))
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        cur["last"] = b["text"].strip()[-160:].replace("\n", " ")
    for r in rows:
        if r["ts"] < a.since:
            continue
        print("%s v%s | %s" % (r["ts"][:16], r["ver"], r["prompt"][:110]))
        for w in r["writes"]:
            print("      WRITE %s" % w)
        print("      END: %s" % r["last"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
