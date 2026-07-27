#!/usr/bin/env python3
"""AI Chat Bridge — MCP (Model Context Protocol) stdio server.

Wraps the bridge HTTP API (bridge_server.py must be running) as MCP tools so
MCP-capable agents (Kilo Code, Cline, Roo Code, Claude Desktop, ...) can call
ChatGPT / Gemini / Qwen directly.

Transport: stdio, newline-delimited JSON-RPC 2.0 (one message per line).
Logs go to stderr only; stdout is reserved for protocol messages.

Tools:
    ask_ai(site, prompt, timeoutSec?)  -> send a prompt, wait for the reply text
    read_last_reply(site)              -> read the last AI reply on the tab
    ai_tab_status(site)                -> tab/composer/message status
    new_ai_chat(site)                  -> start a fresh conversation
"""
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

BASE = "http://127.0.0.1:8788"
TOKEN_FILE = Path(__file__).with_name("bridge_token.txt")

SITES = ["chatgpt", "gemini", "qwen"]

TOOLS = [
    {
        "name": "ask_ai",
        "description": (
            "Send a prompt to an AI chat website (currently only 'gemini' is verified) running in the "
            "user's logged-in browser tab, wait for the answer to finish generating, and return the reply "
            "text. Use this to get a second opinion from another AI model. "
            "Tips: set fresh=true for a clean conversation; use model='3.6 Flash' for simple questions "
            "(10-20s) and model='延伸思考' for complex ones (1-4min)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "site": {"type": "string", "enum": SITES, "description": "Target AI site (gemini verified)"},
                "prompt": {"type": "string", "description": "The prompt to send"},
                "fresh": {"type": "boolean", "description": "Close all tabs of the site and open a fresh one first (recommended)"},
                "model": {"type": "string", "description": "Gemini model label, e.g. '3.6 Flash' or '延伸思考'"},
                "timeoutSec": {"type": "number", "description": "Safety cap seconds; the call returns as soon as the reply completes (default 600)"},
            },
            "required": ["site", "prompt"],
        },
    },
    {
        "name": "read_last_reply",
        "description": "Read the most recent AI reply currently shown in the chat tab of the given site.",
        "inputSchema": {
            "type": "object",
            "properties": {"site": {"type": "string", "enum": SITES}},
            "required": ["site"],
        },
    },
    {
        "name": "ai_tab_status",
        "description": "Check whether the given AI site tab is open, the composer was found (logged in), and message count.",
        "inputSchema": {
            "type": "object",
            "properties": {"site": {"type": "string", "enum": SITES}},
            "required": ["site"],
        },
    },
    {
        "name": "restart_ai_tab",
        "description": "Close every tab of the given AI site and open a fresh one (clean slate before a new task).",
        "inputSchema": {
            "type": "object",
            "properties": {"site": {"type": "string", "enum": SITES}},
            "required": ["site"],
        },
    },
]


def log(msg: str):
    print(f"[mcp-bridge] {msg}", file=sys.stderr, flush=True)


def get_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    return ""


def http_post(path: str, body: dict) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("X-Bridge-Token", get_token())
    try:
        with urllib.request.urlopen(req, timeout=420) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"bridge server unreachable at {BASE} ({e.reason}). Start it with: python bridge_server.py"}


def tool_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(name: str, args: dict) -> dict:
    site = args.get("site", "")
    if name == "ask_ai":
        timeout = float(args.get("timeoutSec") or 600)
        body = {"site": site, "prompt": args.get("prompt", ""), "timeoutSec": timeout}
        if args.get("fresh"):
            body["fresh"] = True
        if args.get("model"):
            body["model"] = args["model"]
        resp = http_post("/ask", body)
        if resp.get("ok") and "result" in resp:
            r = resp["result"]
            text = r.get("text", "")
            model_info = r.get("model")
            if isinstance(model_info, dict) and model_info.get("ok") is False:
                text = f"[model switch failed: {model_info.get('error')}]\n\n" + text
            if r.get("warning"):
                text += f"\n\n[bridge warning] {r['warning']}"
            return tool_result(text or "(empty reply)")
        return tool_result(f"ask_ai failed: {resp.get('error', resp)}", is_error=True)

    if name == "read_last_reply":
        resp = http_post("/command", {"action": "read", "site": site})
        if resp.get("ok"):
            return tool_result(resp["result"].get("text", "") or "(empty)")
        return tool_result(f"read_last_reply failed: {resp.get('error', resp)}", is_error=True)

    if name == "ai_tab_status":
        resp = http_post("/command", {"action": "status", "site": site})
        return tool_result(json.dumps(resp.get("result", resp), ensure_ascii=False, indent=2),
                           is_error=not resp.get("ok", False))

    if name == "restart_ai_tab":
        resp = http_post("/command", {"action": "restart", "site": site, "timeoutSec": 45})
        return tool_result(json.dumps(resp.get("result", resp), ensure_ascii=False),
                           is_error=not resp.get("ok", False))

    return tool_result(f"unknown tool: {name}", is_error=True)


def handle(req: dict):
    method = req.get("method", "")
    rid = req.get("id")

    if method == "initialize":
        client_ver = (req.get("params") or {}).get("protocolVersion", "2024-11-05")
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": client_ver,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ai-chat-bridge", "version": "1.0.0"},
        }}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = req.get("params") or {}
        result = call_tool(params.get("name", ""), params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    if rid is None:
        return None  # notification: no reply
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def main():
    log("MCP stdio server started")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            resp = handle(req)
        except Exception as e:
            log(f"handler error: {e}")
            if req.get("id") is not None:
                resp = {"jsonrpc": "2.0", "id": req["id"],
                        "error": {"code": -32603, "message": str(e)}}
            else:
                resp = None
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
