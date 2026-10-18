"""Running the interface: ``finance ui``.

The server listens on 127.0.0.1, so only this computer can reach it, on port
8765 or a free port when that one is taken. The browser is opened at a login
link with the session's token (see ``security``).

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
from financeproject.web.security import cookie_name, csrf_for, new_token

DEFAULT_PORT = 8765
HOST = "127.0.0.1"


@dataclass(frozen=True)
class Running:
    port: int
    token: str
    pid: int
    ledger: str

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


def serve(db_path: Path, *, port: int = DEFAULT_PORT, open_browser: bool = True, echo: Callable[[str], None] = print) -> None:
    """Start the interface on ``db_path`` and wait until it is stopped."""
    import uvicorn

    from financeproject.web.app import create_app
    from financeproject.web.views.imports import clean_uploads

    existing = read_state()
    if existing is not None and alive(existing):
        echo(f"The interface is already running: {existing.url}")
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
    running = Running(port, token, os.getpid(), str(db_path))
    write_state(running)
    echo(f"financeproject is running at {running.url} (this computer only). Stop it with Ctrl+C or from Settings.")
    echo(f"If the browser does not open, open: {running.login_url}")
    if open_browser:
        threading.Thread(target=_open_when_ready, args=(server, running.login_url), daemon=True).start()
    try:
        server.run()
    finally:
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
