"""Who may use the interface from other devices: the household's password, the paired devices, pairing codes.

- **Password.** Kept as an scrypt hash (RFC 7914; N = 2^17, r = 8, p = 1, a random 16-byte salt: OWASP's
  minimum). Checking one takes a fraction of a second and 128 MB of memory, which also slows down guessing.
- **Devices.** A paired device keeps a random 256-bit token in a cookie; only the token's SHA-256 is stored,
  with the device's name, its kind (a phone or a wall monitor), when it was paired, and when and from which
  address it was last seen. Removing a device ends its access at once.
- **Pairing codes** are made on the computer and live in memory only. One code pairs one device within ten
  minutes; five wrong tries end it, and after ten wrong tries in ten minutes pairing waits.

The password and the devices are in ``devices.json`` in the configuration directory, readable only by the
user. The file is read again when another process (``finance ui --forget-devices``) changes it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from financeproject import config
from financeproject.errors import FinanceError

PHONE = "phone"
MONITOR = "monitor"
KINDS = (PHONE, MONITOR)
MIN_PASSWORD = 8
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**17, 8, 1
SCRYPT_MAXMEM = 256 * 1024 * 1024
"""scrypt needs 128 · r · N bytes (128 MB here); hashlib refuses more than ``maxmem``."""
CODE_LIFETIME = 600.0
"""Seconds a pairing code is valid."""
CODE_TRIES = 5
FAILURE_WINDOW = 600.0
FAILURE_LIMIT = 10
SEEN_EVERY = 300.0
"""Seconds between saves of a device's last-seen time."""
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
"""Pairing code letters: no 0/O, 1/I/L, so a code read aloud or typed is not mistaken."""
CODE_LENGTH = 10
MAX_NAME = 40
TOO_MANY = "Too many wrong tries. Wait ten minutes, then make a new code on the computer."
EXPIRED = "This code has expired or was used already. Make a new one on the computer."
WRONG = "The code or the password is wrong."
PAIRING_MESSAGES = (TOO_MANY, EXPIRED, WRONG)


class PairingError(FinanceError):
    """A pairing attempt that was refused (wrong code or password, expired code, too many tries)."""


def devices_path() -> Path:
    return config.config_dir() / "devices.json"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str, *, salt: bytes | None = None, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> str:
    """``scrypt$N$r$p$salt$hash`` (base64) of a password."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM, dklen=32)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        salt_bytes, expected = base64.b64decode(salt), base64.b64decode(digest)
        computed = hashlib.scrypt(
            password.encode(), salt=salt_bytes, n=int(n), r=int(r), p=int(p), maxmem=SCRYPT_MAXMEM, dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(computed, expected)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def clean_name(name: str, base: str, taken: list[str]) -> str:
    """A device's name as typed, or ``base``, ``base 2`` and so on when it is left empty."""
    name = " ".join(name.split())[:MAX_NAME]
    if name:
        return name
    number = 1
    while (f"{base} {number}" if number > 1 else base) in taken:
        number += 1
    return f"{base} {number}" if number > 1 else base


def normalize_code(code: str) -> str:
    return "".join(c for c in code.upper() if c in ALPHABET)


def show_code(code: str) -> str:
    """``ABCDEFGHJK`` → ``ABCDE-FGHJK``."""
    half = len(code) // 2
    return f"{code[:half]}-{code[half:]}"


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    kind: str
    """``phone`` or ``monitor``."""
    token_hash: str
    paired: str
    """When it was paired (ISO 8601, UTC)."""
    last_seen: str | None = None
    last_address: str | None = None

    @property
    def is_monitor(self) -> bool:
        return self.kind == MONITOR


