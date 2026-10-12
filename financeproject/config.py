"""Where personal data and settings live.

Personal data never goes into the repository. It lives in the per-user data
directory that the operating system provides (found with ``platformdirs``):

- Linux: ``~/.local/share/financeproject/``
- macOS: ``~/Library/Application Support/financeproject/``
- Windows: ``%LOCALAPPDATA%\\financeproject\\``

There you find ``ledger.sqlite`` (the ledger, prices and CPI), ``demo.sqlite``
(the made-up demo household), ``backups/`` and ``output/`` (charts).

Settings live in ``settings.toml`` in the per-user configuration directory
(``~/.config/financeproject/`` on Linux): the EVDS API key and the number
format. The EVDS key can also come from the ``EVDS_API_KEY`` environment
variable. ``FINANCEPROJECT_DATA_DIR``, ``FINANCEPROJECT_CONFIG_DIR`` and
``FINANCEPROJECT_DB`` (the ledger file) override the locations.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import platformdirs

from financeproject.errors import FinanceError

APP_NAME = "financeproject"
DB_FILE_NAME = "ledger.sqlite"
DEMO_FILE_NAME = "demo.sqlite"
SETTINGS_FILE_NAME = "settings.toml"
ENV_DATA_DIR = "FINANCEPROJECT_DATA_DIR"
ENV_CONFIG_DIR = "FINANCEPROJECT_CONFIG_DIR"
ENV_DB = "FINANCEPROJECT_DB"
ENV_EVDS_KEY = "EVDS_API_KEY"


def data_dir() -> Path:
    if custom := os.environ.get(ENV_DATA_DIR):
        return Path(custom).expanduser()
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def config_dir() -> Path:
    if custom := os.environ.get(ENV_CONFIG_DIR):
        return Path(custom).expanduser()
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))


def default_db_path() -> Path:
    if custom := os.environ.get(ENV_DB):
        return Path(custom).expanduser()
    return data_dir() / DB_FILE_NAME


def demo_db_path() -> Path:
    return data_dir() / DEMO_FILE_NAME


def backup_dir() -> Path:
    return data_dir() / "backups"


def output_dir() -> Path:
    return data_dir() / "output"


def settings_path() -> Path:
    return config_dir() / SETTINGS_FILE_NAME


def load_settings() -> dict:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise FinanceError(f"Cannot read the settings file ({path}): {exc}") from exc


def evds_api_key() -> str | None:
    """EVDS key from the environment, else from the settings file."""
    if key := os.environ.get(ENV_EVDS_KEY, "").strip():
        return key
    key = load_settings().get("evds", {}).get("api_key", "")
    return key.strip() or None


def number_format() -> str | None:
    """The saved number format (``"1,234.56"`` or ``"1.234,56"``), if any."""
    value = load_settings().get("display", {}).get("number_format")
    return str(value) if value else None


def save_setting(section: str, key: str, value: str) -> Path:
    """Write one string setting, keeping the others. The file is readable only by the user."""
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    settings.setdefault(section, {})[key] = value.strip()
    lines: list[str] = []
    for name, values in settings.items():
        if not isinstance(values, dict):
            continue
        lines.append(f"[{name}]")
        for k, v in values.items():
            escaped = str(v).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{k} = "{escaped}"')
        lines.append("")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    os.chmod(path, 0o600)
    return path


def save_evds_api_key(key: str) -> Path:
    return save_setting("evds", "api_key", key)
