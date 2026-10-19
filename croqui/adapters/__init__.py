"""Per-stack scanners. Each adapter turns source code into graph nodes/edges."""

from __future__ import annotations

from pathlib import Path

from ._common import iter_files
from .django_py import DjangoScanner
from .express_js import ExpressScanner
from .fastapi_py import FastApiScanner
from .flask_py import FlaskScanner
from .nestjs_ts import NestScanner
from .spring_java import SpringScanner

ADAPTERS = {
    "python/fastapi": FastApiScanner,
    "python/flask": FlaskScanner,
    "python/django": DjangoScanner,
    "node/express": ExpressScanner,
    "node/nestjs": NestScanner,
    "java/spring": SpringScanner,
}


def _is_fastapi(root: Path) -> bool:
    for candidate in ("requirements.txt", "pyproject.toml", "requirements-worker.txt"):
        path = root / candidate
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            if "fastapi" in text:
                return True
    for path in iter_files(root, (".py",)):
        head = path.read_text(encoding="utf-8", errors="replace")[:2000]
        if "APIRouter" in head or "from fastapi" in head:
            return True
    return False


def detect_stacks(root: Path) -> list[str]:
    """Every stack this project uses, in ``ADAPTERS`` order (a monorepo can have several)."""
    found = ["python/fastapi"] if _is_fastapi(root) else []
    for stack, adapter in ADAPTERS.items():
        if stack != "python/fastapi" and adapter.detect(root):
            found.append(stack)
    return found


def detect_stack(root: Path) -> str | None:
    """The first detected stack, or None."""
    stacks = detect_stacks(root)
    return stacks[0] if stacks else None


__all__ = ["ADAPTERS", "detect_stack", "detect_stacks"]
