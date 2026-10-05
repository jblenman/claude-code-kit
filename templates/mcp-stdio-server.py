"""mcp-stdio-server.py: a stdlib-only MCP server skeleton for Claude Code (stdio transport).

Copy this file, rename SERVER_NAME, replace the example tool, keep the protocol layer.

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout, as the MCP specification's "stdio"
transport and Claude Code expect. Nothing but JSON-RPC is ever written to stdout; a stray print()
corrupts the stream and the client drops the server. Logging goes to stderr at WARNING and above
only (Claude Code records every stderr line as an error whatever its level) and, when
MCP_SERVER_LOG names a file, to that file at INFO (DEBUG with MCP_SERVER_DEBUG=1).

Methods: initialize, notifications/initialized, ping, tools/list, tools/call. Anything else,
including the `server/discover` probe that newer clients send before `initialize`, is answered
with JSON-RPC error -32601 (method not found), which the client treats as "not supported".

Protocol versions: the server echoes the client's requested version when it is one of
SUPPORTED_PROTOCOL_VERSIONS and offers FALLBACK_PROTOCOL_VERSION otherwise. CLI 2.1.286+ asks for
2025-11-25; the negotiated version shows in Claude Code's connection log line.

Shutdown: Claude Code ends a stdio server with SIGINT, then SIGTERM about 0.1 s later, then
SIGKILL about 0.5 s after the first signal, and never closes stdin. So the server must exit on
the first signal within a few hundred milliseconds; nothing long-running belongs in the signal
path. Reading stdin to EOF is kept for other clients that do close it.

    python3 mcp-stdio-server.py              # serve (started by Claude Code)
    python3 mcp-stdio-server.py --self-test  # offline: handshake, tools/list, one tools/call, an unknown method

Importable: handle(request) -> response (dict, list for a batch, or None) drives the server without a
subprocess, which is how the self-test and a unit test exercise it. Python 3.8+, stdlib only.
"""
import json
import os
import signal
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

SERVER_NAME = "example-server"          # change me: the name clients show (tools become mcp__<name>__<tool>)
SERVER_VERSION = "0.1.0"
SERVER_TITLE = "Example stdio server"
INSTRUCTIONS = ("Example MCP server from the claude-code-kit template. One tool, echo, returns its input. "
                "Replace this text with how the model should use your tools.")

SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
FALLBACK_PROTOCOL_VERSION = "2025-03-26"

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

STATE = {"initialized": False, "protocol_version": None, "client": None, "framing": "newline"}


# ── Logging (never stdout) ───────────────────────────────────────────────────

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
_DEBUG = os.environ.get("MCP_SERVER_DEBUG", "") not in ("", "0", "false", "no")
_LOG_PATH = os.environ.get("MCP_SERVER_LOG", "")
_LOG_MAX_BYTES = 2 * 1024 * 1024
_STDERR_LEVEL = 30                      # WARNING+: the client logs every stderr line as an error
_FILE_LEVEL = 10 if _DEBUG else 20


def log(level: str, msg: str) -> None:
    n = _LEVELS.get(level, 20)
    line = "{} {:<7} {}".format(time.strftime("%Y-%m-%d %H:%M:%S"), level, msg)
    if n >= _STDERR_LEVEL:
        try:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        except Exception:
            pass
    if _LOG_PATH and n >= _FILE_LEVEL:
        try:
            d = os.path.dirname(os.path.abspath(_LOG_PATH))
            if d:
                os.makedirs(d, exist_ok=True)
            try:
                if os.path.getsize(_LOG_PATH) > _LOG_MAX_BYTES:
                    os.replace(_LOG_PATH, _LOG_PATH + ".1")
            except OSError:
                pass
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ── Tools (replace this section) ─────────────────────────────────────────────

class ToolError(Exception):
    """A tool's own failure: reported to the model as a result with isError, not as a protocol error."""


def tool_echo(args: Dict[str, Any]) -> Any:
    text = args.get("text")
    if not isinstance(text, str):
        raise ToolError("'text' must be a string")
    if args.get("uppercase"):
        text = text.upper()
    return {"text": text, "length": len(text)}


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "echo",
        "description": "Return the given text, optionally upper-cased. Example tool: replace it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The text to return."},
                "uppercase": {"type": "boolean", "description": "Return it upper-cased.", "default": False},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
]
TOOL_HANDLERS = {"echo": tool_echo}


