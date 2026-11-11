#!/usr/bin/env python3
"""prompts-dir-mcp: MCP stdio server that serves a directory of Markdown prompt
templates as MCP prompts (protocol version 2026-07-28).

Features:
  - prompts/list + prompts/get with {{argument}} substitution
  - argument completion through completion/complete (ref/prompt), driven by
    per-argument [enum,lists] declared in the template
  - prompts listChanged through subscriptions/listen (promptsListChanged),
    delivered by a polling watcher over the prompts directory

Zero dependencies, single file, Python 3.9+.

Template format (one .md file per prompt, prompt name = file stem):

    <!-- args
    code: The code to review (required)
    language: Programming language [python,javascript,go,rust]
    -->
    # Request Code Review
    Asks the LLM to analyze code quality and suggest improvements.

    Please review this {{language}} code:

    {{code}}

The args block, the # Title line and the description line are all optional.
"(required)" marks a required argument; "[a,b,c]" declares completion
candidates for that argument. Everything after the description line is the
template body; {{name}} placeholders are substituted from prompts/get
arguments.

Environment:
  PROMPTS_DIR            template directory (default: ./prompts if it exists,
                         else the current working directory)
  PROMPTS_MCP_POLL_MS    watcher poll interval in ms (default: 200)
  PROMPTS_MCP_MAX_PROMPTS  prompts/list cap (default: 500)
"""

import json
import os
import re
import sys
import threading
import time

