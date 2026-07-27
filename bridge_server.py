#!/usr/bin/env python3
"""AI Chat Bridge — local server (stdlib only, no pip install needed).

Architecture:
    your code / CLI --HTTP-->  this server  --WebSocket-->  Chrome extension
                              (127.0.0.1:8788)            (ws://127.0.0.1:8765/ws)

HTTP API (send header  X-Bridge-Token: <token> , token saved in bridge_token.txt):
    GET  /health                      -> {"extension_connected": true/false}
    POST /ask     {"site":"chatgpt","prompt":"...","timeoutSec":240}
    POST /command {"action":"status|read|new_chat|open|navigate","site":...,"url":...}

Run:  python bridge_server.py
"""
import base64
import hashlib
import json
import secrets
import socket
import struct
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WS_HOST, WS_PORT = "127.0.0.1", 8765
HTTP_HOST, HTTP_PORT = "127.0.0.1", 8788
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TOKEN_FILE = Path(__file__).with_name("bridge_token.txt")
CMD_TIMEOUT_DEFAULT = 300  # seconds


# --------------------------------------------------------------------------
# Token
# --------------------------------------------------------------------------
def load_or_create_token() -> str:
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    tok = secrets.token_hex(16)
    TOKEN_FILE.write_text(tok, encoding="utf-8")
    return tok


TOKEN = load_or_create_token()


# --------------------------------------------------------------------------
# Minimal RFC6455 WebSocket server (text frames; client->server frames masked)
# --------------------------------------------------------------------------
def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def ws_send_text(conn: socket.socket, text: str) -> None:
    payload = text.encode("utf-8")
    header = bytearray([0x81])  # FIN + text opcode
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    conn.sendall(bytes(header) + payload)


def ws_recv_message(conn: socket.socket):
    """Returns (opcode, payload-bytes) for a complete (possibly fragmented) message.
    opcode: 0x1 text, 0x8 close, 0x9 ping, 0xA pong."""
    fragments = bytearray()
    msg_opcode = None
    while True:
        b1, b2 = _recv_exact(conn, 2)
        fin = b1 & 0x80
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", _recv_exact(conn, 2))
        elif length == 127:
            (length,) = struct.unpack(">Q", _recv_exact(conn, 8))
        mask = _recv_exact(conn, 4) if masked else None
        payload = bytearray(_recv_exact(conn, length)) if length else bytearray()
        if mask:
            for i in range(len(payload)):
                payload[i] ^= mask[i % 4]

        if opcode == 0x9:  # ping -> pong
            _send_frame(conn, 0xA, bytes(payload))
            continue
        if opcode == 0xA:  # pong
            continue
        if opcode == 0x8:  # close
            return (0x8, bytes(payload))
        if opcode in (0x1, 0x2):
            msg_opcode = opcode
            fragments += payload
        elif opcode == 0x0 and msg_opcode is not None:  # continuation
            fragments += payload
        if fin and msg_opcode is not None:
            return (msg_opcode, bytes(fragments))


def _send_frame(conn: socket.socket, opcode: int, payload: bytes) -> None:
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    conn.sendall(bytes(header) + payload)


# --------------------------------------------------------------------------
# Extension hub: tracks the one connected extension + pending command replies
# --------------------------------------------------------------------------
class ExtensionHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._conn = None
        self._pending = {}  # id -> {"event": Event, "response": dict|None}

    def set_connection(self, conn):
        with self._lock:
            self._conn = conn

    def clear_connection(self, conn):
        with self._lock:
            if self._conn is conn:
                self._conn = None
            for p in self._pending.values():
                if p["response"] is None:
                    p["response"] = {"ok": False, "error": "extension disconnected"}
                    p["event"].set()

    def is_connected(self) -> bool:
        with self._lock:
            return self._conn is not None

    def handle_reply(self, msg: dict):
        mid = msg.get("id")
        if not mid:
            return
        with self._lock:
            p = self._pending.get(mid)
            if p:
                p["response"] = msg
                p["event"].set()

    def send_command(self, action: str, timeout: float = CMD_TIMEOUT_DEFAULT, **fields) -> dict:
        with self._lock:
            conn = self._conn
        if conn is None:
            return {"ok": False, "error": "extension not connected (open Chrome with the AI Chat Bridge extension)"}
        mid = uuid.uuid4().hex
        entry = {"event": threading.Event(), "response": None}
        with self._lock:
            self._pending[mid] = entry
        try:
            ws_send_text(conn, json.dumps({"id": mid, "action": action, **fields}, ensure_ascii=False))
        except OSError as e:
            with self._lock:
                self._pending.pop(mid, None)
            return {"ok": False, "error": f"failed to send to extension: {e}"}
        if not entry["event"].wait(timeout):
            with self._lock:
                self._pending.pop(mid, None)
            return {"ok": False, "error": f"timeout after {timeout}s waiting for extension reply"}
        with self._lock:
            self._pending.pop(mid, None)
        return entry["response"] or {"ok": False, "error": "empty reply"}


