#!/usr/bin/env python3
"""report.py: Claude Code tokens and cost per machine and model, from your collector's Prometheus.

Answers "how many tokens and how much money, per machine and per model, today and this week"
from the counters Claude Code exports (README.md): claude_code_token_usage (label type: input,
output, cacheRead, cacheCreation), claude_code_cost_usage (USD, Claude Code's own estimate) and
claude_code_session_count, all labelled machine / model / session_id.

  python3 report.py                  today (since 00:00 local time) and the last 7 days
  python3 report.py --since 1d       one window: a duration (30m, 12h, 1d, 7d, 2w) or
                                     today / week (since 00:00 / Monday 00:00 local time);
                                     repeat it or use commas for several
  python3 report.py --json           the same as one JSON object
  python3 report.py --url URL        the Prometheus to ask (default: $PROMETHEUS_URL, else
                                     http://$COLLECTOR_HOST:9090, else http://localhost:9090)
  python3 report.py --machines a,b   machine labels to list as "no data" when they are absent
  python3 report.py --self-test      offline tests against an in-process fake Prometheus

Exit codes: 0 ok, 1 Prometheus unreachable, 2 usage error, 3 Prometheus refused a query,
4 request-guard refused the request (a refusal stands).

How a total is computed. Each series belongs to one session (label session_id) and counts up
from 0 at the session's start; the collector re-exports it every 60 s and keeps it for 12 h
after the session's last export. A session's usage inside a window is its highest value in the
window minus its value at the window's start (0 for a session that started inside the window):
    ((max_over_time(m[W]) - (m offset W)) >= 0) or max_over_time(m[W])
summed per machine and model. increase() is not used: it measures growth between scrapes, so
whatever a session's first export carries (all of it, for a short headless run) is never
counted; in a live check it returned 0 for every machine while the sessions had real cost.
A series whose counter restarts inside the window (a resumed session in a new process keeps
its session_id, its counters start again at 0) is recomputed from its raw samples.

Pacing: when this kit's request-guard plugin is next to this folder, every request goes through
its wait_turn() (a private or LAN address passes at once; a public --url is paced and counted).
Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path


def default_url():
    if os.environ.get("PROMETHEUS_URL"):
        return os.environ["PROMETHEUS_URL"]
    host = os.environ.get("COLLECTOR_HOST", "").strip()
    return "http://%s:9090" % host if host else "http://localhost:9090"


TOKEN, COST, SESSIONS = "claude_code_token_usage", "claude_code_cost_usage", "claude_code_session_count"
TYPES = ("input", "output", "cacheRead", "cacheCreation")
LOOKBACK = 300      # Prometheus' instant-vector lookback (5 min): "the value at the window's start"
EXPORT_NOTE = ("A machine exports only from sessions started after the telemetry env block reached "
               "its settings.json (README, Configure each machine): each machine starts exporting "
               "when its sessions restart.")
COST_NOTE = "Cost is Claude Code's estimate (claude_code.cost.usage), not a bill."


class Unreachable(Exception):
    pass


class QueryError(Exception):
    pass


class GuardRefusal(Exception):
    pass


# ---------- Prometheus ----------

def _request_guard():
    """The request-guard module when this kit's plugin is next to this folder (it paces a public
    --url; private addresses pass at once). None when absent."""
    here = Path(__file__).resolve().parent
    try:
        sys.path.insert(0, str(here.parent / "plugins" / "request-guard" / "scripts"))
        import request_guard  # noqa: F401
        return request_guard
    except Exception:
        return None


def prom(base, query, at, timeout=10.0, guard=None):
    """One instant query (/api/v1/query) evaluated at `at` (Unix seconds) -> data."""
    url = base.rstrip("/") + "/api/v1/query"
    if guard is not None:
        try:
            guard.wait_turn(url)
        except guard.Refusal as e:
            raise GuardRefusal(str(e))
    body = urllib.parse.urlencode({"query": query, "time": "%.3f" % at}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ans = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if guard is not None:
            try:
                guard.report(url, e.code)
            except Exception:
                pass
        try:
            ans = json.loads(e.read().decode("utf-8"))
        except Exception:
            raise QueryError("HTTP %s from %s" % (e.code, url))
        raise QueryError(ans.get("error") or "HTTP %s" % e.code)
    except (urllib.error.URLError, OSError) as e:
        raise Unreachable(str(getattr(e, "reason", e)))
    except ValueError:
        raise Unreachable("the answer is not Prometheus JSON (is %s a Prometheus?)" % base)
    if ans.get("status") != "success":
        raise QueryError(ans.get("error") or "status %r" % ans.get("status"))
    return ans["data"]


def _labels(metric):
    return {k: v for k, v in metric.items() if k != "__name__"}


def _esc(v):
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def growth_query(metric, secs):
    w = "%ds" % secs
    return ("((max_over_time({m}[{w}]) - ({m} offset {w})) >= 0) or max_over_time({m}[{w}])"
            .format(m=metric, w=w))


def exact_growth(samples, start_ts):
    """Usage inside the window from one series' raw samples [(ts, value)]; the samples at or
    before start_ts give the value at the window's start."""
    total, prev = 0.0, None
    for ts, v in samples:
        if ts <= start_ts:
            prev = v
            continue
        if prev is None or v < prev:    # started inside the window, or the counter restarted
            total += v
        else:
            total += v - prev
        prev = v
    return total


