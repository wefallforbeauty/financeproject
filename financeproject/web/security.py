"""Keeping the interface to the household.

The interface answers on two sides. **This computer**: the server listens on 127.0.0.1 only. **The home
network** (when it is opened, see ``server.open_lan``): a second server answers over HTTPS on this
computer's private address, to the devices paired with it (see ``devices``). Every request passes these
checks:

- **Host.** The ``Host`` header must name the side's own address: ``127.0.0.1:PORT`` or ``localhost:PORT``;
  on the home network the address, this computer's name or its ``.local`` name. A web page that points a
  domain of its own at the address (DNS rebinding) is refused.
- **Who.** On this computer a random token is made when the server starts; the browser is opened at
  ``/login?token=…``, which stores it in a cookie (``HttpOnly``, ``SameSite=Strict``), and every other page
  needs that cookie. On the home network the client's address must be private, and every page except
  pairing needs a paired device's cookie (``HttpOnly``, ``Secure``). A wall monitor sees its own page only;
  a phone sees everything but this computer's own settings (devices, ledger, backups, keys, stopping).
- **Changes.** A request that changes something (``POST``) must come from the side's own origin (the
  ``Origin`` header, when the browser sends one; an unpaired device must send it) and carry the CSRF
  token, a value derived from the session's token, in a header or a form field.
- **Responses** forbid framing, scripts from anywhere else (Content-Security-Policy) and caching of pages,
  and send no referrer to other sites.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie

from fastapi import HTTPException, Request
from starlette.datastructures import Headers

from financeproject.web.devices import Device
from financeproject.web.lan import is_home

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
OPEN_PATHS = {"/login", "/ping", "/favicon.ico"}
HOME_OPEN_PATHS = {"/pair", "/ca.crt", "/ping", "/favicon.ico"}
"""What an unpaired device on the home network may open (besides ``/static/``)."""
MONITOR_PATHS = {"/monitor", "/monitor/panel", "/monitor/check", "/monitor/guests", "/ca.crt", "/ping", "/favicon.ico"}
COMPUTER_ONLY = (
    "/settings/devices", "/settings/ledger", "/settings/demo", "/settings/backup", "/settings/evds",
    "/settings/shortcut", "/quit", "/login", "/welcome",
)
"""Pages a phone may not open: they belong to this computer (``/settings/backup`` covers the downloads too;
a ledger is created on the computer)."""
DEVICE_COOKIE = "fp_device"
LOCAL, DEVICE, VISITOR = "local", "device", "visitor"
MAX_BODY = 25 * 1024 * 1024
"""The largest request accepted (an uploaded statement), in bytes."""
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>financeproject</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family: system-ui, sans-serif; max-width: 36rem; margin: 4rem auto; padding: 0 1rem; line-height: 1.5">
<h1>financeproject</h1>
{body}
</body></html>"""
DENIED_PAGE = _PAGE.format(body="""<p>Open the interface from the program: <code>finance ui</code></p>
<p lang="tr">Arayüzü programdan açın: <code>finance ui</code></p>""")
NOT_PAIRED_PAGE = _PAGE.format(body="""<p>This device is not paired, or it was removed. On the computer, open Settings →
Phones and monitor, pair a phone or a wall monitor, and scan the code shown there.</p>
<p lang="tr">Bu cihaz eşleştirilmemiş ya da kaldırılmış. Bilgisayarda Ayarlar → Telefonlar ve ekran'ı açın, bir
telefon ya da duvar ekranı eşleştirin ve orada görünen kodu okutun.</p>""")


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


@dataclass(frozen=True)
class Session:
    """Who a request comes from: this computer, a paired device, or an unpaired device on the home network."""

    kind: str
    """``local``, ``device`` or ``visitor``."""
    key: str
    """What the session's messages are kept under: ``local``, the device's id, or ``visitor``."""
    csrf: str
    device: Device | None = None

    @property
    def on_computer(self) -> bool:
        return self.kind == LOCAL


def session_of(request: Request) -> Session:
    """The request's session, as the guard found it (this computer's, for requests that bypass it)."""
    session = request.scope.get("state", {}).get("session")
    return session if session is not None else Session(LOCAL, "local", request.app.state.csrf)


def _cookie(headers: Headers, name: str) -> str | None:
    cookies = SimpleCookie()
    try:
        cookies.load(headers.get("cookie", ""))
    except CookieError:
        return None
    morsel = cookies.get(name)
    return morsel.value if morsel is not None else None


