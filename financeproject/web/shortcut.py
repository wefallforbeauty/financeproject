"""A desktop shortcut that starts the interface (``finance ui --shortcut``).

- **Linux** (and other freedesktop systems): an entry in the application menu
  (``~/.local/share/applications/financeproject.desktop``) and a copy on the
  desktop. Some desktops ask once whether to trust a new launcher.
- **Windows:** a shortcut in the Start menu and on the desktop, made through
  PowerShell; it starts Python without a console window.
- **macOS:** a small application, ``~/Applications/financeproject.app``, made
  with ``osacompile``.

The shortcut runs ``<this Python> -m financeproject ui``, with ``--db`` when the
ledger is not the default one, so it keeps working from the same environment.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from importlib import resources
from pathlib import Path

from financeproject.errors import FinanceError

NAME = "financeproject"


def command(db: Path | None = None, python: str | None = None) -> list[str]:
    return [python or sys.executable, "-m", "financeproject", *(["--db", str(db)] if db else []), "ui"]


def icon_path() -> Path:
    return Path(str(resources.files("financeproject.web").joinpath("static", "img", "icon.svg")))


def _exec_arg(arg: str) -> str:
    """One argument of a desktop entry's Exec key, quoted when needed."""
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'


def desktop_entry(cmd: list[str], icon: Path) -> str:
    return "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            "Version=1.0",
            f"Name={NAME}",
            "GenericName=Household finances",
            "Comment=Open the household ledger in the browser",
            "Exec=" + " ".join(_exec_arg(a) for a in cmd).replace("%", "%%"),
            f"Icon={icon}",
            "Terminal=false",
            "Categories=Office;Finance;",
            "StartupNotify=false",
            "",
        ]
    )


def _desktop_dir(home: Path) -> Path | None:
    try:
        answer = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=5, check=False)
        folder = Path(answer.stdout.strip()) if answer.returncode == 0 and answer.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        folder = None
    folder = folder or home / "Desktop"
    return folder if folder.is_dir() and folder != home else None


def create_linux(cmd: list[str], home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    entry = desktop_entry(cmd, icon_path())
    made = []
    menu = data / "applications" / f"{NAME}.desktop"
    menu.parent.mkdir(parents=True, exist_ok=True)
    menu.write_text(entry, encoding="utf-8")
    menu.chmod(0o755)
    made.append(menu)
    desktop = _desktop_dir(home)
    if desktop is not None:
        copy = desktop / f"{NAME}.desktop"
        copy.write_text(entry, encoding="utf-8")
        copy.chmod(0o755)
        made.append(copy)
    return made


POWERSHELL = (
    "$desktop = [Environment]::GetFolderPath('Desktop'); "
    "$menu = [Environment]::GetFolderPath('Programs'); "
    "foreach ($folder in @($desktop, $menu)) { "
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $folder 'financeproject.lnk')); "
    "$s.TargetPath = $env:FP_TARGET; $s.Arguments = $env:FP_ARGUMENTS; $s.WorkingDirectory = $env:USERPROFILE; "
    "$s.Description = 'Household finances'; $s.Save(); Write-Output (Join-Path $folder 'financeproject.lnk') }"
)


def create_windows(cmd: list[str]) -> list[Path]:
    python = Path(cmd[0])
    windowless = python.with_name("pythonw.exe")
    target = windowless if windowless.exists() else python
    env = {**os.environ, "FP_TARGET": str(target), "FP_ARGUMENTS": subprocess.list2cmdline(cmd[1:])}
    answer = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", POWERSHELL],
        capture_output=True, text=True, env=env, timeout=60, check=False,
    )
    if answer.returncode != 0:
        raise FinanceError(f"PowerShell could not make the shortcut: {answer.stderr.strip()[:300]}")
    return [Path(line) for line in answer.stdout.splitlines() if line.strip()]


def create_macos(cmd: list[str], home: Path | None = None) -> list[Path]:
    target = (home or Path.home()) / "Applications" / f"{NAME}.app"
    target.parent.mkdir(parents=True, exist_ok=True)
    shell = " ".join(shlex.quote(a) for a in cmd) + " >/dev/null 2>&1 &"
    script = 'do shell script "' + shell.replace("\\", "\\\\").replace('"', '\\"') + '"'
    answer = subprocess.run(["osacompile", "-o", str(target), "-e", script], capture_output=True, text=True, timeout=60, check=False)
    if answer.returncode != 0:
        raise FinanceError(f"osacompile could not make the application: {answer.stderr.strip()[:300]}")
    return [target]


def create_shortcut(db: Path | None = None, platform: str | None = None) -> list[Path]:
    """Make the shortcut for this system; returns where it was put."""
    platform = platform or sys.platform
    cmd = command(db)
    if platform.startswith("win"):
        return create_windows(cmd)
    if platform == "darwin":
        return create_macos(cmd)
    return create_linux(cmd)