HUB = ExtensionHub()


def heartbeat_loop(conn: socket.socket, stop: threading.Event):
    """Push a heartbeat every 15s. Incoming WebSocket traffic resets the MV3
    service-worker idle timer, keeping the extension alive during long asks."""
    while not stop.wait(15):
        try:
            ws_send_text(conn, json.dumps({"type": "heartbeat", "at": time.time()}))
        except OSError:
            return


def ws_client_loop(conn: socket.socket):
    HUB.set_connection(conn)
    print(f"[bridge] extension connected from {conn.getpeername()}", flush=True)
    stop = threading.Event()
    hb = threading.Thread(target=heartbeat_loop, args=(conn, stop), daemon=True)
    hb.start()
    try:
        while True:
            opcode, payload = ws_recv_message(conn)
            if opcode == 0x8:
                break
            if opcode != 0x1:
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if msg.get("type") in ("hello", "ping", "pong"):
                continue
            HUB.handle_reply(msg)
    except (ConnectionError, OSError):
        pass
    finally:
        stop.set()
        HUB.clear_connection(conn)
        try:
            conn.close()
        except OSError:
            pass
        print("[bridge] extension disconnected", flush=True)


def websocket_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((WS_HOST, WS_PORT))
    srv.listen(5)
    print(f"[bridge] WebSocket listening on ws://{WS_HOST}:{WS_PORT}/ws", flush=True)
    while True:
        conn, _addr = srv.accept()
        conn.settimeout(None)
        try:
            handshake_done = handle_ws_handshake(conn)
        except (ConnectionError, OSError):
            handshake_done = False
        if handshake_done:
            threading.Thread(target=ws_client_loop, args=(conn,), daemon=True).start()
        else:
            try:
                conn.close()
            except OSError:
                pass


def handle_ws_handshake(conn: socket.socket) -> bool:
    conn.settimeout(10)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return False
        data += chunk
        if len(data) > 65536:
            return False
    head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = head.split("\r\n")
    if not lines or "upgrade" not in head.lower() or "websocket" not in head.lower():
        return False
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    key = headers.get("sec-websocket-key")
    if not key:
        return False
    accept = base64.b64encode(hashlib.sha1((key + WS_MAGIC).encode()).digest()).decode()
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    )
    conn.sendall(resp.encode("latin-1"))
    conn.settimeout(None)
    return True


