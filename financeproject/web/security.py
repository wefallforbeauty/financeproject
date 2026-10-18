"""Keeping the interface to the person at this computer.

The server listens on 127.0.0.1 only, and every request passes these checks:

- **Host.** The ``Host`` header must name this server (``127.0.0.1:PORT`` or
  ``localhost:PORT``). A web page that points a domain of its own at
  127.0.0.1 (DNS rebinding) is refused.
- **Session.** A random token is made when the server starts. The browser is
  opened at ``/login?token=…``, which stores it in a cookie (``HttpOnly``,
  ``SameSite=Strict``); every other page needs that cookie. Other programs
  and web pages do not have it.
- **Changes.** A request that changes something (``POST``) must come from this
  origin (the ``Origin`` header, when the browser sends one) and carry the
  CSRF token, a value derived from the session token, in a header or a form
  field.
- **Responses** forbid framing, scripts from anywhere else
  (Content-Security-Policy) and caching of pages, and send no referrer to
  other sites.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from http.cookies import CookieError, SimpleCookie

from fastapi import HTTPException, Request
from starlette.datastructures import Headers

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
OPEN_PATHS = {"/login", "/ping", "/favicon.ico"}
MAX_BODY = 25 * 1024 * 1024
"""The largest request accepted (an uploaded statement), in bytes."""
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
DENIED_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>financeproject</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family: system-ui, sans-serif; max-width: 36rem; margin: 4rem auto; padding: 0 1rem; line-height: 1.5">
<h1>financeproject</h1>
<p>Open the interface from the program: <code>finance ui</code></p>
<p lang="tr">Arayüzü programdan açın: <code>finance ui</code></p>
</body></html>"""


def new_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_for(token: str) -> str:
    """The CSRF token of a session: a value derived from its token."""
    return hmac.new(token.encode(), b"csrf", hashlib.sha256).hexdigest()


def cookie_name(port: int | None) -> str:
    """Cookies do not tell ports apart, so every port gets its own name."""
    return f"fp_session_{port}" if port else "fp_session"


def allowed_hosts(port: int | None) -> set[str]:
    if port is None:
        return {"127.0.0.1", "localhost"}
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


class Guard:
    """ASGI middleware with the host, session and origin checks, and the response headers."""

    def __init__(self, app, *, token: str, port: int | None, extra_hosts: set[str] | None = None):
        self.app = app
        self.token = token
        self.cookie = cookie_name(port)
        self.hosts = allowed_hosts(port) | (extra_hosts or set())
        self.origins = {f"http://{host}" for host in self.hosts}

    async def _plain(self, send, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        data = body.encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", content_type.encode()),
                    (b"content-length", str(len(data)).encode()),
                    *self._security_headers(),
                ],
            }
        )
        await send({"type": "http.response.body", "body": data})

    @staticmethod
    def _security_headers() -> list[tuple[bytes, bytes]]:
        return [
            (b"content-security-policy", CSP.encode()),
            (b"x-frame-options", b"DENY"),
            (b"x-content-type-options", b"nosniff"),
            # same-origin, not no-referrer: with no-referrer browsers send "Origin: null" on form
            # posts, which the origin check refuses; other sites still get no referrer at all
            (b"referrer-policy", b"same-origin"),
            (b"cross-origin-opener-policy", b"same-origin"),
            (b"cross-origin-resource-policy", b"same-origin"),
        ]

    def signed_in(self, headers: Headers) -> bool:
        cookies = SimpleCookie()
        try:
            cookies.load(headers.get("cookie", ""))
        except CookieError:
            return False
        morsel = cookies.get(self.cookie)
        return morsel is not None and hmac.compare_digest(morsel.value, self.token)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if headers.get("host", "") not in self.hosts:
            await self._plain(send, 400, "This server answers only at its own address.")
            return
        path, method = scope["path"], scope["method"]
        if not (path.startswith("/static/") or path in OPEN_PATHS) and not self.signed_in(headers):
            await self._plain(send, 401, DENIED_PAGE, "text/html; charset=utf-8")
            return
        if method not in SAFE_METHODS:
            origin = headers.get("origin")
            if origin is not None and origin not in self.origins:
                await self._plain(send, 403, "Requests from other sites are refused.")
                return
            length = headers.get("content-length")
            if length is not None and length.isdigit() and int(length) > MAX_BODY:
                await self._plain(send, 413, "The file is too large.")
                return
        static = path.startswith("/static/")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                extra = self._security_headers()
                if not static:
                    extra.append((b"cache-control", b"no-store"))
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def check_csrf(request: Request) -> None:
    """A dependency of every route: changes need the CSRF token (header or form field)."""
    if request.method in SAFE_METHODS:
        return
    expected = request.app.state.csrf
    given = request.headers.get("x-csrf-token")
    if given is None:
        form = await request.form()
        value = form.get("csrf_token")
        given = value if isinstance(value, str) else None
    if not given or not hmac.compare_digest(given, expected):
        raise HTTPException(status_code=403, detail="The form has expired; reload the page and try again.")
