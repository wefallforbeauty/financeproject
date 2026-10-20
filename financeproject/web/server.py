"""Running the interface: ``finance ui``.

The server listens on 127.0.0.1, so only this computer can reach it, on port
8765 or a free port when that one is taken. The browser is opened at a login
link with the session's token (see ``security``).

Opened to the home network (``finance ui --lan``, or Settings → Phones and
monitor), a second server answers over HTTPS on this computer's private address,
port 8766, to paired phones and wall monitors (see ``devices`` and ``tls``). It
runs in a thread of its own next to the first one and stops with it.

While the server runs, ``ui.json`` in the data directory (readable only by the
user) holds its port and token. ``finance ui`` run again opens the browser at
the running server instead of starting another; ``finance ui --stop`` stops it.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from financeproject import config
from financeproject.errors import FinanceError
from financeproject.web.lan import Lan
from financeproject.web.security import cookie_name, csrf_for, new_token

DEFAULT_PORT = 8765
LAN_PORT = 8766
HOST = "127.0.0.1"
CIPHERS = "ECDHE+AESGCM:ECDHE+CHACHA20"
"""TLS 1.2 ciphers with forward secrecy and authenticated encryption (TLS 1.3 has its own, and Python
refuses versions before 1.2); old versions of uvicorn would otherwise allow weaker ones."""


@dataclass(frozen=True)
class Running:
    port: int
    token: str
    pid: int
    ledger: str
    lan: str | None = None
    """The home-network address, while the interface is open to it."""

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}"

    @property
    def login_url(self) -> str:
        return f"{self.url}/login?token={self.token}"


def state_file() -> Path:
    return config.data_dir() / "ui.json"


def read_state() -> Running | None:
    try:
        return Running(**json.loads(state_file().read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def write_state(running: Running) -> None:
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(asdict(running), fh)


def clear_state(pid: int | None = None) -> None:
    running = read_state()
    if running is not None and (pid is None or running.pid == pid):
        state_file().unlink(missing_ok=True)


def alive(running: Running, timeout: float = 1.0) -> bool:
    """True if a financeproject server answers on the running server's port."""
    try:
        answer = httpx.get(f"{running.url}/ping", timeout=timeout)
    except httpx.HTTPError:
        return False
    return answer.status_code == 200 and answer.text == "financeproject"


def free_port(preferred: int = DEFAULT_PORT) -> int:
    """``preferred`` if nothing listens there, otherwise a port the system picks.

    Like the server itself, the check may reuse a port whose last connections are
    still closing (TIME_WAIT), so a server stopped a moment ago does not move the
    next one to another port. (On Windows that option would also allow a port in
    use, so it is left out there.)
    """
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if os.name == "posix":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((HOST, port))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise OSError("No free port.")


def _open_when_ready(server, url: str, wait: float = 15.0) -> None:
    deadline = time.monotonic() + wait
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.1)
    if server.started:
        webbrowser.open(url)


def lan_port() -> int:
    """The home-network port saved in Settings (``[network] port``), or 8766."""
    value = config.get_setting("network", "port", str(LAN_PORT)) or ""
    return int(value) if value.isdigit() else LAN_PORT


_lan_lock = threading.Lock()
"""Opening and closing the home-network side one at a time (two clicks on Open must not start two servers)."""


def _note_lan(url: str | None) -> None:
    running = read_state()
    if running is not None and running.pid == os.getpid():
        write_state(Running(running.port, running.token, running.pid, running.ledger, url))


def open_lan(app, *, address: str | None = None, port: int | None = None, same_network: bool = False) -> Lan:
    """Open the interface to the home network: an HTTPS server on this computer's private address.

    ``address`` (one of ``lan.addresses()``) and ``port`` default to the ones saved in Settings, then to the
    first home-network address and 8766. With ``same_network`` (opening by itself at start) it opens only
    when this computer still has the saved address: on another network, a café's for example, it stays
    closed."""
    with _lan_lock:
        return _open_lan(app, address, port, same_network)