def collect(base, secs, at, timeout=10.0, guard=None):
    """{metric: [(labels, usage in the window)]} and the number of series recomputed from raw
    samples because their counter restarted inside the window."""
    out, restarted = {}, 0
    for metric in (TOKEN, COST, SESSIONS):
        series = {}
        for r in prom(base, growth_query(metric, secs), at, timeout, guard)["result"]:
            lab = _labels(r["metric"])
            series[tuple(sorted(lab.items()))] = (lab, float(r["value"][1]))
        if metric != SESSIONS:          # a session-count series is 1 per process
            q = "resets(%s[%ds]) > 0" % (metric, secs)
            for r in prom(base, q, at, timeout, guard)["result"]:
                lab = _labels(r["metric"])
                sel = "%s{%s}" % (metric, ",".join('%s="%s"' % (k, _esc(v)) for k, v in sorted(lab.items())))
                raw = prom(base, "%s[%ds]" % (sel, secs + LOOKBACK), at, timeout, guard)["result"]
                for s in raw:
                    if _labels(s["metric"]) == lab:   # the matchers also match supersets
                        pts = [(float(t), float(v)) for t, v in s["values"]]
                        series[tuple(sorted(lab.items()))] = (lab, exact_growth(pts, at - secs))
                        restarted += 1
        out[metric] = list(series.values())
    return out, restarted


# ---------- windows ----------

_DUR = re.compile(r"(\d+)([mhdw])")
_UNIT = {"m": (60, "minute"), "h": (3600, "hour"), "d": (86400, "day"), "w": (604800, "week")}


def window(spec, now):
    """'today' / 'week' / a duration like 7d -> (name, label, start datetime, seconds)."""
    s = spec.strip().lower()
    tz = now.strftime("%Z") or now.strftime("%z")
    if s in ("today", "week"):
        start = datetime(now.year, now.month, now.day)
        if s == "week":
            start -= timedelta(days=now.weekday())
        start = start.astimezone()
        label = ("today, since 00:00 %s" % tz if s == "today"
                 else "this week, since Monday %s 00:00 %s" % (start.strftime("%Y-%m-%d"), tz))
    else:
        m = _DUR.fullmatch(s)
        if not m or int(m.group(1)) == 0:
            raise ValueError("--since takes today, week or a duration such as 30m, 12h, 1d, 7d, 2w "
                             "(got %r)" % spec)
        n, (unit_s, unit) = int(m.group(1)), _UNIT[m.group(2)]
        start = now - timedelta(seconds=n * unit_s)
        label = "last %d %s%s" % (n, unit, "" if n == 1 else "s")
    secs = max(60, int(round((now - start).total_seconds())))
    return s, label, start, secs


# ---------- aggregation ----------

def _blank():
    return {"tokens": dict.fromkeys(TYPES + ("other",), 0.0), "cost_usd": 0.0, "sessions": set()}


def _add(dst, src):
    for k, v in src["tokens"].items():
        dst["tokens"][k] = dst["tokens"].get(k, 0.0) + v
    dst["cost_usd"] += src["cost_usd"]
    dst["sessions"] |= src["sessions"]


def aggregate(data):
    """Per (machine, model) rows, per-machine and per-model subtotals, the total, and session
    starts per machine (claude_code_session_count, agents_view excluded)."""
    rows = {}
    for metric in (TOKEN, COST):
        for lab, v in data.get(metric, []):
            if v <= 0:
                continue                # a flat tail of a session from before the window
            r = rows.setdefault((lab.get("machine", "?"), lab.get("model", "?")), _blank())
            if metric == TOKEN:
                t = lab.get("type", "other")
                r["tokens"][t if t in TYPES else "other"] += v
            else:
                r["cost_usd"] += v
            if lab.get("session_id"):
                r["sessions"].add(lab["session_id"])
    starts = {}
    for lab, v in data.get(SESSIONS, []):
        if v > 0 and lab.get("start_type") != "agents_view":
            m = lab.get("machine", "?")
            starts[m] = starts.get(m, 0.0) + v
    by_machine, by_model, total = {}, {}, _blank()
    for (machine, model), r in rows.items():
        _add(by_machine.setdefault(machine, _blank()), r)
        _add(by_model.setdefault(model, _blank()), r)
        _add(total, r)
    return rows, by_machine, by_model, total, starts