LATEST_PROTOCOL_VERSION = "2026-07-28"
PROTOCOL_VERSIONS = ["2026-07-28", "2026-05-12", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

_default_dir = os.path.join(os.getcwd(), "prompts")
PROMPTS_DIR = os.path.realpath(os.environ.get("PROMPTS_DIR") or (_default_dir if os.path.isdir(_default_dir) else os.getcwd()))
POLL_SECONDS = max(0.02, float(os.environ.get("PROMPTS_MCP_POLL_MS", "200")) / 1000.0)
MAX_PROMPTS = int(os.environ.get("PROMPTS_MCP_MAX_PROMPTS", "500"))

MAX_COMPLETION_VALUES = 100

_write_lock = threading.Lock()
_subscriptions = {}  # listen request id -> {"promptsListChanged": bool}
_stop = threading.Event()


def _emit(payload):
    with _write_lock:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()


def _error(rid, code, message):
    _emit({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def _result(rid, result):
    _emit({"jsonrpc": "2.0", "id": rid, "result": result})


# ---------------------------------------------------------------------------
# Template parsing

class Prompt:
    def __init__(self, name, title, description, arguments, body):
        self.name = name
        self.title = title
        self.description = description
        self.arguments = arguments  # [{name, description, required, choices}]
        self.body = body


_ARG_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}")


def parse_template(name, text):
    """Parse a Markdown template into a Prompt. Never raises; a malformed
    file simply yields a plainer prompt."""
    lines = text.split("\n")
    i = 0
    arguments = []
    # Optional args block
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].strip() == "<!-- args":
        i += 1
        while i < len(lines) and lines[i].strip() != "-->":
            m = _ARG_LINE.match(lines[i].strip())
            if m:
                arg_name, rest = m.group(1), m.group(2)
                choices = None
                cm = re.search(r"\[([^\]]+)\]\s*$", rest)
                if cm:
                    choices = [c.strip() for c in cm.group(1).split(",") if c.strip()]
                    rest = rest[: cm.start()].rstrip()
                required = False
                if rest.endswith("(required)"):
                    required = True
                    rest = rest[: -len("(required)")].rstrip()
                arguments.append({"name": arg_name, "description": rest or None,
                                  "required": required, "choices": choices})
            i += 1
        i += 1  # skip -->
    # Optional title
    while i < len(lines) and not lines[i].strip():
        i += 1
    title = None
    if i < len(lines) and lines[i].startswith("# "):
        title = lines[i][2:].strip() or None
        i += 1
    # Optional description (first non-empty line)
    while i < len(lines) and not lines[i].strip():
        i += 1
    description = None
    if i < len(lines):
        description = lines[i].strip() or None
        i += 1
    # Body: remainder, leading blank lines dropped
    while i < len(lines) and not lines[i].strip():
        i += 1
    body = "\n".join(lines[i:]).rstrip("\n")
    return Prompt(name, title, description, arguments, body)


def _template_paths():
    try:
        names = sorted(os.listdir(PROMPTS_DIR))
    except OSError:
        return []
    out = []
    for fn in names:
        if fn.startswith(".") or not fn.endswith(".md"):
            continue
        p = os.path.join(PROMPTS_DIR, fn)
        if os.path.isfile(p):
            out.append(p)
        if len(out) >= MAX_PROMPTS:
            break
    return out


def load_prompts():
    prompts = []
    for p in _template_paths():
        name = os.path.splitext(os.path.basename(p))[0]
        try:
            with open(p, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        prompts.append(parse_template(name, text))
    return prompts


def _listing_signature():
    sig = []
    for p in _template_paths():
        try:
            st = os.stat(p)
        except OSError:
            continue
        sig.append((os.path.basename(p), st.st_mtime_ns, st.st_size))
    return tuple(sig)


# ---------------------------------------------------------------------------
# Watcher thread (prompts listChanged)

def _watcher():
    prev = _listing_signature()
    while not _stop.is_set():
        time.sleep(POLL_SECONDS)
        with _write_lock:
            subs = {sid: dict(s) for sid, s in _subscriptions.items() if s["promptsListChanged"]}
        sig = _listing_signature()
        had_watchers = prev is not None and bool(subs)
        if subs and prev is not None and sig != prev and had_watchers:
            for sid in subs:
                _emit({
                    "jsonrpc": "2.0",
                    "method": "notifications/prompts/list_changed",
                    "params": {"_meta": {"io.modelcontextprotocol/subscriptionId": sid}},
                })
        if subs:
            prev = sig
        elif prev is None:
            prev = sig


# ---------------------------------------------------------------------------
# Subscriptions (listChanged delivery; see basic/patterns/subscriptions)

def _close_subscription(sid, graceful):
    with _write_lock:
        _subscriptions.pop(sid, None)
    if graceful:
        _result(sid, {"resultType": "complete",
                      "_meta": {"io.modelcontextprotocol/subscriptionId": sid}})


def _handle_listen(rid, params):
    if not isinstance(params, dict):
        _error(rid, -32602, "Invalid params: subscriptions/listen requires a params object")
        return
    nf = params.get("notifications", {})
    if not isinstance(nf, dict):
        _error(rid, -32602, "Invalid params: notifications must be an object")
        return
    for key in ("toolsListChanged", "promptsListChanged", "resourcesListChanged"):
        if key in nf and not isinstance(nf[key], bool):
            _error(rid, -32602, "Invalid params: %s must be a boolean" % key)
            return
    if "resourceSubscriptions" in nf:
        rs = nf["resourceSubscriptions"]
        if not isinstance(rs, list) or not all(isinstance(u, str) for u in rs):
            _error(rid, -32602, "Invalid params: resourceSubscriptions must be an array of strings")
            return

    # This server can only honor promptsListChanged; everything else is
    # omitted from the acknowledgment (spec: unsupported types are omitted).
    honored = {}
    if nf.get("promptsListChanged") is True:
        honored["promptsListChanged"] = True

    # Registration and the ack write hold the same lock, so no watcher
    # notification for this subscription can precede the acknowledgment.
    with _write_lock:
        _subscriptions[rid] = {"promptsListChanged": honored.get("promptsListChanged", False)}
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/subscriptions/acknowledged",
            "params": {
                "_meta": {"io.modelcontextprotocol/subscriptionId": rid},
                "notifications": honored,
            },
        }) + "\n")
        sys.stdout.flush()
    # Long-lived request: no JSON-RPC response until the subscription closes.


def _handle_cancelled(params):
    rid = params.get("requestId") if isinstance(params, dict) else None
    if rid in _subscriptions:
        _close_subscription(rid, graceful=True)


# ---------------------------------------------------------------------------
# Prompts methods

def handle_prompts_list(rid):
    out = []
    for pr in load_prompts():
        entry = {"name": pr.name}
        if pr.title:
            entry["title"] = pr.title
        if pr.description:
            entry["description"] = pr.description
        if pr.arguments:
            entry["arguments"] = [
                {k: v for k, v in
                 (("name", a["name"]), ("description", a["description"]), ("required", a["required"]))
                 if v is not None}
                for a in pr.arguments
            ]
        out.append(entry)
    _result(rid, {"resultType": "complete", "prompts": out})