class DeviceStore:
    """The password and the paired devices of ``devices.json``; safe to use from several threads."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else devices_path()
        self._lock = threading.RLock()
        self._stamp: tuple[float, int] | None = None
        self._password: str | None = None
        self._devices: list[Device] = []
        self._saved_seen: dict[str, float] = {}
        self._load()

    # ------------------------------------------------------------------ the file

    def _file_stamp(self) -> tuple[float, int] | None:
        try:
            info = self.path.stat()
        except OSError:
            return None
        return (info.st_mtime, info.st_size)

    def _load(self) -> None:
        stamp = self._file_stamp()
        data: dict = {}
        if stamp is not None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise FinanceError(f"Cannot read {self.path}: {exc}") from exc
        self._password = data.get("password") or None
        self._devices = [Device(**d) for d in data.get("devices", [])]
        self._stamp = stamp

    def _refresh(self) -> None:
        if self._file_stamp() != self._stamp:
            self._load()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"password": self._password, "devices": [asdict(d) for d in self._devices]}
        temporary = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)
        self._stamp = self._file_stamp()

    # ------------------------------------------------------------------ the password

    @property
    def has_password(self) -> bool:
        with self._lock:
            self._refresh()
            return self._password is not None

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise FinanceError(f"The password needs at least {MIN_PASSWORD} characters.")
        stored = hash_password(password)
        with self._lock:
            self._refresh()
            self._password = stored
            self._save()

    def check_password(self, password: str) -> bool:
        with self._lock:
            self._refresh()
            stored = self._password
        return verify_password(password, stored)

    # ------------------------------------------------------------------ devices

    @property
    def devices(self) -> list[Device]:
        with self._lock:
            self._refresh()
            return list(self._devices)

    def add(self, name: str, kind: str, default: str = "") -> tuple[Device, str]:
        """A new device and its token (shown once, to go into the device's cookie). ``default`` names it
        when ``name`` is empty."""
        if kind not in KINDS:
            raise FinanceError(f"Unknown kind of device: {kind}")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._refresh()
            device = Device(
                id=secrets.token_hex(4), kind=kind,
                name=clean_name(name, default or ("Monitor" if kind == MONITOR else "Phone"), [d.name for d in self._devices]),
                token_hash=token_hash(token), paired=now_text(),
            )
            self._devices.append(device)
            self._save()
        return device, token

    def authenticate(self, token: str | None) -> Device | None:
        if not token:
            return None
        wanted = token_hash(token)
        with self._lock:
            self._refresh()
            found = None
            for device in self._devices:
                if hmac.compare_digest(device.token_hash, wanted):
                    found = device
            return found

    def seen(self, device: Device, address: str | None, clock: Callable[[], float] = time.monotonic) -> None:
        """Note that ``device`` was just used; saved at most every few minutes."""
        with self._lock:
            self._refresh()  # a device removed by another process must not come back
            index = next((i for i, d in enumerate(self._devices) if d.id == device.id), None)
            if index is None:
                return
            self._devices[index] = replace(self._devices[index], last_seen=now_text(), last_address=address)
            moment = clock()
            if moment - self._saved_seen.get(device.id, -SEEN_EVERY) >= SEEN_EVERY:
                self._saved_seen[device.id] = moment
                self._save()

    def remove(self, device_id: str) -> Device | None:
        with self._lock:
            self._refresh()
            found = next((d for d in self._devices if d.id == device_id), None)
            if found is not None:
                self._devices = [d for d in self._devices if d.id != device_id]
                self._save()
            return found

    def remove_all(self) -> int:
        with self._lock:
            self._refresh()
            count = len(self._devices)
            self._devices = []
            self._save()
            return count


@dataclass
class Pairing:
    code: str
    kind: str
    expires: float
    tries: int = 0
    device: Device | None = None
    """The device it paired, once used."""


class Pairings:
    """The pairing code in use (one at a time) and the record of wrong tries."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._current: Pairing | None = None
        self._failures: list[float] = []

    def start(self, kind: str) -> Pairing:
        """A new code for a device of ``kind``; an earlier unused code stops working."""
        if kind not in KINDS:
            raise FinanceError(f"Unknown kind of device: {kind}")
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        with self._lock:
            self._current = Pairing(code, kind, self.clock() + CODE_LIFETIME)
            return self._current

    @property
    def current(self) -> Pairing | None:
        """The code in use, until it expires (a used one stays until then, to show which device it paired)."""
        with self._lock:
            pairing = self._current
            if pairing is not None and self.clock() > pairing.expires:
                self._current = pairing = None
            return pairing

    def seconds_left(self, pairing: Pairing) -> int:
        return max(0, int(pairing.expires - self.clock()))

    def cancel(self) -> None:
        with self._lock:
            self._current = None

    def kind_of(self, code: str) -> str | None:
        """The kind of device a valid, unused code pairs (for the pairing page)."""
        pairing = self.current
        if pairing is None or pairing.device is not None or not hmac.compare_digest(normalize_code(code), pairing.code):
            return None
        return pairing.kind

    def redeem(self, code: str, password: str, name: str, store: DeviceStore, default: str = "") -> tuple[Device, str]:
        """Pair a device: the code must be the one in use and the password right."""
        with self._lock:
            now = self.clock()
            self._failures = [t for t in self._failures if now - t < FAILURE_WINDOW]
            if len(self._failures) >= FAILURE_LIMIT:
                raise PairingError(TOO_MANY)
            pairing = self._current
            if pairing is None or pairing.device is not None or now > pairing.expires:
                raise PairingError(EXPIRED)
            right_password = store.check_password(password)  # always checked, so the time taken tells nothing
            if not (hmac.compare_digest(normalize_code(code), pairing.code) and right_password):
                pairing.tries += 1
                self._failures.append(now)
                if pairing.tries >= CODE_TRIES:
                    self._current = None
                raise PairingError(WRONG)
            device, token = store.add(name, pairing.kind, default)
            pairing.device = device
            return device, token