# ── JSON-RPC ─────────────────────────────────────────────────────────────────

def _result(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _text_result(obj: Any, is_error: bool = False) -> Dict[str, Any]:
    text = obj if isinstance(obj, str) else json.dumps(obj, indent=2, ensure_ascii=False)
    res: Dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        res["isError"] = True
    return res


def negotiate_version(requested: Any) -> str:
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return FALLBACK_PROTOCOL_VERSION


def handle_initialize(params: Dict[str, Any]) -> Dict[str, Any]:
    requested = params.get("protocolVersion")
    version = negotiate_version(requested)
    STATE["protocol_version"] = version
    STATE["client"] = params.get("clientInfo")
    log("INFO", "initialize from {} requested {} -> {}".format(json.dumps(params.get("clientInfo")), requested, version))
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION, "title": SERVER_TITLE},
        "instructions": INSTRUCTIONS,
    }


def handle_tools_call(req_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str) or name not in TOOL_HANDLERS:
        return _error(req_id, INVALID_PARAMS, "Unknown tool: {}".format(name))
    args = params.get("arguments")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _result(req_id, _text_result("arguments must be a JSON object", is_error=True))
    t0 = time.time()
    try:
        out = TOOL_HANDLERS[name](args)
        log("INFO", "tools/call {} ok in {:.2f}s".format(name, time.time() - t0))
        return _result(req_id, _text_result(out))
    except ToolError as e:
        log("WARNING", "tools/call {} failed: {}".format(name, e))
        return _result(req_id, _text_result("{} failed: {}".format(name, e), is_error=True))
    except Exception as e:                       # never let a tool bug kill the server
        log("ERROR", "tools/call {} crashed: {}\n{}".format(name, e, traceback.format_exc()))
        return _result(req_id, _text_result("{} crashed: {}: {}".format(name, e.__class__.__name__, e), is_error=True))


def handle_one(msg: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(msg, dict):
        return _error(None, INVALID_REQUEST, "Invalid Request: not a JSON object")
    if msg.get("jsonrpc") != "2.0":
        return _error(msg.get("id"), INVALID_REQUEST, "Invalid Request: jsonrpc must be '2.0'")
    method = msg.get("method")
    if method is None:
        return None                              # a response to a server->client request; we never send any
    req_id = msg.get("id")
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        if "id" in msg:
            return _error(req_id, INVALID_PARAMS, "params must be an object")
        return None
    if "id" not in msg:                          # a notification: never answered
        if method == "notifications/initialized":
            STATE["initialized"] = True
            log("INFO", "client initialized")
        elif method == "notifications/cancelled":
            log("INFO", "cancel requested for {}".format(params.get("requestId")))
        else:
            log("DEBUG", "ignoring notification {}".format(method))
        return None
    try:
        if method == "initialize":
            return _result(req_id, handle_initialize(params))
        if method == "ping":
            return _result(req_id, {})
        if method == "tools/list":
            return _result(req_id, {"tools": TOOLS})
        if method == "tools/call":
            return handle_tools_call(req_id, params)
        # server/discover (a pre-initialize probe), resources/*, prompts/* and anything else
        return _error(req_id, METHOD_NOT_FOUND, "Method not found: {}".format(method))
    except Exception as e:
        log("ERROR", "{} crashed: {}\n{}".format(method, e, traceback.format_exc()))
        return _error(req_id, INTERNAL_ERROR, "Internal error: {}: {}".format(e.__class__.__name__, e))


def handle(request: Any) -> Any:
    """Process one parsed JSON-RPC message (or a batch list). Returns the response dict, a list of
    responses for a batch, or None when nothing is to be sent."""
    if isinstance(request, list):
        if not request:
            return _error(None, INVALID_REQUEST, "Invalid Request: empty batch")
        out = [r for r in (handle_one(m) for m in request) if r is not None]
        return out or None
    return handle_one(request)


def handle_text(line: str) -> Optional[str]:
    """Parse one raw message, handle it, return the encoded response (or None)."""
    try:
        msg = json.loads(line)
    except ValueError as e:
        return json.dumps(_error(None, PARSE_ERROR, "Parse error: {}".format(e)))
    resp = handle(msg)
    return None if resp is None else json.dumps(resp, ensure_ascii=True)


# ── stdio loop ───────────────────────────────────────────────────────────────

def _write_out(text: str) -> None:
    data = text.encode("utf-8")
    if STATE["framing"] == "content-length":
        sys.stdout.buffer.write(b"Content-Length: " + str(len(data)).encode("ascii") + b"\r\n\r\n")
    sys.stdout.buffer.write(data + b"\n")
    sys.stdout.buffer.flush()


def _read_message(stdin) -> Optional[bytes]:
    """Next raw message: newline-delimited, tolerating Content-Length framing from other clients."""
    while True:
        line = stdin.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            try:
                length = int(stripped.split(b":", 1)[1].strip())
            except ValueError:
                continue
            while True:                          # remaining headers up to the blank line
                h = stdin.readline()
                if not h or not h.strip():
                    break
            STATE["framing"] = "content-length"
            body = b""
            while len(body) < length:
                chunk = stdin.read(length - len(body))
                if not chunk:
                    return None
                body += chunk
            return body
        return stripped


class _Stop(Exception):
    pass


def _on_signal(signum, frame):
    raise _Stop(signum)


def serve_stdio() -> int:
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):        # not the main thread, or unsupported on this platform
                pass
    log("INFO", "{} {} starting (pid {}, python {})".format(SERVER_NAME, SERVER_VERSION, os.getpid(), sys.version.split()[0]))
    stdin = sys.stdin.buffer
    try:
        while True:
            raw = _read_message(stdin)
            if raw is None:
                log("INFO", "stdin closed; exiting")
                return 0
            text = raw.decode("utf-8", errors="replace")
            try:
                out = handle_text(text)
            except Exception as e:
                log("ERROR", "unhandled: {}\n{}".format(e, traceback.format_exc()))
                out = json.dumps(_error(None, INTERNAL_ERROR, "Internal error: {}".format(e)))
            if out is not None:
                try:
                    _write_out(out)
                except (BrokenPipeError, OSError):
                    log("INFO", "stdout closed; exiting")
                    return 0
    except (_Stop, KeyboardInterrupt) as e:
        log("INFO", "signal {}; exiting".format(getattr(e, "args", ["?"])[0] if getattr(e, "args", None) else "INT"))
        return 0