def _open_lan(app, address: str | None, port: int | None, same_network: bool) -> Lan:
    import uvicorn

    from financeproject.web import lan, tls

    state = app.state
    if state.lan is not None:
        return state.lan
    if not state.devices.has_password:
        raise FinanceError("Set a password first: Settings → Phones and monitor, or finance ui --password.")
    found = lan.addresses()
    if not found:
        raise FinanceError("This computer has no home-network address (a private one, such as 192.168.1.20).")
    wanted = address or config.get_setting("network", "address")
    if same_network and wanted not in [str(a) for a in found]:
        raise FinanceError(
            f"this computer does not have its home-network address ({wanted or 'none saved'}) now, so it may be on "
            "another network. Open it from Settings → Phones and monitor if this is the home network."
        )
    ip = next((a for a in found if str(a) == wanted), found[0])
    if port is None:
        port = lan_port()
    name = lan.hostname()
    where = tls.ensure(name, found)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "posix":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((str(ip), port))
    except OSError as exc:
        sock.close()
        raise FinanceError(f"Port {port} on {ip} is in use or not available: {exc.strerror or exc}") from exc
    server = uvicorn.Server(
        uvicorn.Config(
            app, ssl_certfile=str(where.server), ssl_keyfile=str(where.server_key), ssl_ciphers=CIPHERS,
            lifespan="off", log_level="warning", access_log=False,
        )
    )
    home = Lan(str(ip), port, tuple(n for n in (name, f"{name}.local") if name))
    state.lan = home  # before the first request, so that the guard knows the second side
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="financeproject-home-network", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        state.lan = None
        sock.close()
        raise FinanceError("The home-network server did not start.")
    state.lan_server = (server, thread, sock)
    _note_lan(home.url)
    return home


def close_lan(app, wait: float = 5.0) -> bool:
    """Close the home-network side; ``False`` if it was not open."""
    with _lan_lock:
        state = app.state
        running, state.lan_server = state.lan_server, None
        state.lan = None
        if running is None:
            return False
        server, thread, sock = running
        server.should_exit = True
        thread.join(wait)
        sock.close()
        _note_lan(None)
        return True


def serve(
    db_path: Path, *, port: int = DEFAULT_PORT, open_browser: bool = True, lan: bool = False, echo: Callable[[str], None] = print
) -> None:
    """Start the interface on ``db_path`` and wait until it is stopped; with ``lan`` (or the setting to open
    whenever it starts) also open it to the home network."""
    import uvicorn

    from financeproject.web.app import create_app
    from financeproject.web.views.imports import clean_uploads

    existing = read_state()
    if existing is not None and alive(existing):
        echo(f"The interface is already running: {existing.url}" + (f" (home network: {existing.lan})" if existing.lan else ""))
        if open_browser:
            webbrowser.open(existing.login_url)
        return
    clear_state()
    clean_uploads()
    token = new_token()
    port = free_port(port)
    app = create_app(db_path, token=token, port=port)
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning", access_log=False))
    app.state.server = server
    app.middleware_stack = app.build_middleware_stack()  # built once, before two servers may ask for it at once
    running = Running(port, token, os.getpid(), str(db_path))
    write_state(running)
    echo(f"financeproject is running at {running.url} on this computer. Stop it with Ctrl+C or from Settings.")
    echo(f"If the browser does not open, open: {running.login_url}")
    if lan or config.get_setting("network", "open") == "on":
        try:
            home = open_lan(app, same_network=not lan)
        except FinanceError as exc:
            echo(f"Not opened to the home network: {exc}")
        else:
            echo(f"Phones and monitors on the home network: {home.url} (pair them in Settings → Phones and monitor).")
    if open_browser:
        threading.Thread(target=_open_when_ready, args=(server, running.login_url), daemon=True).start()
    try:
        server.run()
    finally:
        close_lan(app)
        clear_state(os.getpid())


def stop(wait: float = 10.0) -> bool:
    """Stop the running interface and wait until it is gone; ``False`` if none was running."""
    running = read_state()
    if running is None or not alive(running):
        clear_state()
        return False
    httpx.post(
        f"{running.url}/quit",
        cookies={cookie_name(running.port): running.token},
        headers={"X-CSRF-Token": csrf_for(running.token), "Origin": running.url},
        timeout=5,
    )
    deadline = time.monotonic() + wait
    while alive(running, timeout=0.5) and time.monotonic() < deadline:
        time.sleep(0.1)
    return True
