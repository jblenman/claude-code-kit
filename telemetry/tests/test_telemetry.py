"""Offline tests for telemetry/: no network, no collector binary, no root.

    python3 telemetry/tests/test_telemetry.py        # Windows: py telemetry\\tests\\test_telemetry.py

Stdlib, Python 3.8+. PyYAML is used when installed; otherwise a small YAML-subset reader below
parses the collector config (when PyYAML is present, both are compared). `jq` is needed for the
recipe tests and `bash` for the installer tests; each group is skipped when its tool is missing.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
HERE = TESTS.parent                      # telemetry/
FIXTURE = TESTS / "events-fixture.jsonl"

# Names from https://code.claude.com/docs/en/monitoring-usage.md, fetched 2026-10-04 (every
# backticked OTEL_*/CLAUDE_CODE_* token on the page, every claude_code.* metric and event name).
# Update this snapshot when the page is re-read; the tests fail on any name outside it.
DOC_ENV = {
    "BETA_TRACING_ENDPOINT", "CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY",
    "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE", "CLAUDE_CODE_ENABLE_FEEDBACK_SURVEY_FOR_OTEL",
    "CLAUDE_CODE_ENABLE_TELEMETRY", "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA", "CLAUDE_CODE_MAX_RETRIES",
    "CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH", "CLAUDE_CODE_OTEL_HEADERS_HELPER_DEBOUNCE_MS",
    "CLAUDE_CODE_PROPAGATE_TRACEPARENT",
    "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_RETRY_WATCHDOG", "DISABLE_ERROR_REPORTING",
    "ENABLE_BETA_TRACING_DETAILED", "ENABLE_ENHANCED_TELEMETRY_BETA", "NODE_EXTRA_CA_CERTS",
    "OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "OTEL_EXPORTER_OTLP_CERTIFICATE",
    "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE", "OTEL_EXPORTER_OTLP_CLIENT_KEY",
    "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_HEADERS", "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
    "OTEL_EXPORTER_OTLP_METRICS_CLIENT_KEY", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_HEADERS", "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "OTEL_LOGS_EXPORTER", "OTEL_LOGS_EXPORT_INTERVAL",
    "OTEL_LOG_ASSISTANT_RESPONSES", "OTEL_LOG_MANAGED_SETTINGS", "OTEL_LOG_RAW_API_BODIES",
    "OTEL_LOG_TOOL_CONTENT", "OTEL_LOG_TOOL_DETAILS", "OTEL_LOG_USER_PROMPTS", "OTEL_METRICS_EXPORTER",
    "OTEL_METRICS_INCLUDE_ACCOUNT_UUID", "OTEL_METRICS_INCLUDE_ENTRYPOINT",
    "OTEL_METRICS_INCLUDE_REPOSITORY", "OTEL_METRICS_INCLUDE_RESOURCE_ATTRIBUTES",
    "OTEL_METRICS_INCLUDE_SESSION_ID", "OTEL_METRICS_INCLUDE_VERSION", "OTEL_METRIC_EXPORT_INTERVAL",
    "OTEL_RESOURCE_ATTRIBUTES", "OTEL_TRACES_EXPORTER", "OTEL_TRACES_EXPORT_INTERVAL",
    "TRACEPARENT", "TRACESTATE",
}
DOC_METRICS = {
    "claude_code.session.count", "claude_code.lines_of_code.count", "claude_code.pull_request.count",
    "claude_code.commit.count", "claude_code.cost.usage", "claude_code.token.usage",
    "claude_code.code_edit_tool.decision", "claude_code.active_time.total",
}
DOC_EVENTS = {
    "api_error", "api_refusal", "api_request", "api_request_body", "api_response_body",
    "api_retries_exhausted", "assistant_response", "at_mention", "auth", "compaction",
    "feedback_survey", "hook_execution_complete", "hook_execution_start", "hook_plugin_metrics",
    "hook_registered", "internal_error", "managed_settings_resolved", "mcp_server_connection",
    "permission_mode_changed", "plugin_installed", "plugin_loaded", "retention_sweep",
    "skill_activated", "subagent_completed", "tool_decision", "tool_result", "user_prompt",
}
# Prometheus names the collector produces with UnderscoreEscapingWithoutSuffixes.
PROM_METRICS = {m.replace(".", "_") for m in DOC_METRICS}

DOC_FILES = ["README.md", "queries.md", "verify.md", "otelcol-config.yaml", "settings-env.json",
             "install-collector.sh", "flatten.jq", "tool-decisions.jq", "cost-per-day.jq", "peak-context.jq",
             "report.py"]
OTHER_FILES = ["prometheus-scrape.yaml", "tests/events-fixture.jsonl", "tests/test_telemetry.py"]
# IPv4 literals allowed in these files: the any-address, loopback and the documented examples.
ALLOWED_IPV4 = {"0.0.0.0", "127.0.0.1", "10.0.0.20", "10.0.0.0"}

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - the usual case on a plain Python install
    yaml = None

JQ = shutil.which("jq")
BASH = shutil.which("bash")
IS_WIN = os.name == "nt"


def read(name):
    return (HERE / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- a small YAML-subset reader
# Enough for the two YAML files in this folder: block mappings, block sequences (including
# "- key: value" items), flow sequences "[a, b]", comments, quoted and plain scalars, ints, bools.

def _strip_comment(line):
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _split_flow(s):
    parts, depth, quote, cur = [], 0, None, ""
    for ch in s:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            cur += ch
        elif ch in "[{":
            depth += 1
            cur += ch
        elif ch in "]}":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def _scalar(s):
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        return [_scalar(p) for p in _split_flow(s[1:-1])]
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if s in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def _key_split(content):
    """('key', 'rest') when the line is a mapping entry, else None."""
    if content[:1] in "'\"[{":
        return None
    m = re.match(r"^([^:#]+?):(?:\s+(.*))?$", content)
    if not m:
        return None
    return m.group(1).strip(), (m.group(2) or "").strip()


def mini_yaml(text):
    lines = []
    for raw in text.splitlines():
        s = _strip_comment(raw).rstrip()
        if s.strip():
            lines.append([len(s) - len(s.lstrip(" ")), s.strip()])
    pos = [0]

    def is_item(content):
        return content == "-" or content.startswith("- ")

    def block():
        indent, content = lines[pos[0]]
        return seq(indent) if is_item(content) else mapping(indent)

    def seq(indent):
        out = []
        while pos[0] < len(lines) and lines[pos[0]][0] == indent and is_item(lines[pos[0]][1]):
            content = lines[pos[0]][1][1:].strip()
            if not content:
                pos[0] += 1
                out.append(block())
            elif _key_split(content):
                lines[pos[0]] = [indent + 2, content]      # the item is a mapping that starts here
                out.append(mapping(indent + 2))
            else:
                out.append(_scalar(content))
                pos[0] += 1
        return out

    def mapping(indent):
        out = {}
        while pos[0] < len(lines) and lines[pos[0]][0] == indent and not is_item(lines[pos[0]][1]):
            kv = _key_split(lines[pos[0]][1])
            if kv is None:
                raise ValueError("cannot read line: %r" % lines[pos[0]][1])
            key, rest = kv
            pos[0] += 1
            if rest:
                out[key] = _scalar(rest)
            elif pos[0] < len(lines) and lines[pos[0]][0] > indent:
                out[key] = block()
            elif pos[0] < len(lines) and lines[pos[0]][0] == indent and is_item(lines[pos[0]][1]):
                out[key] = seq(indent)
            else:
                out[key] = None
        return out

    result = block() if lines else None
    if pos[0] != len(lines):
        raise ValueError("unread lines from %r" % lines[pos[0]][1])
    return result


def load_yaml(name):
    return yaml.safe_load(read(name)) if yaml is not None else mini_yaml(read(name))


def run_jq(args, stdin):
    p = subprocess.run([JQ] + args, input=stdin, capture_output=True, text=True, cwd=str(HERE),
                       encoding="utf-8")
    if p.returncode != 0:
        raise AssertionError("jq failed: %s\n%s" % (args, p.stderr))
    return p.stdout


def flattened():
    return run_jq(["-c", "-f", "flatten.jq", str(FIXTURE)], "")


# ---------------------------------------------------------------- tests

class Files(unittest.TestCase):
    def test_all_files_present(self):
        for f in DOC_FILES + OTHER_FILES:
            self.assertTrue((HERE / f).is_file(), f)

    def test_no_emails_tokens_or_real_addresses(self):
        for f in DOC_FILES + OTHER_FILES[:2]:
            text = read(f)
            self.assertNotRegex(text, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", f)
            self.assertNotRegex(text, r"(?i)bearer\s+[a-z0-9]{8,}", f)
            for ip in re.findall(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])", text):
                self.assertIn(ip, ALLOWED_IPV4, "%s: IPv4 literal %s" % (f, ip))

    def test_installer_is_executable(self):
        if IS_WIN:
            self.skipTest("no exec bit on Windows")
        mode = (HERE / "install-collector.sh").stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)


class MiniYaml(unittest.TestCase):
    def test_reader_handles_the_constructs_used(self):
        doc = mini_yaml("a:\n  b: 1   # c\n  l: [x, 'y:z', 3]\n  s:\n    - k: v\n      w: true\n    - plain\n")
        self.assertEqual(doc, {"a": {"b": 1, "l": ["x", "y:z", 3], "s": [{"k": "v", "w": True}, "plain"]}})

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_reader_agrees_with_pyyaml(self):
        for f in ("otelcol-config.yaml", "prometheus-scrape.yaml"):
            self.assertEqual(mini_yaml(read(f)), yaml.safe_load(read(f)), f)


class EnvNames(unittest.TestCase):
    TOKEN = re.compile(r"\b((?:OTEL|CLAUDE_CODE)_[A-Z0-9_]+)\b")

    def test_every_variable_mentioned_is_documented(self):
        for f in DOC_FILES:
            found = set(self.TOKEN.findall(read(f)))
            self.assertTrue(found <= DOC_ENV, "%s: undocumented variables %s" % (f, sorted(found - DOC_ENV)))

    def test_settings_env_block(self):
        env = json.loads(read("settings-env.json"))["env"]
        self.assertTrue(set(env) <= DOC_ENV, sorted(set(env) - DOC_ENV))
        for k in ("CLAUDE_CODE_ENABLE_TELEMETRY", "OTEL_METRICS_EXPORTER", "OTEL_LOGS_EXPORTER",
                  "OTEL_EXPORTER_OTLP_PROTOCOL", "OTEL_EXPORTER_OTLP_ENDPOINT",
                  "OTEL_RESOURCE_ATTRIBUTES", "OTEL_METRIC_EXPORT_INTERVAL"):
            self.assertIn(k, env)
        self.assertEqual(env["CLAUDE_CODE_ENABLE_TELEMETRY"], "1")
        self.assertEqual(env["OTEL_METRICS_EXPORTER"], "otlp")
        self.assertEqual(env["OTEL_LOGS_EXPORTER"], "otlp")
        self.assertIn(env["OTEL_EXPORTER_OTLP_PROTOCOL"], ("grpc", "http/protobuf", "http/json"))
        port = "4317" if env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "grpc" else "4318"
        self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"], "http://COLLECTOR_HOST:" + port)
        self.assertEqual(env["OTEL_RESOURCE_ATTRIBUTES"], "machine=<name>")   # placeholder, documented
        self.assertTrue(env["OTEL_METRIC_EXPORT_INTERVAL"].isdigit())
        self.assertEqual(env.get("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"), "cumulative")
        for k in env:  # settings.json env values must be strings
            self.assertIsInstance(env[k], str)
        self.assertNotIn("OTEL_LOG_USER_PROMPTS", env)  # stays off by design

    def test_filled_in_resource_attribute_has_the_strict_format(self):
        # What the README tells people to put in place of <name>: no spaces, no quotes.
        value = json.loads(read("settings-env.json"))["env"]["OTEL_RESOURCE_ATTRIBUTES"].replace("<name>", "build-box")
        self.assertRegex(value, r"^machine=[A-Za-z0-9_.-]+$")
        self.assertIn("no spaces or quotes", read("README.md"))

    def test_readme_env_block_equals_settings_file(self):
        m = re.search(r"```json\n(\{\n  \"env\": \{.*?\n  \}\n\})\n```", read("README.md"), re.S)
        self.assertIsNotNone(m, "README has no env block")
        self.assertEqual(json.loads(m.group(1)), json.loads(read("settings-env.json")))


class MetricNames(unittest.TestCase):
    def test_dotted_metric_names_are_documented(self):
        for f in ("README.md", "queries.md", "verify.md", "otelcol-config.yaml"):
            found = set(re.findall(r"claude_code\.[a-z_]+\.[a-z_]+", read(f)))
            self.assertTrue(found <= DOC_METRICS, "%s: %s" % (f, sorted(found - DOC_METRICS)))

    def test_prometheus_names_map_to_documented_metrics(self):
        # The README names one suffixed form as the counterexample (what the default strategy produces).
        counterexample = {"claude_code_token_usage_tokens_total"}
        for f in ("queries.md", "verify.md", "README.md"):
            found = set(re.findall(r"\bclaude_code_[a-z_]+", read(f))) - counterexample
            self.assertTrue(found <= PROM_METRICS, "%s: %s" % (f, sorted(found - PROM_METRICS)))

    def test_event_names_in_recipes_are_documented(self):
        for f in ("flatten.jq", "tool-decisions.jq", "cost-per-day.jq", "peak-context.jq", "queries.md", "verify.md"):
            found = set(re.findall(r'\["event\.name"\]\s*==\s*"([a-z_]+)"', read(f)))
            self.assertTrue(found <= DOC_EVENTS, "%s: %s" % (f, sorted(found - DOC_EVENTS)))

    def test_readme_event_lists_are_documented(self):
        text = read("README.md")
        sec = text[text.index("### Events"):text.index("### Attributes")]
        found = set(re.findall(r"`([a-z_]+)`", sec)) - {"claude_code"}
        # attribute names also appear in backticks in that section; keep only event-shaped tokens
        found = {t for t in found if t in DOC_EVENTS or t.endswith((
            "_request", "_result", "_decision", "_prompt", "_error", "_loaded", "_installed",
            "_registered", "_activated", "_completed", "_changed", "_connection", "_mention", "_sweep",
            "_resolved", "_survey", "_metrics", "_start", "_complete", "_exhausted", "_refusal",
            "_body", "_response"))}
        self.assertTrue(found <= DOC_EVENTS, sorted(found - DOC_EVENTS))
        self.assertTrue({"user_prompt", "tool_result", "tool_decision", "api_request", "skill_activated",
                         "hook_registered"} <= found)


class CollectorConfig(unittest.TestCase):
    def setUp(self):
        self.cfg = load_yaml("otelcol-config.yaml")

    def test_receivers(self):
        p = self.cfg["receivers"]["otlp"]["protocols"]
        self.assertTrue(p["grpc"]["endpoint"].endswith(":4317"))
        self.assertTrue(p["http"]["endpoint"].endswith(":4318"))

    def test_pipelines_reference_defined_components(self):
        for kind in ("receivers", "processors", "exporters"):
            defined = set(self.cfg[kind])
            for name, pl in self.cfg["service"]["pipelines"].items():
                used = set(pl.get(kind, []))
                self.assertTrue(used <= defined, "%s/%s: %s" % (name, kind, sorted(used - defined)))
        self.assertEqual(set(self.cfg["service"]["pipelines"]), {"metrics", "logs"})

    def test_memory_limiter_first_in_every_pipeline(self):
        for name, pl in self.cfg["service"]["pipelines"].items():
            self.assertEqual(pl["processors"][0], "memory_limiter", name)

    def test_metrics_pipeline(self):
        pl = self.cfg["service"]["pipelines"]["metrics"]
        self.assertEqual(pl["exporters"], ["prometheus"])
        self.assertIn("deltatocumulative", pl["processors"])
        prom = self.cfg["exporters"]["prometheus"]
        self.assertTrue(prom["endpoint"].endswith(":8889"))
        self.assertEqual(prom["translation_strategy"], "UnderscoreEscapingWithoutSuffixes")

    def test_logs_pipeline_writes_rotated_jsonl(self):
        pl = self.cfg["service"]["pipelines"]["logs"]
        self.assertEqual(pl["exporters"], ["file/events"])
        fe = self.cfg["exporters"]["file/events"]
        self.assertTrue(fe["path"].startswith("/var/lib/otelcol-contrib/events/"))
        self.assertTrue(fe["path"].endswith(".jsonl"))
        self.assertEqual(fe["format"], "json")
        rot = fe["rotation"]
        for k in ("max_megabytes", "max_backups", "max_days"):
            self.assertIsInstance(rot[k], int)
            self.assertGreater(rot[k], 0)

    def test_identity_attributes_dropped_in_both_pipelines(self):
        acts = self.cfg["processors"]["attributes/drop-identity"]["actions"]
        dropped = {a["key"] for a in acts if a["action"] == "delete"}
        self.assertEqual(dropped, {"user.email", "user.account_uuid", "user.account_id"})
        for name, pl in self.cfg["service"]["pipelines"].items():
            self.assertIn("attributes/drop-identity", pl["processors"], name)

    def test_debug_exporter_not_wired(self):
        for pl in self.cfg["service"]["pipelines"].values():
            self.assertNotIn("debug", pl["exporters"])

    def test_scrape_snippet_targets_the_exporter(self):
        jobs = load_yaml("prometheus-scrape.yaml")
        self.assertEqual(jobs[0]["job_name"], "otelcol-claude")
        self.assertEqual(jobs[0]["static_configs"][0]["targets"], ["127.0.0.1:8889"])


@unittest.skipIf(BASH is None, "bash not installed")
class Installer(unittest.TestCase):
    SCRIPT = str(HERE / "install-collector.sh")
    PATHS = ("/etc/otelcol-contrib", "/usr/local/bin/otelcol-contrib", "/etc/systemd/system/otelcol-contrib.service")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="telemetry-test-"))

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def os_release(self, text):
        p = self.tmp / "os-release"
        p.write_text(text, encoding="utf-8")
        return str(p)

    def fake_uname(self, machine):
        d = self.tmp / "bin"
        d.mkdir(exist_ok=True)
        f = d / "uname"
        f.write_text("#!/bin/sh\n[ \"$1\" = -m ] && echo %s || echo Linux\n" % machine, encoding="utf-8")
        f.chmod(0o755)
        return str(d)

    def dry(self, *args, env_extra=None, path_prefix=None):
        env = dict(os.environ, OTELCOL_VERSION="0.0.1")
        env.update(env_extra or {})
        if path_prefix:
            env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")
        return subprocess.run([BASH, self.SCRIPT, "--dry-run"] + list(args), env=env, capture_output=True,
                              text=True, cwd=str(HERE), timeout=120)

    def test_bash_syntax(self):
        p = subprocess.run([BASH, "-n", self.SCRIPT], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_dry_run_writes_nothing_and_lists_actions(self):
        before = {Path(x).exists() for x in self.PATHS}
        p = self.dry("--with-prometheus")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn("DRY RUN finished", p.stdout)
        self.assertIn("otelcol-contrib_0.0.1_linux_", p.stdout)
        self.assertNotIn("\nDO ", p.stdout)
        self.assertIn("machines export to: http://", p.stdout)
        after = {Path(x).exists() for x in self.PATHS}
        self.assertEqual(before, after)

    @unittest.skipIf(IS_WIN, "fake uname needs a POSIX shell")
    def test_asset_follows_the_cpu(self):
        for machine, arch in (("x86_64", "amd64"), ("aarch64", "arm64"), ("arm64", "arm64"), ("amd64", "amd64")):
            p = self.dry(path_prefix=self.fake_uname(machine))
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("arch   %s" % arch, p.stdout, machine)
            self.assertIn("otelcol-contrib_0.0.1_linux_%s.tar.gz" % arch, p.stdout, machine)

    @unittest.skipIf(IS_WIN, "fake uname needs a POSIX shell")
    def test_refuses_32_bit_and_unknown_cpus(self):
        for machine, msg in (("armv7l", "32-bit ARM"), ("riscv64", "unsupported CPU")):
            p = self.dry(path_prefix=self.fake_uname(machine))
            self.assertNotEqual(p.returncode, 0, machine)
            self.assertIn(msg, p.stderr, machine)

    def test_recognises_debian_and_ubuntu(self):
        cases = (
            ('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\n', "Debian GNU/Linux 13"),
            ('PRETTY_NAME="Ubuntu 24.04.1 LTS"\nID=ubuntu\nID_LIKE=debian\n', "Ubuntu 24.04"),
            ('PRETTY_NAME="Raspbian GNU/Linux 12"\nID=raspbian\nID_LIKE=debian\n', "Raspbian"),
            ('PRETTY_NAME="Linux Mint 22"\nID=linuxmint\nID_LIKE="ubuntu debian"\n', "Linux Mint"),
        )
        for text, name in cases:
            p = self.dry(env_extra={"OS_RELEASE_FILE": self.os_release(text)})
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("os     " + name, p.stdout)
            self.assertNotIn("not a Debian or Ubuntu host", p.stdout)

    def test_warns_on_other_systems_in_a_dry_run(self):
        p = self.dry(env_extra={"OS_RELEASE_FILE": self.os_release('PRETTY_NAME="Fedora Linux 41"\nID=fedora\n')})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("not a Debian or Ubuntu host (Fedora Linux 41)", p.stdout)
        p = self.dry(env_extra={"OS_RELEASE_FILE": str(self.tmp / "missing")})
        self.assertIn("not a Debian or Ubuntu host (unknown)", p.stdout)

    def test_real_run_needs_root(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("running as root")
        p = subprocess.run([BASH, self.SCRIPT], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("run as root", p.stderr)

    def test_rejects_bad_version_retention_and_unknown_flag(self):
        p = self.dry(env_extra={"OTELCOL_VERSION": "latest"})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("OTELCOL_VERSION must look like", p.stderr)
        p = self.dry("--with-prometheus", env_extra={"PROM_RETENTION": "forever"})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("PROM_RETENTION must look like", p.stderr)
        p = self.dry("--nope")
        self.assertEqual(p.returncode, 2)

    def test_dry_run_fails_cleanly_when_version_cannot_be_resolved(self):
        # Force the resolution step to fail without the network: an unusable proxy address.
        env = dict(os.environ, https_proxy="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9")
        env.pop("OTELCOL_VERSION", None)
        p = subprocess.run([BASH, self.SCRIPT, "--dry-run"], env=env, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("could not resolve the latest release", p.stderr)

    def test_installer_contents(self):
        text = read("install-collector.sh")
        self.assertIn('ASSET="otelcol-contrib_${VERSION}_linux_${ARCH}.tar.gz"', text)
        self.assertIn("aarch64|arm64) ARCH=arm64", text)
        self.assertIn("x86_64|amd64)  ARCH=amd64", text)
        self.assertIn("useradd --system", text)
        self.assertIn("validate --config=", text)
        self.assertIn("sha256sum -c", text)


class ReportUnits(unittest.TestCase):
    """report.py's arithmetic with in-memory data (no sockets). The HTTP path is covered by
    `python3 report.py --self-test`, which runs against an in-process fake Prometheus."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(HERE))
        import report  # noqa: E402
        cls.r = report
        from datetime import datetime
        cls.now = datetime(2026, 10, 4, 22, 0).astimezone()

    def test_windows(self):
        r, now = self.r, self.now
        self.assertEqual(r.window("1d", now)[3], 86400)
        self.assertEqual(r.window("90m", now)[3], 5400)
        self.assertEqual(r.window("2w", now)[3], 14 * 86400)
        self.assertEqual(r.window("today", now)[3], 22 * 3600)
        for bad in ("yesterday", "0d", "7", "1y"):
            self.assertRaises(ValueError, r.window, bad, now)

    def test_exact_growth_handles_restarts(self):
        g = self.r.exact_growth
        self.assertEqual(g([(10, 5.0), (20, 9.0)], 0), 9.0)                      # started inside
        self.assertEqual(g([(-5, 4.0), (10, 9.0)], 0), 5.0)                      # spans the start
        self.assertEqual(g([(-5, 4.0), (10, 9.0), (20, 2.0), (30, 3.0)], 0), 8.0)  # counter restarted
        self.assertEqual(g([(-5, 4.0), (10, 4.0)], 0), 0.0)                      # flat tail

    def test_growth_query_does_not_use_increase(self):
        q = self.r.growth_query(self.r.COST, 60)
        self.assertNotIn("increase(", q)
        self.assertEqual(q, "((max_over_time(claude_code_cost_usage[60s]) - (claude_code_cost_usage "
                            "offset 60s)) >= 0) or max_over_time(claude_code_cost_usage[60s])")

    def test_aggregate_and_render(self):
        r = self.r
        a1 = {"machine": "laptop", "model": "m1", "session_id": "s1"}
        old = dict(a1, session_id="old", type="cacheRead")
        data = {r.TOKEN: [(dict(a1, type="input"), 10.0), (dict(a1, type="output"), 191.0), (old, 0.0)],
                r.COST: [(a1, 0.05)],
                r.SESSIONS: [(dict(a1, start_type="fresh"), 1.0),
                             ({"machine": "laptop", "start_type": "agents_view"}, 1.0)]}
        w = r.window_result("1d", "last 1 day", self.now, self.now, 86400, data, 0, ("laptop", "desktop"))
        self.assertEqual(w["total"]["tokens"]["total"], 201)
        self.assertEqual(w["total"]["sessions"], 1)             # the zero-growth session is not counted
        self.assertEqual(w["total"]["session_starts"], 1)       # agents_view excluded
        self.assertEqual(w["machines_without_data"], ["desktop"])
        text = r.render({"prometheus": "http://localhost:9090", "evaluated_at_local": "now",
                         "windows": [w], "notes": ["n"]})
        self.assertIn("no data from: desktop", text)
        self.assertIn("201", text)

    def test_default_url_follows_the_environment(self):
        r = self.r
        saved = {k: os.environ.pop(k, None) for k in ("PROMETHEUS_URL", "COLLECTOR_HOST")}
        try:
            self.assertEqual(r.default_url(), "http://localhost:9090")
            os.environ["COLLECTOR_HOST"] = "collector.lan"
            self.assertEqual(r.default_url(), "http://collector.lan:9090")
            os.environ["PROMETHEUS_URL"] = "http://prometheus.example:9090"
            self.assertEqual(r.default_url(), "http://prometheus.example:9090")
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