def _plain(r, **extra):
    tok = {k: int(round(v)) for k, v in r["tokens"].items() if k != "other" or v}
    tok["total"] = int(round(sum(r["tokens"].values())))
    out = dict(extra)
    out.update(sessions=len(r["sessions"]), tokens=tok, cost_usd=round(r["cost_usd"], 6))
    return out


def window_result(name, label, start, end, secs, data, restarted, machines):
    rows, by_machine, by_model, total, starts = aggregate(data)
    seen = sorted(by_machine)
    return {
        "since": name, "label": label, "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"), "seconds": secs,
        "rows": [_plain(rows[k], machine=k[0], model=k[1])
                 for k in sorted(rows, key=lambda k: (k[0], -rows[k]["cost_usd"], k[1]))],
        "by_machine": [_plain(by_machine[m], machine=m, session_starts=int(round(starts.get(m, 0))))
                       for m in seen],
        "by_model": [_plain(by_model[m], model=m)
                     for m in sorted(by_model, key=lambda m: -by_model[m]["cost_usd"])],
        "total": _plain(total, session_starts=int(round(sum(starts.values())))),
        "machines_without_data": [m for m in machines if m not in seen],
        "restarted_series": restarted,
    }


# ---------- text output ----------

def _num(v):
    return "{:,}".format(v)


def _usd(v):
    return "{:,.2f}".format(v) if v >= 1 else "{:.4f}".format(v)


def render(report):
    head = ("machine", "model", "sessions", "input", "output", "cache read", "cache write",
            "tokens", "cost USD")

    def line(machine, model, r):
        t = r["tokens"]
        return (machine, model, _num(r["sessions"]), _num(t.get("input", 0)), _num(t.get("output", 0)),
                _num(t.get("cacheRead", 0)), _num(t.get("cacheCreation", 0)), _num(t["total"]),
                _usd(r["cost_usd"]))

    out = ["Claude Code usage from %s at %s" % (report["prometheus"], report["evaluated_at_local"])]
    for w in report["windows"]:
        out += ["", "%s (%s)" % (w["label"][0].upper() + w["label"][1:], _span(w["seconds"]))]
        if not w["rows"]:
            out.append("  no usage recorded in this window")
        else:
            body = [line(r["machine"], r["model"], r) for r in w["rows"]]
            body.append(None)
            body += [line(r["machine"], "(all models)", r) for r in w["by_machine"]]
            body.append(None)
            body += [line("(all)", r["model"], r) for r in w["by_model"]]
            body.append(None)
            body.append(line("total", "", w["total"]))
            widths = [max(len(row[i]) for row in [head] + [b for b in body if b]) for i in range(len(head))]

            def fmt(row):
                return "  " + "  ".join(c.ljust(widths[i]) if i < 2 else c.rjust(widths[i])
                                        for i, c in enumerate(row)).rstrip()
            out.append(fmt(head))
            for b in body:
                out.append("  " + "-" * (sum(widths) + 2 * (len(widths) - 1)) if b is None else fmt(b))
            starts = ", ".join("%s %d" % (r["machine"], r["session_starts"]) for r in w["by_machine"])
            out.append("  session starts (claude_code_session_count, without agents_view): %s" % starts)
        if w["machines_without_data"]:
            out.append("  no data from: %s" % ", ".join(w["machines_without_data"]))
        if w["restarted_series"]:
            out.append("  %d series restarted inside the window (resumed sessions): recomputed from "
                       "raw samples" % w["restarted_series"])
    out += [""] + ["Note: " + n for n in report["notes"]]
    return "\n".join(out)


def _span(secs):
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = (["%d d" % d] if d else []) + (["%d h" % h] if h else []) + (["%d min" % m] if m or not (d or h) else [])
    return " ".join(parts)


# ---------- main ----------