class Guard:
    """ASGI middleware with the host, session and origin checks of both sides, and the response headers."""

    def __init__(self, app, *, token: str, port: int | None, extra_hosts: set[str] | None = None):
        self.app = app
        self.token = token
        self.csrf = csrf_for(token)
        self.cookie = cookie_name(port)
        self.hosts = allowed_hosts(port) | (extra_hosts or set())
        self.origins = {f"http://{host}" for host in self.hosts}

    async def _plain(self, send, status: int, body: str, content_type: str = "text/plain; charset=utf-8", extra=()) -> None:
        data = body.encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", content_type.encode()),
                    (b"content-length", str(len(data)).encode()),
                    *extra,
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
        value = _cookie(headers, self.cookie)
        return value is not None and hmac.compare_digest(value, self.token)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = scope["app"].state
        lan = getattr(state, "lan", None)
        server = scope.get("server") or (None, None)
        if lan is not None and scope.get("scheme") == "https" and server[1] == lan.port:
            await self._home_network(scope, receive, send, state, lan)
        else:
            await self._this_computer(scope, receive, send)

    async def _this_computer(self, scope, receive, send):
        headers = Headers(scope=scope)
        if headers.get("host", "") not in self.hosts:
            await self._plain(send, 400, "This server answers only at its own address.")
            return
        path = scope["path"]
        if not (path.startswith("/static/") or path in OPEN_PATHS) and not self.signed_in(headers):
            await self._plain(send, 401, DENIED_PAGE, "text/html; charset=utf-8")
            return
        if not await self._change_allowed(scope, send, headers, self.origins, origin_required=False):
            return
        scope.setdefault("state", {})["session"] = Session(LOCAL, "local", self.csrf)
        await self.app(scope, receive, self._with_headers(send, path))

    async def _home_network(self, scope, receive, send, state, lan):
        client = scope.get("client") or ("", 0)
        if not is_home(client[0]):
            await self._plain(send, 403, "Only devices on the home network are served.")
            return
        headers = Headers(scope=scope)
        if headers.get("host", "") not in lan.hosts:
            await self._plain(send, 400, "This server answers only at its own address.")
            return
        path, method = scope["path"], scope["method"]
        static = path.startswith("/static/")
        token = _cookie(headers, DEVICE_COOKIE)
        device = state.devices.authenticate(token)
        if device is None:
            if not (static or path in HOME_OPEN_PATHS):
                await self._plain(send, 401, NOT_PAIRED_PAGE, "text/html; charset=utf-8")
                return
            session = Session(VISITOR, "visitor", "")
        else:
            if device.is_monitor and not (static or path in MONITOR_PATHS):
                if method in SAFE_METHODS:  # the monitor's start address may be the home page
                    await self._plain(send, 303, "", extra=[(b"location", b"/monitor")])
                else:
                    await self._plain(send, 403, "A wall monitor shows its own page only.")
                return
            if not device.is_monitor and path.startswith(COMPUTER_ONLY):
                await self._plain(send, 403, "This page can be opened on the computer only.")
                return
            session = Session(DEVICE, device.id, csrf_for(token or ""), device)
            state.devices.seen(device, client[0])
        if not await self._change_allowed(scope, send, headers, lan.origins, origin_required=device is None):
            return
        scope.setdefault("state", {})["session"] = session
        await self.app(scope, receive, self._with_headers(send, path))

    async def _change_allowed(self, scope, send, headers: Headers, origins: set[str], origin_required: bool) -> bool:
        if scope["method"] in SAFE_METHODS:
            return True
        origin = headers.get("origin")
        if (origin is None and origin_required) or (origin is not None and origin not in origins):
            await self._plain(send, 403, "Requests from other sites are refused.")
            return False
        length = headers.get("content-length")
        if length is not None and length.isdigit() and int(length) > MAX_BODY:
            await self._plain(send, 413, "The file is too large.")
            return False
        return True

    def _with_headers(self, send, path: str):
        static = path.startswith("/static/")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                extra = self._security_headers()
                if not static:
                    extra.append((b"cache-control", b"no-store"))
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        return send_with_headers


async def check_csrf(request: Request) -> None:
    """A dependency of every route: changes need the CSRF token (header or form field).

    An unpaired device has no session to derive a token from; it can only pair, and the guard made it
    send this server's origin instead."""
    if request.method in SAFE_METHODS:
        return
    session = session_of(request)
    if session.kind == VISITOR:
        return
    given = request.headers.get("x-csrf-token")
    if given is None:
        form = await request.form()
        value = form.get("csrf_token")
        given = value if isinstance(value, str) else None
    if not given or not hmac.compare_digest(given, session.csrf):
        raise HTTPException(status_code=403, detail="The form has expired; reload the page and try again.")