# --------------------------------------------------------------------------
# Server-side ask orchestration.
# The ask loop lives HERE (not in the extension) as a sequence of short
# commands, so an MV3 service-worker restart mid-generation does not kill the
# whole ask — polling just resumes after the extension reconnects, and the AI
# site keeps generating in the tab regardless.
# --------------------------------------------------------------------------
def orchestrate_ask(body: dict) -> dict:
    site = body.get("site", "")
    prompt = body.get("prompt", "")
    timeout = float(body.get("timeoutSec", CMD_TIMEOUT_DEFAULT))
    selectors = body.get("selectors")
    model = body.get("model")
    fresh = body.get("fresh")

    if fresh:
        HUB.send_command("restart", timeout=45, site=site)
    if model:
        mres = HUB.send_command("set_model", timeout=25, site=site, model=model, selectors=selectors)
        model_info = mres.get("result") if mres.get("ok") else {"ok": False, "error": mres.get("error")}
    else:
        model_info = None

    baseline = 0
    c = HUB.send_command("count", timeout=30, site=site, selectors=selectors)
    if c.get("ok"):
        baseline = int(c["result"].get("count", 0) or 0)

    s = HUB.send_command("send_prompt", timeout=30, site=site, prompt=prompt, selectors=selectors)
    if not s.get("ok"):
        return s
    via = s["result"].get("via", "?")
    if not s["result"].get("sent"):
        return {"ok": False, "error": "send failed: " + str(s["result"].get("error", "unknown"))}

    def result_payload(text, completed, warning=None):
        p = {
            "site": site,
            "promptChars": len(prompt),
            "replyChars": len(text),
            "text": text,
            "completed": completed,
            "model": model_info,
        }
        if warning:
            p["warning"] = warning
        return p

    deadline = time.time() + timeout
    send_at = time.time()
    saw_new = False
    last_text = ""
    stable = 0
    while time.time() < deadline:
        # Adaptive polling: 1s cadence for the first 90s (fast answers come back
        # quickly), then relax to 5s for long thinking runs. The timeout is only
        # a safety cap — completion returns immediately once detected.
        time.sleep(5.0 if time.time() - send_at > 90 else 1.0)
        st = HUB.send_command("poll_state", timeout=25, site=site, selectors=selectors)
        if not st.get("ok"):
            continue  # transient (SW restart / tab navigating); keep polling
        p = st.get("result") or {}
        count = int(p.get("count", 0) or 0)
        text = p.get("text") or ""
        gen = bool(p.get("generating"))
        if count > baseline:
            saw_new = True
        # Fail fast only when nothing is generating at all.
        if not saw_new and not gen and time.time() - send_at > 20:
            return {"ok": True, "result": result_payload(
                "", False,
                f"no reply and no generating indicator within 20s (send via {via}) — prompt may not have been submitted")}
        if saw_new and not gen and text and text == last_text:
            stable += 1
            need = 3 if time.time() - send_at <= 90 else 2  # ~3s vs ~10s of stable text
            if stable >= need:
                return {"ok": True, "result": result_payload(text, True)}
        else:
            stable = 0
            last_text = text

    return {"ok": True, "result": result_payload(
        last_text, False, "timeout waiting for generation to finish; returning partial/last text")}


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------
class ApiHandler(BaseHTTPRequestHandler):
    server_version = "AIChatBridge/1.0"

    def log_message(self, fmt, *args):  # quieter logs
        print("[http] " + fmt % args, flush=True)

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return self.headers.get("X-Bridge-Token", "") == TOKEN

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"extension_connected": HUB.is_connected(), "time": time.time()})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._json(401, {"ok": False, "error": "missing/invalid X-Bridge-Token (see bridge_token.txt)"})
            return
        body = self._read_body()

        if self.path == "/ask":
            site = body.get("site", "")
            prompt = body.get("prompt", "")
            timeout = float(body.get("timeoutSec", CMD_TIMEOUT_DEFAULT))
            if not site or not prompt:
                self._json(400, {"ok": False, "error": "site and prompt are required"})
                return
            # Server-side orchestration: resumable across SW restarts.
            resp = orchestrate_ask(body)
            self._json(200 if resp.get("ok") else 502, resp)
            return

        if self.path == "/command":
            action = body.get("action", "")
            timeout = float(body.get("timeoutSec", 60))
            fields = {k: v for k, v in body.items() if k not in ("action", "timeoutSec")}
            resp = HUB.send_command(action, timeout=timeout, **fields)
            self._json(200 if resp.get("ok") else 502, resp)
            return

        self._json(404, {"ok": False, "error": "not found"})


def main():
    t = threading.Thread(target=websocket_server, daemon=True)
    t.start()
    httpd = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), ApiHandler)
    print(f"[bridge] HTTP API listening on http://{HTTP_HOST}:{HTTP_PORT}", flush=True)
    print(f"[bridge] auth token (also in {TOKEN_FILE.name}): {TOKEN}", flush=True)
    print("[bridge] ready. Load the extension in Chrome, then use bridge_cli.py or curl.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] bye", flush=True)


if __name__ == "__main__":
    main()