def build(base, specs, now, timeout=10.0, guard=None, machines=()):
    at = now.timestamp()
    windows = []
    for spec in specs:
        name, label, start, secs = window(spec, now)
        data, restarted = collect(base, secs, at, timeout, guard)
        windows.append(window_result(name, label, start, now, secs, data, restarted, machines))
    return {"prometheus": base, "evaluated_at": now.isoformat(timespec="seconds"),
            "evaluated_at_local": now.strftime("%Y-%m-%d %H:%M %Z").strip(),
            "windows": windows, "notes": [EXPORT_NOTE, COST_NOTE]}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Claude Code tokens and cost per machine and model, "
                                             "from the collector's Prometheus.")
    ap.add_argument("--since", action="append",
                    help="today, week, or a duration (30m, 12h, 1d, 7d, 2w); repeatable or "
                         "comma-separated; default: today and 7d")
    ap.add_argument("--json", action="store_true", help="print one JSON object")
    ap.add_argument("--url", default=default_url(), help="Prometheus base URL (default %(default)s)")
    ap.add_argument("--timeout", type=float, default=10.0, help="seconds per request (default 10)")
    ap.add_argument("--machines", default=os.environ.get("TELEMETRY_MACHINES", ""),
                    help="comma-separated machine labels to report as 'no data' when absent "
                         "(default: $TELEMETRY_MACHINES, else none)")
    ap.add_argument("--self-test", action="store_true", help="run the offline tests and exit")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    specs = [s for arg in (a.since or ["today,7d"]) for s in arg.split(",") if s.strip()]
    now = datetime.now().astimezone()
    try:
        for s in specs:
            window(s, now)
    except ValueError as e:
        ap.error(str(e))
    machines = tuple(m.strip() for m in a.machines.split(",") if m.strip())
    fail = None
    try:
        rep = build(a.url, specs, now, a.timeout, _request_guard(), machines)
    except Unreachable as e:
        fail = (1, "Prometheus is unreachable at %s (%s). Check that this machine reaches the "
                   "collector host (same LAN or VPN), then on that host "
                   "`systemctl status prometheus otelcol-contrib`." % (a.url, e))
    except QueryError as e:
        fail = (3, "Prometheus at %s refused a query: %s" % (a.url, e))
    except GuardRefusal as e:
        fail = (4, "request-guard refused the request (a refusal stands): %s" % e)
    if fail:
        if a.json:
            print(json.dumps({"error": fail[1], "prometheus": a.url}))
        print(fail[1], file=sys.stderr)
        return fail[0]
    print(json.dumps(rep, indent=1) if a.json else render(rep))
    return 0


# ---------- offline tests (--self-test) ----------