def handle_prompts_get(rid, params):
    if not isinstance(params, dict):
        _error(rid, -32602, "Invalid params: prompts/get requires a params object")
        return
    name = params.get("name")
    if not isinstance(name, str):
        _error(rid, -32602, "Invalid params: name must be a string")
        return
    args = params.get("arguments", {})
    if not isinstance(args, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in args.items()):
        _error(rid, -32602, "Invalid params: arguments must be an object of string values")
        return
    prompts = {p.name: p for p in load_prompts()}
    pr = prompts.get(name)
    if pr is None:
        _error(rid, -32602, "Invalid params: unknown prompt: %s" % name)
        return
    missing = [a["name"] for a in pr.arguments if a["required"] and a["name"] not in args]
    if missing:
        _error(rid, -32602, "Invalid params: missing required argument(s): %s" % ", ".join(missing))
        return

    def _sub(m):
        key = m.group(1)
        if key in args:
            return args[key]
        # Optional argument not supplied: substitute empty string (documented).
        return ""

    text = _PLACEHOLDER.sub(_sub, pr.body)
    result = {"resultType": "complete",
              "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
    if pr.description:
        result["description"] = pr.description
    _result(rid, result)


# ---------------------------------------------------------------------------
# Completion

def handle_complete(rid, params):
    if not isinstance(params, dict):
        _error(rid, -32602, "Invalid params: completion/complete requires a params object")
        return
    ref = params.get("ref")
    argument = params.get("argument")
    if not isinstance(ref, dict) or ref.get("type") != "ref/prompt" or not isinstance(ref.get("name"), str):
        _error(rid, -32602, "Invalid params: ref must be a ref/prompt with a name")
        return
    if (not isinstance(argument, dict) or not isinstance(argument.get("name"), str)
            or not isinstance(argument.get("value"), str)):
        _error(rid, -32602, "Invalid params: argument must have string name and value")
        return
    prompts = {p.name: p for p in load_prompts()}
    pr = prompts.get(ref["name"])
    if pr is None:
        _error(rid, -32602, "Invalid params: unknown prompt: %s" % ref["name"])
        return
    spec = next((a for a in pr.arguments if a["name"] == argument["name"]), None)
    values = []
    if spec and spec["choices"]:
        prefix = argument["value"].lower()
        values = [c for c in spec["choices"] if c.lower().startswith(prefix)]
    total = len(values)
    values = values[:MAX_COMPLETION_VALUES]
    _result(rid, {"resultType": "complete",
                  "completion": {"values": values, "total": total,
                                 "hasMore": total > len(values)}})


# ---------------------------------------------------------------------------
# Dispatch

def handle_request(req):
    rid = req.get("id")
    method = req.get("method")
    params = req.get("params")

    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        version = requested if requested in PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        _result(rid, {
            "resultType": "complete",
            "protocolVersion": version,
            "capabilities": {
                "prompts": {"listChanged": True},
                "completions": {},
            },
            "serverInfo": {"name": "prompts-dir-mcp", "version": "1.0.0"},
        })
    elif method == "ping":
        _result(rid, {"resultType": "complete"})
    elif method == "prompts/list":
        handle_prompts_list(rid)
    elif method == "prompts/get":
        handle_prompts_get(rid, params)
    elif method == "completion/complete":
        handle_complete(rid, params)
    elif method == "subscriptions/listen":
        _handle_listen(rid, params)
    elif method == "notifications/initialized":
        pass
    elif method == "notifications/cancelled":
        _handle_cancelled(params)
    elif rid is None:
        pass  # unknown notification: ignore
    else:
        _error(rid, -32601, "Method not found: %s" % method)


def main():
    t = threading.Thread(target=_watcher, daemon=True)
    t.start()
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                _error(None, -32700, "Parse error")
                continue
            if not isinstance(req, dict) or "method" not in req:
                _error(req.get("id") if isinstance(req, dict) else None, -32600, "Invalid Request")
                continue
            handle_request(req)
    finally:
        _stop.set()
        for sid in list(_subscriptions):
            _close_subscription(sid, graceful=True)


if __name__ == "__main__":
    main()
