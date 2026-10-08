"""``croqui update``: upgrade croqui however it was installed.

Checks pipx, then a git checkout, then a pip install from PyPI; otherwise prints
what to run. Always says what it is about to run.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"

PACKAGE = "croqui"


def _say(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def _run(cmd: list[str], *, cwd: Path | None = None) -> tuple[int, str]:
    _say(f"  {DIM}$ {' '.join(cmd)}{RESET}")
    try:
        done = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    except OSError as exc:
        return 127, str(exc)
    output = (done.stdout or "") + (done.stderr or "")
    for line in output.strip().splitlines()[-12:]:
        _say(f"    {DIM}{line}{RESET}")
    return done.returncode, output


def package_root() -> Path:
    """The directory holding the installed ``croqui`` package."""
    return Path(__file__).resolve().parent


def source_checkout() -> Path | None:
    """The git checkout croqui is running from, if it is running from one."""
    for candidate in (package_root().parent, *package_root().parents):
        if (candidate / ".git").exists() and (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def is_pipx() -> bool:
    """True when this interpreter lives in a pipx-managed venv for croqui."""
    marker = f"pipx{Path('/')}venvs{Path('/')}"
    prefix = str(Path(sys.prefix).resolve()).replace("\\", "/")
    return "/pipx/venvs/" in prefix or marker.replace("\\", "/") in prefix


def describe() -> dict:
    """How this copy got here — the whole basis for what update will do."""
    checkout = source_checkout()
    return {
        "version": __version__,
        "package": str(package_root()),
        "python": sys.executable,
        "pipx": is_pipx(),
        "checkout": str(checkout) if checkout else None,
        "editable": bool(checkout) and "site-packages" not in str(package_root()),
    }


def _plan(info: dict) -> tuple[str, list[list[str]], Path | None]:
    """(what we call this install, the commands, where to run them)."""
    pipx = shutil.which("pipx")
    if info["pipx"] and pipx:
        return "pipx", [[pipx, "upgrade", PACKAGE]], None

    checkout = Path(info["checkout"]) if info["checkout"] else None
    if checkout:
        commands: list[list[str]] = []
        if shutil.which("git"):
            commands.append(["git", "-C", str(checkout), "pull", "--ff-only"])
        if pipx and not info["editable"]:
            commands.append([pipx, "install", "--force", str(checkout)])
        else:
            commands.append([sys.executable, "-m", "pip", "install", "--upgrade", str(checkout)])
        return "git checkout", commands, checkout

    return "pip", [[sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE]], None


def installed_version() -> str:
    """Ask a fresh interpreter, because this process still holds the old module."""
    try:
        done = subprocess.run(
            [sys.executable, "-c", "import croqui; print(croqui.__version__)"],
            capture_output=True, text=True, cwd=str(Path.home()),
        )
        return done.stdout.strip() or "?"
    except OSError:
        return "?"


def update(*, check_only: bool = False) -> int:
    info = describe()
    kind, commands, cwd = _plan(info)

    _say(f"{BOLD}croqui{RESET} {info['version']} {DIM}({kind}){RESET}")
    _say(f"  {DIM}pacote: {info['package']}{RESET}")
    if info["checkout"]:
        _say(f"  {DIM}checkout: {info['checkout']}{'  (editável)' if info['editable'] else ''}{RESET}")

    if kind == "pip" and not info["checkout"]:
        _say(f"  {DIM}nada de git por perto — vai buscar no PyPI{RESET}")

    if check_only:
        _say("\nfaria:")
        for cmd in commands:
            _say(f"  {DIM}$ {' '.join(cmd)}{RESET}")
        return 0

    _say("")
    for cmd in commands:
        code, output = _run(cmd, cwd=cwd)
        if code != 0:
            _say(f"\n{RED}✗{RESET} falhou: {' '.join(cmd)}")
            if "No matching distribution" in output or "not found" in output.lower():
                _say(f"  {YELLOW}!{RESET} se o croqui ainda não está no PyPI, atualize a partir do "
                     f"checkout: {BOLD}pipx install --force /caminho/do/croqui{RESET}")
            return 1
        # `git pull` on an already-current checkout is a success with nothing to do,
        # and reinstalling the same commit is a waste of ten seconds.
        if cmd[:2] == ["git", "-C"] and "Already up to date" in output:
            _say(f"  {GREEN}✓{RESET} o checkout já estava atualizado")

    after = installed_version()
    if after == info["version"]:
        _say(f"\n{GREEN}✓{RESET} croqui {after} {DIM}(já era a mais recente){RESET}")
    else:
        _say(f"\n{GREEN}✓{RESET} croqui {info['version']} → {BOLD}{after}{RESET}")
    return 0
