"""Invented, loopback-only OpenAI protocol fixture; never used by production code."""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FixtureProvider:
    """A bounded synthetic provider that retains counts, never prompts or credentials."""

    def __init__(self):
        self.calls = {"extraction": 0, "answer": 0}
        self.mode = "ok"
        self.release = threading.Event()
        self.entered = threading.Event()
        self._lock = threading.Lock()
        self._closed = threading.Event()

    def reset(self):
        with self._lock:
            self.calls.update(extraction=0, answer=0)
        self.mode = "ok"
        self.release.clear()
        self.entered.clear()

    def __enter__(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, status, value):
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Cancellation deliberately closes the client connection.

            def do_POST(self):
                if self.path != "/v1/chat/completions":
                    self.respond(404, {"error": {"message": "fixture route unavailable"}})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 262144:
                        raise ValueError
                    value = json.loads(self.rfile.read(size))
                    messages = value["messages"]
                    user = next(m["content"] for m in messages if m["role"] == "user")
                    if not isinstance(user, str):
                        raise ValueError
                    kind = "answer" if "<source_excerpts>" in user else "extraction"
                    if kind == "answer":
                        raw = user.split("<source_excerpts>", 1)[1].split(
                            "</source_excerpts>", 1,
                        )[0]
                        excerpts = json.loads(raw)
                        first_id = excerpts[0]["id"]
                        if not isinstance(first_id, str) or not first_id:
                            raise ValueError
                    elif "<document>" not in user:
                        raise ValueError
                except (ValueError, KeyError, TypeError, StopIteration, IndexError):
                    self.respond(400, {"error": {"message": "invalid fixture request"}})
                    return
                with fixture._lock:
                    fixture.calls[kind] += 1
                fixture.entered.set()
                if fixture.mode == "pause":
                    if not fixture.release.wait(20) or fixture._closed.is_set():
                        self.respond(400, {"error": {"message": "fixture pause ended"}})
                        return
                if fixture.mode == "fail":
                    self.respond(400, {"error": {"message": "scripted fixture failure"}})
                    return
                if kind == "answer":
                    content = {
                        "answer": "Apricot built Cedar.",
                        "citations": [first_id], "insufficient_evidence": False, "conflicts": [],
                    }
                else:
                    content = {
                        "nodes": [
                            {"id": "apricot", "label": "Apricot", "type": "Concept",
                             "properties": {"summary": "Apricot built Cedar in 2042."}},
                            {"id": "cedar", "label": "Cedar", "type": "Concept",
                             "properties": {"summary": "An invented construction fixture."}},
                        ],
                        "edges": [{"source": "apricot", "target": "cedar",
                                   "relationship": "BUILT", "properties": {
                                       "context": "Apricot built Cedar in 2042."}}],
                    }
                self.respond(200, {
                    "id": "h1-synthetic", "object": "chat.completion", "created": 0,
                    "model": "h1-synthetic", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(content)}}],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60},
                })

        class LoopbackServer(ThreadingHTTPServer):
            def server_bind(self):
                # HTTPServer normally reverse-resolves its address via getfqdn.
                # The fixture needs no DNS at all, even for localhost.
                self.socket.bind(self.server_address)
                self.server_address = self.socket.getsockname()
                self.server_name = "127.0.0.1"
                self.server_port = self.server_address[1]

            address_family = socket.AF_INET

        self._server = LoopbackServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_port
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_args):
        self._closed.set()
        self.release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