def self_test():
    import http.server
    import io
    import threading
    import unittest
    from contextlib import redirect_stderr, redirect_stdout

    now = datetime(2026, 10, 4, 22, 0).astimezone()
    at = now.timestamp()

    def vec(rows):
        return {"resultType": "vector",
                "result": [{"metric": m, "value": [at, str(v)]} for m, v in rows]}

    a1 = {"machine": "laptop", "model": "claude-haiku-4-5-20251001", "session_id": "s1"}
    o2 = {"machine": "desktop", "model": "claude-fable-5-1", "session_id": "s2"}
    restart = dict(o2, type="output")
    answers = {
        growth_query(TOKEN, 86400): vec([(dict(a1, type="input"), 10), (dict(a1, type="output"), 191),
                                         (dict(a1, type="cacheRead"), 13803),
                                         (dict(a1, type="cacheCreation"), 25970),
                                         (dict(o2, type="input"), 5), (restart, 700),
                                         (dict(o2, type="cacheRead", session_id="old"), 0)]),
        "resets(%s[86400s]) > 0" % TOKEN: vec([(restart, 1)]),
        growth_query(COST, 86400): vec([(a1, 0.0542853), (o2, 1.25)]),
        "resets(%s[86400s]) > 0" % COST: vec([]),
        growth_query(SESSIONS, 86400): vec([(dict(a1, start_type="fresh"), 1),
                                            (dict(o2, start_type="resume"), 1),
                                            ({"machine": "desktop", "start_type": "agents_view"}, 1)]),
    }
    raw_sel = "%s{%s}[%ds]" % (TOKEN, ",".join('%s="%s"' % (k, _esc(v)) for k, v in sorted(restart.items())),
                               86400 + LOOKBACK)
    # output counter: 300 at the window's start, 500, restart (100), 400 -> 200 + 400 = 600;
    # the second series only shares the matchers (an extra label) and must be ignored
    answers[raw_sel] = {"resultType": "matrix", "result": [
        {"metric": dict(restart, __name__=TOKEN),
         "values": [[at - 86500, "300"], [at - 3000, "500"], [at - 2000, "100"], [at - 60, "400"]]},
        {"metric": dict(restart, __name__=TOKEN, skill_name="x"), "values": [[at - 60, "999"]]}]}

    class Fake(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            form = urllib.parse.parse_qs(self.rfile.read(n).decode())
            q = form.get("query", [""])[0]
            if q == "BAD(":
                code, ans = 400, {"status": "error", "errorType": "bad_data", "error": "parse error"}
            else:
                code, ans = 200, {"status": "success", "data": answers.get(q, vec([]))}
            body = json.dumps(ans).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    machines = ("desktop", "laptop", "workstation")

    class T(unittest.TestCase):
        def test_windows(self):
            self.assertEqual(window("1d", now)[3], 86400)
            self.assertEqual(window("7d", now)[3], 7 * 86400)
            self.assertEqual(window("90m", now)[3], 5400)
            self.assertEqual(window("2w", now)[3], 14 * 86400)
            self.assertEqual(window("today", now)[3], 22 * 3600)
            self.assertLessEqual(window("week", now)[3], 7 * 86400 + 3600)
            self.assertEqual(window("today", now)[2].hour, 0)
            for bad in ("yesterday", "0d", "7", "1y"):
                self.assertRaises(ValueError, window, bad, now)

        def test_exact_growth(self):
            self.assertEqual(exact_growth([(10, 5.0), (20, 9.0)], 0), 9.0)          # started inside
            self.assertEqual(exact_growth([(-5, 4.0), (10, 9.0)], 0), 5.0)          # spans the start
            self.assertEqual(exact_growth([(-5, 4.0), (10, 9.0), (20, 2.0), (30, 3.0)], 0), 8.0)
            self.assertEqual(exact_growth([(-5, 4.0), (10, 4.0)], 0), 0.0)          # flat tail

        def test_growth_query(self):
            self.assertEqual(growth_query(COST, 60),
                             "((max_over_time(claude_code_cost_usage[60s]) - (claude_code_cost_usage "
                             "offset 60s)) >= 0) or max_over_time(claude_code_cost_usage[60s])")

        def test_report_from_fake_prometheus(self):
            rep = build(base, ["1d"], now, machines=machines)
            w = rep["windows"][0]
            rows = {(r["machine"], r["model"]): r for r in w["rows"]}
            lap = rows[("laptop", "claude-haiku-4-5-20251001")]
            self.assertEqual(lap["tokens"], {"input": 10, "output": 191, "cacheRead": 13803,
                                             "cacheCreation": 25970, "total": 39974})
            self.assertAlmostEqual(lap["cost_usd"], 0.0542853, places=6)   # JSON keeps 6 decimals
            desk = rows[("desktop", "claude-fable-5-1")]
            self.assertEqual(desk["tokens"]["output"], 600)       # recomputed across the restart
            self.assertEqual(desk["tokens"]["total"], 605)
            self.assertEqual(desk["sessions"], 1)                 # the zero-growth "old" session is not counted
            self.assertEqual(w["restarted_series"], 1)
            self.assertEqual(w["total"]["session_starts"], 2)     # agents_view excluded
            self.assertEqual(w["machines_without_data"], ["workstation"])
            self.assertAlmostEqual(w["total"]["cost_usd"], 1.3042853, places=6)
            text = render(rep)
            self.assertIn("39,974", text)
            self.assertIn("no data from: workstation", text)

        def test_cli_json(self):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["--url", base, "--since", "1d", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buf.getvalue())["windows"][0]["total"]["sessions"], 2)

        def test_unreachable(self):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["--url", "http://127.0.0.1:9", "--since", "1d", "--timeout", "3", "--json"])
            self.assertEqual(code, 1)
            self.assertIn("unreachable", err.getvalue())
            self.assertIn("error", json.loads(out.getvalue()))

        def test_query_error(self):
            with self.assertRaises(QueryError) as cm:
                prom(base, "BAD(", at)
            self.assertIn("parse error", str(cm.exception))

        def test_default_url(self):
            saved = {k: os.environ.pop(k, None) for k in ("PROMETHEUS_URL", "COLLECTOR_HOST")}
            try:
                self.assertEqual(default_url(), "http://localhost:9090")
                os.environ["COLLECTOR_HOST"] = "collector.lan"
                self.assertEqual(default_url(), "http://collector.lan:9090")
                os.environ["PROMETHEUS_URL"] = "http://prom.example:9090"
                self.assertEqual(default_url(), "http://prom.example:9090")
            finally:
                for k, v in saved.items():
                    os.environ.pop(k, None)
                    if v is not None:
                        os.environ[k] = v

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(T))
    srv.shutdown()
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