def self_test() -> int:
    """Offline: handshake, tools/list, one tools/call, a tool failure, an unknown method, a parse error."""
    def req(i, method, params=None):
        m: Dict[str, Any] = {"jsonrpc": "2.0", "id": i, "method": method}
        if params is not None:
            m["params"] = params
        return m
    init = handle(req(1, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                        "clientInfo": {"name": "self-test", "version": "0"}}))
    assert init["result"]["protocolVersion"] == "2025-11-25", init
    assert handle(req(2, "initialize", {"protocolVersion": "1999-01-01"}))["result"]["protocolVersion"] == FALLBACK_PROTOCOL_VERSION
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None and STATE["initialized"]
    assert handle(req(3, "ping"))["result"] == {}
    tools = handle(req(4, "tools/list"))["result"]["tools"]
    assert [t["name"] for t in tools] == list(TOOL_HANDLERS), tools
    call = handle(req(5, "tools/call", {"name": "echo", "arguments": {"text": "hi", "uppercase": True}}))
    assert '"HI"' in call["result"]["content"][0]["text"] and not call["result"].get("isError"), call
    bad = handle(req(6, "tools/call", {"name": "echo", "arguments": {"text": 5}}))
    assert bad["result"]["isError"] is True, bad
    assert handle(req(7, "tools/call", {"name": "nope"}))["error"]["code"] == INVALID_PARAMS
    assert handle(req(8, "server/discover"))["error"]["code"] == METHOD_NOT_FOUND
    assert handle(req(9, "resources/list"))["error"]["code"] == METHOD_NOT_FOUND
    assert json.loads(handle_text("{not json"))["error"]["code"] == PARSE_ERROR
    assert handle([req(10, "ping"), {"jsonrpc": "2.0", "method": "notifications/x"}]) == [_result(10, {})]
    assert handle({"jsonrpc": "1.0", "id": 11, "method": "ping"})["error"]["code"] == INVALID_REQUEST
    print("self-test ok: {} {}; protocol {}; tools: {}".format(SERVER_NAME, SERVER_VERSION,
          ", ".join(SUPPORTED_PROTOCOL_VERSIONS), ", ".join(TOOL_HANDLERS)), file=sys.stderr)
    return 0


def main(argv: List[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    return serve_stdio()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
