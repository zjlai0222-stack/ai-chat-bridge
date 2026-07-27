#!/usr/bin/env python3
"""AI Chat Bridge — command line client.

Examples:
    python bridge_cli.py health
    python bridge_cli.py status chatgpt
    python bridge_cli.py ask gemini "用三句話解釋量子糾纏"
    python bridge_cli.py ask qwen "幫我寫一首關於秋天的短詩" --timeout 300
    python bridge_cli.py ask chatgpt --file prompt.txt
    python bridge_cli.py read gemini
    python bridge_cli.py new chatgpt
    python bridge_cli.py open qwen
"""
import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

BASE = "http://127.0.0.1:8788"
TOKEN_FILE = Path(__file__).with_name("bridge_token.txt")


def get_token(cli_token=None) -> str:
    if cli_token:
        return cli_token
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    return ""


def http(method: str, path: str, body: dict = None, token: str = ""):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("X-Bridge-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=360) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"cannot reach bridge server at {BASE} ({e.reason}). Start it with: python bridge_server.py"}


def main():
    ap = argparse.ArgumentParser(description="AI Chat Bridge CLI")
    ap.add_argument("command", choices=["health", "status", "ask", "read", "new", "open", "restart", "eval"])
    ap.add_argument("site", nargs="?", choices=["chatgpt", "gemini", "qwen"], help="target site")
    ap.add_argument("prompt", nargs="?", help="prompt text (ask) or JS expression (eval)")
    ap.add_argument("--file", help="read prompt from file (UTF-8)")
    ap.add_argument("--timeout", type=float, default=600, help="safety cap in seconds (ask returns as soon as the reply completes; default 600)")
    ap.add_argument("--selectors", help="JSON object overriding site selectors for this call")
    ap.add_argument("--fresh", action="store_true", help="close all tabs of the site and open a fresh one before asking")
    ap.add_argument("--model", help="model label to select before asking, e.g. '延伸思考' or 'Pro' (Gemini)")
    ap.add_argument("--token", help="bridge token (default: bridge_token.txt)")
    ap.add_argument("--json", action="store_true", help="print full JSON response")
    args = ap.parse_args()

    token = get_token(args.token)

    selectors = None
    if args.selectors:
        try:
            selectors = json.loads(args.selectors)
        except json.JSONDecodeError:
            print("[error] --selectors must be valid JSON", file=sys.stderr)
            sys.exit(2)

    if args.command == "health":
        print(json.dumps(http("GET", "/health"), ensure_ascii=False, indent=2))
        return

    if args.command == "ask":
        prompt = args.prompt
        if args.file:
            prompt = Path(args.file).read_text(encoding="utf-8")
        elif prompt is None and not sys.stdin.isatty():
            prompt = sys.stdin.read()
        if not args.site or not prompt or not prompt.strip():
            print("usage: bridge_cli.py ask <site> <prompt>  (or --file / stdin)", file=sys.stderr)
            sys.exit(2)
        body = {"site": args.site, "prompt": prompt, "timeoutSec": args.timeout}
        if selectors is not None:
            body["selectors"] = selectors
        if args.fresh:
            body["fresh"] = True
        if args.model:
            body["model"] = args.model
        resp = http("POST", "/ask", body, token)
        if args.json:
            print(json.dumps(resp, ensure_ascii=False, indent=2))
            return
        if resp.get("ok") and "result" in resp:
            r = resp["result"]
            print(r.get("text", ""))
            if r.get("warning"):
                print(f"\n[warning] {r['warning']}", file=sys.stderr)
        else:
            print(f"[error] {resp.get('error', resp)}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "eval":
        if not args.site or not args.prompt:
            print("usage: bridge_cli.py eval <site> \"<js expression>\"", file=sys.stderr)
            sys.exit(2)
        resp = http("POST", "/command", {"action": "eval", "site": args.site, "expression": args.prompt, "timeoutSec": args.timeout}, token)
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        if not resp.get("ok"):
            sys.exit(1)
        return

    action_map = {"status": "status", "read": "read", "new": "new_chat", "open": "open", "restart": "restart"}
    action = action_map[args.command]
    if not args.site:
        print(f"usage: bridge_cli.py {args.command} <site>", file=sys.stderr)
        sys.exit(2)
    body = {"action": action, "site": args.site, "timeoutSec": args.timeout}
    if selectors is not None:
        body["selectors"] = selectors
    resp = http("POST", "/command", body, token)
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    if not resp.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
