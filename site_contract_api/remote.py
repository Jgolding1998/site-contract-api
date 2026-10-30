"""Opt-in HTTPS reads to one pinned public destination. No browser-supplied URLs."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import queue
import threading
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit
from urllib.robotparser import RobotFileParser

from .engine import ContractError, MAX_HTML, UTF8_BOM, decode_html

USER_AGENT = "SiteContractAPI/1.0"
MIN_INTERVAL = 60
MAX_ROBOTS = 64 * 1024
# A leading byte-order mark is removed before the HTML byte limit applies.
MAX_PAGE = MAX_HTML + len(UTF8_BOM)
HTTPS_BUDGET = 10
DNS_BUDGET = 3
DNS_SLOT = threading.BoundedSemaphore(1)
IPV6_PUBLIC_UNICAST = ipaddress.ip_network("2000::/3")
IPV6_SPECIAL = tuple(ipaddress.ip_network(value) for value in ("2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20"))


class RemoteError(ContractError):
    pass


def validate_target(url: str, allow_host: str) -> tuple[str, str]:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if (not isinstance(allow_host, str) or host is None or host != allow_host.lower() or allow_host != allow_host.lower()
                or parts.scheme != "https" or parts.port not in (None, 443) or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment or len(url) > 2048
                or any(char.isspace() or ord(char) < 32 for char in url) or "\\" in url or "%" in host):
            raise ValueError()
        path = parts.path or "/"
        if any(segment in {".", ".."} for segment in unquote(path).split("/")):
            raise ValueError()
        (host + path).encode("ascii")
        return host, path
    except (ValueError, UnicodeError, TypeError) as exc:
        raise RemoteError("remote_target", "Use an HTTPS URL with an exact lowercase --allow-host, port 443, an ASCII path without dot segments and no credentials, query or fragment.") from exc


def public_addresses(host: str) -> list[str]:
    if not DNS_SLOT.acquire(blocking=False):
        raise RemoteError("remote_dns", "A previous DNS lookup is still running.")
    channel = queue.Queue(maxsize=1)

    def resolve():
        try:
            channel.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except OSError:
            channel.put(None)
        finally:
            DNS_SLOT.release()

    try:
        threading.Thread(target=resolve, daemon=True).start()
    except RuntimeError as exc:
        DNS_SLOT.release()
        raise RemoteError("remote_dns", "The DNS resolver could not start.") from exc
    try:
        entries = channel.get(timeout=DNS_BUDGET)
        if entries is None:
            raise ValueError()
        addresses = sorted({entry[4][0] for entry in entries})
        if not addresses:
            raise ValueError()
        for address in addresses:
            parsed = ipaddress.ip_address(address)
            effective, special = parsed, False
            if isinstance(parsed, ipaddress.IPv6Address):
                if parsed.ipv4_mapped:
                    effective = parsed.ipv4_mapped
                else:
                    special = (parsed.is_site_local or parsed not in IPV6_PUBLIC_UNICAST
                               or any(parsed in network for network in IPV6_SPECIAL))
            if not effective.is_global or effective.is_multicast or effective.is_reserved or special:
                raise ValueError()
        return addresses
    except (OSError, ValueError, queue.Empty) as exc:
        raise RemoteError("remote_dns", "The destination must resolve exclusively to public addresses.") from exc


class PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        remaining = self.timeout if not hasattr(self, "deadline") else self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        raw = socket.create_connection((self.address, 443), timeout=min(self.timeout, remaining))
        try:
            if hasattr(self, "deadline"):
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                raw.settimeout(min(self.timeout, remaining))
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            cancelled = getattr(self, "cancelled", None)
            if cancelled is not None and cancelled.is_set():
                self.sock.shutdown(socket.SHUT_RDWR)
                self.sock.close()
                raise TimeoutError()
        except BaseException:
            raw.close()
            raise


def bounded_get(host: str, address: str, path: str, limit: int) -> tuple[int, bytes, str]:
    connection = PinnedConnection(host, address, timeout=5)
    started = time.monotonic()
    expired = threading.Event()
    finished = threading.Event()
    lifecycle = threading.Lock()
    transport = [None]
    response = None
    connection.deadline = started + HTTPS_BUDGET
    connection.cancelled = expired

    def abort():
        with lifecycle:
            if finished.is_set():
                return
            expired.set()
            active = transport[0] or connection.sock
            if active:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

    def check_deadline():
        if expired.is_set() or time.monotonic() >= started + HTTPS_BUDGET:
            raise RemoteError("remote_timeout", "Remote read exceeded its time budget.")

    timer = threading.Timer(HTTPS_BUDGET, abort)
    timer.daemon = True
    timer.start()
    try:
        connection.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Accept": "text/html, text/plain;q=0.8", "Connection": "close"})
        with lifecycle:
            transport[0] = connection.sock
        check_deadline()
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise RemoteError("remote_redirect", "Redirects are disabled; configure the final authorized URL.")
        if len(response.headers.as_bytes()) > 8192 or response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise RemoteError("remote_response", "Unsupported or excessive response headers.")
        declared = response.getheader("Content-Length")
        if declared is not None and (not declared.isdigit() or len(declared) > 20):
            raise RemoteError("remote_response", "Remote response has an invalid Content-Length.")
        if declared is not None and int(declared) > limit:
            raise RemoteError("remote_limit", f"Remote response declares {int(declared):,} bytes; the limit is {limit:,} bytes.",
                              [{"code": "remote_bytes", "limit": limit, "actual": int(declared)}])
        chunks, received = [], 0
        while True:
            remaining = HTTPS_BUDGET - (time.monotonic() - started)
            if expired.is_set() or remaining <= 0:
                raise RemoteError("remote_timeout", "Remote read exceeded its time budget.")
            if response.isclosed():
                break
            if transport[0]:
                transport[0].settimeout(min(5, remaining))
            chunk = response.read1(min(16 * 1024, limit + 1 - received))
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
            if received > limit:
                raise RemoteError("remote_limit", f"Remote response is larger than the {limit:,}-byte limit.",
                                  [{"code": "remote_bytes", "limit": limit}])
        check_deadline()
        if declared is not None and received != int(declared):
            raise RemoteError("remote_response", "Remote response was incomplete.")
        return response.status, b"".join(chunks), response.getheader("Content-Type", "")
    except (OSError, http.client.HTTPException, ValueError, AttributeError) as exc:
        if isinstance(exc, RemoteError):
            raise
        if isinstance(exc, TimeoutError) or expired.is_set() or time.monotonic() >= started + HTTPS_BUDGET:
            raise RemoteError("remote_timeout", "Remote read exceeded its time budget.") from exc
        raise RemoteError("remote_failure", "HTTPS read failed. No destination or response content was logged.") from exc
    finally:
        with lifecycle:
            finished.set()
        timer.cancel()
        if response is not None:
            response.close()
        connection.close()


class RemoteSource:
    def __init__(self, url: str, allow_host: str, interval: int = MIN_INTERVAL):
        self.host, self.path = validate_target(url, allow_host)
        if type(interval) is not int or not MIN_INTERVAL <= interval <= 86400:
            raise RemoteError("remote_interval", "Refresh interval must be 60 to 86400 seconds.")
        self.url, self.interval = url, interval
        self.last_attempt = None
        self.cached = None
        self.last_error = None
        self.retrieved_at = None
        self.lock = threading.Lock()

    def read(self) -> str:
        with self.lock:
            return self._read()

    def _read(self) -> str:
        now = time.monotonic()
        if self.last_attempt is not None and now - self.last_attempt < self.interval:
            if self.last_error:
                raise self._cached_error()
            return self.cached
        self.last_attempt = now
        try:
            address = public_addresses(self.host)[0]
            status, body, _ = bounded_get(self.host, address, "/robots.txt", MAX_ROBOTS)
            delay = 1
            if status == 200:
                policy = RobotFileParser()
                try:
                    policy.parse(body.decode("utf-8", "replace").splitlines())
                    delay = max(delay, policy.crawl_delay(USER_AGENT) or 0)
                    rate = policy.request_rate(USER_AGENT)
                    if rate:
                        if rate.requests <= 0 or rate.seconds <= 0:
                            raise ValueError()
                        delay = max(delay, rate.seconds / rate.requests)
                except (ValueError, ZeroDivisionError, OverflowError):
                    raise RemoteError("robots_invalid", "robots.txt contains an unsupported rate or delay.") from None
                if not policy.can_fetch(USER_AGENT, self.url):
                    raise RemoteError("robots_denied", "robots.txt does not permit this source.")
                if delay > 10:
                    raise RemoteError("robots_delay", "The advertised crawl delay exceeds this tool's interactive budget. Use an authorized local capture.")
            elif status != 404:
                raise RemoteError("robots_unavailable", "robots.txt could not be checked; remote extraction is stopped.")
            self.interval = max(self.interval, int(delay) + 1)
            time.sleep(delay)
            status, body, content_type = bounded_get(self.host, address, self.path, MAX_PAGE)
            if status != 200 or content_type.split(";", 1)[0].strip().lower() not in {"text/html", "application/xhtml+xml"}:
                raise RemoteError("remote_response", "Expected a successful HTML response.")
            self.cached = decode_html(body)
            self.retrieved_at = datetime.now(timezone.utc).isoformat()
            self.last_error = None
            return self.cached
        except (ContractError, UnicodeError) as exc:
            # Cached failures keep only the fixed code, message and numeric issues.
            self.last_error = ((exc.code, exc.message, tuple(tuple(issue.items()) for issue in exc.issues)) if isinstance(exc, ContractError)
                               else ("html_encoding", "Remote HTML is not valid UTF-8.", ()))
        raise self._cached_error() from None

    def _cached_error(self) -> RemoteError:
        code, message, issues = self.last_error
        return RemoteError(code, message, [dict(issue) for issue in issues])
