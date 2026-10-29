from __future__ import annotations

from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit

from .engine import ContractError, MAX_HTML, extract, json_object, load_rules

ROOT = Path(__file__).resolve().parent
MAX_BODY = 3 * 1024 * 1024
HEADER_BUDGET = 10
BODY_BUDGET = 10
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer", "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class Source:
    def __init__(self, rules, path=None, remote=None):
        self.rules = load_rules(rules)
        self.path, self.remote = path, remote
        self.lock = threading.Lock()

    def result(self):
        with self.lock:
            if self.remote:
                html = self.remote.read()
                return extract(html, self.rules, source_kind="https", source_url=self.remote.url, retrieved_at=self.remote.retrieved_at)
            try:
                with Path(self.path).open("rb") as stream:
                    body = stream.read(MAX_HTML + 1)
                if len(body) > MAX_HTML:
                    raise ContractError("html_limit", "Configured source exceeds 1 MB.")
                return extract(body.decode("utf-8-sig"), self.rules, source_kind="local")
            except (OSError, UnicodeError) as exc:
                raise ContractError("source", "Configured source is unavailable or is not UTF-8 text.") from exc


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, source, demo_html, demo_rules):
        self.connections = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)
        self.source, self.demo_html, self.demo_rules = source, demo_html, demo_rules
        self.token = secrets.token_urlsafe(32)
        self.jobs = threading.BoundedSemaphore(2)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def handle_error(self, *_):
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "SiteContractAPI"
    sys_version = ""

    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(5)
        self.header_lock = threading.Lock()
        self.headers_pending = True
        self.headers_expired = False
        self.header_timer = threading.Timer(HEADER_BUDGET, self.abort_headers)
        self.header_timer.daemon = True
        self.header_timer.start()

    def abort_headers(self):
        with self.header_lock:
            if not self.headers_pending:
                return
            self.headers_expired = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def stop_header_timer(self):
        with self.header_lock:
            self.headers_pending = False
            expired = self.headers_expired
        self.header_timer.cancel()
        return expired

    def parse_request(self):
        try:
            parsed = super().parse_request()
        finally:
            expired = self.stop_header_timer()
        if expired:
            self.close_connection = True
            return False
        if parsed and self.request_version not in {"HTTP/1.0", "HTTP/1.1"}:
            self.send_error(505)
            return False
        return parsed

    def finish(self):
        self.stop_header_timer()
        super().finish()

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        if getattr(self, "request_version", "HTTP/0.9") == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
        status = code if isinstance(code, int) and 400 <= code <= 599 else 500
        self.send_json(status, {"ok": False, "error": "http_request", "message": "The HTTP request was rejected."})

    def unsupported(self):
        if self.guard():
            self.send_json(405, {"ok": False, "error": "method", "message": "Only GET and POST are supported."})

    do_HEAD = unsupported
    do_OPTIONS = unsupported
    do_PUT = unsupported
    do_DELETE = unsupported
    do_PATCH = unsupported
    do_TRACE = unsupported
    do_CONNECT = unsupported

    def send_body(self, status, body: bytes, content_type="application/json", cookie=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in HEADERS.items():
            self.send_header(key, value)
        if cookie:
            self.send_header("Set-Cookie", f"sca_csrf={self.server.token}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, value):
        self.send_body(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode())

    def guard(self, mutation=False):
        if len(self.headers) > 32 or sum(len(k) + len(v) for k, v in self.headers.items()) > 8192:
            self.send_json(431, {"ok": False, "error": "headers", "message": "Request headers exceed the limit."})
            return False
        if self.headers.get("Transfer-Encoding") is not None or any(len(self.headers.get_all(name, [])) > 1 for name in ("Host", "Origin", "Content-Length", "Content-Type")):
            self.send_json(400, {"ok": False, "error": "headers", "message": "Ambiguous request headers."})
            return False
        expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        host = self.headers.get("Host", "").lower()
        if host not in expected:
            self.send_json(400, {"ok": False, "error": "host", "message": "Host not allowed."})
            return False
        if "?" in self.path or "#" in self.path:
            self.send_json(400, {"ok": False, "error": "parameters", "message": "This service does not accept URL parameters."})
            return False
        if mutation:
            try:
                origin = self.headers.get("Origin", "")
                parsed = urlsplit(origin)
                cookie = SimpleCookie()
                cookie.load(self.headers.get("Cookie", ""))
                supplied = self.headers.get("X-CSRF-Token", "")
                valid = (origin.lower() == "http://" + host and parsed.path == "" and parsed.query == "" and parsed.fragment == ""
                         and self.headers.get("Sec-Fetch-Site", "same-origin") == "same-origin"
                         and bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", supplied))
                         and secrets.compare_digest(supplied, self.server.token)
                         and cookie.get("sca_csrf") is not None
                         and secrets.compare_digest(cookie["sca_csrf"].value, self.server.token))
            except (CookieError, ValueError, TypeError):
                valid = False
            if not valid:
                self.send_json(403, {"ok": False, "error": "origin", "message": "Same-origin request and CSRF token required."})
                return False
        return True

    def do_GET(self):
        if not self.guard():
            return
        if self.path == "/":
            page = (ROOT / "static" / "index.html").read_text(encoding="utf-8").replace("{{CSRF}}", self.server.token)
            self.send_body(200, page.encode(), "text/html; charset=utf-8", cookie=True)
        elif self.path in {"/static/app.js", "/static/styles.css"}:
            mime = "text/javascript" if self.path.endswith(".js") else "text/css"
            self.send_body(200, (ROOT / self.path.lstrip("/")).read_bytes(), mime)
        elif self.path == "/api/demo":
            self.send_json(200, {"html": self.server.demo_html, "rules": self.server.demo_rules})
        elif self.path == "/api/status":
            self.send_json(200, {"mode": "configured HTTPS" if self.server.source.remote else "offline", "endpoint": "/api/data"})
        elif self.path == "/healthz":
            self.send_json(200, {"ok": True})
        elif self.path == "/api/data":
            if not self.server.jobs.acquire(blocking=False):
                self.send_json(503, {"ok": False, "error": "busy", "message": "Processing slots are busy. Try again."})
                return
            try:
                self.send_json(200, self.server.source.result())
            except ContractError as exc:
                self.send_json(409 if exc.code == "schema_drift" else 502, exc.as_dict())
            finally:
                self.server.jobs.release()
        else:
            self.send_json(404, {"ok": False, "error": "not_found", "message": "Route not found."})

    def do_POST(self):
        if not self.guard(mutation=True):
            return
        if self.path != "/api/preview":
            self.send_json(404, {"ok": False, "error": "not_found", "message": "Route not found."})
            return
        if not self.server.jobs.acquire(blocking=False):
            self.send_json(503, {"ok": False, "error": "busy", "message": "Processing slots are busy. Try again."})
            return
        try:
            length = self.headers.get("Content-Length", "")
            if not re.fullmatch(r"[0-9]{1,10}", length) or int(length) > MAX_BODY:
                self.send_json(413, {"ok": False, "error": "body_limit", "message": "A bounded Content-Length is required, up to 3 MB."})
                return
            if self.headers.get("Content-Type", "").lower() != "application/json":
                self.send_json(415, {"ok": False, "error": "content_type", "message": "Expected application/json."})
                return
            started, body, remaining = time.monotonic(), bytearray(), int(length)
            while remaining:
                budget = BODY_BUDGET - (time.monotonic() - started)
                if budget <= 0:
                    raise TimeoutError()
                self.connection.settimeout(min(5, budget))
                chunk = self.rfile.read1(min(65536, remaining))
                if not chunk:
                    raise ValueError()
                body.extend(chunk)
                remaining -= len(chunk)
            value = json.loads(body, object_pairs_hook=json_object)
            if not isinstance(value, dict) or set(value) != {"html", "rules"}:
                raise ValueError()
            self.send_json(200, extract(value["html"], value["rules"]))
        except ContractError as exc:
            self.send_json(422, exc.as_dict())
        except TimeoutError:
            self.send_json(408, {"ok": False, "error": "timeout", "message": "Upload timed out."})
        except (ValueError, UnicodeError, RecursionError):
            self.send_json(400, {"ok": False, "error": "json", "message": "Expected a valid HTML and rules JSON object."})
        finally:
            self.server.jobs.release()