@unittest.skipIf(JQ is None, "jq not installed")
class Recipes(unittest.TestCase):
    def test_flatten_one_object_per_event(self):
        lines = [json.loads(l) for l in flattened().splitlines()]
        self.assertEqual(len(lines), 17)
        names = {}
        for ev in lines:
            names[ev["event.name"]] = names.get(ev["event.name"], 0) + 1
            self.assertIn("machine", ev)                 # resource attribute merged in
            self.assertIn("session.id", ev)
            self.assertEqual(ev["body"], "claude_code." + ev["event.name"])
        self.assertEqual(names, {"api_request": 4, "compaction": 1, "hook_registered": 1, "skill_activated": 1,
                                 "tool_decision": 6, "tool_result": 1, "user_prompt": 3})
        api = next(e for e in lines if e["event.name"] == "api_request")
        self.assertIsInstance(api["input_tokens"], int)  # intValue strings become numbers
        self.assertIsInstance(api["cost_usd"], float)

    def test_tool_decisions_per_session(self):
        out = run_jq(["-s", "-r", "-f", "tool-decisions.jq"], flattened()).splitlines()
        self.assertEqual(out[0].split("\t"), ["machine", "session", "decisions", "accept", "reject", "by_source", "by_tool"])
        rows = {r.split("\t")[0]: r.split("\t") for r in out[1:]}
        self.assertEqual(set(rows), {"desktop", "laptop", "workstation"})
        self.assertEqual(rows["desktop"][2:5], ["2", "1", "1"])
        self.assertEqual(json.loads(rows["desktop"][5]), {"config": 1, "user_reject": 1})
        self.assertEqual(json.loads(rows["desktop"][6]), {"Bash": 1, "Edit": 1})
        self.assertEqual(rows["laptop"][2:5], ["3", "3", "0"])
        self.assertEqual(json.loads(rows["laptop"][5]), {"config": 1, "hook": 1, "user_temporary": 1})
        self.assertEqual(rows["workstation"][2:5], ["1", "0", "1"])
        since = run_jq(["-s", "-r", "--arg", "since", "2026-10-05", "-f", "tool-decisions.jq"], flattened()).splitlines()
        self.assertEqual([r.split("\t")[0] for r in since[1:]], ["workstation"])

    def test_cost_per_day(self):
        out = run_jq(["-s", "-r", "-f", "cost-per-day.jq"], flattened()).splitlines()
        rows = {(r.split("\t")[0], r.split("\t")[1]): r.split("\t") for r in out[1:]}
        self.assertEqual(rows[("desktop", "2026-10-04")][2:5], ["2", "0.05", "101060"])
        self.assertEqual(rows[("laptop", "2026-10-04")][2:5], ["1", "0.0041", "15020"])
        self.assertEqual(rows[("workstation", "2026-10-05")][2:5], ["1", "0.05", "23800"])

    def test_peak_context(self):
        out = run_jq(["-s", "-r", "-f", "peak-context.jq"], flattened()).splitlines()
        rows = {r.split("\t")[0]: r.split("\t") for r in out[1:]}
        self.assertEqual(set(rows), {"desktop", "workstation"})  # the laptop's only request is a subagent's
        self.assertEqual(rows["desktop"][3:6], ["2", "50400", "560"])
        self.assertEqual(rows["workstation"][3:6], ["1", "23000", "800"])

    def test_fixture_has_no_identity_fields_and_redacted_prompts(self):
        for ev in (json.loads(l) for l in flattened().splitlines()):
            self.assertNotIn("user.email", ev)
            if ev["event.name"] == "user_prompt":
                self.assertEqual(ev["prompt"], "<REDACTED>")


if __name__ == "__main__":
    unittest.main(verbosity=1)
