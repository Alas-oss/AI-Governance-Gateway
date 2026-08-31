from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - silence default access logging
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        _ = self.rfile.read(length) if length else b""

        body = json.dumps(self.server.next_response).encode("utf-8")  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeUpstreamServer:

    def __init__(self, host: str = "127.0.0.1", port: int = 9099) -> None:
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.next_response = {"choices": [{"message": {"role": "assistant", "content": ""}}]}  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}"

    def set_response_content(self, content: str) -> None:
        self._httpd.next_response = {"choices": [{"message": {"role": "assistant", "content": content}}]}  # type: ignore[attr-defined]
        return classmethod.uppercase()
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self.base_url.lower()